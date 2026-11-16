"""How far a Tello misses what it was told to do.

A Tello knows where it is by accumulating optical-flow displacement, so every
move ends a little off and the error depends on what the downward camera can
see: texture and light. No public source measures error against commanded
distance, so the numbers below are anchored on what was observed flying the real
aircraft (a 50 cm move lands 2-5 cm off, more over longer moves in a poor room).
The takeoff, heading and hover figures are engineering estimates. They all live
in :data:`PROFILES` so that measurements can replace them.

Errors change where a manoeuvre *ends*, never the smoothness of getting there:
the animation still interpolates to the perturbed target, so the jerk the scorer
measures is untouched. Nothing here is drawn per frame; see
:mod:`comp2.sim.disturbance` for why that matters.
"""

from __future__ import annotations

import dataclasses
import random
from dataclasses import dataclass
from typing import NamedTuple

# A draw further out than this would be an aircraft fault, not positioning error.
TRUNCATE_SIGMA = 2.5


@dataclass(frozen=True)
class FlightProfile:
    """One-sigma error figures for one quality of flying conditions."""

    along_floor_m: float = 0.0  # error of even the shortest move
    along_fraction: float = 0.0  # per metre flown
    along_growth_per_m: float = 0.0  # poor conditions make long moves worse
    lateral_ratio: float = 0.0  # sideways error as a fraction of along-track
    vertical_floor_m: float = 0.0
    vertical_fraction: float = 0.0
    flight_bias: float = 0.0  # scale error held for a whole flight
    yaw_floor_deg: float = 0.0
    yaw_fraction: float = 0.0  # per degree turned
    yaw_per_m_deg: float = 0.0  # heading picked up by translating
    takeoff_height_m: float = 0.0
    takeoff_shift_m: float = 0.0
    gust_m: float = 0.0  # a few seconds' wander about the commanded path
    slow_drift_m: float = 0.0  # the same, over tens of seconds
    rc_gain: float = 0.0  # stick-to-velocity error, per axis, per flight

    def scaled(self, k: float) -> FlightProfile:
        """Every magnitude times ``k``; the shape of the model is kept."""
        keep = {"along_growth_per_m", "lateral_ratio"}
        return dataclasses.replace(
            self,
            **{
                f.name: getattr(self, f.name) * k
                for f in dataclasses.fields(self)
                if f.name not in keep
            },
        )


PROFILES: dict[str, FlightProfile] = {
    "ideal": FlightProfile(),
    "good": FlightProfile(
        along_floor_m=0.010,
        along_fraction=0.025,
        lateral_ratio=0.5,
        vertical_floor_m=0.008,
        vertical_fraction=0.02,
        flight_bias=0.01,
        yaw_floor_deg=0.5,
        yaw_fraction=0.005,
        yaw_per_m_deg=0.3,
        takeoff_height_m=0.04,
        takeoff_shift_m=0.02,
        gust_m=0.015,
        slow_drift_m=0.02,
        rc_gain=0.03,
    ),
    "typical": FlightProfile(
        along_floor_m=0.015,
        along_fraction=0.04,
        along_growth_per_m=0.15,
        lateral_ratio=0.5,
        vertical_floor_m=0.012,
        vertical_fraction=0.03,
        flight_bias=0.02,
        yaw_floor_deg=1.0,
        yaw_fraction=0.01,
        yaw_per_m_deg=0.6,
        takeoff_height_m=0.08,
        takeoff_shift_m=0.04,
        gust_m=0.03,
        slow_drift_m=0.05,
        rc_gain=0.05,
    ),
    # Low-texture floor or dim light: the manual's own warning about the vision
    # positioning system.
    "poor": FlightProfile(
        along_floor_m=0.025,
        along_fraction=0.06,
        along_growth_per_m=0.5,
        lateral_ratio=0.7,
        vertical_floor_m=0.02,
        vertical_fraction=0.05,
        flight_bias=0.04,
        yaw_floor_deg=2.0,
        yaw_fraction=0.02,
        yaw_per_m_deg=1.5,
        takeoff_height_m=0.12,
        takeoff_shift_m=0.08,
        gust_m=0.06,
        slow_drift_m=0.12,
        rc_gain=0.10,
    ),
}

# `noise` predates the profiles: this value of it means exactly `typical`.
NOISE_REFERENCE = 0.05


def resolve(flight: str = "ideal", noise: float = 0.0) -> FlightProfile:
    """The profile for a named quality, or for the older numeric ``noise``."""
    if noise:
        return PROFILES["typical"].scaled(noise / NOISE_REFERENCE)
    try:
        return PROFILES[flight]
    except KeyError:
        raise ValueError(
            f"unknown flight profile {flight!r}; choose from {', '.join(PROFILES)}"
        ) from None


class TranslationError(NamedTuple):
    along_m: float  # in the direction of travel
    lateral_m: float  # to the right of it
    vertical_m: float
    heading_deg: float


class FlightError:
    """Draws the errors for one flight, reproducibly from ``seed``."""

    def __init__(self, profile: FlightProfile, seed: int = 0):
        self.profile = profile
        self._rng = random.Random(seed)
        self._bias = 0.0
        self.rc_gain = (1.0, 1.0, 1.0, 1.0)

    def _draw(self, sigma: float) -> float:
        if sigma <= 0:
            return 0.0
        limit = TRUNCATE_SIGMA * sigma
        return max(-limit, min(limit, self._rng.gauss(0.0, sigma)))

    def begin_flight(self) -> None:
        """Draw what stays wrong until the next landing."""
        self._bias = self._draw(self.profile.flight_bias)
        self.rc_gain = tuple(1.0 + self._draw(self.profile.rc_gain) for _ in range(4))

    def _along_sigma(self, dist_m: float) -> float:
        p = self.profile
        grown = p.along_fraction * dist_m * (1.0 + p.along_growth_per_m * dist_m)
        return p.along_floor_m + grown

    def translation(self, horizontal_m: float, vertical_m: float) -> TranslationError:
        """Error for a move of this horizontal length and signed climb."""
        if horizontal_m <= 0 and vertical_m == 0:
            return TranslationError(0.0, 0.0, 0.0, 0.0)
        p = self.profile
        climb = abs(vertical_m)
        # A vertical move has no direction of travel in plan, but it still
        # wanders in plan: both horizontal axes get the sideways figure.
        if horizontal_m > 0:
            along = self._draw(self._along_sigma(horizontal_m))
            along += self._bias * horizontal_m
        else:
            along = self._draw(p.lateral_ratio * self._along_sigma(climb))
        lateral = self._draw(
            p.lateral_ratio * self._along_sigma(max(horizontal_m, climb))
        )
        vertical = self._draw(p.vertical_floor_m + p.vertical_fraction * climb)
        vertical += self._bias * vertical_m
        heading = self._draw(p.yaw_per_m_deg * (horizontal_m + climb))
        return TranslationError(along, lateral, vertical, heading)

    def rotation(self, deg: float) -> float:
        """Degrees to add to a commanded turn of ``deg``."""
        p = self.profile
        return self._draw(p.yaw_floor_deg + p.yaw_fraction * abs(deg))

    def takeoff(self) -> tuple[float, float, float]:
        """(x, y, height) offsets from the nominal takeoff, in metres."""
        p = self.profile
        return (
            self._draw(p.takeoff_shift_m),
            self._draw(p.takeoff_shift_m),
            self._draw(p.takeoff_height_m),
        )
