"""The Python pathway: a plain, synchronous drone API for teams.

A ``Drone`` here talks to a ``DroneAdapter`` — the simulator or the real Tello —
through the checks in this module (clamping, the emergency-stop flag). The
``squadrone`` IDE client reaches the same class through the server, so a program
flies identically whether it runs from an IDE or under ``--script``.

A lap, the discrete way::

    from comp2.api import Drone

    drone = Drone()
    drone.takeoff()
    drone.forward(150)
    drone.turn_right(90)
    drone.land()

Or the racing way, with velocity control and your own OpenCV on the camera::

    import cv2
    from comp2.api import Drone

    drone = Drone()
    drone.takeoff()
    while not at_final_gate:
        frame = drone.frame()              # BGR image from the front camera
        # ... your gate detection here ...
        drone.fly(lr=0, fb=60, ud=climb, yaw=steer, seconds=0.2)
    drone.land()

Run a file alongside the server, so the video feed and EMERGENCY STOP button
stay live while your code flies::

    python -m comp2 --script my_lap.py --drone sim

``print()`` shows up in the terminal and in the browser's system log. Teams
flying from their own IDE use the ``squadrone`` client instead (see
``client/``).

Run it standalone (no window, no e-stop button — testing only)::

    Drone(sim=True)
"""

import asyncio
import contextlib
import runpy
import sys
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import overlay
from .drone.base import DroneAdapter

MIN_MOVE_CM, MAX_MOVE_CM = 20, 500  # Tello firmware limits, not preferences
MIN_TURN_DEG, MAX_TURN_DEG = 1, 360
MIN_SPEED_CMS, MAX_SPEED_CMS = 10, 100
MAX_GO_CM = 500
GO_MIN_CM = 20  # per-axis minimum magnitude for go/curve, unless the axis is 0
MAX_RC = 100


class EmergencyStop(BaseException):
    """Raised inside a student script when the stop flag is set.

    Deliberately a ``BaseException`` rather than an ``Exception``: a script that
    wraps its flight loop in ``try/except Exception`` must not be able to
    swallow the emergency stop and leave the drone flying.
    """


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


@dataclass
class Session:
    """What one script run shares with whoever is hosting it.

    Under ``--script`` or an IDE connection the server builds this; standalone,
    ``Drone`` builds its own. Either way ``stop`` is the single flag every API call consults.
    """

    drone: DroneAdapter
    get_frame: Callable[[], object] = lambda: None
    emit: Callable[[dict], None] = lambda ev: None
    #: Where :meth:`Drone.show_detections` puts its markers. Display only —
    #: the host composites them onto the operator's copy of the video.
    show_overlay: Callable[[tuple], None] = lambda markers: None
    stop: threading.Event = field(default_factory=threading.Event)


# Single-session by construction, exactly like app.state.active_run. Set by the
# script runner on the worker thread before the student's file is loaded, so a
# bare `Drone()` in their script finds the server's adapter and stop flag with
# no ceremony.
_active: Session | None = None
_active_lock = threading.Lock()


def bind_session(session: Session) -> None:
    global _active
    with _active_lock:
        _active = session


def clear_session(expect: Session | None = None) -> None:
    """Unbind. ``expect`` guards against a stopped-but-still-running script
    clearing a session that a later run has since installed."""
    global _active
    with _active_lock:
        if expect is None or _active is expect:
            _active = None


def current_session() -> Session | None:
    with _active_lock:
        return _active


def _standalone_session(adapter=None, *, sim=False, track=None) -> Session:
    if adapter is None:
        if sim:
            from .sim.drone import SimDrone

            adapter = SimDrone(track_name=track) if track else SimDrone()
        else:
            from .drone.mock import MockDrone

            adapter = MockDrone()
    adapter.connect()
    return Session(drone=adapter, get_frame=adapter.get_frame)


class Drone:
    """Your drone.

    ``Drone()`` on its own is what you want: launched with
    ``python -m comp2 --script yours.py --drone sim`` it picks up whichever
    drone the server connected to. ``Drone(sim=True)`` runs the simulator by
    itself, with no video window and no emergency-stop button.

    Every method checks the emergency stop before it does anything, so pressing
    STOP interrupts your program even from inside a ``while True:`` loop.
    """

    def __init__(
        self,
        adapter: DroneAdapter | None = None,
        *,
        sim: bool = False,
        track: str | None = None,
        _session: Session | None = None,
    ):
        # ``_session`` is the server's private doorway for a remote IDE
        # controller. Contestant code never receives the adapter itself, which
        # keeps simulator-only pose and scoring data outside the public API.
        session = _session or current_session()
        if session is None:
            session = _standalone_session(adapter, sim=sim, track=track)
        # captured, never re-read: a stopped script's Drone must stay pinned to
        # its own (stopped) session even after a later run binds a new one
        self._s = session
        self._d = session.drone

    # ------------------------------------------------------------------ safety

    def _check(self):
        if self._s.stop.is_set():
            raise EmergencyStop("emergency stop")

    def _act(self, fn, *args):
        self._check()
        fn(*args)
        self._check()  # a command that finished after STOP ends the script here

    def _warn(self, message: str):
        print(f"! {message}")  # the terminal they launched from
        self._s.emit({"type": "warning", "message": message})

    def _cm(self, name: str, cm) -> int:
        cm = round(cm)
        fixed = _clamp(cm, MIN_MOVE_CM, MAX_MOVE_CM)
        if fixed != cm:
            # nudge, don't crash — arithmetic producing 5 should not end the run
            self._warn(
                f"{name}({cm}) is outside {MIN_MOVE_CM}-{MAX_MOVE_CM} cm; using {fixed}"
            )
        return fixed

    def _deg(self, name: str, deg) -> int:
        deg = round(deg)
        fixed = _clamp(deg, MIN_TURN_DEG, MAX_TURN_DEG)
        if fixed != deg:
            self._warn(
                f"{name}({deg}) is outside {MIN_TURN_DEG}-{MAX_TURN_DEG} deg; using {fixed}"
            )
        return fixed

    def _rc(self, name: str, v) -> int:
        v = round(v)
        fixed = _clamp(v, -MAX_RC, MAX_RC)
        if fixed != v:
            self._warn(f"{name}({v}) is outside -{MAX_RC}..{MAX_RC}; using {fixed}")
        return fixed

    def _go(self, name: str, v) -> int:
        v = round(v)
        fixed = _clamp(v, -MAX_GO_CM, MAX_GO_CM)
        if fixed != 0 and abs(fixed) < GO_MIN_CM:
            self._warn(
                f"{name}({v}) is under the Tello's {GO_MIN_CM} cm per-axis "
                f"minimum; using 0"
            )
            return 0
        if fixed != v:
            self._warn(f"{name}({v}) is outside ±{MAX_GO_CM} cm; using {fixed}")
        return fixed

    def _speed(self, v) -> int:
        v = round(v)
        fixed = _clamp(v, MIN_SPEED_CMS, MAX_SPEED_CMS)
        if fixed != v:
            self._warn(
                f"speed({v}) is outside {MIN_SPEED_CMS}-{MAX_SPEED_CMS} cm/s; using {fixed}"
            )
        return fixed

    # ------------------------------------------------------------------ flight

    def takeoff(self):
        """Take off and hover. Do this before any other movement."""
        self._act(self._d.takeoff)

    def land(self):
        """Land and stop the motors."""
        self._act(self._d.land)

    def forward(self, cm: float):
        """Fly forwards `cm` centimetres (20-500)."""
        self._act(self._d.move, "forward", self._cm("forward", cm))

    def back(self, cm: float):
        """Fly backwards `cm` centimetres (20-500)."""
        self._act(self._d.move, "back", self._cm("back", cm))

    def left(self, cm: float):
        """Slide left `cm` centimetres, still facing the same way (20-500)."""
        self._act(self._d.move, "left", self._cm("left", cm))

    def right(self, cm: float):
        """Slide right `cm` centimetres, still facing the same way (20-500)."""
        self._act(self._d.move, "right", self._cm("right", cm))

    def up(self, cm: float):
        """Climb `cm` centimetres (20-500)."""
        self._act(self._d.move, "up", self._cm("up", cm))

    def down(self, cm: float):
        """Descend `cm` centimetres (20-500)."""
        self._act(self._d.move, "down", self._cm("down", cm))

    def turn_right(self, deg: float):
        """Turn clockwise by `deg` degrees, staying in the same spot."""
        self._act(self._d.rotate, "cw", self._deg("turn_right", deg))

    def turn_left(self, deg: float):
        """Turn anticlockwise by `deg` degrees, staying in the same spot."""
        self._act(self._d.rotate, "ccw", self._deg("turn_left", deg))

    def flip(self, direction: str = "back"):
        """Do a flip: "forward", "back", "left" or "right". Needs >50% battery."""
        self._act(self._d.flip, direction)

    def wait(self, seconds: float):
        """Hover in place for `seconds`. Interrupted immediately by the
        emergency stop."""
        self._check()
        if self._s.stop.wait(timeout=max(0.0, seconds)):
            raise EmergencyStop("emergency stop")

    # --------------------------------------------------------------- racing

    def fly(
        self,
        lr: float = 0,
        fb: float = 0,
        ud: float = 0,
        yaw: float = 0,
        seconds: float | None = None,
    ):
        """Velocity control — the racing primitive.

        Each channel is a stick percentage, -100..100: `lr` right(+)/left(-),
        `fb` forward(+)/back(-), `ud` up(+)/down(-), `yaw` clockwise(+).

        With ``seconds`` the setpoint is held for that long and then the drone
        hovers — the safe shape for a loop. Without it the setpoint *stays
        applied* until your next ``fly()`` or ``hover()``; the stop button
        always centres the sticks for you.
        """
        args = (
            self._rc("lr", lr),
            self._rc("fb", fb),
            self._rc("ud", ud),
            self._rc("yaw", yaw),
        )
        self._act(self._d.send_rc, *args)
        if seconds is not None:
            try:
                self.wait(seconds)
            finally:
                self._d.send_rc(0, 0, 0, 0)

    def hover(self):
        """Centre the sticks: stop moving and hold position."""
        self._act(self._d.send_rc, 0, 0, 0, 0)

    def go(self, x_cm: float = 0, y_cm: float = 0, z_cm: float = 0, speed: float = 50):
        """Fly a straight line to a body-frame offset without stopping between
        axes: `x_cm` forward, `y_cm` left, `z_cm` up (each 0 or ±20-500 cm),
        at `speed` cm/s (10-100)."""
        self._act(
            self._d.go_xyz,
            self._go("x_cm", x_cm),
            self._go("y_cm", y_cm),
            self._go("z_cm", z_cm),
            self._speed(speed),
        )

    def curve(
        self,
        x1: float,
        y1: float,
        z1: float,
        x2: float,
        y2: float,
        z2: float,
        speed: float = 50,
    ):
        """Fly a smooth arc through body-frame point 1, ending at point 2 —
        the way to carry speed through a gate that is off to one side."""
        self._act(
            self._d.curve_xyz,
            self._go("x1", x1),
            self._go("y1", y1),
            self._go("z1", z1),
            self._go("x2", x2),
            self._go("y2", y2),
            self._go("z2", z2),
            self._speed(speed),
        )

    # --------------------------------------------------------------- sensing

    def enable_mission_pads(self) -> None:
        """Require downward mission-pad support; call before takeoff."""
        self._act(self._d.enable_mission_pads)

    def disable_mission_pads(self) -> None:
        self._act(self._d.disable_mission_pads)

    def mission_pad(self) -> dict | None:
        """One fresh pad-local observation, or None when no pad is detected."""
        self._check()
        reading = self._d.mission_pad()
        self._check()
        return reading

    def center_on_mission_pad(
        self, pad_id: int, height_cm: float = 80, speed: float = 20
    ) -> None:
        """Centre above the specified visible pad at 30–120 cm; never land."""
        self._check()
        # Never silently substitute a different landmark identity.
        if type(pad_id) is not int or not 1 <= pad_id <= 8:
            raise ValueError("pad_id must be an integer from 1 to 8")
        height = round(height_cm)
        fixed = _clamp(height, 30, 120)
        if fixed != height:
            self._warn(f"pad height {height} cm outside 30–120; using {fixed}")
        self._act(self._d.center_on_mission_pad, pad_id, fixed, self._speed(speed))

    def frame(self):
        """The latest BGR camera frame, or None before the stream starts.

        This is the raw sensor image — run your own OpenCV gate detection on
        it. The simulator's frames come through the same camera model as the
        real Tello, so a detector tuned here transfers to the cage.
        """
        self._check()
        return self._s.get_frame()

    def show_detections(self, detections, *, size=None) -> None:
        """Outline what your own detector found, on the operator's video.

        Display only, like the simulator's minimap: the outlines are drawn on
        the copy the cockpit shows, never on the frames :meth:`frame` returns,
        so they can never feed back into your own detection.

        ``detections`` is a sequence of outlines in the pixel coordinates of
        the frame you analysed — each one a sequence of ``(x, y)`` points, or a
        ``(points, label)`` pair to caption it. Pass ``()`` when you see
        nothing, or the last box lingers for a moment and then clears::

            box = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
            drone.show_detections([(box, f"gate {distance:.2f} m")])

        ``size`` is ``(height, width)`` of that frame; by default the current
        camera frame's own size is used, which is what you want unless you
        detected on a resized image.
        """
        self._check()
        if size is None:
            size = getattr(self._s.get_frame(), "shape", None)
            if size is None:
                return  # no stream yet: nothing to draw the outline against
        self._s.show_overlay(overlay.markers(detections, size[1], size[0]))

    @property
    def battery(self) -> int:
        """Battery charge, 0-100."""
        self._check()
        return self._d.battery()

    @property
    def height(self) -> float:
        """Height estimate in cm (Tello SDK telemetry or simulated floor height).

        Missing, stale or invalid Tello telemetry raises RuntimeError.
        """
        self._check()
        reader = getattr(self._d, "height_cm", None)
        if callable(reader):
            return float(reader())
        z = getattr(self._d, "z", None)
        return round(z * 100, 1) if z is not None else 0.0


class _MirroredStream:
    """A script's ``print`` output, sent to the browser as well as the terminal.

    The operator watching the cockpit may not be the person at the launching
    terminal, so ``print`` — the first debugging tool anyone reaches for — also
    lands in the browser's system log. Writes are mirrored, not diverted: a
    ``--script`` author at a console keeps the output they already had.
    """

    def __init__(self, wrapped, emit: Callable[[dict], None], stream: str):
        self._wrapped = wrapped
        self._emit = emit
        self._stream = stream

    def write(self, text: str) -> int:
        if text:
            self._emit({"type": "output", "stream": self._stream, "text": text})
        if self._wrapped is not None:
            try:
                return self._wrapped.write(text)
            except Exception:
                pass
        return len(text)

    def flush(self) -> None:
        if self._wrapped is not None:
            try:
                self._wrapped.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True


class ScriptRun:
    """One team-script execution (``--script``).

    Exposes ``request_stop()`` plus ``await run()``, the same surface as the
    server's IDE connection, so the server can hold either in
    ``app.state.active_run`` and route stop and e-stop to it without caring
    which pathway is flying. That shared slot is also what makes a script run
    and an IDE connection mutually exclusive.

    ``path`` is the team's own Python file on disk, run exactly as
    ``python their_file.py`` would.

    The script body runs on a **daemon** thread of our own rather than
    ``asyncio.to_thread``: the interpreter joins executor threads at exit, so a
    runaway loop in a pooled thread would wedge shutdown.
    """

    def __init__(
        self,
        drone: DroneAdapter,
        emit: Callable[[dict], None],
        *,
        get_frame: Callable[[], object] | None = None,
        show_overlay: Callable[[tuple], None] | None = None,
        path: str | Path | None = None,
        name: str | None = None,
        grace_s: float = 3.0,
    ):
        self._drone = drone
        self._raw_emit = emit
        self.path = Path(path) if path else None
        self.name = name or (self.path.name if self.path else "program.py")
        self.grace_s = grace_s
        self.session = Session(
            drone=drone,
            get_frame=get_frame or drone.get_frame,
            emit=self._emit,
            show_overlay=show_overlay or (lambda markers: None),
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._fut: asyncio.Future | None = None
        self._watchdog = None
        self._emergency = False  # set by an EMERGENCY STOP: no recovery landing

    def _emit(self, ev: dict):
        loop = self._loop
        if loop is None or threading.get_ident() == self._loop_thread:
            self._raw_emit(ev)
        else:
            loop.call_soon_threadsafe(self._raw_emit, ev)  # worker thread -> loop

    def request_stop(self, emergency: bool = False):
        """Ask the script to stop. Called from the server's e-stop/stop handlers.

        ``emergency`` skips the recovery hover-and-land in :meth:`run`: the
        server is cutting the motors, and landing a falling aircraft is not a
        recovery.
        """
        if emergency:
            self._emergency = True
        self.session.stop.set()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._arm_watchdog)

    def _arm_watchdog(self):
        if self._watchdog is None and self._fut is not None and not self._fut.done():
            self._watchdog = self._loop.call_later(self.grace_s, self._abandon)

    def _abandon(self):
        """The script ignored the stop flag — give up waiting for its thread.

        Only reachable if the script is looping without calling any drone
        method. The drone is already stopped by the server, and the script's
        ``Drone`` can never command it again, so the only thing left to do is
        free the run slot.
        """
        self._resolve(
            "abandoned",
            "script did not stop — a loop with no drone commands? "
            "It can no longer control the drone.",
        )

    def _resolve(self, reason: str, detail: str):
        if self._fut is not None and not self._fut.done():
            self._fut.set_result((reason, detail))

    async def run(self, target: Callable[[], None] | None = None) -> tuple[str, str]:
        """Run the script (or a callable, for tests) and report how it ended."""
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._fut = self._loop.create_future()
        self._emit({"type": "script", "state": "started", "name": self.name})
        threading.Thread(
            target=self._worker, args=(target,), name="student-script", daemon=True
        ).start()
        reason, detail = await self._fut
        if self._watchdog is not None:
            self._watchdog.cancel()
        if reason != "done":
            clear_session(self.session)  # no-op unless the thread is abandoned
        # Completion also retires the command owner. A script can return (or
        # sys.exit) immediately after an unbounded fly(), leaving RC active.
        self.session.stop.set()
        if not self._emergency:
            # centre the sticks before landing: the script may have died with an
            # rc setpoint applied, and land() alone must not race a live stick.
            # Bounded, so a silent aircraft cannot hold the run slot for good.
            timeout = getattr(self._drone, "command_timeout_s", 45.0)
            for method, args in (("send_rc", (0, 0, 0, 0)), ("land", ())):
                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(getattr(self._drone, method), *args), timeout
                    )
                except Exception:
                    pass
        self._emit({"type": "script", "state": reason, "detail": detail})
        self._emit({"type": "finished", "reason": reason, "detail": detail})
        return reason, detail

    def _worker(self, target):
        bind_session(self.session)
        reason, detail = "done", ""
        # Only one run exists at a time (app.state.active_run is a single slot), so
        # swapping the process streams for the length of it is safe. uvicorn's
        # log handlers captured the real stderr at startup and are unaffected.
        out = _MirroredStream(sys.stdout, self._emit, "stdout")
        err = _MirroredStream(sys.stderr, self._emit, "stderr")
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    if self.session.stop.is_set():
                        raise EmergencyStop("stopped before start")
                    (target or self._load)()
                except EmergencyStop:
                    reason = "stopped"
                except SystemExit:
                    pass  # a script's sys.exit() is a clean finish
                except BaseException as exc:
                    reason, detail = "error", f"{type(exc).__name__}: {exc}"
                    self._print_traceback(exc)  # the author needs the line number
        finally:
            clear_session(self.session)
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._resolve, reason, detail)
            except RuntimeError:
                pass  # loop already closed — an abandoned script winding down

    @property
    def _origin(self) -> str:
        """The filename the author's own frames carry in a traceback."""
        return str(self.path) if self.path is not None else self.name

    def _print_traceback(self, exc: BaseException) -> None:
        """Print the traceback starting at the author's first frame.

        Everything above it — ``_worker``, ``_load``, ``runpy`` — is our
        plumbing, and a student reading five frames of it to find their own
        line number is five frames of noise.
        """
        tb = exc.__traceback__
        while tb is not None and tb.tb_frame.f_code.co_filename != self._origin:
            tb = tb.tb_next
        traceback.print_exception(type(exc), exc, tb or exc.__traceback__)

    def _load(self):
        # the team's own program, run exactly as `python their_file.py` would
        if self.path is None:
            raise ValueError("ScriptRun needs a path or a callable")
        runpy.run_path(str(self.path), run_name="__main__")
