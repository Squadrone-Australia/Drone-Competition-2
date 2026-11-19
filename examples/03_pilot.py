"""Fly the drone by keyboard, watch its camera, and record what it sees.

Start Squadrone first (`python -m comp2`) and leave its page open, then run
this file from your IDE, or:

    python examples/03_pilot.py [--url ws://127.0.0.1:8765/ws] [--record-dir DIR]

and press Connect.

Use it to look round the arena from the drone's own point of view, and to
collect video and stills of the gates from every angle and distance to build
and test your detector on. Nothing here detects a gate or flies by itself:
that part is your entry.

It is also a worked example of reading the camera while the drone flies: see
`Pilot._camera` below, which is the only code that touches `drone.frame()`.

Keys (click the video first, so that the window and not a text box has focus):

    T take off            L land            Space hover (let go of everything)
    W / S forward / back  A / D left / right
    R / F or Up / Down    up / down
    Q / E or Left / Right turn left / right
    V start / stop recording               P save a snapshot
    Esc STOP: disconnect, and Squadrone centres the sticks and lands

The drone moves only while a key, or an on-screen button, is held, at the
speeds on the two sliders (100% is about 1 m/s, or 100 deg/s, in the
simulator), and it stops when you let go. Keys combine: W and D together fly
diagonally. Under the hood each held moment is a `drone.fly(..., seconds=0.2)`
pulse, and Squadrone centres the sticks at the end of every pulse, so letting
go, the window losing focus, or this program dying all stop the aircraft
within one pulse. The cockpit's emergency stop always works as well. With a
real Tello, clear the flight area before you take off.

Recordings go to the record folder (default: `recordings/` in the current
folder). `flight_<time>.mp4` holds exactly the pixels `drone.frame()` returned,
without the text drawn over the picture here, and plays back in real time.
`snapshot_<time>.png` is a single raw frame.

Needs OpenCV and tkinter. The one-step setup's `venv` already has OpenCV; in
an interpreter of your own, `python -m pip install opencv-python`. tkinter
comes with the python.org and uv builds of Python (on Debian or Ubuntu:
apt install python3-tk).
"""

from __future__ import annotations

import argparse
import collections
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import cv2
import numpy as np
import squadrone
from PIL import Image, ImageTk
from squadrone import Drone, DroneError, EmergencyStop

PULSE_S = 0.2  # how long each fly() holds the sticks before Squadrone centres them
RECORD_FPS = 30.0
TELEMETRY_S = 3.0  # how often battery and height are read while nothing is held
DISPLAY_MAX_WIDTH = 960
ERROR_EVERY_S = 2.0  # a refused pulse is reported at most this often
# X11 autorepeat sends a release and a press for every repeat; a release
# followed this soon by a press of the same key is one long press.
RELEASE_DEBOUNCE_MS = 40

# Each stick key pushes one channel one way; opposite keys cancel out.
STICKS = {
    "w": ("fb", 1),
    "s": ("fb", -1),
    "d": ("lr", 1),
    "a": ("lr", -1),
    "r": ("ud", 1),
    "f": ("ud", -1),
    "e": ("yaw", 1),
    "q": ("yaw", -1),
}
ARROWS = {"Up": "r", "Down": "f", "Left": "q", "Right": "e"}


def stick_vector(held, speed: int, yaw_speed: int) -> dict[str, int]:
    """The `fly()` arguments for the keys held, at the given percentages."""
    sticks = {"lr": 0, "fb": 0, "ud": 0, "yaw": 0}
    for key in held:
        axis, sign = STICKS[key]
        sticks[axis] += sign * (yaw_speed if axis == "yaw" else speed)
    return sticks


def stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


class Recorder:
    """Writes camera frames to an MP4 that plays back at the speed it was flown.

    Frames do not arrive at a fixed rate, so each one is written as many times
    as the clock says are due since the recording began: a stall in the stream
    is a frozen picture in the video, not a jump cut.
    """

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.path: Path | None = None
        self._lock = threading.Lock()
        self._active = False
        self._writer = None
        self._size: tuple[int, int] | None = None
        self._t0 = 0.0
        self._written = 0

    @property
    def recording(self) -> bool:
        return self._active

    def elapsed_s(self) -> float:
        return self._written / RECORD_FPS

    def start(self) -> Path:
        with self._lock:
            self.folder.mkdir(parents=True, exist_ok=True)
            self.path = self.folder / f"flight_{stamp()}.mp4"
            self._writer, self._size, self._written = None, None, 0
            self._active = True
            return self.path

    def write(self, frame: np.ndarray, t: float) -> None:
        with self._lock:
            if not self._active:
                return
            if self._writer is None:
                h, w = frame.shape[:2]
                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                self._writer = cv2.VideoWriter(
                    str(self.path), fourcc, RECORD_FPS, (w, h)
                )
                self._size, self._t0 = (w, h), t
            if frame.shape[1::-1] != self._size:  # switched to a different camera
                frame = cv2.resize(frame, self._size)
            due = int((t - self._t0) * RECORD_FPS) + 1
            while self._written < due:
                self._writer.write(frame)
                self._written += 1

    def stop(self) -> Path | None:
        """Finish the file; its path, or None if no frame ever arrived."""
        with self._lock:
            was_active, self._active = self._active, False
            if self._writer is not None:
                self._writer.release()
                self._writer = None
            return self.path if was_active and self._written else None


class Pilot:
    def __init__(self, root: tk.Tk, args: argparse.Namespace):
        self.root = root
        self.recorder = Recorder(args.record_dir)
        self.drone: Drone | None = None
        self.stop = threading.Event()
        self.events: queue.Queue = queue.Queue()  # worker threads -> the window
        self.jobs: queue.Queue = queue.Queue()  # the window -> the flying thread
        self.busy = False  # a take off, land or hover is being flown
        self.connecting = False
        self.sticks = stick_vector((), 0, 0)
        self.battery: int | None = None
        self.height_cm: float | None = None
        # The camera thread's latest frame and its arrival times, for the window.
        self._frame_lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._frame_id = 0
        self._shown_id = 0
        self._arrivals: collections.deque = collections.deque(maxlen=30)
        self._photo = None
        self._held_keys: set[str] = set()
        self._held_mouse: set[str] = set()
        self._release_timers: dict[str, str] = {}
        self._error_t = -1e9

        root.title("Squadrone pilot")
        root.protocol("WM_DELETE_WINDOW", self._close)
        self.url = tk.StringVar(value=args.url or "")
        self.speed = tk.IntVar(value=40)
        self.yaw_speed = tk.IntVar(value=50)
        self._build()
        root.bind("<KeyPress>", self._key)
        root.bind("<KeyRelease>", self._key_release)
        # A release that never arrives (focus moved away mid-press) must not
        # leave the sticks deflected.
        root.bind("<FocusOut>", lambda _e: self._release_all())
        # Clicking a control hands the keys back to the window, so Space and
        # the arrows fly the drone instead of pressing that control.
        root.bind_all("<ButtonRelease-1>", self._refocus, add="+")
        self._refresh()
        root.after(30, self._tick)

    # ------------------------------------------------------------ layout

    def _build(self) -> None:
        left = ttk.Frame(self.root)
        left.pack(side="left", fill="both", expand=True, padx=6, pady=6)
        right = ttk.Frame(self.root)
        right.pack(side="right", fill="y", padx=6, pady=6)

        self.video = ttk.Label(left, text="No video", anchor="center")
        self.video.pack(fill="both", expand=True)
        self.log = tk.Text(left, height=8, width=90, state="disabled")
        self.log.pack(fill="x", pady=(6, 0))

        link = ttk.LabelFrame(right, text="Connection")
        link.pack(fill="x")
        ttk.Label(link, text="Address (blank: this computer)").pack(anchor="w", padx=4)
        ttk.Entry(link, textvariable=self.url, width=32).pack(padx=4, pady=(0, 4))
        self.connect_button = ttk.Button(link, text="Connect", command=self._connect)
        self.connect_button.pack(fill="x", padx=4, pady=(0, 4))

        fly = ttk.LabelFrame(right, text="Fly (hold to move)")
        fly.pack(fill="x", pady=6)
        self.flight_buttons: list[ttk.Button] = []
        row = ttk.Frame(fly)
        row.pack(fill="x", padx=4, pady=4)
        for text, call in (
            ("Take off (T)", self._takeoff),
            ("Land (L)", self._land),
            ("Hover (Space)", self._hover),
        ):
            button = ttk.Button(row, text=text, command=call)
            button.pack(side="left", padx=(0, 4))
            self.flight_buttons.append(button)
        sliders = ttk.Frame(fly)
        sliders.pack(fill="x", padx=4)
        for r, (text, var) in enumerate(
            (("Speed %", self.speed), ("Turn %", self.yaw_speed))
        ):
            ttk.Label(sliders, text=text).grid(row=r, column=0, sticky="w")
            tk.Scale(
                sliders, variable=var, from_=10, to=100, resolution=5,
                orient="horizontal", length=190, takefocus=0,
                command=lambda _v: self._update_sticks(),
            ).grid(row=r, column=1, sticky="ew")  # fmt: skip
        pad = ttk.Frame(fly)
        pad.pack(padx=4, pady=6)
        for label, key, r, c in (
            ("Turn left", "q", 0, 0),
            ("Forward", "w", 0, 1),
            ("Turn right", "e", 0, 2),
            ("Left", "a", 1, 0),
            ("Back", "s", 1, 1),
            ("Right", "d", 1, 2),
            ("Up", "r", 2, 0),
            ("Down", "f", 2, 2),
        ):
            button = ttk.Button(pad, text=f"{label} ({key.upper()})", width=13)
            # Press and release, not a click: it flies for as long as it is held.
            button.bind("<ButtonPress-1>", lambda _e, k=key: self._pad(k, True))
            button.bind("<ButtonRelease-1>", lambda _e, k=key: self._pad(k, False))
            button.grid(row=r, column=c, padx=1, pady=1)
            self.flight_buttons.append(button)

        camera = ttk.LabelFrame(right, text="Camera")
        camera.pack(fill="x", pady=6)
        self.record_button = ttk.Button(
            camera, text="Start recording (V)", command=self._toggle_record
        )
        self.record_button.pack(fill="x", padx=4, pady=(4, 2))
        self.snapshot_button = ttk.Button(
            camera, text="Save snapshot (P)", command=self._snapshot
        )
        self.snapshot_button.pack(fill="x", padx=4, pady=(2, 4))

        self.stop_button = ttk.Button(
            right, text="STOP: disconnect + land (Esc)", command=self._disconnect
        )
        self.stop_button.pack(fill="x", pady=6)
        self.status = ttk.Label(right, text="Not connected", wraplength=300)
        self.status.pack(fill="x")

    def _refresh(self) -> None:
        connected = self.drone is not None
        for button in self.flight_buttons:
            button.configure(
                state="normal" if connected and not self.busy else "disabled"
            )
        self.stop_button.configure(state="normal" if connected else "disabled")
        self.snapshot_button.configure(state="normal" if connected else "disabled")
        self.record_button.configure(
            text="Stop recording (V)"
            if self.recorder.recording
            else "Start recording (V)",
            state="normal" if connected or self.recorder.recording else "disabled",
        )
        if connected:
            self.connect_button.configure(text="Disconnect (lands)", state="normal")
        else:
            self.connect_button.configure(
                text="Connecting..." if self.connecting else "Connect",
                state="disabled" if self.connecting else "normal",
            )

    def _say(self, text: str, error: bool = False) -> None:
        self.status.configure(text=text, foreground="#b00020" if error else "")
        self._append(text)

    def _append(self, line: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _post(self, kind: str, payload=None) -> None:
        """Tk is single-threaded: other threads hand their news to the window."""
        self.events.put((kind, payload))

    # ------------------------------------------------------------ connect

    def _connect(self) -> None:
        if self.drone is not None:
            self._disconnect()
            return
        if self.connecting:
            return
        self.connecting = True
        self._say("Connecting...")
        self._refresh()
        url = self.url.get().strip() or None
        threading.Thread(target=self._open, args=(url,), daemon=True).start()

    def _open(self, url: str | None) -> None:
        try:
            drone = Drone(url)
        except squadrone.ConnectionError as exc:
            self._post("error", str(exc))
            return
        if drone.frame(timeout=10.0) is None:
            drone.close()
            self._post(
                "error",
                "No camera picture arrived. Is a drone selected in the cockpit?",
            )
            return
        self._post("connected", drone)

    def _start(self, drone: Drone) -> None:
        self.drone = drone
        self.stop = threading.Event()
        self.connecting = False
        self._release_all()  # keys held before connecting do not count
        for target in (self._camera, self._fly):
            threading.Thread(
                target=target, args=(drone, self.stop), daemon=True
            ).start()
        self._say("Connected. Take off with T, then hold W/A/S/D to fly.")

    def _disconnect(
        self,
        message: str = "Disconnected: Squadrone centres the sticks and lands",
        wait: bool = False,
    ) -> None:
        """Hand control back at once: closing the client lands the drone."""
        drone = self.drone
        if drone is None:
            return
        self.stop.set()
        self.drone = None
        self.busy = False
        self._release_all()
        self._finish_recording()
        if wait:
            drone.close()
        else:
            # close() waits for the socket to shut; keep the window responsive.
            threading.Thread(target=drone.close, daemon=True).start()
        self.video.configure(image="", text="No video")
        self._photo = None
        self._say(message, error=True)
        self._refresh()

    # ------------------------------------------------------------ threads

    def _camera(self, drone: Drone, stop: threading.Event) -> None:
        """Read the camera for as long as the session lasts.

        This is how to get frames while the drone flies: a thread of its own,
        so that waiting for a picture never holds up a command and a long
        command never holds up the pictures.

        `drone.frame(fresh=True)` waits for a picture that arrived *after* the
        call, so no frame is handled twice. It returns a NumPy uint8 array of
        shape (height, width, 3) in BGR order, ready for OpenCV, and a copy you
        may draw on. On a timeout, or once the session is over, it returns
        None, never a stale picture: always handle None.
        """
        while not stop.is_set():
            frame = drone.frame(timeout=1.0, fresh=True)
            if frame is None:
                stop.wait(0.05)  # no new picture: the stream paused, or we are closing
                continue
            now = time.monotonic()
            # Your own vision code would go here, on `frame`. This example only
            # records it and hands it to the window to show.
            self.recorder.write(frame, now)
            with self._frame_lock:
                self._frame = frame
                self._frame_id += 1
                self._arrivals.append(now)

    def _fly(self, drone: Drone, stop: threading.Event) -> None:
        """The one thread that sends commands, so they reach the drone in order."""
        last_telemetry = 0.0
        while not stop.is_set():
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                job = None
            sticks = self.sticks
            try:
                if job is not None:
                    label, command = job
                    command(drone)
                    self._post("done", f"{label}: done")
                elif any(sticks.values()):
                    # Blocks for PULSE_S, then Squadrone centres the sticks.
                    drone.fly(**sticks, seconds=PULSE_S)
                elif time.monotonic() - last_telemetry >= TELEMETRY_S:
                    last_telemetry = time.monotonic()
                    self.battery, self.height_cm = drone.battery, drone.height
                else:
                    stop.wait(0.03)
            except EmergencyStop as exc:
                self._post("ended", (drone, f"Emergency stop: {exc}"))
                return
            except squadrone.ConnectionError as exc:
                self._post("ended", (drone, f"Connection lost: {exc}"))
                return
            except DroneError as exc:
                if job is not None:
                    self._post("failed", f"{job[0]} failed: {exc}")
                elif time.monotonic() - self._error_t >= ERROR_EVERY_S:
                    self._error_t = time.monotonic()  # not five times a second
                    self._post("log", f"Drone error: {exc}")

    # ------------------------------------------------------------ commands

    def _submit(self, label: str, command) -> None:
        """One take off, land or hover at a time; a second press is ignored."""
        if self.drone is None or self.busy:
            return
        self.busy = True
        self._say(f"{label}...")
        self._refresh()
        self.jobs.put((label, command))

    def _takeoff(self) -> None:
        if self.drone is None or self.busy:
            return
        if messagebox.askokcancel(
            "Take off", "Is the flight area clear?\n\nTake off now?"
        ):
            self._submit("Take off", lambda d: d.takeoff())

    def _land(self) -> None:
        self._release_all()

        def land(drone: Drone) -> None:
            drone.hover()
            drone.land()

        self._submit("Land", land)

    def _hover(self) -> None:
        self._release_all()
        self._submit("Hover", lambda d: d.hover())

    # ---------------------------------------------------------- recording

    def _toggle_record(self) -> None:
        if self.recorder.recording:
            self._finish_recording()
        elif self.drone is not None:
            path = self.recorder.start()
            self._say(f"Recording to {path}")
        self._refresh()

    def _finish_recording(self) -> None:
        if not self.recorder.recording:
            return
        path = self.recorder.stop()
        self._say(
            f"Saved {path}" if path else "Recording stopped before any frame arrived"
        )

    def _snapshot(self) -> None:
        with self._frame_lock:
            frame = self._frame
        if frame is None or self.drone is None:
            return
        folder = self.recorder.folder
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"snapshot_{stamp()}_{int(time.time() * 1000) % 1000:03d}.png"
        cv2.imwrite(str(path), frame)
        self._say(f"Saved {path}")

    # ------------------------------------------------------------ sticks

    def _update_sticks(self) -> None:
        self.sticks = stick_vector(
            self._held_keys | self._held_mouse, self.speed.get(), self.yaw_speed.get()
        )

    def _release_all(self) -> None:
        for timer in self._release_timers.values():
            self.root.after_cancel(timer)
        self._release_timers.clear()
        self._held_keys.clear()
        self._held_mouse.clear()
        self._update_sticks()

    def _pad(self, key: str, down: bool) -> None:
        if down and (self.drone is None or self.busy):
            return  # a disabled ttk button still sees the press
        (self._held_mouse.add if down else self._held_mouse.discard)(key)
        self._update_sticks()

    def _refocus(self, event: tk.Event) -> None:
        if not isinstance(event.widget, (tk.Entry, ttk.Entry)):
            self.root.focus_set()

    # -------------------------------------------------------------- keys

    @staticmethod
    def _stick_key(event: tk.Event) -> str | None:
        key = ARROWS.get(event.keysym, event.keysym.lower())
        return key if key in STICKS else None

    def _key(self, event: tk.Event) -> None:
        if event.keysym == "Escape":
            self._disconnect()
            return
        if isinstance(self.root.focus_get(), (tk.Entry, ttk.Entry)):
            return  # typing an address
        stick = self._stick_key(event)
        if stick is not None:
            timer = self._release_timers.pop(stick, None)
            if timer is not None:  # an autorepeat, not a new press
                self.root.after_cancel(timer)
            if (
                stick not in self._held_keys
                and self.drone is not None
                and not self.busy
            ):
                self._held_keys.add(stick)
                self._update_sticks()
            return
        action = {
            "t": self._takeoff,
            "l": self._land,
            "space": self._hover,
            "v": self._toggle_record,
            "p": self._snapshot,
        }.get(event.keysym.lower())
        if action is not None:
            action()

    def _key_release(self, event: tk.Event) -> None:
        key = self._stick_key(event)
        if key is None or key not in self._held_keys:
            return
        if key in self._release_timers:
            self.root.after_cancel(self._release_timers[key])
        self._release_timers[key] = self.root.after(
            RELEASE_DEBOUNCE_MS, lambda: self._released(key)
        )

    def _released(self, key: str) -> None:
        self._release_timers.pop(key, None)
        self._held_keys.discard(key)
        self._update_sticks()

    # -------------------------------------------------------------- loop

    def _tick(self) -> None:
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "connected":
                self._start(payload)
            elif kind == "error":
                self.connecting = False
                self._say(payload, error=True)
            elif kind in ("done", "failed"):
                self.busy = False
                self._say(payload, error=kind == "failed")
            elif kind == "log":
                self._append(payload)
            elif kind == "ended":
                drone, message = payload
                if drone is self.drone:  # not a late word from an older session
                    self._disconnect(message)
            self._refresh()
        self._show_frame()
        self.root.after(30, self._tick)

    def _hud(self) -> list[str]:
        battery = "--" if self.battery is None else f"{self.battery}%"
        height = "--" if self.height_cm is None else f"{self.height_cm:.0f} cm"
        with self._frame_lock:
            times = list(self._arrivals)
        fps = (
            (len(times) - 1) / (times[-1] - times[0])
            if len(times) > 1 and times[-1] > times[0]
            else 0.0
        )
        lines = [f"battery {battery}   height {height}   {fps:.0f} fps"]
        held = {k: v for k, v in self.sticks.items() if v}
        if held:
            lines.append("sticks " + "  ".join(f"{k} {v:+d}" for k, v in held.items()))
        if self.recorder.recording:
            lines.append(f"REC {self.recorder.elapsed_s():.0f} s")
        return lines

    def _show_frame(self) -> None:
        with self._frame_lock:
            frame, frame_id = self._frame, self._frame_id
        if self.drone is None or frame is None or frame_id == self._shown_id:
            return
        self._shown_id = frame_id
        h, w = frame.shape[:2]
        scale = min(1.0, DISPLAY_MAX_WIDTH / w)
        view = (
            cv2.resize(frame, (round(w * scale), round(h * scale)))
            if scale < 1
            else frame.copy()
        )
        # Drawn on the window's copy only: the recording stays the raw camera.
        lines = self._hud()
        band = view[: 10 + 22 * len(lines)]
        band[:] = band // 3  # darken behind the text so it reads on any scene
        for i, line in enumerate(lines):
            colour = (60, 60, 255) if line.startswith("REC") else (255, 255, 255)
            cv2.putText(
                view,
                line,
                (10, 22 + 22 * i),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                colour,
                1,
                cv2.LINE_AA,
            )
        self._photo = ImageTk.PhotoImage(
            Image.fromarray(cv2.cvtColor(view, cv2.COLOR_BGR2RGB))
        )
        self.video.configure(image=self._photo, text="")

    def _close(self) -> None:
        self._disconnect(wait=True)  # the landing request goes out before we exit
        self.root.destroy()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", help="Squadrone address (default: this computer)")
    parser.add_argument(
        "--record-dir",
        type=Path,
        default=Path("recordings"),
        help="where videos and snapshots go",
    )
    args = parser.parse_args(argv)
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise SystemExit(f"Cannot open a window: {exc}") from exc
    Pilot(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()
