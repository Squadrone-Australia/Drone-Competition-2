"""Hardware height reaches the public API without accepting stale telemetry.

Telemetry is preferred when it gets through; a firewall that drops :8890 must
not cost a team its height, so the adapter then asks ``tof?`` instead.
"""

import pytest

from comp2.api import Drone, Session
from tests.tello_fakes import FakeLink, FakeState, connected_tello


def hardware_api(packets=(), tof="ok"):
    link = FakeLink(replies={"tof?": tof})
    adapter = connected_tello(link, FakeState(packets))
    return Drone(_session=Session(adapter)), link


@pytest.mark.parametrize("height", [0, 82, 150])
def test_public_height_reads_the_tello_state_in_centimetres(height):
    api, link = hardware_api([{"h": height, "tof": 65}])
    assert api.height == height
    assert "tof?" not in link.calls


def test_only_a_packet_after_the_call_counts():
    api, _ = hardware_api([{"h": 82}, {"h": 120}])
    assert api.height == 82
    assert api.height == 120


def test_blocked_telemetry_falls_back_to_asking_the_range_finder():
    api, link = hardware_api([], tof="801mm")
    assert api.height == pytest.approx(80.1)
    assert link.calls == ["tof?"]


def test_no_telemetry_socket_at_all_also_asks():
    link = FakeLink(replies={"tof?": "450mm"})
    api = Drone(_session=Session(connected_tello(link, state=None)))
    assert api.height == 45


def test_a_packet_without_height_falls_back_too():
    api, link = hardware_api([{"mid": -1}], tof="300mm")
    assert api.height == 30


def test_an_unreadable_range_finder_reply_is_not_a_height():
    api, _ = hardware_api([], tof="ok")
    with pytest.raises(RuntimeError, match="unreadable height"):
        _ = api.height


@pytest.mark.parametrize("height", [None, -1, float("nan"), float("inf"), 3001, True])
def test_height_rejects_invalid_values(height):
    api, _ = hardware_api([{"h": height}])
    with pytest.raises(RuntimeError, match="invalid"):
        _ = api.height
