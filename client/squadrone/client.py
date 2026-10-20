"""Synchronous client for code run from a contestant's own IDE.

The Squadrone process owns the simulator or Tello. This module owns no drone
model and receives no arena-absolute pose; it exposes only the competition API
and the raw forward camera frame.
"""

from __future__ import annotations

import atexit
import io
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from itertools import count
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import numpy as np
from PIL import Image
from websockets.sync.client import connect

DEFAULT_URL = "ws://127.0.0.1:8765/ws"

# `control_ended` reasons that mean the operator took control away. Anything
# else (shutdown, disconnected, a dropped socket) is a lost connection.
_STOP_REASONS = frozenset({"estopped", "stopped"})
# What servers that predate the explicit `stopped` flag send for a stopped call.
_LEGACY_STOP_ERROR = "control session stopped"


class DroneError(RuntimeError):
    """A command was rejected or failed inside Squadrone."""


class ConnectionError(DroneError):
    """The Squadrone application is unavailable or disconnected."""


class EmergencyStop(BaseException):
    """The browser operator stopped this control session."""


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    reply: dict[str, Any] | None = None


def _python_url(url: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["role"] = "python"
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def _wire_detections(detections) -> list:
    """Detector output as plain JSON: [[[x, y], ...], label] per outline."""
    shapes = []
    for detection in detections or ():
        items = list(detection)
        label = ""
        if len(items) == 2 and isinstance(items[1], str):
            items, label = list(items[0]), items[1]
        shapes.append([[[float(p[0]), float(p[1])] for p in items], str(label)])
    return shapes


class Drone:
    """The simulator or Tello currently selected in the Squadrone browser.

    Construct this class after starting Squadrone. Calls are synchronous, so
    IDE breakpoints and tracebacks behave like an ordinary local Python API.
    """

    # Why the session ended, once it has. Class-level so a half-built instance
    # (a test double, or an __init__ that failed early) still answers.
    _stopped = False
    _end_detail: str | None = None

    def __init__(self, url: str | None = None, *, timeout: float = 30.0):
        base_url = url or os.environ.get("SQUADRONE_URL", DEFAULT_URL)
        self.url = _python_url(base_url)
        self.timeout = timeout
        self._socket = None
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._frame_ready = threading.Condition(self._state_lock)
        self._hello = threading.Event()
        self._hello_reply: dict[str, Any] | None = None
        self._pending: dict[int, _Pending] = {}
        self._ids = count(1)
        self._frame: np.ndarray | None = None
        self._frame_sequence = 0
        self._closed = False
        self._reader: threading.Thread | None = None

        try:
            self._socket = connect(
                self.url,
                open_timeout=timeout,
                close_timeout=min(timeout, 3.0),
                max_size=4 * 1024 * 1024,
            )
        except Exception as exc:
            raise ConnectionError(
                f"Could not connect to Squadrone at {base_url}. "
                "Start the Squadrone application, then run this script again."
            ) from exc

        self._reader = threading.Thread(
            target=self._read_messages, name="squadrone-client", daemon=True
        )
        self._reader.start()
        if not self._hello.wait(timeout):
            self.close()
            raise ConnectionError("Squadrone did not complete the client handshake")
        hello = self._hello_reply or {}
        if not hello.get("accepted"):
            self.close()
            raise ConnectionError(
                hello.get("message") or "Squadrone refused the connection"
            )
        atexit.register(self.close)

    def _read_messages(self) -> None:
        error = "Squadrone disconnected"
        stopped = False
        try:
            for message in self._socket:
                if isinstance(message, bytes):
                    try:
                        with Image.open(io.BytesIO(message)) as image:
                            rgb = np.asarray(image.convert("RGB"))
                    except Exception:
                        # One damaged JPEG is a dropped frame, not a dead
                        # session: frame() already copes with a gap.
                        continue
                    frame = rgb[:, :, ::-1].copy()
                    with self._frame_ready:
                        self._frame = frame
                        self._frame_sequence += 1
                        self._frame_ready.notify_all()
                    continue
                try:
                    data = json.loads(message)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                kind = data.get("type")
                if kind == "hello":
                    self._hello_reply = data
                    self._hello.set()
                elif kind == "result":
                    with self._state_lock:
                        pending = self._pending.get(data.get("id"))
                    if pending is not None:
                        pending.reply = data
                        pending.event.set()
                elif kind == "control_ended":
                    error = data.get("detail") or data.get("reason") or error
                    stopped = data.get("reason") in _STOP_REASONS
                elif kind == "warning":
                    # the same "! ..." a script run from a terminal prints
                    print(f"! {data.get('message', '')}", file=sys.stderr, flush=True)
        except Exception as exc:
            error = str(exc) or error
        finally:
            self._hello.set()
            with self._frame_ready:
                self._stopped = stopped
                self._end_detail = error
                self._closed = True
                pending = list(self._pending.values())
                self._frame_ready.notify_all()
            for request in pending:
                if request.reply is None:
                    request.reply = {
                        "ok": False,
                        "error": error,
                        "stopped": stopped,
                        "disconnected": not stopped,
                    }
                request.event.set()

    def _ended(self) -> DroneError | EmergencyStop:
        """The exception for using a session that is already over.

        A stop stays a stop, so a loop that swallows DroneError cannot keep
        issuing commands after the operator pressed Stop.
        """
        if self._stopped:
            return EmergencyStop(self._end_detail or "control session stopped")
        return ConnectionError(self._end_detail or "Squadrone is not connected")

    def _send(self, message: dict[str, Any]) -> None:
        if self._closed or self._socket is None:
            raise self._ended()
        try:
            with self._send_lock:
                self._socket.send(json.dumps(message))
        except Exception as exc:
            raise ConnectionError("Could not send a command to Squadrone") from exc

    def _call(self, method: str, **args):
        call_id = next(self._ids)
        pending = _Pending()
        with self._state_lock:
            self._pending[call_id] = pending
        try:
            self._send({"type": "call", "id": call_id, "method": method, "args": args})
            if not pending.event.wait(self.timeout):
                raise ConnectionError(f"Squadrone timed out while running {method}()")
            reply = pending.reply or {}
            if not reply.get("ok"):
                message = reply.get("error") or f"{method}() failed"
                # Never infer a stop from the wording: the Tello's own errors
                # say things like "the drone's motors are stopped".
                if reply.get("stopped") or message == _LEGACY_STOP_ERROR:
                    raise EmergencyStop(message)
                if reply.get("disconnected"):
                    raise ConnectionError(message)
                raise DroneError(message)
            return reply.get("result")
        finally:
            with self._state_lock:
                self._pending.pop(call_id, None)

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._send({"type": "release"})
        except (DroneError, EmergencyStop):
            pass  # the session ended while we were closing it; nothing to release
        finally:
            self._closed = True
            if self._socket is not None:
                try:
                    self._socket.close()
                except Exception:
                    pass

    def __enter__(self) -> Drone:
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def takeoff(self) -> None:
        self._call("takeoff")

    def land(self) -> None:
        self._call("land")

    def forward(self, cm: float) -> None:
        self._call("forward", cm=cm)

    def back(self, cm: float) -> None:
        self._call("back", cm=cm)

    def left(self, cm: float) -> None:
        self._call("left", cm=cm)

    def right(self, cm: float) -> None:
        self._call("right", cm=cm)

    def up(self, cm: float) -> None:
        self._call("up", cm=cm)

    def down(self, cm: float) -> None:
        self._call("down", cm=cm)

    def turn_right(self, deg: float) -> None:
        self._call("turn_right", deg=deg)

    def turn_left(self, deg: float) -> None:
        self._call("turn_left", deg=deg)

    def flip(self, direction: str = "back") -> None:
        self._call("flip", direction=direction)

    def wait(self, seconds: float) -> None:
        self._call("wait", seconds=seconds)

    def fly(
        self,
        lr: float = 0,
        fb: float = 0,
        ud: float = 0,
        yaw: float = 0,
        *,
        seconds: float | None = None,
    ) -> None:
        if seconds is None:
            raise ValueError(
                "IDE-controlled fly() requires seconds so debugger pauses cannot "
                "leave the sticks applied"
            )
        self._call("fly", lr=lr, fb=fb, ud=ud, yaw=yaw, seconds=seconds)

    def hover(self) -> None:
        self._call("hover")

    def go(
        self, x_cm: float = 0, y_cm: float = 0, z_cm: float = 0, speed: float = 50
    ) -> None:
        self._call("go", x_cm=x_cm, y_cm=y_cm, z_cm=z_cm, speed=speed)

    def curve(
        self,
        x1: float,
        y1: float,
        z1: float,
        x2: float,
        y2: float,
        z2: float,
        speed: float = 50,
    ) -> None:
        self._call("curve", x1=x1, y1=y1, z1=z1, x2=x2, y2=y2, z2=z2, speed=speed)

    def frame(
        self, timeout: float | None = None, *, fresh: bool = False
    ) -> np.ndarray | None:
        """Return raw BGR pixels; fresh=True waits for a frame received after this call.

        A stopped stream returns None on timeout instead of replaying cached
        pixels as a new control measurement.
        """
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        with self._frame_ready:
            sequence = self._frame_sequence
            while (
                self._frame is None or (fresh and self._frame_sequence == sequence)
            ) and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._frame_ready.wait(remaining)
            return None if self._closed or self._frame is None else self._frame.copy()

    def show_detections(self, detections, *, size=None) -> None:
        """Outline what your detector found on the Squadrone cockpit video.

        Display only: the outlines go on the operator's copy of the video, not
        on the frames :meth:`frame` returns, so they can never feed back into
        your own detection. They are drawn on whichever frame the cockpit shows
        next, so at speed the box trails the gate by a frame or so — this is a
        view of what your program believes, not a measurement.

        ``detections`` is a sequence of outlines in the pixel coordinates of
        the frame you analysed: each a sequence of ``(x, y)`` points, or a
        ``(points, label)`` pair to caption it. Pass ``()`` when you detect
        nothing, or the last outline lingers briefly and then clears::

            box = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
            drone.show_detections([(box, f"gate {distance:.2f} m")])

        ``size`` is ``(height, width)`` of the frame those pixels came from;
        the latest camera frame's own size is used by default.
        """
        if size is None:
            with self._frame_ready:  # the lock that guards the latest frame
                size = None if self._frame is None else self._frame.shape
            if size is None:
                return  # no stream yet: nothing to draw the outline against
        self._call(
            "show_detections",
            detections=_wire_detections(detections),
            size=[int(size[0]), int(size[1])],
        )

    @property
    def battery(self) -> int:
        return int(self._call("battery"))

    @property
    def height(self) -> float:
        return float(self._call("height"))

    def enable_mission_pads(self) -> None:
        """Enable downward detection; unsupported hardware raises DroneError."""
        self._call("enable_mission_pads")

    def disable_mission_pads(self) -> None:
        self._call("disable_mission_pads")

    def mission_pad(self) -> dict | None:
        """Fresh {id, x_cm, y_cm, z_cm} in pad coordinates, or None if unseen."""
        return self._call("mission_pad")

    def center_on_mission_pad(
        self, pad_id: int, height_cm: float = 80, speed: float = 20
    ) -> None:
        self._call(
            "center_on_mission_pad", pad_id=pad_id, height_cm=height_cm, speed=speed
        )
