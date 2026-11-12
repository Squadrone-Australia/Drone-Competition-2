"""The server on a real socket, against a client process that dies.

TestClient always closes its sockets politely, so it cannot show what a team's
program does when it is killed from the IDE or crashes hard: the connection
just vanishes, without a close frame.
"""

import logging
import socket
import subprocess
import sys
import textwrap
import threading
import time

import pytest
import uvicorn

from comp2.server import create_app
from comp2.sim.drone import SimDrone

CLIENT = textwrap.dedent(
    """
    import sys
    from squadrone import Drone

    drone = Drone(sys.argv[1])
    drone.takeoff()
    print("airborne", flush=True)
    while True:
        drone.fly(fb=10, yaw=20, seconds=0.2)
    """
)


class _Errors(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _until(condition, seconds, what):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def live_server():
    pytest.importorskip("squadrone")
    drone = SimDrone(delay=0, arena_name="clean")
    app = create_app(drone)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    errors = _Errors()
    loggers = [logging.getLogger(n) for n in ("uvicorn.error", "comp2.server")]
    for logger in loggers:
        logger.addHandler(errors)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _until(lambda: server.started, 10, "the server to start")
    try:
        yield app, drone, f"ws://127.0.0.1:{port}/ws", errors
    finally:
        server.should_exit = True
        thread.join(10)
        for logger in loggers:
            logger.removeHandler(errors)


def test_a_killed_ide_program_lands_the_drone_without_a_server_error(live_server):
    app, drone, url, errors = live_server
    client = subprocess.Popen(
        [sys.executable, "-c", CLIENT, url],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert client.stdout.readline().strip() == "airborne", client.stdout.read()
        time.sleep(0.5)  # mid-flight, with video streaming to it
    finally:
        client.kill()
        client.wait(10)
    _until(lambda: app.state.remote_run is None, 15, "the controller to be released")
    _until(lambda: not drone.flying, 15, "the drone to land")
    assert not errors.records, [r.getMessage() for r in errors.records]
