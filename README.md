# COMP2 Drone Racer — ICONIP 2026 Autonomous Drone Racing

[![CI](https://github.com/Squadrone-Australia/Drone-Competition-2/actions/workflows/ci.yml/badge.svg)](https://github.com/Squadrone-Australia/Drone-Competition-2/actions/workflows/ci.yml)

[![Download the latest gate dataset](https://img.shields.io/github/v/release/Squadrone-Australia/Drone-Competition-2?filter=dataset-*&style=for-the-badge&label=Download%20gate%20dataset&color=e8590c)](https://github.com/Squadrone-Australia/Drone-Competition-2/releases/latest)

> **Always use the latest gate dataset.** The button above shows the newest
> version and opens its release page. Older versions stay online, but are
> missing data.

The practice and development platform for the ICONIP 2026 autonomous drone
racing competition: a Ryze Tello must fly itself through a series of
ring gates — Squadrone fabric rings, orange top and bottom and
black-and-white checkered left and right, each standing on the floor on a PVC
stand of its own — in a 6 m × 4 m × 3 m cage. The competition uses two gate
types:

- **Gate A**: 0.70 m across the outside around a 0.50 m opening.
- **Gate B**: about 0.77 m across the outside (including a 2 cm black binding)
  around an opening of about 0.58 m, with a lighter, more yellow orange and a
  printed ArUco marker hanging under it.

The simulator draws gate A by default; a track can set each gate's size (see
[Tracks](#tracks)).

This app gives every team the same flight environment:

- a **simulator** — the published tracks in 3D, a camera feed rendered through
  the real Tello's calibrated camera, and live practice scoring;
- an **external Python API** for code run directly from each team's IDE,
  selected interpreter, virtual environment and project directory;
- a fixed browser cockpit that keeps the camera, arena, track, scoring and
  safety controls visible while the team's program runs;
- a **one-click switch to the real Tello** once your lap works in the sim.

The platform ships **no gate detector** — vision-based gate perception is your
team's core task. `drone.frame()` hands you the raw BGR image; what you do
with it is the competition. To train or test a detector, start from the
[gate dataset](#gate-dataset): pictures of both gate types from a real Tello.

## Quick start

### One-step setup

Open a terminal in the folder where you want the app and run one line. It
downloads the code into `Drone-Competition-2`, gets Python 3.13/3.14 if you
don't have it (via [uv](https://docs.astral.sh/uv/), no admin rights needed),
installs the app and the `squadrone` client into `Drone-Competition-2/venv`,
and starts the simulator in your browser. Git is used when installed, and is
not required.

```powershell
# Windows (PowerShell)
irm https://raw.githubusercontent.com/Squadrone-Australia/Drone-Competition-2/main/scripts/install.ps1 | iex
```

```bash
# macOS / Linux
curl -LsSf https://raw.githubusercontent.com/Squadrone-Australia/Drone-Competition-2/main/scripts/install.sh | sh
```

When it finishes it prints the interpreter to select in your IDE
(`...\venv\Scripts\python.exe` or `.../venv/bin/python`); that interpreter
already has the client installed. Set `COMP2_DIR` to install somewhere else.

**Starting it again.** The setup also creates a launcher:

| System  | Launcher                                                                       |
| ------- | ------------------------------------------------------------------------------ |
| Windows | **Start COMP2** on your desktop, or `Start COMP2.cmd` in `Drone-Competition-2` |
| macOS   | `Start COMP2.command` in `Drone-Competition-2` (double-click it in Finder)     |
| Linux   | `Drone-Competition-2/start-comp2.sh`                                           |

Every start updates COMP2 first: it downloads the latest code, reinstalls
only when something changed, and then opens the simulator. Offline, or if the
update fails, it starts the version you already have. If the simulator is
already running, the launcher just opens it in your browser.

While it runs, the simulator page tells you when a newer version is out. Stop
COMP2 and start it again from the launcher to get it. After an update, the
page lists what changed. `python -m comp2 --no-update-check` turns both off.

On a slow or restricted network, uv's
own `UV_DEFAULT_INDEX` (a PyPI mirror) and `UV_PYTHON_INSTALL_MIRROR` variables
are honoured. On Linux, OpenCV needs `libgl1` and `libglib2.0-0`; the script
tells you if they are missing.

### Manual setup

Install [Git](https://git-scm.com/downloads) and
[Python 3.13+](https://www.python.org/downloads/) (on Windows, tick _"Add
python.exe to PATH"_), then clone the repository, create a virtualenv, install
the app and run it. The app opens in your browser.

```powershell
# Windows
git clone https://github.com/Squadrone-Australia/Drone-Competition-2.git
cd Drone-Competition-2
python -m venv venv
venv\Scripts\python -m pip install -e .
venv\Scripts\python -m comp2
```

```bash
# macOS / Linux
git clone https://github.com/Squadrone-Australia/Drone-Competition-2.git
cd Drone-Competition-2
python3 -m venv venv
venv/bin/python -m pip install -e .
venv/bin/python -m comp2
```

Once installed, you only need to `cd` into the folder and run the last line to
start again. Options:

```powershell
python -m comp2                                   # simulator, Circuit 1 (oval)
python -m comp2 --track circuit2_figure_eight          # Level 2, the figure eight
python -m comp2 --track circuit3_double_figure_eight   # Level 3, the double eight
python -m comp2 --drone tello                     # start against real hardware
```

(Prefix with `venv\Scripts\` or `venv/bin/`, or activate the virtualenv first.)

Install the lightweight client into the interpreter selected in your IDE. The
app needs Python 3.13+, but the client works on Python 3.11+, so your IDE's
interpreter can be older than the app's:

```powershell
python -m pip install .\client
```

The client does not include OpenCV; add it with
`python -m pip install opencv-python` if your code (or `examples/03_pilot.py`)
uses `cv2`. The app's own `venv` already has it.

Competition distributions include this `client` directory. Organisers can
also build a wheel for teams to install before the event:

```powershell
python -m pip wheel .\client --wheel-dir dist\client
```

## Using the app

The app does not run your code. It runs the simulator (or talks to the real
Tello) and **listens for commands** from your program. So there are always two
steps, in two places:

1. **Start the app** (`python -m comp2`) and leave the browser page open. It now
   waits on `ws://127.0.0.1:8765/ws`.
2. **Run your script from your IDE.** When it calls `Drone()`, it connects to the
   app, and each call (`takeoff()`, `fly()`, `go()`, `frame()`, …) is carried out
   by the simulator. You see the drone fly, the camera feed and the score in the
   browser; your `print` output and tracebacks stay in the IDE terminal.

Your script must use the **`squadrone` client library from this repository**
(`from squadrone import Drone`, installed with `python -m pip install .\client`
into your IDE's interpreter, see above). The app only understands that
library's protocol, so other drone libraries such as `djitellopy`, or raw Tello
UDP commands, will not control the simulator.

If the app is not running when your script starts, `Drone()` fails with a
connection error. Start the app first, then run the script. The same script
flies the simulator or the real Tello, depending on which one you select in the
browser header; the script does not change.

## Updates and feedback

This app may be updated in the future (fixes, new tracks, new client features).
How you get an update depends on how you installed it:

- **One-step setup:** start COMP2 from its launcher. Every start updates the
  app and the client in `Drone-Competition-2/venv` (see
  [One-step setup](#one-step-setup)). If your IDE runs your code with **another
  interpreter**, also reinstall the client there:
  `python -m pip install .\client` from the `Drone-Competition-2` folder.
- **Manual setup:** pull the code and reinstall both yourself:

  ```powershell
  git pull
  python -m pip install -e .           # in the app's venv, picks up new dependencies
  python -m pip install .\client       # in your IDE's interpreter, updates the client
  ```

If you run into any issues, you are welcome to
[submit an issue on GitHub](https://github.com/Squadrone-Australia/Drone-Competition-2/issues).
Please include what you did, what you expected, what happened instead, and any
error text from the browser's System log or your IDE terminal.

## The app

- **IDE connection status** in the header shows whether an external Python
  process owns control. Target switching is locked while it is connected.
- **3D arena** stays visible beside the drone camera: third-person, orbit and
  top-down cameras, with the
  flown racing line drawn as a trail. The next gate is green, passed gates
  grey.
- **Drone camera** is a permanent panel, not a floating window. It shows the
  raw sensor stream your OpenCV code receives.
- **Track panel**: the published circuit from above. Tracks are JSON files in
  `comp2/tracks/`; the picker switches between them.
- **Race panel**: one ring per gate. A ring fills orange once flown and prints
  how many centimetres off the gate's centre you crossed. The next gate is
  green. A practice score card appears when the lap ends (see
  [Practice scoring](#practice-scoring-in-the-simulator): it is not the competition score).
- **System log** shows connection, race and safety messages.

There is no browser coding tab or browser Run button. Students edit, run and
debug their Python in their own IDE while this page remains the live simulator
and competition display.

## Practice scoring in the simulator

**The simulator's score card is not the competition score.** It measures time
and gates passed, and adds two practice measures:

- **Smoothness.** A rough guide to how round your flight is.
- **Centring.** How close to the middle of each gate you crossed. The closer to
  the middle, the further you were from clipping the ring and crashing.

Use the card to tell whether a change to your code made a lap better or worse.
Do not tune your program to its exact numbers.

| Metric     | How the simulator measures it                                                                                        |
| ---------- | -------------------------------------------------------------------------------------------------------------------- |
| Smoothness | RMS jerk over the whole flown path (sampled at 30 Hz)                                                                |
| Accuracy   | offset from each gate's centre at the moment of crossing: 1 dead centre, 0 at the rim of the gate's circular opening |
| Time       | first takeoff to final landing on the finish pad                                                                     |

`total = difficulty × 100 × (0.4·smoothness + 0.3·accuracy + 0.3·time)` —
weights and reference times live in each track's JSON.

A gate only counts if crossed **in order** and **in the marked direction**;
landing anywhere but the finish pad (or before the last gate) is a DNF.

## Python pathway from your IDE

```python
from squadrone import Drone

with Drone() as drone:  # connects to the running Squadrone application
    drone.takeoff()
    drone.fly(fb=70, seconds=3.0)  # stick input: forward at 70% for 3 s
    drone.go(100, 0, 50, speed=60)  # glide 1 m forward and 0.5 m up
    frame = drone.frame()  # BGR image for your gate detector
    drone.land()
```

Run this file normally from the IDE. Squadrone keeps the video, scoring and
Emergency Stop active while the team's own interpreter executes the code:

```powershell
C:\team\project\.venv\Scripts\python.exe C:\team\project\main.py
```

The browser operator selects Simulator or Real Tello. The student program does
not change. Output and tracebacks remain in the IDE terminal. External
`drone.fly()` calls require `seconds` so pausing in a debugger cannot leave a
stick command applied indefinitely.

For an organiser-run script using the bundled interpreter,
`python -m comp2 --script my_lap.py` remains available as a command-line
fallback. Such a script must `from comp2.api import Drone`; the programs in
`examples/` use the IDE client and are meant to be run from your IDE, not with
`--script`.

### Documentation

| Read this                                              | When                                                                                                                                           |
| ------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| [Squadrone client guide](docs/squadrone-guide.md)      | Start here: install, connect, control styles, camera and vision loop, errors and the emergency stop, simulator vs. real Tello, troubleshooting |
| [Drone operations reference](docs/drone-operations.md) | Look up an operation: every argument, range, coordinate convention, return value and safety behaviour                                          |
| [`examples/`](examples/)                               | `01_first_lap.py`, `02_smooth_lap.py`: Circuit 1 by discrete commands, by velocity; `03_pilot.py`: fly by keyboard and record video            |

Things worth knowing before you start: all calls block until finished;
distances are centimetres, angles degrees, and every direction is relative to
the drone's nose; out-of-range values are clamped with a `!` warning in your IDE
terminal instead of ending the flight; and `with Drone() as drone:` makes a
crash, Ctrl+C or the program ending centre the sticks and land the drone. The
client connects to `ws://127.0.0.1:8765/ws` unless you pass a URL or set
`SQUADRONE_URL`.

## Gate dataset

Real Tello camera frames of the competition's ring gates, for gate detection
and pose estimation. **Download the
[latest dataset release](https://github.com/Squadrone-Australia/Drone-Competition-2/releases/latest)**
(currently v1.2, `iconip2026-gates-v1.2.zip`, about 440 MB). The release page
lists every version's changes and describes the layout and the label format
in full; the `README.md` inside the zip is the full datasheet.

The competition uses two gate types, and the dataset has both:

- **Gate A**, labelled: 2,347 frames at 960×720: 1,794 for training, 353 for
  validation and 200 for testing. 339 of them have no gate in view. Filmed in
  an indoor room, in good and dim light, and in the netted drone cage.
- **Gate B**, unlabelled: 1,262 frames from five settings, indoors and
  outdoors. Gate B is slightly larger than gate A and has a printed ArUco
  marker hanging under it; `gate_spec.json` gives its measurements. Labelling
  these frames, or using them some other way, is part of the task.
- Each labelled gate has a bounding box and nine keypoints, in YOLO pose and
  COCO Keypoints formats. Fit the keypoints to `gate_spec.json` (the gate's 3D
  points) with PnP to get range, sideways offset and yaw.
- `camera/tello_camera.json` is the Tello's measured camera calibration. Use it
  for range: the nominal field of view gives ranges about 30% short. See
  [Camera calibration](#camera-calibration) for the values.
- Left and right are as the camera sees the ring. If you flip images to
  augment, reorder the keypoints with the `flip_idx` in `data.yaml`, or every
  label is silently wrong.
- Seven gate A clips hover at tape-measured distances from 0.5 to 3.0 m, so
  you can check your range estimate.

To collect pictures of your own, `examples/03_pilot.py` records the raw camera
while you fly.

### Camera calibration

The Tello's camera was calibrated with a ChArUco board at 960×720: 48 views,
0.28 px RMS reprojection error. The result ships with this app as
[`comp2/vision/tello_camera.json`](comp2/vision/tello_camera.json), with the
full report beside it. The simulator renders through the same focal length and
principal point, so geometry worked out in the simulator carries over to the
real camera.

| Value           | In the file                  | At 960×720                   |
| --------------- | ---------------------------- | ---------------------------- |
| Focal length    | `focal_per_width` 0.9795     | fx ≈ fy ≈ 940.4 px           |
| Principal point | `cx_fraction`, `cy_fraction` | (457.6, 331.1) px            |
| Field of view   |                              | 54.1° × 41.9°                |
| Distortion      | `distortion`                 | k1 k2 p1 p2 k3, OpenCV order |

All values are fractions of the frame width (cx, focal) or height (cy), so they
hold for the simulator's 640×480 frames too. Multiply by your frame's size to
get an OpenCV camera matrix. The distortion is small, at most about 12 px in
the corners of a 960×720 frame. The simulator leaves it out, so if you undistort
real frames, do not undistort the simulator's.

DJI's published "82.6°" field of view is the diagonal, and it is far wider than
the real camera's 65°. Code that assumes it underestimates ranges by about 30%.

## Tracks

A track file is the whole circuit: arena size, takeoff/landing pads, ordered
gates (x, y, z, yaw, diameters), difficulty and scoring constants. The official
layouts are published before the competition; to rehearse a new one, drop a
JSON file in `comp2/tracks/` and restart:

```json
{
	"name": "My track",
	"difficulty": 1.2,
	"arena": { "width_m": 6.0, "depth_m": 4.0, "height_m": 3.0 },
	"takeoff": [1.0, 2.0],
	"takeoff_heading_deg": 90,
	"landing": [5.0, 2.0],
	"t_ref_s": 30.0,
	"gates": [
		{
			"x": 3.0,
			"y": 2.0,
			"z": 1.5,
			"yaw_deg": 90,
			"diameter_m": 0.7,
			"opening_diameter_m": 0.5
		}
	]
}
```

This one takes off at the west end facing east, flies through a single gate in
the middle of the cage and lands at the east end. The coordinates are the same
as the official tracks':

- **x** runs along the cage's 6 m length (`width_m`), from west (0) to east;
  **y** across its 4 m (`depth_m`), from south (0) to north; **z** is height
  above the floor. All in metres.
- **Headings** are compass degrees, clockwise seen from above: 0 = north (+y),
  90 = east (+x), 180 = south, 270 = west (−x). `takeoff_heading_deg` is the
  way the drone faces on the pad (default 0), and a gate's `yaw_deg` is the
  direction you must fly through it.

`diameter_m` is the ring's outer size and `opening_diameter_m` its clear
opening (defaults 0.70 and 0.50); a pass is measured against the opening. A `weights`
object (`smoothness`, `accuracy`, `time`) overrides the default 0.4/0.3/0.3.

## Flying the real Tello

1. Power the Tello, join its `TELLO-…` Wi-Fi.
2. Click **Use real Tello** in the header.
3. Run the same Python program. The Emergency Stop button always takes priority.

Note the real drone has no arena-absolute position, so the 3D view, plan and
scoring panels are simulator-only; on hardware you fly by camera and by the
published track measurements.

While your computer is joined to the Tello's Wi-Fi, that is usually its only
network, and the Tello gives no internet access. If your program calls an
online service, such as a hosted LLM, give the computer a second connection
(Ethernet, a USB Wi-Fi adapter or USB tethering to a phone) and test it before
the event.

## Development

Contributors need the test, lint and packaging tools on top of the app. Teams
do not:

```bash
venv/bin/python -m pip install -e ".[dev]"
```

```bash
venv/bin/python -m pytest            # Python tests (hardware-free)
node --test tests/js                 # frontend serializer tests
ruff format --check .                # formatting gate (ruff format . to apply)
ruff check .                         # lint gate
pyinstaller comp2.spec --noconfirm   # standalone dist/comp2/comp2.exe
```

CI runs the lint and test steps on every push and pull request
(`.github/workflows/ci.yml`), across Linux and Windows on Python 3.13 and 3.14.

User-facing documentation lives in `docs/squadrone-guide.md` (how to use it) and
`docs/drone-operations.md` (per-operation reference); update them alongside any
change to the `squadrone` client or the `Drone` API, and keep each fact in one
place. `docs/architecture/platform-options.md` is a historical decision record
from an earlier, block-based version of the project.

## License

MIT, see [LICENSE](LICENSE). The vendored three.js (MIT, `comp2/frontend/vendor/three.LICENSE`)
keeps its own license.
