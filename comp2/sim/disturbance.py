"""Air that will not hold still.

A real Tello does not fly where it is told. It wanders — a few centimetres, over
a few seconds — as the room's air moves and its own attitude estimate drifts.
The simulator used to fly perfectly, which quietly made every open-loop strategy
look better than it will be on the day.

**Why this is a sum of slow sinusoids and not a random walk.** The scorer's
smoothness term is RMS *jerk*, the third derivative of the sampled path
(:mod:`comp2.sim.race`), computed by dividing a third difference by ``dt**3``.
At 30 Hz ``dt**3`` is 3.7e-5, so the divisor amplifies small per-sample
differences by about 27,000. An Ornstein-Uhlenbeck process is a diffusion: it is
nowhere differentiable and its per-step increment is white however long its
correlation time, so it lands squarely on that amplification. Measured against
``J_REF = 300``:

======================================  ======  ==========
model                                    jerk   smoothness
======================================  ======  ==========
OU, tau 3 s, sigma 5 cm                    860       0.057
OU, tau 3 s, sigma 3 cm                    516       0.179
band-limited sinusoids, 5 cm                0.2      0.999
======================================  ======  ==========

A sinusoid of amplitude A at frequency f has peak jerk ``A*(2*pi*f)**3``: at
5 cm and 0.25 Hz that is 0.19 m/s^3, about 1/1500 of ``J_REF``. So this gives
visible, realistic wander that costs accuracy — as it should, the drone really
is off course — without destroying a smoothness score it has no business
touching. It is also analytic, seedable and stateless, a pure function of flight
time, which is what lets it be applied in one place instead of being threaded
through every motion path.

One consequence worth knowing: ``SimDrone.pose()`` rounds to millimetres, and
that quantisation alone costs about 33 of jerk on an otherwise perfect path. Any
disturbance added here must therefore be far larger than a millimetre and stay
well under about 0.5 Hz.
"""

from __future__ import annotations

import math
import random

# Slow enough that the third derivative stays negligible, fast enough to be a
# nuisance to a controller over one lap.
MIN_HZ = 0.05
MAX_HZ = 0.40
COMPONENTS = 3  # sinusoids per axis


class Gusts:
    """Band-limited drift, as a pure function of flight time.

    ``scale`` is the approximate peak excursion in metres per axis. ``vertical``
    scales the z axis separately: on a real Tello the barometric altitude hold
    wanders further, and slower, than the horizontal position does.
    """

    def __init__(self, seed: int = 0, scale: float = 0.0, vertical: float = 1.6):
        self.scale = scale
        rng = random.Random(seed)
        self._axes = []
        for axis in range(3):
            amp = scale * (vertical if axis == 2 else 1.0) / COMPONENTS
            band_lo = MIN_HZ * (0.5 if axis == 2 else 1.0)  # baro wanders slower
            self._axes.append(
                [
                    (
                        rng.uniform(band_lo, MAX_HZ),
                        rng.uniform(0.0, 2 * math.pi),
                        amp * rng.uniform(0.6, 1.4),
                    )
                    for _ in range(COMPONENTS)
                ]
            )

    def offset(self, t: float) -> tuple[float, float, float]:
        """Displacement from the commanded position, in metres."""
        if self.scale <= 0:
            return (0.0, 0.0, 0.0)
        return tuple(
            sum(a * math.sin(2 * math.pi * f * t + p) for f, p, a in axis)
            for axis in self._axes
        )

    def velocity(self, t: float) -> tuple[float, float, float]:
        """The analytic derivative — what the camera sees as motion blur."""
        if self.scale <= 0:
            return (0.0, 0.0, 0.0)
        return tuple(
            sum(
                a * 2 * math.pi * f * math.cos(2 * math.pi * f * t + p)
                for f, p, a in axis
            )
            for axis in self._axes
        )
