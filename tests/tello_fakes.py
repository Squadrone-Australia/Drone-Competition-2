"""Stand-ins for the Tello link and telemetry, shared by the adapter tests.

The real :class:`~comp2.drone.tello_link.TelloLink` is exercised end to end
against a fake aircraft in ``test_tello_link.py``; these fakes only record what
the adapter asked of it.
"""

from comp2.drone.tello import TelloDrone
from comp2.drone.tello_link import TelloError


class FakeLink:
    def __init__(self, calls=None, replies=None, refuse=()):
        self.calls = [] if calls is None else calls
        self.replies = {"battery?": "87", **(replies or {})}
        self.refuse = set(refuse)
        self.closed = False

    def handshake(self):
        self.calls.append("command")

    def send(self, command, timeout=None):
        self.calls.append(command)
        if command in self.refuse:
            raise TelloError(f"the drone refused '{command}' (error)")
        return self.replies.get(command, "ok")

    def interrupt(self, command, timeout=None):
        self.calls.append(("interrupt", command))
        return "ok"

    def send_no_reply(self, command):
        self.calls.append(("no_reply", command))

    def close(self):
        self.closed = True


class FakeState:
    """Telemetry that arrives as ``packets`` lists it, then goes quiet."""

    def __init__(self, packets=()):
        self._packets = list(packets)
        self.seq = 0
        self.closed = False

    def wait_fresh(self, after_seq, timeout):
        if not self._packets:
            return None
        self.seq += 1
        return self._packets.pop(0)

    def close(self):
        self.closed = True


def connected_tello(link=None, state=None) -> TelloDrone:
    """A TelloDrone wired to fakes, with no sockets and no video."""
    adapter = TelloDrone()
    adapter._link = link or FakeLink()
    adapter._state = state
    adapter.link_ok = True
    return adapter
