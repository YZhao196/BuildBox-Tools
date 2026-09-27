"""The live channel: asking a device to do something, and hearing what happened.

This is in memory and has no storage, which is a decision rather than an
omission. A command that outlives the connection it was sent on has no meaning,
because the caller that asked for it is gone too — so this is a channel, not a
record, and nothing here is durable.

Two properties the rest of the library leans on:

* **A command that was never sent is never sent.** A timeout resolves to a
  failure with a reason, never to silence and never to success, and the command
  is taken back out of the queue in the same breath — so a caller told "nothing
  was confirmed" cannot have the relay fire a moment later.
* **Delivery is at-least-once, and that is not hidden.** A command that had
  already reached the device when the wait ran out may still be applied; the
  device end keeps the ids it has applied and treats a repeat as a no-op, but
  exactly-once is not achievable over a network that can drop an answer, and
  this library does not claim it. `ok=False` means *unconfirmed*, which is what
  it says.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import protocol

#: How long a device holds an empty long poll open before answering "nothing".
DEFAULT_POLL_WAIT = 20.0

#: How long a caller waits for a device to report what a command did.
DEFAULT_COMMAND_TIMEOUT = 30.0


@dataclass
class Command:
    """Work for a device, in the form the wire takes."""

    cmd_id: str
    module_id: str
    action: str
    cmd: str = ""
    target: Optional[str] = None
    timeout_ms: int = int(DEFAULT_COMMAND_TIMEOUT * 1000)

    @property
    def reads(self) -> bool:
        """Whether this only observes."""
        return protocol.is_read(self.action)

    @property
    def writes(self) -> bool:
        return not self.reads

    def as_dict(self) -> Dict[str, Any]:
        message: Dict[str, Any] = {
            "v": protocol.VERSION,
            "type": "command",
            "cmdId": self.cmd_id,
            "moduleId": self.module_id,
            "action": self.action,
            "cmd": self.cmd,
            "timeoutMs": self.timeout_ms,
        }
        if self.target is not None:
            message["target"] = self.target
        return message


@dataclass
class Result:
    """What a device reported doing, or a timeout said it did not."""

    cmd_id: str
    module_id: str
    ok: bool
    output: List[str] = field(default_factory=list)
    code: Optional[int] = None
    reason: Optional[str] = None

    @classmethod
    def from_wire(cls, body: Dict[str, Any]) -> "Result":
        return cls(
            cmd_id=body.get("cmdId", ""),
            module_id=body.get("moduleId", ""),
            ok=bool(body.get("ok", False)),
            output=[str(line) for line in body.get("output") or []],
            code=body.get("code"),
            reason=body.get("reason"),
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "v": protocol.VERSION,
            "type": "result",
            "cmdId": self.cmd_id,
            "moduleId": self.module_id,
            "ok": self.ok,
            "output": list(self.output),
            **({"code": self.code} if self.code is not None else {}),
            **({"reason": self.reason} if self.reason else {}),
        }


class _Waiter:
    """A device holding a long poll open. At most one per device."""

    __slots__ = ("event", "command")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.command: Optional[Command] = None


class _InFlight:
    """A command given to a device that has not been answered yet."""

    __slots__ = ("event", "result", "device_id")

    def __init__(self, device_id: str) -> None:
        self.event = threading.Event()
        self.result: Optional[Result] = None
        #: Who it was sent to, so another device cannot answer it.
        self.device_id = device_id


class Channel:
    """Commands on their way down, and the devices waiting for them."""

    def __init__(
        self,
        *,
        poll_wait: float = DEFAULT_POLL_WAIT,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> None:
        self.poll_wait = float(poll_wait)
        self.command_timeout = float(command_timeout)
        self._queued: Dict[str, List[Command]] = {}
        self._waiting: Dict[str, _Waiter] = {}
        self._in_flight: Dict[str, _InFlight] = {}
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- #
    # Down                                                                #
    # ---------------------------------------------------------------- #

    def dispatch(
        self,
        device_id: str,
        command: Command,
        timeout: Optional[float] = None,
    ) -> Result:
        """Hand a command to a device and wait for what it did.

        Blocks until the device answers or the timeout elapses. A timeout is a
        failure, not a pause: `ok=False` with a reason saying nothing was
        confirmed.
        """
        seconds = self.command_timeout if timeout is None else float(timeout)
        entry = _InFlight(device_id)

        with self._lock:
            self._in_flight[command.cmd_id] = entry
            waiter = self._waiting.pop(device_id, None)
            if waiter is not None:
                waiter.command = command
                waiter.event.set()
            else:
                self._queued.setdefault(device_id, []).append(command)

        answered = entry.event.wait(seconds)

        with self._lock:
            self._in_flight.pop(command.cmd_id, None)
            if answered and entry.result is not None:
                return entry.result
            # Nothing answered, so the command must not be left where the
            # device can still collect it. A caller told "nothing was
            # confirmed" and then having the relay fire anyway is the exact
            # lie this library exists to prevent — and a retry would queue a
            # second command under a new id, which the device's own
            # de-duplication could not catch.
            self._unqueue(device_id, command.cmd_id)

        return Result(
            cmd_id=command.cmd_id,
            module_id=command.module_id,
            ok=False,
            reason=(
                f"The device did not answer within {round(seconds)} s. "
                "Nothing was confirmed."
            ),
        )

    def _unqueue(self, device_id: str, cmd_id: str) -> None:
        """Drop a command from a device's queue. Caller holds the lock."""
        queue = self._queued.get(device_id)
        if not queue:
            return
        remaining = [command for command in queue if command.cmd_id != cmd_id]
        if remaining:
            self._queued[device_id] = remaining
        else:
            del self._queued[device_id]

    def complete(self, result: Result, device_id: Optional[str] = None) -> bool:
        """A device reporting what a command did. False if nothing was waiting.

        A late answer to a command nobody is waiting for is not an error — the
        caller may already have given up — so this reports rather than raises.
        When `device_id` is given, an answer from any other device is refused:
        ids are unguessable, but a command belongs to the device it was sent to
        and nothing else should be able to claim it.
        """
        with self._lock:
            entry = self._in_flight.get(result.cmd_id)
            if entry is None:
                return False
            if device_id is not None and entry.device_id != device_id:
                return False
            del self._in_flight[result.cmd_id]
            entry.result = result
            entry.event.set()
            return True

    # ---------------------------------------------------------------- #
    # Up                                                                  #
    # ---------------------------------------------------------------- #

    def next_command(
        self, device_id: str, wait: Optional[float] = None
    ) -> Optional[Command]:
        """The next command for a device, or None when the wait elapses.

        This is the long poll. Holding the request open — rather than answering
        "nothing" immediately — is what keeps a command's latency down without a
        socket or a broker, and it doubles as the device's heartbeat.
        """
        seconds = self.poll_wait if wait is None else max(float(wait), 0.0)

        with self._lock:
            queue = self._queued.get(device_id)
            if queue:
                return queue.pop(0)

            # A device that polls again while its old poll is still open would
            # otherwise leave that one to time out on its own — harmless, but
            # it holds a thread for no reason.
            previous = self._waiting.get(device_id)
            if previous is not None:
                previous.event.set()

            waiter = _Waiter()
            self._waiting[device_id] = waiter

        if not waiter.event.wait(seconds):
            # The wait elapsed with nothing to do. `wait` returns False here and
            # True when it was set, but a device that was woken and then lost the
            # race simply reports nothing, which is the honest answer either way.
            pass

        with self._lock:
            if self._waiting.get(device_id) is waiter:
                self._waiting.pop(device_id, None)
            return waiter.command

    def forget(self, device_id: str) -> None:
        """Drop everything pending for a device. Used when it is revoked."""
        with self._lock:
            self._queued.pop(device_id, None)
            waiter = self._waiting.pop(device_id, None)
        if waiter is not None:
            waiter.event.set()

    def pending(self, device_id: str) -> int:
        """How many commands are queued for a device. Diagnostics, not policy."""
        with self._lock:
            return len(self._queued.get(device_id, ()))


def new_command(
    module_id: str,
    action: str,
    *,
    cmd: str = "",
    target: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Command:
    """Build a command with a fresh id.

    Ids are random rather than sequential so two callers cannot collide, and so
    a device cannot guess one to suppress.
    """
    seconds = DEFAULT_COMMAND_TIMEOUT if timeout is None else float(timeout)
    return Command(
        cmd_id=uuid.uuid4().hex,
        module_id=module_id,
        action=action,
        cmd=cmd,
        target=target,
        timeout_ms=int(seconds * 1000),
    )
