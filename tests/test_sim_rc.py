"""The rc-velocity integrator: the sim half of continuous control."""

import time

import pytest

from comp2.sim.drone import RC_MAX_SPEED_MS, SimDrone
from comp2.sim.world import World


def drone():
    d = SimDrone(
        world=World(
            width_m=4.0,
            depth_m=4.0,
            height_m=4.0,
            gates=[],
            start=(2.0, 2.0),
            finish=(2.0, 2.0),
        ),
        delay=0,
    )
    d.takeoff()
    return d


def test_rc_setpoint_moves_the_drone_continuously():
    d = drone()
    d.send_rc(0, 100, 0, 0)
    time.sleep(0.6)
    d.send_rc(0, 0, 0, 0)
    assert d.y > 2.2  # moved forward
    assert d.x == pytest.approx(2.0, abs=0.01)


def test_rc_velocity_converges_to_the_commanded_speed():
    d = drone()
    d.send_rc(0, 100, 0, 0)
    time.sleep(1.0)  # >> tau, so v ~= max
    y0, t0 = d.y, time.perf_counter()
    time.sleep(0.3)
    speed = (d.y - y0) / (time.perf_counter() - t0)
    d.send_rc(0, 0, 0, 0)
    assert speed == pytest.approx(RC_MAX_SPEED_MS, rel=0.2)


def test_zero_rc_decays_to_hover():
    d = drone()
    d.send_rc(0, 100, 0, 0)
    time.sleep(0.4)
    d.send_rc(0, 0, 0, 0)
    time.sleep(0.6)  # decay
    y = d.y
    time.sleep(0.2)
    assert d.y == pytest.approx(y, abs=0.01)  # stopped drifting
    assert d.pitch == pytest.approx(0.0, abs=0.5)


def test_rc_yaw_turns_the_drone():
    d = drone()
    d.send_rc(0, 0, 0, 100)
    time.sleep(0.5)
    d.send_rc(0, 0, 0, 0)
    assert d.heading > 10


def test_rc_respects_the_walls():
    d = drone()
    d.send_rc(0, 100, 0, 0)
    time.sleep(2.5)  # would be ~2.5 m in a 4 m room
    d.send_rc(0, 0, 0, 0)
    assert d.y <= 3.8 + 1e-6


def test_rc_while_landed_is_refused_but_zero_is_accepted():
    d = SimDrone(world=World(gates=[]), delay=0)
    d.send_rc(0, 0, 0, 0)  # stop paths always send this — must not raise
    with pytest.raises(RuntimeError):
        d.send_rc(0, 50, 0, 0)


def test_emergency_kills_the_rc_setpoint():
    d = drone()
    d.send_rc(0, 100, 0, 0)
    time.sleep(0.2)
    d.emergency()
    y = d.y
    time.sleep(0.3)
    assert d.y == y  # no further motion
    assert not d.flying and d.z == 0.0


def test_discrete_commands_zero_the_rc_target_first():
    d = drone()
    d.send_rc(0, 100, 0, 0)
    time.sleep(0.2)
    d.move("forward", 50)  # discrete command takes over
    y_after_move = d.y
    time.sleep(0.3)
    assert d.y == pytest.approx(y_after_move, abs=0.02), (
        "rc setpoint survived a discrete command"
    )


def test_reset_clears_the_rc_state():
    d = drone()
    d.send_rc(0, 100, 0, 0)
    time.sleep(0.2)
    d.reset()
    assert (d.x, d.y, d.z, d.flying) == (2.0, 2.0, 0.0, False)
    time.sleep(0.3)
    assert (d.x, d.y) == (2.0, 2.0)  # no ghost velocity after reset
