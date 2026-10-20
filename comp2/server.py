import asyncio
import json
import logging
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

import cv2
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from . import overlay
from .api import Drone, EmergencyStop, ScriptRun, Session
from .drone.base import DroneAdapter
from .sim.race import RUNNING, RaceScorer

log = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent / "frontend"
FRAME_INTERVAL = 0.1  # ~10 fps
# Pose is a handful of floats, so it can run far faster than video. It has to:
# at 10 Hz the third-person view stutters, and the race scorer's gate-crossing
# interpolation gets noticeably coarser.
POSE_INTERVAL = 1 / 30
RACE_TICK_INTERVAL = 0.25  # how often the running race clock is re-broadcast
SCRIPT_CLIENT_WAIT = 5.0  # how long a --script run holds off for the browser
REMOTE_FRAME_QUALITY = 90
REMOTE_MAX_FLY_SECONDS = 10.0
# How long shutdown waits for a landing or emergency stop still in flight.
SHUTDOWN_GRACE_S = 10.0
#: How often the watchdog looks at the hardware link, and how long it waits
#: between reconnection attempts. A reboot of the aircraft takes several
#: seconds; retrying faster than this only fills the console.
LINK_CHECK_INTERVAL = 2.0
#: How often an idle aircraft is asked ``battery?`` to prove it is still there.
#: Between runs it is the only proof when its camera is firewalled off. Never
#: while a run is flying: the SDK does not number its replies, so a poll slipped
#: in from here would queue behind a long move or be refused by a busy aircraft.
LIVENESS_INTERVAL = 5.0
#: What the operator is told when the aircraft answers but its video never
#: arrives: a firewall dropping :11111, or a drone some other program told
#: ``streamoff`` (only a reboot of the drone brings that video back).
VIDEO_BLOCKED_MESSAGE = (
    "Connected to the drone, but no video is arriving on UDP port 11111 - check "
    "the laptop firewall, or reboot the drone if another program turned its "
    "video off. The drone can still fly."
)
#: The other way the camera stays dark, told apart because its cure is on this
#: laptop: some other program already holds :11111.
VIDEO_PORT_BUSY_MESSAGE = (
    "Connected to the drone, but another program on this laptop is using the "
    "video port (UDP 11111), so the camera can't be shown. The drone can still fly."
)
#: The console line for each camera state, as ``(video_blocked, port_busy)``.
_VIDEO_MESSAGES = {
    (True, True): VIDEO_PORT_BUSY_MESSAGE,
    (True, False): VIDEO_BLOCKED_MESSAGE,
}

# The external client deliberately mirrors only the public Drone API. Keeping
# this as a fixed allow-list prevents a method name from becoming an arbitrary
# getattr into the server, its adapter, or simulator-only pose surface.
REMOTE_METHODS = frozenset(
    {
        "takeoff",
        "land",
        "forward",
        "back",
        "left",
        "right",
        "up",
        "down",
        "turn_right",
        "turn_left",
        "flip",
        "wait",
        "fly",
        "hover",
        "go",
        "curve",
        "battery",
        "height",
        "enable_mission_pads",
        "disable_mission_pads",
        "mission_pad",
        "center_on_mission_pad",
        # Display only: it draws the team's own detections on the operator's
        # copy of the video and can neither move the drone nor sense anything.
        "show_detections",
    }
)


@dataclass
class RemoteRun:
    """One external IDE connection occupying the shared run slot."""

    websocket: WebSocket
    session: Session
    controller: Drone
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    finishing: bool = False
    started: bool = False

    def request_stop(self, emergency: bool = False) -> None:
        # the IDE path never auto-lands on its own; `emergency` is accepted so
        # every occupant of the run slot shares one stop signature
        self.session.stop.set()


def _origin_allowed(ws: WebSocket) -> bool:
    """Whether this socket may be opened.

    A browser always sends ``Origin``. It must match the ``Host`` the page was
    served from *and* that host must be this machine. Matching alone is not
    enough: in a DNS-rebinding attack the attacker's page makes its own name
    resolve to 127.0.0.1, and the browser then sends an ``Origin`` and ``Host``
    that agree with each other, ``evil.example:8765`` for both.

    A connection with no ``Origin`` is not a browser — the ``squadrone`` IDE
    client, a test, a diagnostic script — and the same-origin policy never
    protected those, so they are left alone. A rebinding page cannot take that
    route: browsers always send ``Origin`` on a WebSocket handshake.
    """
    origin = ws.headers.get("origin")
    if origin is None:
        return True
    host = ws.headers.get("host")
    if not host or urlsplit(origin).netloc != host:
        return False
    return _is_this_machine(host)


# What a page on this machine is called in its address bar. The server only ever
# listens on loopback, so no other name is one the operator can have typed.
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_this_machine(host: str) -> bool:
    try:
        name = urlsplit(f"//{host}").hostname
    except ValueError:
        return False
    return name in _LOOPBACK_NAMES


class _RevalidatingStatic(StaticFiles):
    """The cockpit, served so a reload always sees the build on disk.

    Every script still carries a ``?v=`` token, but an ES module's *imports*
    carry their own: bump a module and forget the token inside the file that
    imports it, and the browser keeps serving the old one — a 3D view that
    stubbornly renders last week's gates while the video feed, which cannot be
    cached, shows the new ones. ``no-cache`` means revalidate, not refetch, so
    an unchanged asset is still a 304 off the local disk.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


def _new_tello() -> DroneAdapter:
    # Keep this lazy: a simulator launch must not touch the hardware pathway.
    # The adapter is constructed only after a deliberate click in the browser.
    from .drone.tello import TelloDrone

    return TelloDrone()


def _new_simulator() -> DroneAdapter:
    from .sim.drone import SimDrone

    return SimDrone()


def _retire_drone(adapter: DroneAdapter | None) -> None:
    """Release an adapter that is no longer flying. Never raises."""
    if adapter is None:
        return
    try:
        adapter.close()
    except Exception:
        pass


def create_app(
    drone: DroneAdapter,
    *,
    script: str | Path | None = None,
    script_delay: float = 1.5,
    tello_factory: Callable[[], DroneAdapter] = _new_tello,
    simulator_factory: Callable[[], DroneAdapter] = _new_simulator,
) -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        startup_error = None
        try:
            drone.connect()
        except Exception as exc:
            # `--drone tello` launched before the Wi-Fi is joined must not kill
            # the server. The watchdog keeps trying, so joining the network is
            # all the operator has to do, and the cockpit says what is wrong.
            drone.link_ok = False
            startup_error = f"could not connect to the drone: {exc}"
            log.warning(startup_error)
        app.state.drone = drone
        # Last link state broadcast to the browser. Tracked separately from
        # ``drone.link_ok`` so the watchdog reports transitions, not every tick.
        app.state.link_ok = startup_error is None
        app.state.link_message = startup_error
        # Last camera state broadcast: True while the aircraft answers but no
        # video has ever arrived, and whether another program holds :11111.
        app.state.video_blocked = False
        app.state.video_port_busy = False
        # Keep the configured simulator (noise, chosen track) available while a
        # real Tello is active so the UI can switch back to the same track
        # rather than silently creating a different one.
        app.state.simulator = drone if drone.mode == "sim" else None
        app.state.drone_switching = False
        app.state.latest_frame = None
        # What the flying program's own detector last found, for the display
        # copy of the video only. Written from a script/IDE worker thread, read
        # by the video loop: one attribute store each, never a partial update.
        app.state.overlay = ()
        app.state.overlay_at = 0.0
        app.state.clients = set()
        app.state.remote_run = None
        # ground truth for the race panel; None whenever the adapter has no
        # arena to score against (mock, real Tello)
        app.state.scorer = _new_scorer(app)
        app.state.race_sent = None
        # one slot for whatever is flying — a --script ScriptRun or an IDE
        # RemoteRun. Both expose request_stop(), so stop/e-stop routing below is
        # pathway-agnostic, and a second program cannot start while one is flying.
        app.state.active_run = None
        app.state.video_task = asyncio.create_task(_video_loop(app))
        app.state.pose_task = asyncio.create_task(_pose_loop(app))
        app.state.link_task = asyncio.create_task(_link_loop(app))
        app.state.liveness_task = asyncio.create_task(_liveness_loop(app))
        app.state.script_task = (
            asyncio.create_task(_script_loop(app)) if script else None
        )
        yield
        if app.state.remote_run is not None:
            await _finish_remote(
                app, app.state.remote_run, "shutdown", "Squadrone is shutting down"
            )
        # Detached work here lands or stops the drone, so it is waited for, not
        # cancelled — but only for so long: a silent aircraft must not hold the
        # program open for ever.
        if background_tasks:
            await asyncio.wait(list(background_tasks), timeout=SHUTDOWN_GRACE_S)
        app.state.video_task.cancel()
        app.state.pose_task.cancel()
        app.state.link_task.cancel()
        app.state.liveness_task.cancel()
        if app.state.script_task:
            # cancelling the task only drops the await — tell the script itself
            if isinstance(app.state.active_run, ScriptRun):
                app.state.active_run.request_stop()
            app.state.script_task.cancel()
        # Release both adapters: the video decoder thread is a daemon, and
        # leaving it reading while the interpreter tears the process down is
        # how a clean exit turns into a segfault.
        for adapter in {
            id(a): a for a in (app.state.drone, app.state.simulator) if a is not None
        }.values():
            await asyncio.to_thread(_retire_drone, adapter)

    app = FastAPI(lifespan=lifespan)

    async def _video_loop(app: FastAPI):
        while True:
            active_drone = app.state.drone
            try:
                frame = await asyncio.to_thread(active_drone.get_frame)
            except Exception:
                # A camera that throws must not take the video loop down with
                # it: this task never restarts, so the picture would stay black
                # even after the drone came back. The watchdog does the repair.
                active_drone.link_ok = False
                frame = None
            if frame is not None:
                # the raw sensor frame is what a team's own OpenCV should see;
                # the minimap and any other overlay go on a display copy only
                app.state.latest_frame = frame.copy()
                small = (
                    cv2.resize(frame, (640, 480))
                    if frame.shape[1] != 640
                    else frame.copy()
                )
                display = active_drone.annotate(small)
                markers = app.state.overlay
                if markers:
                    if time.monotonic() - app.state.overlay_at <= overlay.TTL_S:
                        display = overlay.draw(display, markers)
                    else:
                        app.state.overlay = ()
                ok, jpeg = cv2.imencode(".jpg", display, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ok:
                    await _broadcast_bytes(app, jpeg.tobytes())
                # The IDE receives a separately encoded copy of the raw sensor
                # frame. Display annotations never enter a team's detector.
                remote = app.state.remote_run
                if remote is not None:
                    raw_ok, raw_jpeg = cv2.imencode(
                        ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, REMOTE_FRAME_QUALITY]
                    )
                    if raw_ok:
                        await _remote_send_bytes(remote, raw_jpeg.tobytes())
            await asyncio.sleep(FRAME_INTERVAL)

    async def _pose_loop(app: FastAPI):
        """Feed the browser's third-person view and the race scorer.

        Pose broadcast is display data only — see ``DroneAdapter.pose``.
        Adapters that have no arena-absolute pose (mock, real Tello) return
        None and this loop idles.
        """
        last_drone = None
        last = None
        last_race_tick = 0.0
        while True:
            active_drone = app.state.drone
            if active_drone is not last_drone:
                last_drone = active_drone
                last = None
            pose = active_drone.pose()
            if pose is not None:
                if pose != last and app.state.clients:
                    last = pose
                    await _broadcast_json(app, {"type": "pose", **pose})
                scorer = app.state.scorer
                if scorer is not None:
                    # every sample counts, changed pose or not: a hovering drone
                    # still burns race time and still contributes to smoothness
                    changed = scorer.sample(pose, time.monotonic())
                    now = time.monotonic()
                    if changed or (
                        scorer.state_name == RUNNING
                        and now - last_race_tick >= RACE_TICK_INTERVAL
                    ):
                        last_race_tick = now
                        await _publish_race(app)
            await asyncio.sleep(POSE_INTERVAL)

    def _new_scorer(app: FastAPI):
        scene = app.state.drone.scene()
        return RaceScorer(scene) if scene else None

    def _current_track(app: FastAPI):
        scene = app.state.drone.scene()
        return scene.get("track") if scene else None

    async def _link_loop(app: FastAPI):
        """Watch the hardware link and put it back up by itself.

        A Tello that is rebooted mid-session comes back as a brand-new SDK
        session: the old one answers nothing and its video never resumes.
        Nothing in the protocol announces that, so it is noticed the only way
        it can be (the adapter stops getting frames and answers) and repaired
        by connecting again from scratch, rather than by restarting the program.
        """
        while True:
            await asyncio.sleep(LINK_CHECK_INTERVAL)
            active_drone = app.state.drone
            if app.state.drone_switching:
                continue
            blocked = bool(getattr(active_drone, "video_blocked", False))
            busy = blocked and bool(getattr(active_drone, "video_port_busy", False))
            if (blocked, busy) != (app.state.video_blocked, app.state.video_port_busy):
                # Not a link state: the aircraft still answers and still flies,
                # and reconnecting cannot open a firewall, so this is said once,
                # plainly, and the watchdog below leaves the link alone.
                app.state.video_blocked = blocked
                app.state.video_port_busy = busy
                await _publish_link(
                    app,
                    app.state.link_ok,
                    _VIDEO_MESSAGES.get(
                        (blocked, busy), "the drone's camera is working"
                    ),
                )
            if active_drone.link_ok:
                if not app.state.link_ok:
                    await _publish_link(app, True, "drone reconnected")
                continue
            if app.state.link_ok:
                await _publish_link(app, False, "lost contact with the drone")
            # Never reconnect underneath a flying program: it is issuing
            # commands on this same adapter from a worker thread. The run fails
            # on its first unanswered command anyway, and the retry below picks
            # the link up as soon as it has stopped.
            if app.state.active_run is not None:
                continue
            try:
                await asyncio.to_thread(active_drone.reconnect)
            except Exception:
                continue  # still gone: try again next tick, quietly
            if app.state.drone is active_drone and active_drone.link_ok:
                app.state.latest_frame = None
                await _publish_link(app, True, "drone reconnected")

    async def _liveness_loop(app: FastAPI):
        """Ask an idle aircraft for its charge, so a silent one is noticed.

        Video silence already clears ``link_ok``, but not when the camera has
        never worked (a firewall), and then this is the only signal left.
        """
        while True:
            await asyncio.sleep(LIVENESS_INTERVAL)
            active_drone = app.state.drone
            if (
                active_drone.mode != "tello"
                or not active_drone.link_ok
                or app.state.active_run is not None
                or app.state.drone_switching
            ):
                continue
            try:
                await _drone_call(active_drone, "battery")
            except Exception:
                active_drone.link_ok = False

    async def _publish_link(app: FastAPI, ok: bool, message: str | None):
        app.state.link_ok = ok
        app.state.link_message = message
        await _broadcast_json(app, _link_message(app))

    def _link_message(app: FastAPI) -> dict:
        return {
            "type": "drone_link",
            "ok": app.state.link_ok,
            "mode": app.state.drone.mode,
            "message": app.state.link_message,
            "video_blocked": app.state.video_blocked,
        }

    async def _publish_race(app: FastAPI):
        scorer = app.state.scorer
        if scorer is None:
            return
        state = scorer.state()
        if state != app.state.race_sent:
            app.state.race_sent = state
            await _broadcast_json(app, {"type": "race", **state})

    #: Strong references to detached work. The event loop only keeps a weak
    #: one, so a bare ``create_task`` can be collected mid-flight.
    background_tasks: set = set()

    def _set_overlay(markers: tuple) -> None:
        """Take detection markers from whatever is flying. Called off-loop."""
        app.state.overlay = markers
        app.state.overlay_at = time.monotonic()

    async def _drone_call(adapter: DroneAdapter, method: str, *args):
        """An adapter call bounded by its ``command_timeout_s``. Raises on
        timeout like any other failed command."""
        timeout = getattr(adapter, "command_timeout_s", 45.0)
        return await asyncio.wait_for(
            asyncio.to_thread(getattr(adapter, method), *args), timeout
        )

    def _spawn(coro):
        """Run ``coro`` detached, keeping it alive until it finishes."""
        task = asyncio.ensure_future(coro)
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        return task

    async def _broadcast_bytes(app, data: bytes):
        for ws in list(app.state.clients):
            try:
                await ws.send_bytes(data)
            except Exception:
                app.state.clients.discard(ws)

    async def _reply_error(ws: WebSocket, message: str):
        """Tell one browser something went wrong, without closing its socket."""
        try:
            await ws.send_text(json.dumps({"type": "error", "message": message}))
        except Exception:
            app.state.clients.discard(ws)

    async def _broadcast_json(app, data: dict):
        for ws in list(app.state.clients):
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                app.state.clients.discard(ws)

    async def _forward_session_event(run: RemoteRun, ev: dict) -> None:
        """An IDE session's event, to the page and to the IDE that caused it."""
        await _broadcast_json(app, ev)
        await _remote_send_json(run, ev)

    async def _remote_send_json(run: RemoteRun, data: dict) -> bool:
        try:
            async with run.send_lock:
                await run.websocket.send_text(json.dumps(data))
            return True
        except Exception:
            return False

    async def _remote_send_bytes(run: RemoteRun, data: bytes) -> bool:
        try:
            async with run.send_lock:
                await run.websocket.send_bytes(data)
            return True
        except Exception:
            return False

    async def _emergency_stop(app: FastAPI, remote: RemoteRun | None) -> None:
        """Cut the motors, release any IDE controller, and say honestly
        whether the cut reached the drone."""
        drone = app.state.drone
        # Centre explicitly before the emergency, on every path.
        try:
            await _drone_call(drone, "send_rc", 0, 0, 0, 0)
        except Exception:
            pass
        failure = None
        try:
            await _drone_call(drone, "emergency")
        except TimeoutError:
            failure = f"no answer within {drone.command_timeout_s:g}s"
        except Exception as exc:
            failure = str(exc) or type(exc).__name__
        # The controller is revoked either way: whatever happened to the motors,
        # the program that was flying must not keep sending commands.
        if remote is not None:
            await _finish_remote(app, remote, "estopped", "emergency stop", land=False)
        if failure is None:
            await _broadcast_json(app, {"type": "estopped", "ok": True})
        else:
            # Before, an unreachable drone raised straight out of the handler:
            # no `estopped`, no error, a dead socket, and a page that silently
            # reconnected as if the stop had simply happened.
            log.warning("emergency stop did not reach the drone: %s", failure)
            await _broadcast_json(
                app,
                {
                    "type": "estopped",
                    "ok": False,
                    "message": f"EMERGENCY STOP could not reach the drone: {failure}",
                },
            )

    async def _finish_remote(
        app: FastAPI,
        run: RemoteRun,
        reason: str,
        detail: str = "",
        *,
        land: bool = True,
    ) -> None:
        """Revoke an IDE controller, centre sticks, then land and release it.

        The sequence runs on a task of its own and is awaited through a shield.
        Callers include the IDE socket's ``finally`` block, and that coroutine
        can be *cancelled* rather than merely disconnected — a server shutting
        down, or a test client closing its socket. Cancelled inline, the
        teardown stopped at its first ``await``: the sticks were centred, but
        the drone was never landed, the run slot was never released, and nobody
        was told. Shielded, a cancelled caller stops waiting but the landing
        still happens.
        """
        if run.finishing:
            return
        run.finishing = True
        run.request_stop()
        await asyncio.shield(_spawn(_teardown_remote(app, run, reason, detail, land)))

    async def _teardown_remote(
        app: FastAPI, run: RemoteRun, reason: str, detail: str, land: bool
    ) -> None:
        # This order is a safety invariant on both simulator and hardware.
        try:
            await _drone_call(app.state.drone, "send_rc", 0, 0, 0, 0)
        except Exception:
            pass
        if land:
            try:
                await _drone_call(app.state.drone, "land")
            except Exception:
                pass

        if app.state.remote_run is run:
            app.state.remote_run = None
        if app.state.active_run is run:
            app.state.active_run = None
        # Nothing is detecting any more; the last outline must not hang over
        # the video as if it were current.
        app.state.overlay = ()

        await _remote_send_json(
            run, {"type": "control_ended", "reason": reason, "detail": detail}
        )
        await _broadcast_json(
            app, {"type": "python_client", "connected": False, "reason": reason}
        )
        if run.started:
            await _broadcast_json(
                app, {"type": "finished", "reason": reason, "detail": detail}
            )
        try:
            await run.websocket.close(code=1000)
        except Exception:
            pass

    async def _reset(app: FastAPI):
        """Start state, for the drone and for everything watching it."""
        active_drone = app.state.drone
        await asyncio.to_thread(active_drone.reset)
        app.state.overlay = ()  # a fresh attempt starts with a clean video
        app.state.scorer = _new_scorer(app)
        app.state.race_sent = None
        # `repositioned` is the difference between "the drone is on its pad" and
        # "the counters are zero but the aircraft has not moved". The browser must
        # not claim the former on hardware, where reset() is a no-op.
        await _broadcast_json(
            app, {"type": "reset", "repositioned": active_drone.can_reset}
        )
        await _publish_race(app)

    async def _load_track(app: FastAPI, name):
        """Switch tracks, then put everything watching back to the start."""
        active_drone = app.state.drone
        await asyncio.to_thread(lambda: active_drone.load_track(name))
        await _reset(app)
        await _broadcast_json(app, {"type": "scene", "scene": active_drone.scene()})
        await _broadcast_json(
            app,
            {
                "type": "tracks",
                "tracks": active_drone.track_catalog(),
                "current": _current_track(app),
            },
        )

    async def _load_arena(app: FastAPI, msg):
        """Switch how the room looks, then put everything watching back."""
        active_drone = app.state.drone
        if msg.get("randomize"):
            await asyncio.to_thread(
                lambda: active_drone.randomize_arena(msg.get("seed"))
            )
        else:
            parts = {k: v for k, v in (msg.get("parts") or {}).items() if v}
            await asyncio.to_thread(
                lambda: active_drone.load_arena(msg.get("name"), **parts)
            )
        await _reset(app)
        await _broadcast_json(app, {"type": "scene", "scene": active_drone.scene()})
        await _broadcast_json(app, _arena_message(active_drone))

    def _arena_message(active_drone):
        return {
            "type": "arena",
            "catalog": active_drone.arena_catalog(),
            "current": _arena_selection(active_drone),
        }

    def _arena_selection(active_drone):
        appearance = getattr(active_drone, "world", None)
        appearance = getattr(appearance, "appearance", None)
        if appearance is None:
            return None
        return {
            "name": getattr(active_drone, "arena_name", None),
            "light_intensity": appearance.light.intensity,
            "mount": appearance.mount,
        }

    async def _announce_drone(app: FastAPI, candidate: DroneAdapter):
        previous = app.state.drone
        app.state.drone = candidate
        app.state.drone_switching = False
        # Let go of what we just stopped flying — except the configured
        # simulator, which is kept warm so switching back restores the same
        # track. A retired Tello still owns its video port and decoder thread.
        if previous is not candidate and previous is not app.state.simulator:
            await asyncio.to_thread(_retire_drone, previous)
        app.state.latest_frame = None
        app.state.scorer = _new_scorer(app)
        app.state.race_sent = None
        app.state.video_blocked = False
        app.state.video_port_busy = False
        await _publish_link(app, candidate.link_ok, None)
        await _broadcast_json(app, {"type": "scene", "scene": candidate.scene()})
        await _broadcast_json(
            app,
            {
                "type": "tracks",
                "tracks": candidate.track_catalog(),
                "current": _current_track(app),
            },
        )
        await _broadcast_json(
            app, {"type": "drone_mode", "mode": candidate.mode, "switching": False}
        )

    async def _switch_to_tello(app: FastAPI):
        """Connect hardware first, then atomically make it the active adapter.

        Keeping the simulator active until ``connect`` succeeds means a missing
        Tello or incorrect Wi-Fi never leaves the application without a usable
        drone. Runs are refused while the connection attempt is in flight.

        Also the manual reconnect path: a rebooted Tello needs a whole new
        adapter, and the old one is closed *first*, because the command and
        video ports are fixed and the new link could not bind them otherwise.
        """
        app.state.drone_switching = True
        await _broadcast_json(
            app, {"type": "drone_mode", "mode": app.state.drone.mode, "switching": True}
        )
        if app.state.drone.mode == "tello":
            await asyncio.to_thread(_retire_drone, app.state.drone)
        candidate = None
        try:
            candidate = tello_factory()
            await asyncio.to_thread(candidate.connect)
        except Exception as exc:
            if candidate is not None:
                # half-open sockets are still sockets
                await asyncio.to_thread(_retire_drone, candidate)
            app.state.drone_switching = False
            await _broadcast_json(
                app,
                {
                    "type": "drone_mode",
                    "mode": app.state.drone.mode,
                    "switching": False,
                },
            )
            await _broadcast_json(
                app, {"type": "error", "message": f"could not connect to Tello: {exc}"}
            )
            return
        await _announce_drone(app, candidate)

    async def _switch_to_simulator(app: FastAPI):
        """Restore the configured simulator, creating one for Tello-first launches."""
        app.state.drone_switching = True
        await _broadcast_json(
            app, {"type": "drone_mode", "mode": app.state.drone.mode, "switching": True}
        )
        try:
            candidate = app.state.simulator or simulator_factory()
            if app.state.simulator is None:
                await asyncio.to_thread(candidate.connect)
                app.state.simulator = candidate
        except Exception as exc:
            app.state.drone_switching = False
            await _broadcast_json(
                app,
                {
                    "type": "drone_mode",
                    "mode": app.state.drone.mode,
                    "switching": False,
                },
            )
            await _broadcast_json(
                app, {"type": "error", "message": f"could not start simulator: {exc}"}
            )
            return
        await _announce_drone(app, candidate)

    def _emit(ev: dict):
        """Broadcast an event. Must be called on the event loop thread —
        ``ScriptRun`` marshals its worker thread's events through it."""
        asyncio.ensure_future(_broadcast_json(app, ev))

    async def _script_loop(app: FastAPI):
        # don't fly before anyone can see it — wait for the browser, but not
        # forever, so `--no-browser --script` still runs
        await asyncio.sleep(script_delay)
        waited = 0.0
        while not app.state.clients and waited < SCRIPT_CLIENT_WAIT:
            await asyncio.sleep(0.05)
            waited += 0.05
        # A click during the short startup delay may still be connecting the
        # Tello. Never capture the simulator halfway through that handoff and
        # then run it invisibly behind the hardware camera feed.
        while app.state.drone_switching:
            await asyncio.sleep(0.05)
        run = ScriptRun(
            app.state.drone,
            _emit,
            get_frame=lambda: app.state.latest_frame,
            show_overlay=_set_overlay,
            path=script,
        )
        app.state.active_run = run
        try:
            await run.run()
        finally:
            if app.state.active_run is run:
                app.state.active_run = None

    async def _python_endpoint(ws: WebSocket) -> None:
        """Serve one IDE-run Python process through the public Drone surface."""
        if app.state.drone_switching:
            await ws.send_text(
                json.dumps(
                    {
                        "type": "hello",
                        "accepted": False,
                        "message": "wait for the drone connection",
                    }
                )
            )
            await ws.close(code=1008)
            return
        if app.state.active_run is not None:
            await ws.send_text(
                json.dumps(
                    {
                        "type": "hello",
                        "accepted": False,
                        "message": "another program is already connected or running",
                    }
                )
            )
            await ws.close(code=1008)
            return

        # Claim the shared slot before the first await. A simultaneous target
        # switch or second IDE connection must see the claim and stop rather
        # than swapping the adapter during setup.
        loop = asyncio.get_running_loop()
        holder: dict = {}

        def session_emit(ev: dict) -> None:
            # The controller runs on a worker thread. Without this the session
            # had no emit at all, so a warning from an IDE call — a clamped
            # distance, say — printed only to the server's own terminal, which
            # the team at the IDE never sees.
            loop.call_soon_threadsafe(_spawn, _forward_session_event(holder["run"], ev))

        session = Session(
            drone=app.state.drone,
            get_frame=lambda: app.state.latest_frame,
            emit=session_emit,
            show_overlay=_set_overlay,
        )
        run = RemoteRun(ws, session, Drone(_session=session))
        holder["run"] = run
        app.state.remote_run = run
        app.state.active_run = run
        # Connecting from the IDE begins a fresh practice attempt, just like
        # pressing Run in the legacy browser pathway.
        await _reset(app)
        await _remote_send_json(
            run,
            {
                "type": "hello",
                "accepted": True,
                "protocol": 1,
                "drone": app.state.drone.mode,
                "message": "connected to Squadrone",
            },
        )
        await _broadcast_json(
            app,
            {"type": "python_client", "connected": True, "drone": app.state.drone.mode},
        )

        try:
            while app.state.remote_run is run and not run.finishing:
                try:
                    msg = json.loads(await ws.receive_text())
                except (json.JSONDecodeError, KeyError):
                    msg = (
                        None  # not JSON, or a binary frame (receive_text has no "text")
                    )
                if not isinstance(msg, dict):
                    # Answered like any other unsupported message. Before, JSON
                    # that was not an object raised AttributeError out of this
                    # loop and tore the session down mid-flight.
                    await _remote_send_json(
                        run,
                        {"type": "error", "message": "messages must be JSON objects"},
                    )
                    continue
                kind = msg.get("type")
                if kind == "heartbeat":
                    await _remote_send_json(run, {"type": "heartbeat"})
                    continue
                if kind == "release":
                    await _finish_remote(app, run, "done")
                    return
                if kind != "call":
                    await _remote_send_json(
                        run,
                        {
                            "type": "error",
                            "message": "unsupported Python client message",
                        },
                    )
                    continue

                call_id = msg.get("id")
                method_name = msg.get("method")
                args = msg.get("args", {})
                if method_name not in REMOTE_METHODS or not isinstance(args, dict):
                    await _remote_send_json(
                        run,
                        {
                            "type": "result",
                            "id": call_id,
                            "ok": False,
                            "error": "unsupported Drone operation",
                        },
                    )
                    continue
                if method_name == "fly":
                    seconds = args.get("seconds")
                    if (
                        not isinstance(seconds, (int, float))
                        or isinstance(seconds, bool)
                        or not 0 <= seconds <= REMOTE_MAX_FLY_SECONDS
                    ):
                        await _remote_send_json(
                            run,
                            {
                                "type": "result",
                                "id": call_id,
                                "ok": False,
                                "error": (
                                    "external fly() requires seconds between 0 and "
                                    f"{REMOTE_MAX_FLY_SECONDS:g}"
                                ),
                            },
                        )
                        continue

                if not run.started and method_name == "takeoff":
                    run.started = True
                    await _broadcast_json(
                        app,
                        {"type": "script", "state": "started", "name": "external IDE"},
                    )
                await _broadcast_json(
                    app,
                    {
                        "type": "execution",
                        "kind": "call",
                        "adapter": app.state.drone.mode,
                        "method": method_name,
                        "args": list(args.values()),
                    },
                )
                try:
                    if method_name in ("battery", "height"):
                        result = await asyncio.to_thread(
                            partial(getattr, run.controller, method_name)
                        )
                    else:
                        method = getattr(run.controller, method_name)
                        result = await asyncio.to_thread(partial(method, **args))
                except EmergencyStop:
                    await _remote_send_json(
                        run,
                        {
                            "type": "result",
                            "id": call_id,
                            "ok": False,
                            "error": "control session stopped",
                            "stopped": True,
                        },
                    )
                    return
                except Exception as exc:
                    await _remote_send_json(
                        run,
                        {
                            "type": "result",
                            "id": call_id,
                            "ok": False,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
                else:
                    await _remote_send_json(
                        run,
                        {"type": "result", "id": call_id, "ok": True, "result": result},
                    )
                    if method_name == "land" and run.started:
                        run.started = False
                        await _broadcast_json(
                            app, {"type": "finished", "reason": "done", "detail": ""}
                        )
        except WebSocketDisconnect:
            pass
        finally:
            await _finish_remote(app, run, "disconnected", "Python client disconnected")

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        if not _origin_allowed(ws):
            # WebSockets are outside the same-origin policy, so without this any
            # page open in the same browser could connect as `?role=python` and
            # take the controller: takeoff, fly, flip.
            await ws.close(code=1008)
            return
        await ws.accept()
        if ws.query_params.get("role") == "python":
            await _python_endpoint(ws)
            return
        app.state.clients.add(ws)

        # The track is sent per client on connect and re-broadcast to everyone
        # whenever the picker changes it — it is static for the length of a run,
        # not for the length of the session.
        active_drone = app.state.drone
        await ws.send_text(json.dumps({"type": "scene", "scene": active_drone.scene()}))
        # Pose is state as well as animation. A stationary simulator will not
        # trigger the change-only pose loop after a browser reconnects.
        initial_pose = active_drone.pose()
        if initial_pose is not None:
            await ws.send_text(json.dumps({"type": "pose", **initial_pose}))
        await ws.send_text(
            json.dumps(
                {
                    "type": "tracks",
                    "tracks": active_drone.track_catalog(),
                    "current": _current_track(app),
                }
            )
        )
        if hasattr(active_drone, "arena_catalog"):
            await ws.send_text(json.dumps(_arena_message(active_drone)))
        if app.state.scorer is not None:
            await ws.send_text(json.dumps({"type": "race", **app.state.scorer.state()}))
        await ws.send_text(
            json.dumps(
                {
                    "type": "drone_mode",
                    "mode": active_drone.mode,
                    "switching": app.state.drone_switching,
                }
            )
        )
        await ws.send_text(json.dumps(_link_message(app)))
        await ws.send_text(
            json.dumps(
                {
                    "type": "python_client",
                    "connected": app.state.remote_run is not None,
                    "drone": active_drone.mode,
                }
            )
        )

        try:
            while True:
                msg = None
                try:
                    try:
                        msg = json.loads(await ws.receive_text())
                    except (json.JSONDecodeError, KeyError):
                        # not JSON, or a binary frame (receive_text has no "text")
                        await _reply_error(
                            ws,
                            "that message was not something this program understands",
                        )
                        continue
                    # The page only ever sends objects with a string `type`;
                    # anything else is not worth this connection.
                    if not isinstance(msg, dict) or not isinstance(
                        msg.get("type"), str
                    ):
                        await _reply_error(ws, "message had no type")
                        continue
                    if msg["type"] == "stop":
                        if app.state.remote_run is not None:
                            # Detached: landing a drone that has stopped answering
                            # takes up to its whole timeout, and awaiting that here
                            # would freeze every other control on this page.
                            _spawn(
                                _finish_remote(
                                    app,
                                    app.state.remote_run,
                                    "stopped",
                                    "stopped from the browser",
                                )
                            )
                        elif app.state.active_run:
                            app.state.active_run.request_stop()
                    elif msg["type"] == "reset":
                        if app.state.drone_switching:
                            await _broadcast_json(
                                app,
                                {
                                    "type": "error",
                                    "message": "wait for the drone connection",
                                },
                            )
                        elif app.state.active_run is not None:
                            await _broadcast_json(
                                app,
                                {
                                    "type": "error",
                                    "message": "stop the run before resetting",
                                },
                            )
                        else:
                            await _reset(app)
                    elif msg["type"] == "track":
                        # Switching tracks mid-flight would move the gates out from
                        # under a running race, so it waits — same rule as reset.
                        if app.state.drone_switching:
                            await _broadcast_json(
                                app,
                                {
                                    "type": "error",
                                    "message": "wait for the drone connection",
                                },
                            )
                        elif app.state.drone.track_catalog() is None:
                            await _broadcast_json(
                                app,
                                {
                                    "type": "error",
                                    "message": "this drone has no track to switch",
                                },
                            )
                        elif app.state.active_run is not None:
                            await _broadcast_json(
                                app,
                                {
                                    "type": "error",
                                    "message": "stop the run before changing the track",
                                },
                            )
                        else:
                            await _load_track(app, msg.get("name"))
                    elif msg["type"] == "arena":
                        # Same rule as a track change: swapping the scenery
                        # mid-flight would move the world under a running race.
                        if app.state.drone_switching:
                            await _broadcast_json(
                                app,
                                {
                                    "type": "error",
                                    "message": "wait for the drone connection",
                                },
                            )
                        elif not hasattr(app.state.drone, "arena_catalog"):
                            await _broadcast_json(
                                app,
                                {
                                    "type": "error",
                                    "message": "this drone has no arena to switch",
                                },
                            )
                        elif app.state.active_run is not None:
                            await _broadcast_json(
                                app,
                                {
                                    "type": "error",
                                    "message": "stop the run before changing the arena",
                                },
                            )
                        else:
                            await _load_arena(app, msg)
                    elif msg["type"] == "estop":
                        if app.state.active_run:
                            app.state.active_run.request_stop(emergency=True)
                        # Not awaited here: this is the page's receive loop, and a
                        # drone that has stopped answering would stall every later
                        # message from it — a second press included.
                        _spawn(_emergency_stop(app, app.state.remote_run))
                    elif msg["type"] == "reconnect_drone":
                        if app.state.drone.mode != "tello":
                            await _reply_error(
                                ws, "only the real Tello can be reconnected"
                            )
                        elif app.state.active_run is not None:
                            await _reply_error(ws, "stop the run before reconnecting")
                        elif app.state.drone_switching:
                            await _reply_error(
                                ws, "a drone connection is already in progress"
                            )
                        else:
                            await _switch_to_tello(app)
                    elif msg["type"] == "switch_drone":
                        requested_mode = msg.get("mode")
                        if requested_mode not in ("sim", "tello"):
                            await _broadcast_json(
                                app,
                                {"type": "error", "message": "unsupported drone mode"},
                            )
                        elif app.state.drone.mode == requested_mode:
                            await _broadcast_json(
                                app,
                                {
                                    "type": "drone_mode",
                                    "mode": requested_mode,
                                    "switching": False,
                                },
                            )
                        elif app.state.active_run is not None:
                            await _broadcast_json(
                                app,
                                {
                                    "type": "error",
                                    "message": "stop the run before switching drones",
                                },
                            )
                        elif app.state.drone_switching:
                            await _broadcast_json(
                                app,
                                {
                                    "type": "error",
                                    "message": "a drone connection is already in progress",
                                },
                            )
                        elif requested_mode == "tello":
                            await _switch_to_tello(app)
                        else:
                            await _switch_to_simulator(app)
                    else:
                        # Silently ignoring an unknown type makes a version
                        # mismatch look like a button that does nothing.
                        await _reply_error(
                            ws, f"this program does not understand '{msg['type']}'"
                        )
                except WebSocketDisconnect:
                    raise
                except Exception as exc:
                    # One malformed or unlucky message must never take the socket
                    # with it: the page would reconnect in silence and the student
                    # would just see their controls stop working.
                    kind = msg.get("type") if isinstance(msg, dict) else "unreadable"
                    log.exception("error handling %s message", kind)
                    await _reply_error(ws, f"could not do that: {exc}")
        except WebSocketDisconnect:
            pass
        finally:
            app.state.clients.discard(ws)

    if FRONTEND_DIR.exists():
        app.mount(
            "/", _RevalidatingStatic(directory=FRONTEND_DIR, html=True), name="frontend"
        )
    return app
