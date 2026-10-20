# Squadrone client guide

How to fly the simulator, and then the real Tello, from your own Python code
using the `squadrone` library. This guide explains the concepts and shows the
patterns you will actually use. For every operation, argument, range and unit,
see the [Drone operations reference](drone-operations.md).

Contents:

1. [How it fits together](#1-how-it-fits-together)
2. [Install](#2-install)
3. [Your first flight](#3-your-first-flight)
4. [The connection](#4-the-connection)
5. [How commands behave](#5-how-commands-behave)
6. [Choosing a control style](#6-choosing-a-control-style)
7. [Using the camera](#7-using-the-camera)
8. [Errors and the emergency stop](#8-errors-and-the-emergency-stop)
9. [Simulator and real Tello](#9-simulator-and-real-tello)
10. [Debugging and workflow](#10-debugging-and-workflow)
11. [Troubleshooting](#11-troubleshooting)
12. [Cheat sheet](#12-cheat-sheet)

## 1. How it fits together

Two separate programs cooperate:

```text
┌─────────────────────────────┐   WebSocket    ┌──────────────────────────┐
│ Squadrone application       │ ◄────────────► │ Your program             │
│ (python -m comp2)           │  ws://127.0.0.1│ (your IDE, your venv)    │
│                             │  :8765/ws      │                          │
│  • simulator OR real Tello  │                │  from squadrone import   │
│  • 3D arena, camera, scoring│                │      Drone               │
│  • browser cockpit + E-STOP │                │                          │
└─────────────────────────────┘                └──────────────────────────┘
```

- The **Squadrone application** owns the drone (simulated or real), the video,
  the scoring and the Emergency Stop button. You leave its browser page open
  while you fly.
- **Your program** is ordinary Python run from your own IDE, interpreter and
  virtual environment. It imports `Drone` from the small `squadrone` package
  (the `client/` directory of this repository) and calls methods on it.

The browser operator chooses *Simulator* or *Real Tello*. Your code does not
change between them. The client deliberately exposes no arena position and no
gate detector: your program learns about the world only through the camera
(`frame()`), `battery`, `height`, and what it commanded.

## 2. Install

The client needs Python 3.11 or newer. Install it into the **same interpreter
your IDE uses to run your code**:

```powershell
python -m pip install .\client          # from the repository root
```

This also installs its three dependencies: `websockets`, `numpy` and `Pillow`.
It does not install OpenCV. Install that yourself if you process the camera
frames with it (`python -m pip install opencv-python`).

Check that the right interpreter has it:

```powershell
python -c "from squadrone import Drone; print(Drone)"
```

If you edit the library itself, install in editable mode with
`python -m pip install -e .\client`. Whenever you pull a newer version of this
repository, install `client/` again: some features, such as
`frame(fresh=True)` and `show_detections()`, only exist in newer client
versions.

## 3. Your first flight

The application does not run your code; it waits for commands from it. So start
the application first, then run your script. While the application is running,
your script's `Drone()` connects to it and the simulator carries out each call.
This only works with the `squadrone` library from this repository (section 2),
not with other drone libraries.

1. Start the Squadrone application and leave its browser page open:

   ```powershell
   python -m comp2              # from the repository root, in its venv
   ```

2. Run this file from your IDE (or `python my_flight.py`):

   ```python
   from squadrone import Drone

   with Drone() as drone:
       print(f"Battery: {drone.battery}%")
       drone.takeoff()
       drone.forward(100)  # 1 m forward
       drone.turn_right(90)
       drone.forward(100)
       drone.land()
   ```

Watch the 3D arena and camera in the browser. Prints and tracebacks appear in
your own IDE terminal, not in the browser.

The two shipped examples are good next reads: `examples/01_first_lap.py` flies
Circuit 1 with discrete commands and `examples/02_smooth_lap.py` flies it with
continuous velocity control.

## 4. The connection

### Creating a `Drone`

```python
Drone()  # ws://127.0.0.1:8765/ws
Drone("ws://192.168.1.20:8765/ws")  # another machine
Drone(timeout=60)  # wait up to 60 s for any reply
```

The address resolves in this order: the `url` argument, then the
`SQUADRONE_URL` environment variable, then `ws://127.0.0.1:8765/ws`. If you
start the application on another port (`python -m comp2 --port 9000`), pass
that port here or set the variable:

```powershell
$env:SQUADRONE_URL = "ws://127.0.0.1:9000/ws"
```

`timeout` (default 30 s) is how long the client waits to connect and how long
it waits for each command to be answered. See
[Timeouts](#timeouts-and-long-commands) below.

### One controller at a time

Only one program can control the drone at once. A second `Drone()` raises
`ConnectionError("another program is already connected or running")`, and so
does connecting while the operator is switching between simulator and Tello.
While your program is connected, the browser locks the target switch and shows
the IDE connection status in the header.

Connecting also starts a **fresh practice attempt**: the race clock, trail and
gate progress reset, and the clock starts at `takeoff()`.

### Always close the connection

Use the context manager so cleanup happens even when your code crashes:

```python
with Drone() as drone:
    ...
```

Leaving the `with` block, an uncaught exception, Ctrl+C, stopping the run in
your IDE, or the program simply ending all close the connection. Squadrone then
**centres the sticks and lands the drone**. This is a safety net, not a plan:
finish your normal flights with `drone.hover()` and `drone.land()` so the intent
is explicit and your landing is scored properly.

Without a context manager, call `drone.close()` yourself. `close()` is safe to
call more than once.

## 5. How commands behave

### Calls block

Every command is synchronous. `drone.forward(100)` returns only when the move
has finished, so the next line of Python runs after the drone has arrived.
Commands return `None`, except for reads (`battery`, `height`, `frame()`,
`mission_pad()`).

`battery` and `height` are **properties**: write `drone.battery`, not
`drone.battery()`.

### Units and directions

Distances are centimetres, angles are degrees, durations are seconds, and
stick values are percentages (−100 to 100). Every direction is relative to the
drone's own nose at that moment, never to the arena:

| You write | The drone |
| --- | --- |
| `forward(cm)` / `back(cm)` | moves along its nose |
| `left(cm)` / `right(cm)` | slides sideways, nose unchanged |
| `up(cm)` / `down(cm)` | changes altitude |
| `turn_right(deg)` / `turn_left(deg)` | rotates clockwise / anticlockwise on the spot |
| `fly(lr=+)` | right |
| `go(y_cm=+)` | **left** (Tello convention, the opposite of `lr`) |

That last row is the classic trap: positive `lr` in `fly()` is right, positive
`y_cm` in `go()` and `curve()` is left.

### Out-of-range values are clamped, not fatal

Arithmetic that produces `forward(5)` would otherwise crash a flight. Instead
the value is rounded and clamped, and a warning is printed in your IDE terminal:

```text
! forward(5) is outside 20-500 cm; using 20
```

Discrete moves accept 20–500 cm, turns 1–360°, stick values ±100, `go()` axes
0 or ±20–500 cm, and `speed` 10–100 cm/s. Treat a warning as a bug in your
calculation, not as noise: the drone did something other than what you asked.

### Timeouts and long commands

The client gives up on a command after `timeout` seconds and raises
`ConnectionError("Squadrone timed out while running …")`. Commands that
legitimately run longer than that need a larger timeout:

- `wait(seconds)` with `seconds` greater than the timeout.
- Very slow `go()` / `curve()`: `go(x_cm=500, speed=10)` takes about 50 s.

```python
with Drone(timeout=90) as drone:
    ...
```

Prefer short, bounded commands in loops; that is also what keeps a debugger
pause safe.

## 6. Choosing a control style

| Style | Calls | Best for | Trade-off |
| --- | --- | --- | --- |
| Discrete | `forward`, `left`, `up`, `turn_right`, … | First laps, learning the geometry | Stops between commands, so a poor smoothness score |
| Point-to-point | `go`, `curve` | Clean diagonals and arcs through a known gate | Open loop; the drone cannot react while it moves |
| Velocity | `fly(lr, fb, ud, yaw, seconds=…)` | Racing lines and any closed-loop control | You must keep re-issuing it; needs tuning |

Smoothness is scored on the jerk of your whole flown path, so continuous
control scores better than stop-and-go moves. A good progression is to get a
lap working with discrete commands, then replace stretches of it with `go`,
`curve` and finally `fly`.

### `fly()` always needs `seconds`

```python
drone.fly(fb=60, ud=10, yaw=-20, seconds=0.3)  # climb and arc left for 0.3 s
```

The stick values are held for `seconds` (0–10), then the sticks are centred and
the drone hovers. Calling `fly()` without `seconds` raises `ValueError` before
anything is sent. This is deliberate: if you pause at a debugger breakpoint, the
drone must not be left flying on a stale command.

For a continuous curve, chain `fly()` calls and change the inputs between
them. Every call ends by centring the sticks, and the next call's values
arrive one network round trip later, so many very short steps can make the
motion stutter. Longer steps are smoother but react more slowly; the closed-loop
example below uses 0.1 s and `examples/02_smooth_lap.py` uses open-loop steps
of a second or more. Measure your own score card to find the balance. Stick
percentages are not speeds: in the simulator 100 is about 1 m/s (or 100°/s of
yaw), and a real Tello varies with battery and conditions.

### Tuning tip: open-loop numbers drift

`examples/02_smooth_lap.py` flies a whole lap on fixed stick values and
durations. That is dead reckoning: it works only as long as the drone does what
you assumed, and the real Tello will not stop at the same height twice. Use the
camera to correct it (next section).

## 7. Using the camera

### `frame()`

```python
frame = drone.frame()  # latest image, BGR, or None
frame = drone.frame(fresh=True, timeout=0.8)  # wait for a NEW image
```

The result is a NumPy `uint8` array of shape `(480, 640, 3)` in **BGR** order,
ready for OpenCV. The platform ships no gate detector. What you do with the
image is your competition entry.

- Plain `frame()` returns the most recent image immediately. If your program
  loops quickly, that may be the same image you already processed.
- `frame(fresh=True)` blocks until an image that arrived *after the call* is
  available. Use it in control loops so each decision is based on new pixels.
- On timeout, or when the stream stops, you get `None`, never a replayed old
  image. Always handle `None`; hovering is a sensible fallback.
- The array is a copy. Draw on it or change it freely.

The image is the raw sensor view. Anything Squadrone draws on its own display,
including the cockpit's minimap and your `show_detections()` outlines, is never
present in the frames you receive. In the simulator, frames come through the
real Tello's camera model, so a detector tuned there is a fair starting point
for the cage, although real lighting and netting will still need re-tuning.

### `show_detections()`

Outline what your detector believes on the operator's copy of the video:

```python
box = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
drone.show_detections([(box, "gate")])  # outline + caption
drone.show_detections([])  # nothing seen: clear it
```

It is display only. It cannot move the drone and cannot feed back into
`frame()`. Pixel coordinates refer to the frame you analysed; pass
`size=(height, width)` if you resized it first. Outlines you stop updating fade
after about a second. Use it freely while developing: seeing your detector's
output beside the live video is the fastest way to find out why a gate was
missed.

### A complete closed-loop example

This steers towards the centre of the orange top and bottom arcs of a gate. It
is deliberately simple. It does not know about gate order, distance or
direction, so treat it as a skeleton to build your own detector on. It runs
unchanged in the simulator on Circuit 1:

```python
import cv2
import numpy as np

from squadrone import Drone

# The gate's orange renders red-orange, so hue wraps around 0 in OpenCV's 0-179.
ORANGE = [
    (np.array([0, 120, 120]), np.array([15, 255, 255])),
    (np.array([170, 120, 120]), np.array([179, 255, 255])),
]


def find_gate(frame):
    """Bounding box (x, y, w, h) around the orange top and bottom arcs, or None."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, *ORANGE[0]) | cv2.inRange(hsv, *ORANGE[1])
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    arcs = [c for c in contours if cv2.contourArea(c) > 300]  # ignore specks
    if not arcs:
        return None
    return cv2.boundingRect(np.vstack(arcs))


with Drone() as drone:
    drone.takeoff()
    drone.up(100)  # Gate 1 of Circuit 1 is 2 m up
    for _ in range(30):
        frame = drone.frame(fresh=True, timeout=1.0)
        if frame is None:  # stream stalled: do not act on stale pixels
            drone.hover()
            continue
        found = find_gate(frame)
        if found is None:
            drone.show_detections([])  # clear the last outline
            drone.fly(fb=15, seconds=0.1)  # creep forward until a gate shows up
            continue
        x, y, w, h = found
        box = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        drone.show_detections([(box, f"gate {w}px")])
        offset = (x + w / 2) / frame.shape[1] - 0.5  # -0.5 left .. +0.5 right
        drone.fly(fb=30, yaw=120 * offset, seconds=0.1)
    drone.hover()
    drone.land()
```

The pattern to keep: **fresh frame → decide → one short, bounded `fly()`**,
with an explicit fallback for `None`.

## 8. Errors and the emergency stop

`squadrone` exports three exceptions. The
[operations reference](drone-operations.md#errors-stopping-and-cleanup) lists
exactly when each is raised; in practice:

- **`DroneError`**: a command failed. Catch it if you can recover.
- **`ConnectionError`** (a `DroneError`): Squadrone is unreachable, refused you,
  shut down, or timed out. Catch it if you can recover.
- **`EmergencyStop`**: the operator pressed Stop or Emergency Stop. **Do not
  catch it.**

A bad call that never leaves your machine, such as `fly()` without `seconds`,
raises a plain `ValueError`: that is a bug to fix, not a condition to handle.

Three things catch people out:

- `squadrone.ConnectionError` is **not** Python's built-in `ConnectionError`.
  If you write `from squadrone import ConnectionError`, you shadow the built-in
  one for that file. Import it under another name if you also need the
  built-in:

  ```python
  from squadrone import ConnectionError as SquadroneConnectionError
  ```

- `EmergencyStop` inherits from `BaseException`, not `Exception`, exactly so
  that a broad `except Exception:` in your control loop cannot swallow a safety
  stop and carry on flying. Do not use a bare `except:` around flight code.

- After a stop, every further call raises `EmergencyStop` again, so a loop that
  catches `DroneError` cannot keep issuing commands. Fix the cause, then start a
  new `Drone()`. Losing the connection is different: a command in flight when
  Squadrone shuts down or the link drops raises `ConnectionError`, never
  `EmergencyStop`.

```python
from squadrone import Drone, DroneError

try:
    with Drone() as drone:
        drone.takeoff()
        drone.forward(150)
        drone.land()
except DroneError as exc:
    print(f"Flight command failed: {exc}")
# EmergencyStop is not a DroneError, so it ends the program here
```

The browser's Stop and Emergency Stop buttons always take priority over your
code. Closing the connection, or losing it, centres the sticks before landing.
A single damaged video frame is discarded: `frame()` simply returns the next
good one.

## 9. Simulator and real Tello

The program is identical. The differences are in what the world gives back:

| | Simulator | Real Tello |
| --- | --- | --- |
| Selected by | operator, in the browser header | operator, after joining the `TELLO-…` Wi-Fi |
| Arena, trail, race panel, score | shown | not available (no absolute position) |
| `height` | modelled height above the floor | the SDK's reported height; stale or missing telemetry raises an error instead of returning 0 |
| Mission pads | pad 1 at the takeoff spot, limited visibility | supported hardware only; otherwise raises `DroneError` |
| `flip()` | visual only | refused when the battery is low (around 50%) or there is no clearance |
| Repeatability | repeatable unless `--noise` is set | varies with battery, air and floor |

Rehearse on the simulator, then re-check everything that depends on distance,
speed and height on the real aircraft with generous clearance. The steps to
switch to hardware are in the README's "Flying the real Tello" section; the
per-operation behaviour on hardware is in the
[Drone operations reference](drone-operations.md).

## 10. Debugging and workflow

- **Run it like any Python program.** Set breakpoints, step, print, and read
  tracebacks in your IDE. Nothing needs to run inside the browser.
- **Breakpoints are safe between commands.** The drone hovers while you are
  stopped, and `fly(..., seconds=…)` re-centres its own sticks. Do not leave a
  real drone hovering on a breakpoint for long: the battery drains and the
  aircraft's own safety behaviour may land it.
- **Print what you commanded.** A line such as
  `print(f"yaw={yaw:.0f} offset={offset:+.2f}")` inside your loop is the cheapest
  way to see why a controller oscillates.
- **Overlay your detector.** Use `show_detections()` so you can watch the
  cockpit video and your code's opinion at the same time.
- **Watch for `!` warnings.** They mean a value was clamped.
- **Re-run freely.** Each new `Drone()` resets the practice attempt. To compare
  runs, note the score card after each lap.
- **Try other tracks.** Start with `python -m comp2 --track circuit2_figure_eight`
  (or `circuit3_double_figure_eight`), or pick one in the browser while no
  program is connected. Add `--noise 0.05` to the application to make movement
  drift like real hardware; a program that survives noise is more likely to
  survive the cage.
- **Simulate without the cockpit.** For tests that must not need the browser,
  `from comp2.api import Drone` with `Drone(sim=True)` runs a standalone
  simulator in your process (no video window, no emergency stop). This is for
  testing only; the IDE client is the way to fly with the cockpit. For an
  organiser-run script, `python -m comp2 --script my_lap.py` runs it alongside
  the server using the bundled interpreter; that script must import `Drone`
  from `comp2.api`, not from `squadrone`, and the programs in `examples/` are
  written for the IDE client, so run them from your IDE instead.

## 11. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `ConnectionError: Could not connect to Squadrone at …` | The application is not running, or is on another port/host. Start it, or pass the right URL / set `SQUADRONE_URL`. |
| `ConnectionError: another program is already connected or running` | Another script (or an earlier one that has not finished closing) holds the controller. Stop it. Only one program may fly at a time. |
| `ConnectionError: wait for the drone connection` | The operator is switching between simulator and Tello. Retry in a few seconds. |
| `ConnectionError: Squadrone timed out while running …` | The command took longer than `timeout`. Raise `Drone(timeout=…)` for long `wait()` or slow `go()`/`curve()`, or check the application is still responsive. |
| `ModuleNotFoundError: No module named 'squadrone'` | The client is not installed in the interpreter your IDE runs. Install `client/` into that interpreter (section 2). |
| `ModuleNotFoundError: No module named 'cv2'` | OpenCV is not part of the client. `python -m pip install opencv-python`. |
| `ValueError: IDE-controlled fly() requires seconds …` | Pass `seconds=` (0–10) to every `fly()` call. |
| `DroneError: external fly() requires seconds between 0 and 10` | `seconds` was outside 0–10. Use a shorter step in a loop. |
| `frame()` returns `None` | No image yet, a stalled stream, or a timeout. Give it a larger `timeout` at start-up, and handle `None` in loops. |
| `! forward(5) is outside 20-500 cm; using 20` | Clamping warning. Fix the calculation that produced the value. |
| `DroneError: … Enable mission-pad detection before reading it` | Call `enable_mission_pads()` before `mission_pad()`. |
| Program stops with `EmergencyStop` | The operator pressed Stop. This is by design; restart with a new `Drone()`. |
| Drone landed on its own when the script ended | By design: closing the connection centres the sticks and lands. |

## 12. Cheat sheet

```python
from squadrone import Drone, DroneError, EmergencyStop
from squadrone import ConnectionError as SquadroneConnectionError  # not the built-in

drone = Drone(url=None, timeout=30.0)  # or: with Drone() as drone:
drone.close()

# flight
drone.takeoff()
drone.land()
drone.hover()
drone.forward(cm)
drone.back(cm)
drone.left(cm)
drone.right(cm)  # 20-500
drone.up(cm)
drone.down(cm)  # 20-500
drone.turn_right(deg)
drone.turn_left(deg)  # 1-360
drone.flip("back")  # "forward" | "back" | "left" | "right"
drone.wait(seconds)

# racing
drone.fly(lr=0, fb=0, ud=0, yaw=0, seconds=0.2)  # +-100, seconds 0-10
drone.go(x_cm=0, y_cm=0, z_cm=0, speed=50)  # x fwd, y LEFT, z up
drone.curve(x1, y1, z1, x2, y2, z2, speed=50)

# senses
drone.frame(timeout=None, fresh=False)  # BGR ndarray (480, 640, 3) or None
drone.show_detections(detections, size=None)
drone.battery  # property, int 0-100
drone.height  # property, float cm

# mission pads (optional)
drone.enable_mission_pads()
drone.mission_pad()
drone.disable_mission_pads()
drone.center_on_mission_pad(pad_id, height_cm=80, speed=20)
```
