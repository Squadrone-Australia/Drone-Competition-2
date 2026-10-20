"""The simulator's imperfections: drift, takeoff spread, a noisy altimeter.

A simulator that flies perfectly quietly rewards strategies that will not
survive the arena. These add the parts of a real Tello that misbehave — and,
just as importantly, pin down how far they are allowed to misbehave, because a
disturbance model that wrecks the scoring is no more honest than none at all.
"""

import math

import pytest

from comp2.sim.disturbance import Gusts
from comp2.sim.drone import TAKEOFF_MAX_M, TAKEOFF_MIN_M, SimDrone
from comp2.sim.race import J_REF


def jerk_rms(samples):
    """The scorer's own smoothness measure, over a sampled path."""
    total = count = 0
    for i in range(3, len(samples)):
        dt = (samples[i][0] - samples[i - 3][0]) / 3
        if dt <= 1e-4:
            continue
        j = 0.0
        for axis in (1, 2, 3):
            third = (
                samples[i][axis]
                - 3 * samples[i - 1][axis]
                + 3 * samples[i - 2][axis]
                - samples[i - 3][axis]
            )
            j += (third / dt**3) ** 2
        total += j
        count += 1
    return math.sqrt(total / count) if count else 0.0


# --- the disturbance must not destroy the score --------------------------


def test_drift_is_band_limited_enough_to_preserve_smoothness():
    """The test that catches a random-walk disturbance model.

    Smoothness is RMS *jerk* over the 30 Hz pose stream, a third derivative, so
    the divisor amplifies per-sample noise about 27,000-fold. A diffusion — an
    Ornstein-Uhlenbeck process, say — has white increments however long its
    correlation time and scores near zero here. A sum of sub-hertz sinusoids
    moves the aircraft just as far and costs almost nothing.
    """
    gusts = Gusts(seed=3, scale=0.05)
    samples = [
        (i / 30.0, *(3.0 + d for d in gusts.offset(i / 30.0))) for i in range(900)
    ]
    jerk = jerk_rms(samples)
    assert jerk < 0.05 * J_REF, f"jerk {jerk:.1f} would gut the smoothness score"
    assert math.exp(-jerk / J_REF) > 0.95


def test_drift_actually_moves_the_aircraft():
    """The other half: a disturbance that scores well by doing nothing is no use."""
    gusts = Gusts(seed=3, scale=0.05)
    excursions = [
        max(abs(gusts.offset(t / 30.0)[axis]) for t in range(900)) for axis in range(3)
    ]
    assert min(excursions) > 0.01, "the air is not moving the aircraft at all"
    assert max(excursions) < 0.5, "the air is throwing the aircraft across the cage"


def test_no_noise_means_no_drift_at_all():
    """The deterministic path the rest of the suite depends on."""
    assert Gusts(seed=1, scale=0.0).offset(12.3) == (0.0, 0.0, 0.0)
    drone = SimDrone(delay=0, noise=0.0, arena_name="clean")
    drone.takeoff()
    assert drone.z == pytest.approx(1.0, abs=1e-9)
    assert drone.pose()["x"] == pytest.approx(drone.world.start_xy[0])


# --- takeoff ---------------------------------------------------------------


def test_takeoff_height_varies_between_flights():
    """A real Tello does not stop at the same height twice."""
    heights = []
    for seed in range(12):
        drone = SimDrone(delay=0, noise=0.05, seed=seed, arena_name="clean")
        drone.takeoff()
        heights.append(drone.z)
    assert len(set(heights)) > 1, "every takeoff reached exactly the same height"
    assert max(heights) - min(heights) > 0.02
    assert all(TAKEOFF_MIN_M <= h <= TAKEOFF_MAX_M for h in heights)


def test_takeoff_height_is_exact_without_noise():
    drone = SimDrone(delay=0, noise=0.0, arena_name="clean")
    drone.takeoff()
    assert drone.z == 1.0


# --- the altimeter ---------------------------------------------------------


def test_reported_height_is_quantised_like_the_sdk():
    """The Tello reports whole centimetres, however still the air is."""
    drone = SimDrone(delay=0, noise=0.0, arena_name="clean")
    drone.takeoff()
    drone.z = 1.234
    assert drone.height_cm() == pytest.approx(123.0)


def test_reported_height_follows_the_drift_not_the_command():
    """A climb loop reading this is reading the air, not its own intention."""
    drone = SimDrone(delay=0, noise=0.05, seed=2, arena_name="clean")
    drone.takeoff()
    drone._flight_t = 7.0
    assert drone.height_cm() != pytest.approx(drone.z * 100, abs=0.5)


# --- the aircraft stays inside the world ----------------------------------


def test_drift_cannot_push_the_camera_through_the_floor():
    """Regression: a downward gust once put the viewpoint under the world.

    The camera then looked up at the underside of the floor, with the gloss
    reflections hanging above it — a completely unusable frame at exactly the
    moment after takeoff when a program starts looking for a gate.
    """
    drone = SimDrone(delay=0, noise=0.2, seed=5, arena_name="clean")
    drone.takeoff()
    drone.z = 0.3
    for step in range(1200):
        drone._flight_t = step * 0.05
        x, y, z = drone._observed()
        assert z > 0.0, "the camera went underground"
        assert 0.0 <= x <= drone.world.width_m
        assert 0.0 <= y <= drone.world.depth_m


def test_a_landed_drone_does_not_drift():
    drone = SimDrone(delay=0, noise=0.2, seed=5, arena_name="clean")
    for step in range(50):
        drone._flight_t = step * 0.1
        assert drone._observed() == (drone.x, drone.y, drone.z)


# --- the rotate-noise scaling bug -----------------------------------------


def test_rotate_error_scales_with_the_noise_setting():
    """It used to be pinned at 2 degrees, so --noise 0.001 was as bad as 0.5."""

    def spread(noise):
        errors = []
        for seed in range(24):
            drone = SimDrone(delay=0, noise=noise, seed=seed, arena_name="clean")
            start = drone.heading
            drone.takeoff()
            drone.rotate("cw", 90)
            # Heading wraps, so compare the turn actually made against the 90
            # asked for, on the shorter way round.
            turned = (drone.heading - start + 180) % 360 - 180
            errors.append(abs(turned - 90))
        return sum(errors) / len(errors)

    loud, quiet = spread(0.2), spread(0.02)
    assert loud > quiet * 3, f"noise 0.2 gave {loud:.2f} deg, 0.02 gave {quiet:.2f}"
    assert quiet < 2.0
