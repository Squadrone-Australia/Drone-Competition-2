# Squadrone client

The `squadrone` library that a COMP2 team's program uses to fly the simulator
or the real Tello. It needs Python 3.11 or newer.

Install it into the Python interpreter selected in your IDE, from the root of
the Drone-Competition-2 repository:

```powershell
python -m pip install .\client
```

or from a wheel your organisers hand out
(`python -m pip install squadrone_comp2-<version>-py3-none-any.whl`). The
package is named `squadrone-comp2`; you import it as `squadrone`. The one-step
setup's `venv` already has it installed.

Start the Squadrone application, then run your program normally:

```python
from squadrone import Drone

with Drone() as drone:
    drone.takeoff()
    frame = drone.frame()
    drone.fly(fb=40, seconds=0.2)
    drone.land()
```

`Drone()` connects to `ws://127.0.0.1:8765/ws` by default. Set the
`SQUADRONE_URL` environment variable if the simulator is on another host.

`with Drone() as drone:` centres the sticks and lands the drone even if your
program crashes. Only one program can control the drone at a time. The client
does not include OpenCV: `python -m pip install opencv-python` if you need it.

Documentation:

- [Squadrone client guide](../docs/squadrone-guide.md): how to use the library,
  with patterns and troubleshooting.
- [Drone operations reference](../docs/drone-operations.md): every command
  argument, range and unit.
