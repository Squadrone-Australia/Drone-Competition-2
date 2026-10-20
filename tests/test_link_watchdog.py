"""The server notices a lost Tello and puts the link back up by itself.

A rebooted aircraft is a new SDK session that announces nothing, so the only
cure used to be restarting the program. The watchdog reconnects once no run is
flying, and the cockpit is told about it either way.
"""

import threading

import pytest
from fastapi.testclient import TestClient

import comp2.server as server
from comp2.drone.mock import MockDrone
from comp2.server import create_app
from tests.test_fault_recovery import next_of


class FlakyTello(MockDrone):
    """A Tello whose link can be cut and that counts its reconnects."""

    mode = "tello"

    def __init__(self, connect_fails=False):
        super().__init__()
        self.link_ok = True
        self.connect_fails = connect_fails
        self.reconnected = threading.Event()
        self.closed = 0

    def connect(self):
        self.log.append(("connect",))
        if self.connect_fails:
            raise OSError("no route to 192.168.10.1")
        self.link_ok = True

    def reconnect(self):
        self.connect_fails = False
        super().reconnect()
        self.reconnected.set()

    def close(self):
        self.closed += 1
        self.link_ok = False


@pytest.fixture(autouse=True)
def fast_watchdog(monkeypatch):
    monkeypatch.setattr(server, "LINK_CHECK_INTERVAL", 0.05)
    monkeypatch.setattr(server, "LIVENESS_INTERVAL", 0.05)


def test_the_cockpit_learns_the_link_state_on_connect():
    app = create_app(FlakyTello())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        link = next_of(ws, {"drone_link"})
        assert link["ok"] is True and link["mode"] == "tello"


def test_a_lost_link_is_reported_and_reconnected_when_idle():
    drone = FlakyTello()
    app = create_app(drone)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        next_of(ws, {"drone_link"})
        drone.link_ok = False
        lost = next_of(ws, {"drone_link"})
        assert lost["ok"] is False and "lost contact" in lost["message"]
        back = next_of(ws, {"drone_link"})
        assert back["ok"] is True and "reconnected" in back["message"]
    assert drone.reconnected.is_set()


def test_no_reconnect_underneath_a_flying_program():
    drone = FlakyTello()
    app = create_app(drone)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        next_of(ws, {"drone_link"})
        app.state.active_run = object()  # something is flying
        drone.link_ok = False
        assert next_of(ws, {"drone_link"})["ok"] is False
        assert not drone.reconnected.wait(0.3)
        app.state.active_run = None
        assert drone.reconnected.wait(2.0)


def test_a_tello_launch_before_joining_its_wifi_does_not_kill_the_server():
    drone = FlakyTello(connect_fails=True)
    app = create_app(drone)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        first = next_of(ws, {"drone_link"})
        if first["ok"] is False:
            assert "could not connect" in first["message"]
            first = next_of(ws, {"drone_link"})
        assert first["ok"] is True  # the watchdog got it once the Wi-Fi was there


def test_blocked_video_is_said_once_and_is_not_a_lost_link():
    drone = FlakyTello()
    app = create_app(drone)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        next_of(ws, {"drone_link"})
        drone.video_blocked = True
        msg = next_of(ws, {"drone_link"})
        assert msg["ok"] is True and msg["video_blocked"] is True
        assert "11111" in msg["message"]
    assert not drone.reconnected.is_set()


def test_an_idle_aircraft_that_stops_answering_is_noticed():
    class Mute(FlakyTello):
        def battery(self):
            raise TimeoutError("no answer")

    drone = Mute()
    app = create_app(drone)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        next_of(ws, {"drone_link"})
        assert next_of(ws, {"drone_link"})["ok"] is False


def test_manual_reconnect_closes_the_old_adapter_before_connecting_a_new_one():
    old, new = FlakyTello(), FlakyTello()
    order = []
    old.close = lambda: order.append("close old")
    new.connect = lambda: order.append("connect new")
    app = create_app(old, tello_factory=lambda: new)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        next_of(ws, {"drone_mode"})
        order.clear()
        ws.send_json({"type": "reconnect_drone"})
        assert next_of(ws, {"drone_mode"})["switching"] is True
        assert next_of(ws, {"drone_mode"})["switching"] is False
        assert app.state.drone is new
    # the command and video ports are fixed: the new link cannot bind them
    # while the old adapter still holds them
    assert order[:2] == ["close old", "connect new"]


def test_manual_reconnect_is_refused_mid_run_and_on_the_simulator():
    app = create_app(FlakyTello())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        next_of(ws, {"drone_link"})
        app.state.active_run = object()
        ws.send_json({"type": "reconnect_drone"})
        assert "stop the run" in next_of(ws, {"error"})["message"]
        app.state.active_run = None

    app = create_app(MockDrone())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "reconnect_drone"})
        assert "only the real Tello" in next_of(ws, {"error"})["message"]
