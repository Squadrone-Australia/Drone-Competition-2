from abc import ABC, abstractmethod

import numpy as np


class DroneAdapter(ABC):
    #: Short identifier sent to the browser so it can make the active flight
    #: target unambiguous. Concrete adapters override this where appropriate.
    mode: str = "mock"

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def takeoff(self) -> None: ...

    @abstractmethod
    def land(self) -> None: ...

    @abstractmethod
    def emergency(self) -> None: ...

    @abstractmethod
    def move(self, direction: str, cm: int) -> None: ...

    @abstractmethod
    def rotate(self, direction: str, deg: int) -> None: ...

    @abstractmethod
    def flip(self, direction: str) -> None: ...

    @abstractmethod
    def get_frame(self) -> np.ndarray | None: ...

    @abstractmethod
    def battery(self) -> int: ...

    def enable_mission_pads(self) -> None:
        """Enable downward pad detection, or raise before flight if unsupported."""
        raise RuntimeError(f"{type(self).__name__} does not support mission pads")

    def disable_mission_pads(self) -> None:
        """Disable a previously enabled mission-pad detector."""
        raise RuntimeError(f"{type(self).__name__} does not support mission pads")

    def mission_pad(self) -> dict | None:
        """Fresh {id, x_cm, y_cm, z_cm} in the detected pad's local frame.

        None means no detection; stalled/invalid telemetry raises. No arena
        coordinates or heading are exposed. This structured sensor is Python
        only, like frame(), so its ID and coordinates stay in one observation.
        """
        raise RuntimeError(f"{type(self).__name__} does not support mission pads")

    def center_on_mission_pad(self, pad_id: int, height_cm: int, speed: int) -> None:
        """Move over the expected, currently detected pad, retaining heading."""
        raise RuntimeError(f"{type(self).__name__} does not support mission pads")

    def close(self) -> None:
        """Release whatever this adapter holds. Default: nothing to release.

        Called when the adapter stops being the active drone — a switch to
        another one, or shutdown. Adapters that own OS resources (the Tello
        owns its video port and a decoder thread) override this; it must never
        fly the aircraft and must never raise.
        """

    #: Whether the adapter currently believes it can still reach its aircraft.
    #: Only hardware can lose a link, so the simulated adapters leave this True
    #: forever; :class:`~comp2.drone.tello.TelloDrone` clears it when the
    #: aircraft stops answering, and the server's watchdog reconnects.
    link_ok: bool = True

    def reconnect(self) -> None:
        """Re-establish the link to the aircraft, from scratch.

        A rebooted Tello is a brand-new SDK session — the old one cannot be
        resumed, so the default is a full close-and-connect rather than a
        keepalive. Safe to call repeatedly: it is the watchdog's retry.
        """
        self.close()
        self.connect()

    # --- continuous control ------------------------------------------------
    # The racing primitives. All body-frame relative: the Tello has no
    # arena-absolute positioning, so neither does this surface — parity between
    # the simulator and hardware is the point.

    def send_rc(self, lr: int, fb: int, ud: int, yaw: int) -> None:
        """Velocity setpoint, each channel -100..100, held until the next call.

        ``send_rc(0, 0, 0, 0)`` means hover, and every stop path sends it — an
        rc setpoint must never outlive the program that set it.
        """
        raise NotImplementedError(f"{type(self).__name__} has no rc control")

    def go_xyz(self, x_cm: int, y_cm: int, z_cm: int, speed_cms: int) -> None:
        """Fly a straight line to a body-frame offset: x forward, y left, z up."""
        raise NotImplementedError(f"{type(self).__name__} has no go control")

    def curve_xyz(
        self, x1: int, y1: int, z1: int, x2: int, y2: int, z2: int, speed_cms: int
    ) -> None:
        """Fly a curve through body-frame point 1, ending at point 2."""
        raise NotImplementedError(f"{type(self).__name__} has no curve control")

    #: Whether :meth:`reset` actually moves the aircraft. False on hardware, and
    #: the UI says so rather than claiming a reposition that did not happen.
    can_reset: bool = False

    #: How long one command may block before the server gives up on it, in
    #: seconds, on top of however long the manoeuvre itself should take. A
    #: command the aircraft accepts but never answers otherwise parks its
    #: caller for good — and when that caller is a mission or a teardown,
    #: nothing can stop it. The Tello link already sizes each wait to the
    #: manoeuvre (``tello_link.timeout_for``); this is the backstop above it.
    command_timeout_s: float = 45.0

    def reset(self) -> None:
        """Put a simulated drone back on its takeoff pad.

        Called before every run so a program always starts from the same state.
        Real hardware cannot teleport, so this is a no-op there and
        ``can_reset`` stays False.
        """

    def annotate(self, frame: np.ndarray) -> np.ndarray:
        """Adapter-specific debug overlay, applied to the *display* copy only.

        The camera stream itself stays raw — teams run their own gate detection
        on it, and an overlay would be a false feature in their pipeline.
        """
        return frame

    # --- display feeds ----------------------------------------------------
    # Optional, and display/scoring-only: the browser's third-person view and
    # the race scorer need to know where the drone is. An adapter that cannot
    # know (a real Tello has no arena-absolute position) returns None; the
    # browser hides the 3D stage and the server scores nothing.
    #
    # These are deliberately NOT part of the sensing surface. Nothing in
    # comp2/api.py may call them, and the contestant API must not expose
    # them — the published track makes gate *coordinates* public, but live
    # absolute pose is something the real drone simply does not have.

    def pose(self) -> dict | None:
        """Arena-absolute pose for rendering: x, y, z, heading, roll, pitch, flying."""
        return None

    def scene(self) -> dict | None:
        """Static track description for rendering and scoring: extents, pads,
        gates, difficulty and scoring constants."""
        return None

    # --- track feed --------------------------------------------------------
    # The browser's track picker. Published layouts only — there is no
    # in-browser editing, because a track must match the physical cage.

    def track_catalog(self) -> list | None:
        """The tracks this adapter can fly, or None if it has no arena."""
        return None

    def load_track(self, name=None):
        """Switch to a published track layout. No-op without an arena."""
        return
