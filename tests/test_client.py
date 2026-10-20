import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "client"))

from squadrone.client import Drone, _python_url, _wire_detections


class RecordingDrone(Drone):
    def __init__(self):
        self.calls = []

    def _call(self, method, **args):
        self.calls.append((method, args))
        return 73 if method == "battery" else None


def test_python_client_role_is_added_without_losing_existing_query_parameters():
    assert _python_url("ws://localhost:8765/ws?token=abc") in {
        "ws://localhost:8765/ws?token=abc&role=python",
        "ws://localhost:8765/ws?role=python&token=abc",
    }


def test_external_api_serializes_the_public_drone_operations():
    drone = RecordingDrone()
    drone.takeoff()
    drone.forward(60)
    drone.fly(fb=40, yaw=-5, seconds=0.2)
    assert drone.battery == 73
    assert drone.calls == [
        ("takeoff", {}),
        ("forward", {"cm": 60}),
        ("fly", {"lr": 0, "fb": 40, "ud": 0, "yaw": -5, "seconds": 0.2}),
        ("battery", {}),
    ]


def test_external_api_refuses_an_unbounded_rc_setpoint_before_sending_it():
    drone = RecordingDrone()
    with pytest.raises(ValueError, match="requires seconds"):
        drone.fly(fb=80)
    assert drone.calls == []


def test_external_client_serializes_pad_setup_measurement_and_centring():
    drone = RecordingDrone()
    drone.enable_mission_pads()
    assert drone.mission_pad() is None
    drone.center_on_mission_pad(2, height_cm=80, speed=20)
    drone.disable_mission_pads()
    assert drone.calls == [
        ("enable_mission_pads", {}),
        ("mission_pad", {}),
        ("center_on_mission_pad", {"pad_id": 2, "height_cm": 80, "speed": 20}),
        ("disable_mission_pads", {}),
    ]


def test_external_client_sends_detections_as_plain_json_with_the_frame_size():
    import numpy as np

    drone = RecordingDrone()
    corners = np.array([[10.0, 20.0], [30.0, 20.0], [30.0, 60.0], [10.0, 60.0]])
    drone.show_detections([(corners, "gate 1.20 m")], size=(480, 640))
    method, args = drone.calls[0]
    assert method == "show_detections"
    assert args == {
        "detections": [
            [[[10.0, 20.0], [30.0, 20.0], [30.0, 60.0], [10.0, 60.0]], "gate 1.20 m"]
        ],
        "size": [480, 640],
    }
    import json

    json.dumps(args)  # the whole call has to survive the WebSocket


def test_the_detection_call_matches_the_signature_that_serves_it():
    import inspect

    from comp2.api import Drone as BundledDrone
    from comp2.server import REMOTE_METHODS

    drone = RecordingDrone()
    drone.show_detections([[(1, 2), (3, 4)]], size=(480, 640))
    method, args = drone.calls[0]
    assert method in REMOTE_METHODS
    inspect.signature(getattr(BundledDrone, method)).bind(None, **args)


def test_unlabelled_and_empty_detections_survive_the_wire_conversion():
    assert _wire_detections([[(1, 2), (3, 4)]]) == [[[[1.0, 2.0], [3.0, 4.0]], ""]]
    assert _wire_detections(()) == []
    assert _wire_detections(None) == []


def test_detections_sent_before_the_first_frame_are_dropped_not_guessed():
    client = camera_client()
    client._frame = None
    client.calls = []
    client._call = lambda method, **args: client.calls.append((method, args))
    client.show_detections([[(1, 2), (3, 4)]])
    assert client.calls == []


def camera_client():
    import threading

    import numpy as np

    client = Drone.__new__(Drone)
    client.timeout = 0.05
    client._frame_ready = threading.Condition()
    client._frame_sequence = 1
    client._frame = np.zeros((2, 2, 3), dtype=np.uint8)
    client._closed = False
    return client


def test_fresh_frame_does_not_replay_cached_pixels_when_stream_stalls():
    client = camera_client()
    assert client.frame(fresh=True, timeout=0.01) is None
    assert client.frame() is not None  # old latest-frame API remains available


def test_fresh_frame_waits_for_a_new_arrival_and_returns_a_copy():
    import threading

    client = camera_client()

    def publish():
        with client._frame_ready:
            client._frame[:] = 42
            client._frame_sequence += 1
            client._frame_ready.notify_all()

    timer = threading.Timer(0.01, publish)
    timer.start()
    frame = client.frame(fresh=True, timeout=0.5)
    timer.join()
    assert frame is not None and (frame == 42).all()
    frame[:] = 0
    assert (client._frame == 42).all()


def test_disconnected_camera_does_not_return_cached_frame():
    client = camera_client()
    client._closed = True
    assert client.frame(fresh=True) is None


# --- error handling over a scripted connection ----------------------------------
#
# These drive the real reader thread and the real `_call`, with only the
# WebSocket replaced, so what the program sees is what a team would see.

_EOF = object()


class ScriptedServer:
    """Stands in for the WebSocket: ``on_call`` decides how Squadrone answers."""

    def __init__(self, on_call=None):
        import queue

        self.inbox = queue.Queue()
        self.sent = []
        self.on_call = on_call or (lambda server, call: None)

    def push(self, message):
        self.inbox.put(message if isinstance(message, bytes) else json.dumps(message))

    def hang_up(self):
        self.inbox.put(_EOF)

    def __iter__(self):
        return self

    def __next__(self):
        import queue

        try:
            item = self.inbox.get(timeout=5)
        except queue.Empty:
            raise StopIteration from None
        if item is _EOF:
            raise StopIteration
        return item

    def send(self, text):
        message = json.loads(text)
        self.sent.append(message)
        if message.get("type") == "call":
            self.on_call(self, message)

    def close(self):
        self.hang_up()


def reply(server, call, **fields):
    server.push({"type": "result", "id": call["id"], **fields})


def connect_to(monkeypatch, server):
    import squadrone.client as client_module

    monkeypatch.setattr(client_module, "connect", lambda *a, **k: server)
    monkeypatch.setattr(client_module.atexit, "register", lambda fn: None)
    server.push({"type": "hello", "accepted": True})
    return Drone("ws://scripted/ws", timeout=2)


def test_a_hardware_error_that_mentions_stopped_is_a_drone_error(monkeypatch):
    """The Tello reports "the drone's motors are stopped". That is a failed
    command, not an operator stop, and must be catchable as DroneError."""
    from squadrone import DroneError

    server = ScriptedServer(
        lambda s, call: reply(
            s,
            call,
            ok=False,
            error="RuntimeError: the drone's motors are stopped",
        )
    )
    drone = connect_to(monkeypatch, server)
    with pytest.raises(DroneError, match="motors are stopped"):
        drone.forward(100)


def test_the_server_stop_result_raises_emergency_stop(monkeypatch):
    from squadrone import EmergencyStop

    for extra in ({"stopped": True}, {}):  # new servers flag it; older ones do not
        server = ScriptedServer(
            lambda s, call, extra=extra: reply(
                s, call, ok=False, error="control session stopped", **extra
            )
        )
        drone = connect_to(monkeypatch, server)
        with pytest.raises(EmergencyStop):
            drone.forward(100)


def test_losing_the_server_mid_command_is_a_connection_error(monkeypatch):
    """A crashed or closed server is not an operator stop. It used to surface as
    EmergencyStop, which `except DroneError` cannot catch."""
    from squadrone import ConnectionError as SquadroneConnectionError

    server = ScriptedServer(lambda s, call: s.hang_up())
    drone = connect_to(monkeypatch, server)
    with pytest.raises(SquadroneConnectionError):
        drone.forward(100)


def test_a_server_shutdown_mid_command_is_a_connection_error(monkeypatch):
    from squadrone import ConnectionError as SquadroneConnectionError

    def shutting_down(server, call):
        server.push(
            {
                "type": "control_ended",
                "reason": "shutdown",
                "detail": "Squadrone is shutting down",
            }
        )
        server.hang_up()

    drone = connect_to(monkeypatch, ScriptedServer(shutting_down))
    with pytest.raises(SquadroneConnectionError, match="shutting down"):
        drone.forward(100)


def test_an_emergency_stop_mid_command_raises_emergency_stop_and_stays_stopped(
    monkeypatch,
):
    from squadrone import EmergencyStop

    def estopped(server, call):
        server.push(
            {"type": "control_ended", "reason": "estopped", "detail": "emergency stop"}
        )
        server.hang_up()

    drone = connect_to(monkeypatch, ScriptedServer(estopped))
    with pytest.raises(EmergencyStop):
        drone.forward(100)
    # a loop that swallows DroneError must not be able to keep "flying" afterwards
    with pytest.raises(EmergencyStop):
        drone.forward(100)
    drone.close()  # cleanup after a stop never raises


def test_a_corrupt_video_frame_does_not_end_the_session(monkeypatch):
    import io

    from PIL import Image

    jpeg = io.BytesIO()
    Image.new("RGB", (8, 6), (255, 0, 0)).save(jpeg, "JPEG")

    def answer(server, call):
        server.push(b"\xff\xd8 this is not a jpeg")
        server.push(jpeg.getvalue())
        reply(server, call, ok=True, result=None)

    drone = connect_to(monkeypatch, ScriptedServer(answer))
    drone.forward(100)  # still connected after the bad frame
    frame = drone.frame(timeout=1)
    assert frame is not None and frame.shape == (6, 8, 3)
    drone.forward(100)
