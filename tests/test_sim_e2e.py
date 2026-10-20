"""The public Drone API flying the simulator end to end — no hardware, no browser."""

import pytest

from comp2.api import Drone
from comp2.sim.drone import SimDrone
from comp2.sim.world import Gate, World


def straight_track():
    return World(
        width_m=4.0,
        depth_m=6.0,
        height_m=3.0,
        gates=[Gate(1.5, 2.0, 1.0, 0.0)],
        start=(1.5, 0.7),
        finish=(1.5, 4.0),
        name="straight",
    )


def test_a_discrete_lap_flies_the_track():
    sim = SimDrone(world=straight_track(), delay=0)
    drone = Drone(sim)
    # takeoff at 1 m, fly straight through the gate, land on the far pad
    drone.takeoff()
    drone.forward(330)
    drone.land()
    assert not sim.flying
    assert round(sim.y, 2) == 4.0  # on the landing pad


def test_a_glide_lap_flies_the_track_without_stops():
    sim = SimDrone(world=straight_track(), delay=0)
    drone = Drone(sim)
    drone.takeoff()
    drone.go(330, 0, 0, speed=100)
    drone.land()
    assert not sim.flying
    assert round(sim.y, 2) == 4.0


def test_an_error_when_flying_before_takeoff():
    drone = Drone(SimDrone(world=straight_track(), delay=0))
    with pytest.raises(RuntimeError):
        drone.forward(50)
