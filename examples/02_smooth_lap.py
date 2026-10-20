"""The same oval, flown smoothly with velocity control.

Start Squadrone first (`python -m comp2`) and leave its page open, then run
this file the way you run any Python program — from your IDE, or:

    python examples/02_smooth_lap.py

`drone.fly()` is stick input: it sets a velocity and keeps it until you change
it, so the drone carries speed through the whole lap instead of stopping
between commands. That matters twice over — trajectory smoothness is a scored
metric, and the brief's +10 bonus goes only to a continuous curved flight that
reasonably follows the published oval geometry. A lap built from straight
segments and abrupt heading changes, like 01_first_lap.py, does not qualify.

The trick is to fly the oval the way a car takes a bend: hold a forward speed
and yaw at the same time, so the nose stays on the tangent. A gate's normal
lies along that tangent, so arriving nose-first is arriving square. Forward
speed over yaw rate is your turn radius — 0.55 m/s at 70 deg/s is about
0.45 m, which is the tightest corner the 6 x 4 m cage leaves you at the ends
of the two long straights (y = 3.0 out, y = 1.0 back).

Nothing here is closed-loop: it is dead reckoning, tuned against the published
geometry, and it drifts the moment the drone does. Every number is one to
re-derive — and to back up with the camera (03_vision_gate.py) once you trust
the shape of the lap.
"""

from squadrone import Drone

drone = Drone()

drone.takeoff()
drone.fly(fb=55, ud=50, seconds=1.6)  # roll off the pad, climbing towards Gate 1
drone.fly(fb=65, ud=10, seconds=1.9)  # level out at 2.0 m and through Gate 1
drone.fly(fb=55, yaw=-70, seconds=1.3)  # bank left into the west end
drone.fly(fb=55, ud=-27, seconds=1.9)  # across the west end, down to Gate 2's height
drone.fly(fb=55, yaw=-70, seconds=1.3)  # roll out heading east on y = 1.0
drone.fly(fb=75, seconds=5.4)  # the long straight, through Gate 2
drone.fly(fb=55, yaw=-70, seconds=1.3)  # bank left into the east end
drone.fly(fb=55, ud=-26, seconds=1.9)  # across the east end, down to Gate 3's height
drone.fly(fb=55, yaw=-70, seconds=1.3)  # roll out heading west, square on to Gate 3
drone.fly(fb=70, seconds=3.1)  # through Gate 3 and home along y = 3.0

drone.hover()
drone.land()
