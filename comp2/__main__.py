import argparse
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from .server import create_app


def main():
    ap = argparse.ArgumentParser("comp2")
    ap.add_argument(
        "--drone",
        choices=["mock", "tello", "sim"],
        default="sim",
        help="initial drone (default: simulator; the browser can switch between "
        "the simulator and Tello)",
    )
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument(
        "--script",
        type=Path,
        default=None,
        help="run a Python lap alongside the server "
        "(video and EMERGENCY STOP stay live)",
    )
    ap.add_argument(
        "--noise", type=float, default=0.0, help="sim only: movement drift, e.g. 0.05"
    )
    ap.add_argument(
        "--arena",
        default="cage",
        help="sim only: how the room around the gates looks — 'cage' is the "
        "competition venue (netting, hall, glossy floor, camera artefacts), "
        "'clean' the bare studio room (also switchable in the browser)",
    )
    ap.add_argument(
        "--track",
        default="circuit1_oval",
        help="sim only: which track to start on (also switchable in "
        "the browser; see comp2/tracks/*.json)",
    )
    args = ap.parse_args()
    if args.script and not args.script.is_file():
        sys.exit(f"comp2: no such script: {args.script}")
    if args.drone == "tello":
        from .drone.tello import TelloDrone

        drone = TelloDrone()
    elif args.drone == "sim":
        from .sim.drone import SimDrone

        drone = SimDrone(noise=args.noise, track_name=args.track, arena_name=args.arena)
    else:
        from .drone.mock import MockDrone

        drone = MockDrone()
    if not args.no_browser:
        # A unique query makes an already-open competition tab load the current
        # frontend instead of merely coming to the foreground with stale HTML.
        launch_url = f"http://localhost:{args.port}/?launch={time.time_ns()}"
        threading.Timer(1.0, lambda: webbrowser.open(launch_url)).start()
    uvicorn.run(create_app(drone, script=args.script), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
