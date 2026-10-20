"""Your first lap: Circuit 1, flown as discrete steps.

Start Squadrone first (`python -m comp2`) and leave its page open, then run
this file the way you run any Python program — from your IDE, or:

    python examples/01_first_lap.py

Circuit 1 is the published oval: take off from the pad at (3.00, 3.00) facing
Gate 1, then through Gate 1 at (1.00, 3.00, z=2.0), Gate 2 at (3.00, 1.00,
z=1.5) and Gate 3 at (5.00, 3.00, z=1.0), and back to the pad. Every number
below is derived from that published geometry — see
comp2/tracks/circuit1_oval.json. x runs along the cage's 6 m length, y across
its 4 m width.

A gate only counts when you cross it along its normal, so the lap is a lap of
the cage: west along y = 3.0 through Gate 1, down the west end, east along
y = 1.0 through Gate 2, up the east end, then west along y = 3.0 again through
Gate 3 and straight onto the pad. Each command stops the drone before the next
one starts, which makes that easy to reason about — and costs smoothness
points. The brief's +10 smooth-trajectory bonus is not for flying like this;
compare your score with 02_smooth_lap.py, which flies the same circuit on
velocity control.
"""

from squadrone import Drone

drone = Drone()

drone.takeoff()  # hover at 1 m on the pad, already facing Gate 1
drone.up(100)  # climb to Gate 1's height (2.0 m)

# --- Gate 1 (1.00, 3.00), crossed heading 270 deg -------------------------
drone.forward(230)  # straight down y = 3.0, through Gate 1

# --- Gate 2 (3.00, 1.00), crossed heading 090 deg -------------------------
drone.turn_left(180)  # come round to face east
drone.down(50)  # Gate 2 sits 0.5 m lower
drone.right(200)  # cross the cage onto the gate's axis (y = 1.0)
drone.forward(490)  # down the length of the cage, through Gate 2

# --- Gate 3 (5.00, 3.00), crossed heading 270 deg -------------------------
drone.turn_left(90)  # face north at the east end
drone.down(50)  # Gate 3 is the lowest, at 1.0 m
drone.forward(200)  # back up to y = 3.0, clear of the gate
drone.turn_left(90)  # face west, square on to Gate 3

# --- home -----------------------------------------------------------------
drone.forward(260)  # through Gate 3 and straight back over the pad
drone.land()  # the clock stops when you land on the pad
