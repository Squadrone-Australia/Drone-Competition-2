"""The Tello adapter, with no aircraft: a fake link and a loopback video stream."""

import socket
import threading
import time

import numpy as np
import pytest

import comp2.drone.tello as t
from comp2.drone.tello_link import TelloNoReply
from tests.tello_fakes import FakeLink, connected_tello

# --- commands --------------------------------------------------------------


def test_adapter_maps_commands_to_sdk_strings():
    d = connected_tello()
    d.takeoff()
    d.move("forward", 40)
    d.rotate("cw", 90)
    d.rotate("ccw", 45)
    d.flip("back")
    assert d.battery() == 87
    assert d._link.calls == [
        "takeoff",
        "forward 40",
        "cw 90",
        "ccw 45",
        "flip b",
        "battery?",
    ]


def test_adapter_maps_continuous_control():
    d = connected_tello()
    d.send_rc(0, 60, 0, -10)
    d.go_xyz(100, 0, 20, 50)
    d.curve_xyz(50, 50, 0, 100, 0, 0, 60)
    assert d._link.calls == [
        ("no_reply", "rc 0 60 0 -10"),  # the SDK never answers rc
        "go 100 0 20 50",
        "curve 50 50 0 100 0 0 60",
    ]


def test_land_and_emergency_centre_the_sticks_first():
    d = connected_tello()
    d.land()
    d.emergency()
    assert d._link.calls == [
        ("no_reply", "rc 0 0 0 0"),
        "land",
        ("no_reply", "rc 0 0 0 0"),
        ("interrupt", "emergency"),
    ]


def test_a_refusal_keeps_the_link_but_silence_drops_it():
    d = connected_tello(FakeLink(refuse={"flip b"}))
    with pytest.raises(Exception, match="refused"):
        d.flip("back")
    assert d.link_ok

    def silent(command, timeout=None):
        raise TelloNoReply("no answer")

    d._link.send = silent
    with pytest.raises(TelloNoReply):
        d.takeoff()
    assert not d.link_ok


def test_unreadable_battery_is_an_error_not_a_number():
    d = connected_tello(FakeLink(replies={"battery?": "ok"}))
    with pytest.raises(Exception, match="unreadable battery"):
        d.battery()


def test_rc_without_a_link_is_a_no_op_so_teardown_never_raises():
    t.TelloDrone().send_rc(0, 0, 0, 0)


# --- connection lifecycle ----------------------------------------------------


@pytest.fixture
def offline_video(monkeypatch):
    """Connect without any video or telemetry socket."""
    monkeypatch.setattr(t.TelloDrone, "_open_reader", lambda self: None)
    monkeypatch.setattr(t, "TelloState", _no_state)


def _no_state():
    raise OSError("port busy")


def test_connect_handshakes_and_starts_the_stream_without_telemetry(
    monkeypatch, offline_video
):
    links = []
    monkeypatch.setattr(t, "TelloLink", lambda: links.append(FakeLink()) or links[-1])
    d = t.TelloDrone()
    d.connect()
    assert links[0].calls == ["command", "streamon"]
    assert d.link_ok and d._state is None


def test_reconnect_is_a_fresh_link_and_closing_never_flies(monkeypatch, offline_video):
    links = []
    monkeypatch.setattr(t, "TelloLink", lambda: links.append(FakeLink()) or links[-1])
    d = t.TelloDrone()
    d.connect()
    d.reconnect()
    assert len(links) == 2 and links[0].closed and not links[1].closed
    d.close()
    assert links[1].closed and not d.link_ok
    flown = [
        c for link in links for c in link.calls if c not in ("command", "streamon")
    ]
    assert flown == []  # no land, no streamoff (which kills video until a reboot)


def test_a_failed_handshake_releases_the_socket(monkeypatch, offline_video):
    link = FakeLink()

    def no_answer():
        raise TelloNoReply("nothing answered")

    link.handshake = no_answer
    monkeypatch.setattr(t, "TelloLink", lambda: link)
    d = t.TelloDrone()
    with pytest.raises(TelloNoReply):
        d.connect()
    assert link.closed and not d.link_ok


# --- video -------------------------------------------------------------------
#
# The reader owns its socket and decoder, and only its worker thread touches
# either: freeing a decoder's input under a thread still reading it is the
# use-after-free that used to kill the process with no traceback.


class FakeAvFrame:
    def __init__(self, value):
        self.value = value

    def to_ndarray(self, format=None):
        return np.full((4, 4, 3), self.value, np.uint8)


class FakeDecoder:
    """Every datagram is one packet, every packet one frame."""

    def parse(self, data):
        return [data]

    def decode(self, packet):
        return [FakeAvFrame(packet[0])]


@pytest.fixture
def loopback_video(monkeypatch):
    """Readers bind an ephemeral port and accept datagrams from 127.0.0.1."""
    readers = []
    original = t.FrameReader.open.__func__

    def open_loopback(cls, **kwargs):
        reader = original(
            cls,
            port=0,
            host="127.0.0.1",
            source_ports=(9,),
            decoder_factory=FakeDecoder,
        )
        readers.append(reader)
        return reader

    monkeypatch.setattr(t.FrameReader, "open", classmethod(open_loopback))
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    yield (
        readers,
        lambda value: sender.sendto(bytes([value]), ("127.0.0.1", readers[-1].port)),
    )
    sender.close()
    for reader in readers:
        reader.close()


def _wait_for_frame(drone, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = drone.get_frame()
        if frame is not None:
            return frame
        time.sleep(0.01)
    raise AssertionError("no frame arrived")


def test_frames_arrive_as_bgr_and_closing_releases_the_port(loopback_video):
    readers, send = loopback_video
    d = connected_tello()
    d._open_reader()
    send(200)
    assert _wait_for_frame(d).shape == (4, 4, 3)
    port = readers[0].port
    worker = readers[0]._worker

    d.close()

    assert not worker.is_alive()
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("", port))  # the port is free again
    probe.close()
    assert d.get_frame() is None


def test_the_socket_is_only_closed_by_its_own_worker(loopback_video, monkeypatch):
    readers, send = loopback_video
    d = connected_tello()
    d._open_reader()
    closed_by = []
    sock = readers[0]._sock
    real_close = sock.close

    class Watched:
        def __getattr__(self, name):
            return getattr(sock, name)

        def close(self):
            closed_by.append(threading.current_thread())
            real_close()

    readers[0]._sock = Watched()
    d.close()
    assert closed_by and all(th is not threading.current_thread() for th in closed_by)


def test_a_stalled_stream_expires_its_last_frame(loopback_video, monkeypatch):
    readers, send = loopback_video
    d = connected_tello()
    d._open_reader()
    send(7)
    _wait_for_frame(d)
    later = time.monotonic() + t._FRAME_MAX_AGE_S + 0.1
    monkeypatch.setattr(t.time, "monotonic", lambda: later)
    assert readers[0].frame is None


def test_a_stream_that_stops_is_a_lost_link(loopback_video, monkeypatch):
    _, send = loopback_video
    d = connected_tello()
    d._open_reader()
    send(1)
    _wait_for_frame(d)
    later = time.monotonic() + d.link_timeout_s + 1
    monkeypatch.setattr(t.time, "monotonic", lambda: later)
    assert d.get_frame() is None
    assert not d.link_ok and not d.video_blocked


def test_a_stream_that_never_starts_is_blocked_video_not_a_lost_link(
    loopback_video, monkeypatch
):
    d = connected_tello()
    d._frame_at = time.monotonic()
    d._open_reader()
    later = time.monotonic() + d.link_timeout_s + 1
    monkeypatch.setattr(t.time, "monotonic", lambda: later)
    assert d.get_frame() is None
    assert d.link_ok and d.video_blocked


def test_a_video_port_held_elsewhere_is_reported_at_once():
    holder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    holder.bind(("", 0))
    original = t.FrameReader.open.__func__
    try:
        d = connected_tello()
        t.FrameReader.open = classmethod(
            lambda cls, **kw: original(cls, port=holder.getsockname()[1])
        )
        assert d._open_reader() is None
        assert d.video_port_busy and d.video_blocked and d.link_ok
    finally:
        t.FrameReader.open = classmethod(original)
        holder.close()
