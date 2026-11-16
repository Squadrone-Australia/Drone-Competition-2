import argparse
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from .server import create_app
from .sim.flight_error import PROFILES


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
        "--no-update-check",
        action="store_true",
        help="don't ask GitHub whether a newer COMP2 is out, and don't list "
        "what the last update changed",
    )
    ap.add_argument(
        "--script",
        type=Path,
        default=None,
        help="run a Python lap alongside the server "
        "(video and EMERGENCY STOP stay live)",
    )
    ap.add_argument(
        "--flight",
        choices=list(PROFILES),
        default="typical",
        help="sim only: how accurately the drone flies — 'typical' is a real "
        "Tello indoors, 'good' a well-lit textured floor, 'poor' a dim or "
        "featureless one, 'ideal' exactly as commanded (default: typical)",
    )
    ap.add_argument(
        "--noise",
        type=float,
        default=None,
        help="sim only: scale the typical errors instead of naming a --flight "
        "profile (0.05 is typical, 0 is ideal)",
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
    from .sim.drone import SimDrone

    def new_simulator():
        # Also what the cockpit's switch back from a Tello builds, so that
        # switch does not quietly lose these options.
        return SimDrone(
            noise=args.noise or 0.0,
            flight="ideal" if args.noise is not None else args.flight,
            track_name=args.track,
            arena_name=args.arena,
        )

    if args.drone == "tello":
        from .drone.tello import TelloDrone

        drone = TelloDrone()
    elif args.drone == "sim":
        drone = new_simulator()
    else:
        from .drone.mock import MockDrone

        drone = MockDrone()
    if not args.no_browser:
        # A unique query makes an already-open competition tab load the current
        # frontend instead of merely coming to the foreground with stale HTML.
        launch_url = f"http://localhost:{args.port}/?launch={time.time_ns()}"
        threading.Timer(1.0, lambda: webbrowser.open(launch_url)).start()
    uvicorn.run(
        create_app(
            drone,
            script=args.script,
            simulator_factory=new_simulator,
            check_updates=not args.no_update_check,
        ),
        host="127.0.0.1",
        port=args.port,
    )


if __name__ == "__main__":
    main()
