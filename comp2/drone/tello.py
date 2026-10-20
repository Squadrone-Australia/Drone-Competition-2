import math
import re
import socket
import threading
import time

import av
import numpy as np

from .base import DroneAdapter
from .tello_link import (
    TELLO_ADDRESS,
    TelloError,
    TelloInterrupted,
    TelloLink,
    TelloState,
)

_FLIP_CODE = {"forward": "f", "back": "b", "left": "l", "right": "r"}
#: Where the aircraft sends its H.264 stream once told ``streamon``. Fixed by
#: the SDK on a standard Tello — only the EDU can be asked to use another port.
VIDEO_PORT = 11111
#: The ports the aircraft sends that stream *from*, as measured on a Tello EDU
#: (field tests, 2026-10-12): 62512 every time but once, when it was 62513.
#: Punching to a port it is not using costs one ignored datagram.
VIDEO_SOURCE_PORTS = (62512, 62513)
#: How long to wait after a failed video-stream open before trying again. The
#: video loop asks for a frame ten times a second, so without this a port that
#: cannot be bound would be retried ten times a second.
_REOPEN_INTERVAL_S = 2.0
#: How long the worker blocks in one receive before it looks at its stop flag
#: again — and so the longest a close() normally waits.
_RECV_TIMEOUT_S = 0.5
#: While no video is arriving, how often to punch again. A punch is only needed
#: until the stream flows (the stream itself keeps the firewall's entry alive),
#: but an aircraft that reboots, or a stream that stalls long enough for the
#: entry to expire, needs a fresh one.
_PUNCH_INTERVAL_S = 1.0
#: How long a teardown waits for the worker to notice. Longer than one
#: receive timeout, so the normal path always joins.
_VIDEO_JOIN_TIMEOUT_S = 2.0
#: How many packets in a row may fail to decode before the decoder is replaced.
#: About two seconds of video at the Tello's rate: one bad packet is normal on
#: Wi-Fi, and only a decoder stuck in a bad state needs starting over.
_MAX_DECODE_FAILURES = 60
#: How old the last decoded frame may be before :attr:`FrameReader.frame`
#: stops handing it out. A team's detector must never mistake a frozen picture
#: for the gate still being where it was.
_FRAME_MAX_AGE_S = 0.75
#: How long the video may go without a new decoded frame before the adapter
#: calls the link dead. A rebooted or out-of-range Tello stops sending, so
#: silence — not an error — is what a lost aircraft looks like. Long enough to
#: cover stream start-up; the server's watchdog reconnects once this expires.
LINK_TIMEOUT_S = 6.0
#: How long height and mission-pad readings wait for a telemetry packet that
#: arrived *after* they were asked for. The Tello sends about ten a second.
_STATE_WAIT_TIMEOUT_S = 0.75
_TOF_REPLY = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*mm\s*$", re.IGNORECASE)


def _h264_decoder():
    return av.CodecContext.create("h264", "r")


class FrameReader:
    """Receives the Tello's video on a socket it owns, and decodes it.

    **Owning the socket is what gets video through a firewall.** The stream is
    pushed to :11111 unasked, so a laptop firewall that drops unsolicited
    inbound UDP (a university-managed laptop on the Tello's *Public* Wi-Fi)
    drops all of it. But a firewall lets in *replies*: traffic from an address
    and port this socket has itself sent to. So while no video is arriving, the
    worker sends a one-byte datagram from :11111 to each port the aircraft
    streams from — a UDP hole-punch — and the stream that follows is filed as
    the reply. Field test (2026-10-12, Linux, ufw on): no punch, nothing
    arrived; punched, the full stream did. That only works if the punch leaves
    from the very socket the video arrives on, which is why this reads the
    datagrams itself instead of handing ``udp://@:11111`` to FFmpeg.

    **Only the worker thread touches the socket or the decoder.** djitellopy's
    reader let FFmpeg own the socket inside a container, and freeing an
    ``AVFormatContext`` from one thread while another sat in
    ``av_read_frame()`` was a use-after-free that killed the process with no
    traceback. Here a receive always returns within ``_RECV_TIMEOUT_S``, and a
    teardown just sets an event and joins.

    The Tello sends raw H.264 (Annex B) cut into ~1460-byte datagrams with no
    framing of its own, so datagrams go straight into the decoder's parser,
    which finds the frame boundaries.
    """

    def __init__(
        self,
        port: int = VIDEO_PORT,
        host: str = TELLO_ADDRESS[0],
        source_ports=VIDEO_SOURCE_PORTS,
        decoder_factory=None,
    ):
        self._host = host
        self._punch_to = [(host, p) for p in source_ports]
        # resolved here rather than as a default argument so that tests can
        # substitute the av layer wholesale
        self._new_decoder = decoder_factory or _h264_decoder
        self._lock = threading.Lock()
        self._frame = None
        self._frame_at = float("-inf")
        self._stop = threading.Event()
        # Bound here rather than on the thread so that a port someone else
        # holds is reported to the caller instead of dying in the worker.
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._sock.bind(("", port))
        except OSError:
            self._sock.close()
            raise
        self._sock.settimeout(_RECV_TIMEOUT_S)
        self.port = self._sock.getsockname()[1]
        self._worker = threading.Thread(
            target=self._run, name="tello-video", daemon=True
        )
        self._started = False

    @classmethod
    def open(cls, **kwargs) -> "FrameReader":
        """Start a reader on the aircraft's video port."""
        reader = cls(**kwargs)
        reader.start()
        return reader

    def start(self) -> None:
        self._started = True
        self._worker.start()

    @property
    def frame(self):
        """Recent decoded RGB frame, or None before startup/after a stream stall."""
        with self._lock:
            return (
                self._frame
                if time.monotonic() - self._frame_at <= _FRAME_MAX_AGE_S
                else None
            )

    @property
    def alive(self) -> bool:
        """False once the worker has stopped — the reader needs reopening."""
        return self._started and self._worker.is_alive()

    def close(self, timeout: float = _VIDEO_JOIN_TIMEOUT_S) -> bool:
        """Stop decoding and give the UDP port back. Idempotent.

        Returns whether the worker actually finished. It normally does; if it
        somehow did not, its socket is left for it to close on its way out.
        """
        self._stop.set()
        if not self._started:
            self._sock.close()
            return True
        if threading.current_thread() is not self._worker:
            self._worker.join(timeout)
        return not self._worker.is_alive()

    # --- worker thread only ------------------------------------------------

    def _punch(self) -> None:
        for target in self._punch_to:
            try:
                self._sock.sendto(b"\x00", target)
            except OSError:
                pass  # no route yet (Wi-Fi still joining): the next punch will do

    def _run(self) -> None:
        decoder = self._new_decoder()
        failures = 0
        last_data = last_punch = float("-inf")
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if (
                    now - last_data >= _PUNCH_INTERVAL_S
                    and now - last_punch >= _PUNCH_INTERVAL_S
                ):
                    self._punch()
                    last_punch = now
                try:
                    data, source = self._sock.recvfrom(65536)
                except TimeoutError:
                    continue
                except ConnectionResetError:
                    # Windows reports an ICMP "port unreachable" from an earlier
                    # punch as a failed receive. The socket is fine.
                    continue
                except OSError:
                    return  # closed under us
                if source[0] != self._host:
                    continue
                last_data = time.monotonic()
                try:
                    packets = decoder.parse(data)
                except Exception:
                    decoder, failures = self._new_decoder(), 0
                    continue
                for packet in packets:
                    try:
                        frames = decoder.decode(packet)
                    except Exception:
                        # A lost datagram leaves a frame the decoder cannot
                        # use. Skip it: the decoder resynchronises by itself at
                        # the next keyframe. Throwing it away instead would also
                        # throw away the good stream its parser has buffered.
                        # Only a decoder that keeps failing is replaced.
                        failures += 1
                        if failures >= _MAX_DECODE_FAILURES:
                            decoder, failures = self._new_decoder(), 0
                            break
                        continue
                    failures = 0
                    for frame in frames:
                        image = frame.to_ndarray(format="rgb24")
                        with self._lock:
                            self._frame = image
                            self._frame_at = time.monotonic()
        finally:
            try:
                self._sock.close()
            except OSError:
                pass


class TelloDrone(DroneAdapter):
    """Adapter for a real DJI Tello, built to survive the aircraft going away
    and a laptop firewall that will not let it talk back.

    Commands go over :class:`~comp2.drone.tello_link.TelloLink`, which needs no
    firewall rule because it only ever reads replies to what it sent — see that
    module for why djitellopy was replaced. Every connection is a *fresh* link
    and every disconnection is explicit: a rebooted Tello is a new SDK session.

    Video is pushed to :11111 unasked, which a laptop firewall drops;
    :class:`FrameReader` punches a hole for it from its own socket. A stream
    that still never delivers a single frame is reported as ``video_blocked``
    rather than as a lost drone, because the aircraft is still answering and
    still flies.

    Telemetry (:8890) is collected when it gets through, never waited for.
    Height prefers it and falls back to asking ``tof?``; mission pads cannot be
    asked for, so they say plainly when telemetry is blocked.
    """

    mode = "tello"

    def __init__(
        self,
        link_timeout_s: float = LINK_TIMEOUT_S,
        command_timeout_s: float = DroneAdapter.command_timeout_s,
    ):
        self._link: TelloLink | None = None
        self._state: TelloState | None = None
        self._reader = None
        self._mission_pads_enabled = False
        self.link_timeout_s = link_timeout_s
        self.command_timeout_s = command_timeout_s
        self.link_ok = False
        #: True when the aircraft answers commands but no video frame has ever
        #: arrived this session: a firewall the punch did not get through, or
        #: a drone some other program told ``streamoff`` — or, at once, a video
        #: port some other program holds (``video_port_busy``).
        self.video_blocked = False
        #: True while :11111 is held by some other program on this laptop.
        self.video_port_busy = False
        self._seen_frame = False
        self._last_frame = None  # identity of the last *decoded* frame
        self._frame_at = 0.0
        self._reopen_at = 0.0

    # --- connection lifecycle --------------------------------------------

    def connect(self):
        """Enter SDK mode on a fresh link, then start the video stream.

        Tears down whatever came before, so this doubles as the reconnect path
        (see :meth:`DroneAdapter.reconnect`). Waits for no telemetry: the
        handshake's own reply is the proof that the aircraft is there, and
        telemetry is exactly what a firewall drops.
        """
        self.close()
        link = TelloLink()
        try:
            link.handshake()
            link.send("streamon")
        except Exception:
            link.close()
            raise
        self._link = link
        self.link_ok = True
        self.video_blocked = False
        self.video_port_busy = False
        self._mission_pads_enabled = False
        self._seen_frame = False
        self._last_frame = None
        self._frame_at = time.monotonic()
        self._reopen_at = 0.0
        try:
            self._state = TelloState()
        except OSError:
            self._state = None  # :8890 is held elsewhere; height asks instead
        # Best effort: the control link is what "connected" means, and a stream
        # that needs another second is not a failed connection. get_frame()
        # retries, and reports video_blocked if it never comes up at all.
        self._open_reader()

    def close(self):
        """Stop video and telemetry and drop the command link. Never flies.

        Deliberately sends no ``streamoff``. Once the aircraft has been told
        ``streamoff``, a later ``streamon`` answers ``ok`` but no video comes
        until the drone is rebooted (field test, 2026-10-12, Tello EDU). With
        nobody reading :11111 the datagrams are simply dropped.
        """
        self._release_reader()
        state, self._state = self._state, None
        if state is not None:
            state.close()
        link, self._link = self._link, None
        self.link_ok = False
        self._mission_pads_enabled = False
        if link is not None:
            link.close()

    def _open_reader(self):
        """Start the background decoder, or arm a retry. Never raises."""
        if self._link is None:
            return None
        try:
            self._reader = FrameReader.open()
        except OSError:
            # the bind failed: another program holds the video port
            self._reader = None
            self._reopen_at = time.monotonic() + _REOPEN_INTERVAL_S
            self.video_port_busy = True
            self.video_blocked = True
            return None
        except Exception:
            self._reader = None
            self._reopen_at = time.monotonic() + _REOPEN_INTERVAL_S
            return None
        if self.video_port_busy:
            # The port came free. Give the stream a full link_timeout_s to
            # start before its silence is read as anything.
            self.video_port_busy = False
            self.video_blocked = False
            self._frame_at = time.monotonic()
        return self._reader

    def _release_reader(self):
        reader, self._reader = self._reader, None
        if reader is None:
            return
        try:
            reader.close()
        except Exception:
            pass

    def close_video(self) -> None:
        """Stop the decoder and give its UDP port back. Never raises."""
        self._release_reader()

    def _send(self, command: str) -> str:
        """Run one aircraft command, noting a link loss before re-raising.

        A *refusal* is not a link loss: the aircraft answered, so the link is
        demonstrably up. Silence and transport errors are; the cleared flag is
        what lets the server's watchdog reconnect once the run stops.
        """
        link = self._link
        if link is None:
            raise RuntimeError("the Tello is not connected")
        try:
            return link.send(command)
        except (TelloError, TelloInterrupted):
            raise
        except Exception:
            self.link_ok = False
            raise

    # --- flight ----------------------------------------------------------

    def takeoff(self):
        self._send("takeoff")

    def land(self):
        self.send_rc(0, 0, 0, 0)
        self._send("land")

    def emergency(self):
        """Cut the motors *now*, even while a long move is still waiting.

        Ordinary commands take turns on the link; this one cannot queue behind
        a 30-second move, so it goes through :meth:`TelloLink.interrupt`.
        """
        self.send_rc(0, 0, 0, 0)
        link = self._link
        if link is None:
            raise RuntimeError("the Tello is not connected")
        try:
            link.interrupt("emergency")
        except TelloError:
            raise
        except Exception:
            self.link_ok = False
            raise

    def move(self, direction, cm):
        self._send(f"{direction} {int(cm)}")

    def rotate(self, direction, deg):
        self._send(f"{'cw' if direction == 'cw' else 'ccw'} {int(deg)}")

    def flip(self, direction):
        self._send(f"flip {_FLIP_CODE[direction]}")

    # --- continuous control ------------------------------------------------

    def send_rc(self, lr, fb, ud, yaw):
        """Fire and forget: the SDK sends no reply to ``rc``, and a setpoint
        must never queue behind a manoeuvre that is still waiting for one."""
        link = self._link
        if link is None:
            return  # nothing is flying on this adapter, so nothing to centre
        link.send_no_reply(f"rc {int(lr)} {int(fb)} {int(ud)} {int(yaw)}")

    def go_xyz(self, x_cm, y_cm, z_cm, speed_cms):
        self._send(f"go {int(x_cm)} {int(y_cm)} {int(z_cm)} {int(speed_cms)}")

    def curve_xyz(self, x1, y1, z1, x2, y2, z2, speed_cms):
        self._send(
            f"curve {int(x1)} {int(y1)} {int(z1)} "
            f"{int(x2)} {int(y2)} {int(z2)} {int(speed_cms)}"
        )

    # --- sensing ---------------------------------------------------------

    def get_frame(self) -> np.ndarray | None:
        """Latest camera frame, or None while the stream is not delivering.

        Silence is what a lost aircraft looks like, so a stream that stays
        quiet past ``link_timeout_s`` is reported through :meth:`_note_silence`.
        """
        now = time.monotonic()
        reader = self._reader
        if reader is not None and not reader.alive:
            # the decoder thread stopped: let go of it and retry
            self._release_reader()
            reader = None
            self._reopen_at = now + _REOPEN_INTERVAL_S
        if reader is None:
            if now < self._reopen_at:
                return None
            reader = self._open_reader()
            if reader is None:
                self._note_silence(now)
                return None
        try:
            frame = reader.frame
        except Exception:
            self._release_reader()
            self._reopen_at = now + _REOPEN_INTERVAL_S
            self._note_silence(now)
            return None
        if frame is None:
            self._note_silence(now)
            return None
        if frame is not self._last_frame:
            self._last_frame = frame
            self._frame_at = now
            self._seen_frame = True
            self.video_blocked = False
        elif self._note_silence(now):
            return None
        return frame[:, :, ::-1].copy()  # RGB→BGR

    def _note_silence(self, now: float) -> bool:
        """Decide what a quiet stream means, once it has been quiet too long.

        A stream that delivered frames and then stopped is a lost aircraft:
        clear ``link_ok`` and let the watchdog reconnect. A stream that has
        *never* delivered a frame this session, from an aircraft that answered
        the handshake, is a firewall or a drone whose video was switched off
        elsewhere — reconnecting cannot fix either, so it is reported as
        ``video_blocked`` and the link stays up.
        """
        if now - self._frame_at < self.link_timeout_s:
            return False
        if self._seen_frame:
            self.link_ok = False
        else:
            self.video_blocked = True
        return True

    def battery(self) -> int:
        # Asked for, not read from telemetry: telemetry is pushed to :8890
        # unasked, and that is exactly what a managed laptop's firewall drops.
        reply = self._send("battery?")
        try:
            return int(reply)
        except ValueError:
            raise TelloError(
                f"the drone sent an unreadable battery level: {reply!r}"
            ) from None

    def _fresh_state(self) -> dict | None:
        """One telemetry packet received after this call, or None on silence.

        Never trusts a cached packet: a value that stopped updating is exactly
        what a stalled or firewalled stream looks like.
        """
        state = self._state
        if state is None:
            return None
        return state.wait_fresh(state.seq, _STATE_WAIT_TIMEOUT_S)

    def height_cm(self) -> float:
        """Fresh height in cm: telemetry ``h`` if it arrives, else ``tof?``.

        ``h`` is the SDK's height above the takeoff point. The fallback asks
        the downward time-of-flight sensor over the command link, which a
        firewall cannot drop; over the cage's flat floor the two agree.
        """
        state = self._fresh_state()
        if state is not None and "h" in state:
            return _valid_height(state["h"])
        reply = self._send("tof?")
        match = _TOF_REPLY.match(reply)
        if match is None:
            raise TelloError(f"the drone sent an unreadable height: {reply!r}")
        return _valid_height(float(match.group(1)) / 10)

    def enable_mission_pads(self) -> None:
        self._mission_pads_enabled = False
        try:
            self._send("mon")
            self._send("mdirection 0")
        except Exception as exc:
            try:
                self._send("moff")
            except Exception:
                pass
            raise RuntimeError(
                "Could not enable downward mission-pad detection; "
                "check SDK support and connection before takeoff"
            ) from exc
        self._mission_pads_enabled = True

    def disable_mission_pads(self) -> None:
        try:
            self._send("moff")
        finally:
            self._mission_pads_enabled = False

    def mission_pad(self) -> dict | None:
        if not self._mission_pads_enabled:
            raise RuntimeError("Enable mission-pad detection before reading it")
        state = self._fresh_state()
        if state is None:
            raise RuntimeError(
                "Tello telemetry is not arriving on UDP port 8890 — a laptop "
                "firewall is probably dropping it, and mission pads need it"
            )
        mid = state.get("mid")
        if type(mid) is not int or mid not in (-1, *range(1, 9)):
            raise RuntimeError("Invalid mission-pad ID telemetry")
        if mid == -1:
            return None
        coords = [state.get(axis) for axis in ("x", "y", "z")]
        if not all(type(v) in (int, float) and math.isfinite(v) for v in coords):
            raise RuntimeError("Invalid mission-pad coordinates")
        x, y, z = coords
        # Downward detection is documented only between 0.3 and 1.2 metres.
        if not (30 <= z <= 120 and abs(x) <= 100 and abs(y) <= 100):
            raise RuntimeError("Mission-pad coordinates outside detection range")
        return {"id": mid, "x_cm": float(x), "y_cm": float(y), "z_cm": float(z)}

    def center_on_mission_pad(self, pad_id: int, height_cm: int, speed: int) -> None:
        self.send_rc(0, 0, 0, 0)
        reading = self.mission_pad()
        if reading is None or reading["id"] != pad_id:
            raise RuntimeError(f"Mission pad {pad_id} is not currently detected")
        self._send(f"go 0 0 {int(height_cm)} {int(speed)} m{int(pad_id)}")


def _valid_height(height) -> float:
    if (
        isinstance(height, bool)
        or not isinstance(height, (int, float))
        or not math.isfinite(height)
        or not 0 <= height <= 3000
    ):
        raise RuntimeError("Tello height telemetry is invalid")
    return float(height)
