# Squadrone client

Install this package into the Python interpreter selected in your IDE:

```powershell
python -m pip install .\client
```

Start the Squadrone application, then run your program normally:

```python
from squadrone import Drone

drone = Drone()
drone.takeoff()
frame = drone.frame()
drone.fly(fb=40, seconds=0.2)
drone.land()
```

`Drone()` connects to `ws://127.0.0.1:8765/ws` by default. Set the
`SQUADRONE_URL` environment variable if the simulator is on another host.

Use `with Drone() as drone:` so the drone is centred and landed even if your
program crashes. Only one program can control the drone at a time.

Documentation:

- [Squadrone client guide](../docs/squadrone-guide.md): how to use the library,
  with patterns and troubleshooting.
- [Drone operations reference](../docs/drone-operations.md): every command
  argument, range and unit.
