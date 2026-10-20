"""What the program does when the aircraft, the team's program, or the wire
misbehaves.

Regressions for faults first found in Drone-Competition-1 by injecting hardware
failures in software, and confirmed here against this codebase: an emergency
stop that cannot reach the drone, malformed websocket traffic, a page from
another site taking the IDE controller, and warnings nobody could see.
"""

import asyncio
import json
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from comp2.api import Drone, ScriptRun
from comp2.drone.mock import MockDrone
from comp2.server import create_app


def next_of(ws, types, limit=600):
    """The next text message whose type is in ``types`` (skips frames/others)."""
    for _ in range(limit):
        msg = ws.receive()
        if msg.get("type") == "websocket.close":
            raise AssertionError(f"socket closed while waiting for {types}")
        if msg.get("text"):
            data = json.loads(msg["text"])
            if data["type"] in types:
                return data
    raise AssertionError(f"no {types} message")


class Unreachable(MockDrone):
    def emergency(self):
        raise RuntimeError("no answer")


# --- emergency stop -----------------------------------------------------------


async def test_an_emergency_stopped_script_is_not_landed():
    drone = MockDrone()
    run = ScriptRun(drone, lambda ev: None)
    started = threading.Event()

    def script():
        d = Drone()
        d.takeoff()
        started.set()
        while True:
            d.wait(0.05)

    task = asyncio.get_running_loop().create_task(run.run(script))
    await asyncio.to_thread(started.wait, 2)
    run.request_stop(emergency=True)
    reason, _ = await asyncio.wait_for(task, 10)
    assert reason == "stopped"
    assert ("land",) not in drone.log


def test_an_unreachable_drone_is_reported_not_announced_as_stopped():
    """This used to raise out of the handler: no `estopped`, no error, and a
    dead socket the page reconnected in silence."""
    with (
        TestClient(create_app(Unreachable())) as client,
        client.websocket_connect("/ws") as ws,
    ):
        ws.send_json({"type": "estop"})
        msg = next_of(ws, {"estopped"})
        assert msg["ok"] is False and "could not reach" in msg["message"]
        ws.send_json({"type": "nonsense"})
        next_of(ws, {"error"})  # the socket is still serving


def test_a_successful_emergency_says_so():
    with (
        TestClient(create_app(MockDrone())) as client,
        client.websocket_connect("/ws") as ws,
    ):
        ws.send_json({"type": "estop"})
        assert next_of(ws, {"estopped"})["ok"] is True


def test_an_emergency_stop_revokes_the_ide_without_landing():
    drone = MockDrone()
    with (
        TestClient(create_app(drone)) as client,
        client.websocket_connect("/ws") as browser,
        client.websocket_connect("/ws?role=python") as ide,
    ):
        assert ide.receive_json()["accepted"] is True
        browser.send_json({"type": "estop"})
        assert next_of(ide, {"control_ended"})["reason"] == "estopped"
        assert next_of(browser, {"estopped"})["ok"] is True
    assert ("emergency",) in drone.log and ("land",) not in drone.log


# --- malformed traffic ------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "42",
        "null",
        "{}",
        '{"type": "nonsense"}',
    ],
)
def test_a_malformed_browser_message_does_not_kill_the_socket(payload):
    with (
        TestClient(create_app(MockDrone())) as client,
        client.websocket_connect("/ws") as ws,
    ):
        ws.send_text(payload)
        next_of(ws, {"error"})
        ws.send_json({"type": "nonsense"})
        next_of(ws, {"error"})  # still answering


@pytest.mark.parametrize("payload", ["42", "not json", b"\x00\x01"])
def test_a_malformed_ide_message_is_answered_and_the_session_continues(payload):
    with (
        TestClient(create_app(MockDrone())) as client,
        client.websocket_connect("/ws?role=python") as ide,
    ):
        assert ide.receive_json()["accepted"] is True
        if isinstance(payload, bytes):
            ide.send_bytes(payload)
        else:
            ide.send_text(payload)
        assert next_of(ide, {"error"})["message"] == "messages must be JSON objects"
        ide.send_json({"type": "call", "id": 1, "method": "battery", "args": {}})
        assert next_of(ide, {"result"})["ok"] is True


# --- the controller is not open to every page the student visits -----------------


def browser_headers(name, *, origin_port=8765, host_port=8765):
    """What a browser sends when a page served from ``name`` opens the socket.

    TestClient always sends ``Host: testserver`` for a WebSocket, whatever its
    ``base_url``, so the headers a real browser would send are spelled out.
    """
    return {
        "origin": f"http://{name}:{origin_port}",
        "host": f"{name}:{host_port}",
    }


def refused(client, path, headers):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(path, headers=headers) as ws:
            ws.receive_json()


@pytest.mark.parametrize("path", ["/ws", "/ws?role=python"])
def test_a_foreign_origin_is_refused(path):
    with TestClient(create_app(MockDrone())) as client:
        refused(client, path, {"origin": "http://evil.example"})


@pytest.mark.parametrize("path", ["/ws", "/ws?role=python"])
@pytest.mark.parametrize(
    "name", ["evil.example", "192.168.1.50", "localhost.evil.example"]
)
def test_a_rebound_hostname_is_refused(path, name):
    """DNS rebinding: the attacker's page makes its own name resolve to
    127.0.0.1, so the browser sends ``Origin`` and ``Host`` that agree with each
    other. Agreeing is not enough: the name has to be this machine's."""
    with TestClient(create_app(MockDrone())) as client:
        refused(client, path, browser_headers(name))


@pytest.mark.parametrize("name", ["localhost", "127.0.0.1", "[::1]"])
def test_the_page_connects_from_any_name_for_this_machine(name):
    with TestClient(create_app(MockDrone())) as client:
        with client.websocket_connect("/ws", headers=browser_headers(name)) as ws:
            assert ws.receive_json()["type"] == "scene"


@pytest.mark.parametrize("path", ["/ws", "/ws?role=python"])
def test_another_local_pages_origin_is_refused(path):
    """Same machine name, different port: some other program's page."""
    with TestClient(create_app(MockDrone())) as client:
        refused(client, path, browser_headers("localhost", origin_port=9999))


def test_the_page_and_the_ide_client_still_connect():
    with TestClient(create_app(MockDrone())) as client:
        with client.websocket_connect(
            "/ws", headers=browser_headers("localhost")
        ) as ws:
            assert ws.receive_json()["type"] == "scene"
        with client.websocket_connect(
            "/ws?role=python"
        ) as ide:  # no Origin, like squadrone
            assert ide.receive_json()["accepted"] is True


# --- warnings reach someone who can act on them ----------------------------------


def test_an_ide_warning_reaches_the_ide_and_the_page():
    with (
        TestClient(create_app(MockDrone())) as client,
        client.websocket_connect("/ws") as browser,
        client.websocket_connect("/ws?role=python") as ide,
    ):
        assert ide.receive_json()["accepted"] is True
        for call_id, method, args in ((1, "takeoff", {}), (2, "forward", {"cm": 5})):
            ide.send_json(
                {"type": "call", "id": call_id, "method": method, "args": args}
            )
        warning = next_of(ide, {"warning"})
        assert "outside 20-500 cm" in warning["message"]
        assert "outside 20-500 cm" in next_of(browser, {"warning"})["message"]
