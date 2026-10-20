import json
from contextlib import contextmanager

import numpy as np
import pytest
from fastapi.testclient import TestClient

from comp2.drone.mock import MockDrone
from comp2.server import create_app
from comp2.sim.drone import SimDrone
from comp2.sim.world import Gate, World


def collect_until(ws, want_type, limit=50):
    for _ in range(limit):
        msg = ws.receive()
        if msg.get("bytes"):
            if want_type == "frame":
                return msg["bytes"]
            continue
        data = json.loads(msg["text"])
        if data["type"] == want_type:
            return data
    raise AssertionError(f"no {want_type} message")


def collect_where(ws, want_type, pred, limit=400):
    """Like ``collect_until``, but skips messages of the right type that are not
    yet the state we are waiting for — race state arrives repeatedly."""
    for _ in range(limit):
        msg = ws.receive()
        if not msg.get("text"):
            continue
        data = json.loads(msg["text"])
        if data["type"] == want_type and pred(data):
            return data
    raise AssertionError(f"no matching {want_type} message")


CALLS_FOREVER = 5000  # message budget when a flight has queued a lot of telemetry


def ide_call(ide, method, **args):
    """One synchronous call on the IDE socket, the way the squadrone client makes it."""
    ide.send_json({"type": "call", "id": 1, "method": method, "args": args})
    result = collect_until(ide, "result", limit=CALLS_FOREVER)
    assert result["ok"] is True, result
    return result


@contextmanager
def ide_flight(client, browser=None):
    """An IDE program connected for the length of the block.

    Leaving the block disconnects, which lands the drone. Pass the browser socket
    to also wait until Squadrone has released the drone, so the next flight is
    not refused as "already connected".
    """
    with client.websocket_connect("/ws?role=python") as ide:
        assert ide.receive_json()["accepted"] is True
        yield ide
    if browser is not None:
        collect_where(
            browser,
            "python_client",
            lambda msg: not msg["connected"],
            limit=CALLS_FOREVER,
        )


def fly(ide, *calls):
    """Make each ``(method, args)`` call in order."""
    for method, args in calls:
        ide_call(ide, method, **args)


def test_video_frames_are_jpeg_and_raw():
    """The stream is the raw sensor image — teams run their own OpenCV on it."""
    app = create_app(MockDrone())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        frame = collect_until(ws, "frame")
        assert frame[:2] == b"\xff\xd8"  # JPEG magic


def test_scene_is_sent_once_on_connect():
    """The 3D view is built from this one message; if it never arrives the stage
    stays empty for the whole session, with no retry."""
    app = create_app(SimDrone(delay=0))
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        msg = json.loads(ws.receive()["text"])
        assert msg["type"] == "scene"
        assert msg["scene"]["width_m"] > 0 and msg["scene"]["gates"]


def test_scene_is_null_for_a_drone_with_no_arena():
    app = create_app(MockDrone())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        assert json.loads(ws.receive()["text"]) == {"type": "scene", "scene": None}


def test_current_pose_is_sent_on_connect_even_when_the_drone_is_stationary():
    drone = SimDrone(delay=0)
    drone.x, drone.y, drone.z, drone.heading = 4.2, 1.3, 0.0, 137.0
    app = create_app(drone)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        assert json.loads(ws.receive()["text"])["type"] == "scene"
        pose = json.loads(ws.receive()["text"])
    assert pose["type"] == "pose"
    assert (pose["x"], pose["y"], pose["z"], pose["heading"]) == (4.2, 1.3, 0.0, 137.0)


def test_pose_is_broadcast_while_flying():
    app = create_app(SimDrone(delay=0.1))
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        with ide_flight(client) as ide:
            fly(ide, ("takeoff", {}), ("forward", {"cm": 100}), ("land", {}))
        heights = set()
        for _ in range(CALLS_FOREVER):
            msg = ws.receive()
            if not msg.get("text"):
                continue
            data = json.loads(msg["text"])
            if data["type"] == "pose":
                heights.add(data["z"])
            if data["type"] == "finished":
                break
        # more than start and end: the climb itself was broadcast
        assert len(heights) > 2


def test_a_new_connection_starts_from_the_takeoff_pad_every_time():
    """Two identical programs must land the drone in the same place."""
    drone = SimDrone(delay=0)
    app = create_app(drone)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ends = []
        for _ in range(2):
            with ide_flight(client, ws) as ide:
                fly(ide, ("takeoff", {}), ("forward", {"cm": 100}))
            ends.append((drone.x, drone.y, drone.heading))
    assert ends[0] == ends[1]


def test_reset_message_returns_the_drone_and_tells_the_clients():
    drone = SimDrone(delay=0)
    start = drone.world.start_xy
    app = create_app(drone)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        with ide_flight(client, ws) as ide:
            fly(ide, ("takeoff", {}), ("forward", {"cm": 100}))
        assert (drone.x, drone.y) != start  # it flew off the pad
        ws.send_json({"type": "reset"})
        collect_until(ws, "reset", limit=CALLS_FOREVER)
    assert (drone.x, drone.y, drone.flying) == (start[0], start[1], False)


def test_reset_on_hardware_clears_state_but_admits_it_did_not_move_the_drone():
    drone = MockDrone()  # like TelloDrone: no reset support
    app = create_app(drone)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "reset"})
        msg = collect_until(ws, "reset", limit=200)
    assert msg["repositioned"] is False


def test_reset_is_refused_while_a_program_is_connected():
    app = create_app(SimDrone(delay=0.2))
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        with ide_flight(client):
            collect_until(ws, "reset", limit=CALLS_FOREVER)  # the connect-time reset
            ws.send_json({"type": "reset"})
            err = collect_until(ws, "error", limit=CALLS_FOREVER)
            assert "stop the run" in err["message"]


def test_estop_calls_emergency():
    drone = MockDrone()
    app = create_app(drone)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "estop"})
        collect_until(ws, "estopped")
    assert ("emergency",) in drone.log


def test_browser_can_switch_from_simulator_to_a_connected_tello():
    simulator = SimDrone(delay=0)
    tello = MockDrone()
    tello.mode = "tello"
    app = create_app(simulator, tello_factory=lambda: tello)

    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        initial = collect_until(ws, "drone_mode")
        assert initial == {"type": "drone_mode", "mode": "sim", "switching": False}

        ws.send_json({"type": "switch_drone", "mode": "tello"})
        assert collect_until(ws, "drone_mode")["switching"] is True
        connected = collect_until(ws, "drone_mode")
        assert connected == {"type": "drone_mode", "mode": "tello", "switching": False}

        with ide_flight(client) as ide:
            fly(ide, ("takeoff", {}), ("land", {}))
        assert collect_until(ws, "finished", limit=CALLS_FOREVER)["reason"] == "done"

    assert ("connect",) in tello.log
    assert ("takeoff",) in tello.log and ("land",) in tello.log


def test_failed_tello_connection_keeps_the_simulator_active():
    class UnavailableTello(MockDrone):
        mode = "tello"

        def connect(self):
            raise RuntimeError("not reachable")

    simulator = SimDrone(delay=0)
    app = create_app(simulator, tello_factory=UnavailableTello)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        collect_until(ws, "drone_mode")
        ws.send_json({"type": "switch_drone", "mode": "tello"})
        assert collect_until(ws, "drone_mode")["switching"] is True
        mode = collect_until(ws, "drone_mode")
        error = collect_until(ws, "error")
        assert mode == {"type": "drone_mode", "mode": "sim", "switching": False}
        assert "could not connect" in error["message"]
        assert app.state.drone is simulator


# --- tracks ----------------------------------------------------------------


def test_the_track_list_is_offered_on_connect():
    app = create_app(SimDrone(delay=0))
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        msg = collect_until(ws, "tracks")
        assert [t["id"] for t in msg["tracks"]] == [
            "circuit1_oval",
            "circuit2_figure_eight",
            "circuit3_double_figure_eight",
        ]
        assert msg["current"] == "circuit1_oval"


def test_a_drone_with_no_arena_offers_no_tracks():
    app = create_app(MockDrone())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        msg = collect_until(ws, "tracks")
        assert msg["tracks"] is None and msg["current"] is None


def test_switching_track_rebuilds_the_scene_and_re_sends_it():
    drone = SimDrone(delay=0)
    app = create_app(drone)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        collect_until(ws, "tracks")
        ws.send_json({"type": "track", "name": "circuit2_figure_eight"})
        scene = collect_where(
            ws, "scene", lambda m: m["scene"]["name"] == "Circuit 2 - Figure Eight"
        )
        assert len(scene["scene"]["gates"]) == 3
    assert (drone.x, drone.y) == drone.world.start_xy


def test_track_switch_is_refused_while_a_program_is_connected():
    app = create_app(SimDrone(delay=0.2))
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        with ide_flight(client):
            collect_until(ws, "reset", limit=CALLS_FOREVER)  # the connect-time reset
            ws.send_json({"type": "track", "name": "circuit2_figure_eight"})
            err = collect_until(ws, "error", limit=CALLS_FOREVER)
            assert "stop the run" in err["message"]


def test_a_drone_with_no_arena_says_so_rather_than_failing_quietly():
    app = create_app(MockDrone())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "track", "name": "circuit1_oval"})
        assert "no track" in collect_until(ws, "error", limit=200)["message"]


# --- racing ----------------------------------------------------------------


def _straight_track_drone(delay=0.08):
    world = World(
        width_m=4.0,
        depth_m=6.0,
        height_m=3.0,
        gates=[Gate(1.5, 2.0, 1.0, 0.0)],
        start=(1.5, 0.7),
        finish=(1.5, 4.0),
        name="straight",
        t_ref_s=10.0,
    )
    return SimDrone(world=world, delay=delay)


# takeoff (1 m up), through the 1 m-high gate, land on the far pad
STRAIGHT_LAP = (("takeoff", {}), ("forward", {"cm": 330}), ("land", {}))


def test_flying_the_lap_finishes_the_race_with_a_score():
    app = create_app(_straight_track_drone())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        with ide_flight(client) as ide:
            fly(ide, *STRAIGHT_LAP)
        done = collect_where(
            ws, "race", lambda m: m["state"] == "finished", limit=CALLS_FOREVER
        )
        assert done["passed"] == done["total"] == 1
        assert done["gates"][0]["passed"] is True
        # straight through the middle — tolerance covers the 30 Hz sampling grid
        assert done["gates"][0]["offset_m"] < 0.2
        assert done["score"]["total"] > 0
        assert done["elapsed_s"] > 0


def test_landing_early_reports_a_dnf():
    app = create_app(_straight_track_drone())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        with ide_flight(client) as ide:
            fly(ide, ("takeoff", {}), ("land", {}))
        done = collect_where(
            ws, "race", lambda m: m["state"] == "dnf", limit=CALLS_FOREVER
        )
        assert done["passed"] == 0


def test_a_drone_with_no_arena_reports_no_race_state():
    app = create_app(MockDrone())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        with ide_flight(client) as ide:
            fly(ide, ("takeoff", {}), ("land", {}))
        seen = []
        for _ in range(CALLS_FOREVER):
            msg = ws.receive()
            if not msg.get("text"):
                continue
            data = json.loads(msg["text"])
            seen.append(data["type"])
            if data["type"] == "finished":
                break
        assert "race" not in seen


# --- --script ------------------------------------------------------------------


def test_a_script_runs_alongside_the_server_and_reports_to_the_browser(tmp_path):
    drone = MockDrone()
    script = tmp_path / "lap.py"
    script.write_text(
        "from comp2.api import Drone\n"
        "d = Drone()\n"
        "d.takeoff()\n"
        "print('hello cage')\n"
        "d.forward(60)\n"
        "d.land()\n",
        encoding="utf-8",
    )
    app = create_app(drone, script=script, script_delay=0)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        out = collect_where(ws, "output", lambda m: "hello cage" in m["text"])
        assert out["stream"] == "stdout"
        assert collect_until(ws, "finished", limit=CALLS_FOREVER)["reason"] == "done"
    assert ("move", "forward", 60) in drone.log


def test_a_failing_script_reports_the_error_and_frees_the_run_slot(tmp_path):
    script = tmp_path / "lap.py"
    script.write_text("raise ValueError('boom')\n", encoding="utf-8")
    app = create_app(MockDrone(), script=script, script_delay=0)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        fin = collect_until(ws, "finished", limit=CALLS_FOREVER)
        assert fin["reason"] == "error" and "boom" in fin["detail"]
        assert app.state.active_run is None


def test_the_cockpit_is_served_so_a_reload_cannot_keep_a_stale_module():
    """A cached ES module is invisible: the video feed updates, the 3D view
    silently does not."""
    app = create_app(MockDrone())
    with TestClient(app) as client:
        for path in ("/", "/scene3d.js", "/style.css"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert "no-cache" in response.headers["cache-control"], path


def test_a_versioned_module_chain_bumps_as_one():
    """`?v=` on a script tag only busts that file. A module's imports carry
    tokens of their own, so they must move together or the browser keeps the
    old import — the 3D view rendering last week's gates beside a live feed
    that shows this week's."""
    import re

    from comp2.server import FRONTEND_DIR

    index = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    entry = dict(re.findall(r'src="([\w.-]+\.js)\?v=([\w-]+)"', index))
    for source in FRONTEND_DIR.glob("*.js"):
        imports = re.findall(
            r'from "\./([\w.-]+\.js)\?v=([\w-]+)"',
            source.read_text(encoding="utf-8"),
        )
        if not imports:
            continue
        assert source.name in entry, f"{source.name} is loaded without a token"
        for name, token in imports:
            assert (FRONTEND_DIR / name).exists(), f"{source.name} imports {name}"
            assert token == entry[source.name], (
                f"{source.name} is served as ?v={entry[source.name]} but imports "
                f"{name} as ?v={token}; bump both or the old {name} survives"
            )


# --- external IDE Python client --------------------------------------------


def test_external_python_client_controls_the_selected_adapter():
    drone = MockDrone()
    app = create_app(drone)
    with TestClient(app) as client, client.websocket_connect("/ws?role=python") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["accepted"] is True
        assert hello["drone"] == "mock"

        ws.send_json({"type": "call", "id": 1, "method": "takeoff", "args": {}})
        assert collect_until(ws, "result")["ok"] is True
        ws.send_json({"type": "call", "id": 2, "method": "forward", "args": {"cm": 60}})
        result = collect_until(ws, "result")
        assert result["id"] == 2 and result["ok"] is True
        ws.send_json({"type": "call", "id": 3, "method": "land", "args": {}})
        assert collect_until(ws, "result")["ok"] is True

    assert ("takeoff",) in drone.log
    assert ("move", "forward", 60) in drone.log


@pytest.mark.parametrize("width", [64, 640])
@pytest.mark.parametrize("in_place", [False, True])
def test_external_python_client_receives_raw_camera_frames(width, in_place):
    class DisplayAnnotatedMock(MockDrone):
        def annotate(self, frame):
            display = frame if in_place else frame.copy()
            display[:] = 255
            return display

    drone = DisplayAnnotatedMock(
        frame_factory=lambda: np.zeros((width * 3 // 4, width, 3), dtype=np.uint8)
    )
    app = create_app(drone)
    with TestClient(app) as client, client.websocket_connect("/ws?role=python") as ws:
        assert ws.receive_json()["accepted"] is True
        jpeg = collect_until(ws, "frame", limit=200)
    import cv2

    frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert frame is not None and float(frame.mean()) < 5


def test_detection_outlines_reach_the_cockpit_but_never_the_sensor_stream():
    import cv2

    drone = MockDrone(
        frame_factory=lambda: np.zeros((480, 640, 3), dtype=np.uint8),
    )
    app = create_app(drone)
    box = [[160, 120], [480, 120], [480, 360], [160, 360]]
    with TestClient(app) as client, client.websocket_connect("/ws?role=python") as ide:
        assert ide.receive_json()["accepted"] is True
        ide.send_json(
            {
                "type": "call",
                "id": 1,
                "method": "show_detections",
                "args": {"detections": [[box, "gate 1.00 m"]], "size": [480, 640]},
            }
        )
        assert collect_until(ide, "result")["ok"] is True
        with client.websocket_connect("/ws") as ws:
            display = cv2.imdecode(
                np.frombuffer(collect_until(ws, "frame", limit=200), np.uint8),
                cv2.IMREAD_COLOR,
            )
        raw = cv2.imdecode(
            np.frombuffer(collect_until(ide, "frame", limit=200), np.uint8),
            cv2.IMREAD_COLOR,
        )
    assert display[:, :, 1].max() > 100  # the outline is on the operator's copy
    assert raw.max() < 5  # and never on the frames a detector consumes
    # A released controller is not still detecting anything.
    assert app.state.overlay == ()


def test_a_malformed_detection_is_rejected_without_ending_the_session():
    app = create_app(MockDrone())
    with TestClient(app) as client, client.websocket_connect("/ws?role=python") as ws:
        assert ws.receive_json()["accepted"] is True
        ws.send_json(
            {
                "type": "call",
                "id": 4,
                "method": "show_detections",
                "args": {"detections": [[[1, 2]]], "size": [480, 640]},
            }
        )
        result = collect_until(ws, "result")
        assert result["ok"] is False and "ValueError" in result["error"]
        ws.send_json({"type": "call", "id": 5, "method": "battery", "args": {}})
        assert collect_until(ws, "result")["ok"] is True
    assert app.state.overlay == ()


def test_external_python_client_cannot_call_server_or_pose_internals():
    app = create_app(SimDrone(delay=0))
    with TestClient(app) as client, client.websocket_connect("/ws?role=python") as ws:
        assert ws.receive_json()["accepted"] is True
        ws.send_json({"type": "call", "id": 7, "method": "pose", "args": {}})
        result = collect_until(ws, "result")
    assert result == {
        "type": "result",
        "id": 7,
        "ok": False,
        "error": "unsupported Drone operation",
    }


def test_external_fly_requires_a_bounded_duration():
    drone = MockDrone()
    app = create_app(drone)
    with TestClient(app) as client, client.websocket_connect("/ws?role=python") as ws:
        assert ws.receive_json()["accepted"] is True
        ws.send_json({"type": "call", "id": 4, "method": "fly", "args": {"fb": 80}})
        result = collect_until(ws, "result")
    assert result["ok"] is False and "requires seconds" in result["error"]
    assert ("send_rc", 0, 80, 0, 0) not in drone.log


def test_external_disconnect_centres_sticks_before_landing():
    drone = MockDrone()
    app = create_app(drone)
    with TestClient(app) as client, client.websocket_connect("/ws") as browser:
        collect_until(browser, "python_client")
        with client.websocket_connect("/ws?role=python") as ws:
            assert ws.receive_json()["accepted"] is True
            connected = collect_where(
                browser, "python_client", lambda msg: msg["connected"]
            )
            assert connected["drone"] == "mock"
        collect_where(browser, "python_client", lambda msg: not msg["connected"])

    centre = drone.log.index(("send_rc", 0, 0, 0, 0))
    land = drone.log.index(("land",))
    assert centre < land


def test_only_one_external_python_client_can_hold_control():
    app = create_app(MockDrone())
    with (
        TestClient(app) as client,
        client.websocket_connect("/ws?role=python") as first,
    ):
        assert first.receive_json()["accepted"] is True
        with client.websocket_connect("/ws?role=python") as second:
            refused = second.receive_json()
            assert refused["type"] == "hello" and refused["accepted"] is False
            assert "already" in refused["message"]


def test_arena_catalog_is_offered_and_switchable(tmp_path):
    """The cockpit's arena picker, end to end over the socket.

    Tracks are read-only in the browser because they must match the physical
    cage. The arena is not: varying the venue is the point.
    """
    drone = SimDrone(delay=0, arena_name="clean")
    app = create_app(drone)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        catalog = _await_message(ws, "arena")
        assert "floor" in catalog["catalog"]["libraries"]
        assert catalog["current"]["name"] == "clean"

        ws.send_json({"type": "arena", "name": "cage"})
        assert _await_message(ws, "arena")["current"]["name"] == "cage"
        assert drone.world.appearance.net is not None

        ws.send_json({"type": "arena", "randomize": True, "seed": 5})
        assert _await_message(ws, "arena")["current"]["name"] == "random"

        ws.send_json(
            {"type": "arena", "parts": {"floor": "grey_sports", "hall": "lab"}}
        )
        _await_message(ws, "arena")
        assert drone.world.appearance.hall is not None


def _await_message(ws, kind, limit=400):
    """The socket also carries binary JPEG frames, so skip anything untexted."""
    return collect_where(ws, kind, lambda _data: True, limit)
