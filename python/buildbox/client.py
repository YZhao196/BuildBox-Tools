"""The client: report readings up, answer commands back down.

The shape of a device built on this:

    from buildbox import Client

    bb = Client("http://127.0.0.1:8787", token=TOKEN)

    @bb.on_command
    def handle(command):
        if command.matches("kill_switch"):
            return 0            # reported back as the exit code
        return None

    while True:
        bb.send_data("temperature", read_sensor(), unit="C")
        bb.check()              # answer anything waiting, then carry on

or, if the device has nothing to do but be commanded:

    bb.run_forever()

Two properties this module is built around, because the server is:

* **A write is never invented.** If a command cannot be performed the device
  answers ``ok=False`` with a reason. It never answers success for work it did
  not finish, because a kill switch that reports having stopped something is the
  most dangerous lie this system could tell.
* **Readings are labelled.** Everything sent here is marked as coming from a
  device, and the server refuses anything that is not — so a value this program
  reports can never be mistaken for one the model made up.
"""

from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from . import protocol
from .protocol import READ_ACTIONS

DEFAULT_URL = os.environ.get("BUILDBOX_URL", "http://127.0.0.1:8787")
DEFAULT_TOKEN = os.environ.get("BUILDBOX_TOKEN")

#: A word in a command: letters, digits and underscores, so ``kill_switch`` is
#: one word and ``stop-robot`` is two.
_WORD = re.compile(r"[a-z0-9_]+")


def words(text: Optional[str]) -> tuple:
    """The lower-cased words of ``text``, which is what matching compares."""
    return tuple(_WORD.findall((text or "").lower()))


#: A long poll returns within its wait, so the socket needs to outlast it.
POLL_SLACK_SECONDS = 10.0


class BuildBoxError(Exception):
    """Anything the server refused, with its own explanation where it gave one."""


class AuthError(BuildBoxError):
    """The token is missing, unknown or revoked."""


class ScopeError(BuildBoxError):
    """The device is not scoped to that module."""


class ShapeError(BuildBoxError):
    """The reading is not one that module's preset could have produced."""


@dataclass
class Command:
    """Something the server has asked this device to do."""

    cmd_id: str
    module_id: str
    action: str
    cmd: str
    target: Optional[str] = None
    timeout_ms: int = 30_000

    @property
    def reads(self) -> bool:
        """Whether this only observes. The server has already confirmed writes."""
        return self.action in READ_ACTIONS

    @property
    def writes(self) -> bool:
        return not self.reads

    def matches(self, text: str) -> bool:
        """Whether the command text contains the words of ``text``, in order.

        Whole words, case-insensitively: ``sudo systemctl stop robot`` matches
        ``stop`` and ``stop robot``, but ``stop homebridge`` does not match
        ``home`` — a substring would hand a stop to a homing handler.
        """
        needle = words(text)
        haystack = words(self.cmd)
        if not needle:
            return False
        return any(
            haystack[i : i + len(needle)] == needle
            for i in range(len(haystack) - len(needle) + 1)
        )

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"<command {self.action} on {self.module_id}: {self.cmd!r}>"


@dataclass
class Result:
    """What a handler decided, in the form the server takes."""

    ok: bool = True
    output: List[str] = field(default_factory=list)
    code: Optional[int] = None
    reason: Optional[str] = None


#: What a handler may return: nothing, an exit code, some text, or a Result.
Outcome = Union[None, bool, int, str, Sequence[str], Result]

#: What a sensor may return: one number, several readings at once, or a
#: structural reading already built by `buildbox.data`.
Reading = Union[float, int, Mapping[str, float], Sequence[Dict[str, Any]], None]


@dataclass
class Sensor:
    """Something a device reads, how often, and what it is called.

    `read` is your function. It is called on its own interval, so it should
    return promptly — it runs on the sampler, and a reader that blocks for a
    second holds up nothing else but does decide how fast that one series can
    go.

    It may return:

    * a number — reported under `key`;
    * a mapping of name to number — reported as several series at once, which is
      what a device reading temperature *and* humidity wants;
    * a list of events built by `buildbox.data` — a structural reading, because a
      laser sweep or a joint pose is not a series of numbers and must not be sent
      as one;
    * `None` — nothing to report yet, which is not a failure. A sensor with no
      fix and a sensor that is broken are different things and must not be
      reported the same way.
    """

    key: str
    read: Callable[[], Reading]
    unit: Optional[str] = None
    every: float = 1.0
    module: Optional[str] = None
    #: Why the last read failed, or None. Set by the sampler, so a device with
    #: its own loop can see that a reader has been failing without having to
    #: watch the server's log.
    last_error: Optional[str] = None


def interpret(value: Outcome) -> Result:
    """Turn a handler's return value into a Result.

    Deliberately narrow, and the narrowness is the point:
    ``0`` means it worked and anything else means it did not, so a handler that
    returns an exit code needs no ceremony; ``None`` means it worked and has
    nothing to say. Anything a handler cannot express this way it can express by
    returning a ``Result`` itself.
    """
    if value is None:
        return Result(ok=True)
    if isinstance(value, Result):
        return value
    # Before int: bool is a subclass of it, and True should read as success
    # rather than as exit code 1.
    if isinstance(value, bool):
        return Result(ok=value, code=0 if value else 1)
    if isinstance(value, int):
        return Result(ok=value == 0, code=value)
    if isinstance(value, str):
        return Result(ok=True, output=[value])
    if isinstance(value, (list, tuple)):
        return Result(ok=True, output=[str(line) for line in value])
    raise TypeError(
        "A command handler must return None, a bool, an exit code, a string, a "
        f"list of strings, or a Result — not {type(value).__name__}."
    )


class Client:
    """A device's connection to a server.

    The token is what identifies the device; the server reads the device's scope
    from its own database on every request, so nothing this class sends can widen
    what the device is allowed to touch.
    """

    def __init__(
        self,
        url: Optional[str] = None,
        token: Optional[str] = None,
        *,
        module: Optional[str] = None,
        timeout: float = 30.0,
        poll_wait: float = 20.0,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        self.url = (url or DEFAULT_URL or "").rstrip("/")
        self.token = token or DEFAULT_TOKEN
        if not self.url:
            raise ValueError("No server URL. Pass url= or set BUILDBOX_URL.")
        if not self.token:
            raise ValueError("No device token. Pass token= or set BUILDBOX_TOKEN.")

        self.timeout = timeout
        self.poll_wait = poll_wait
        self.on_error = on_error

        self._module = module
        #: (rank, key, predicate, handler). Rank is (constraints named, words in
        #: the match): the highest-ranked match wins, so a catch-all registered
        #: for logging does not swallow the specific handlers after it, and an
        #: equal tie is refused rather than settled by registration order. Key
        #: is (action, match words), which must be unique.
        self._handlers: List[
            tuple[tuple[int, int], tuple, Callable[[Command], bool], Callable[[Command], Outcome]]
        ] = []
        #: What this device reads, and how often.
        self._sensors: List[Sensor] = []
        #: What each applied command id came to, so a redelivered command is not
        #: applied twice and is answered with its original result. The transport
        #: gives at-least-once and nothing better is honestly achievable, so
        #: doing the work once is this end's job.
        self._applied: "OrderedDict[str, Result]" = OrderedDict()
        self._applied_limit = 512

    # ---------------------------------------------------------------- #
    # HTTP                                                             #
    # ---------------------------------------------------------------- #

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[Any] = None,
        *,
        timeout: Optional[float] = None,
    ) -> tuple[int, Optional[Any]]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.url + path, data=body, method=method)
        request.add_header("authorization", f"Bearer {self.token}")
        request.add_header("accept", "application/json")
        if body is not None:
            request.add_header("content-type", "application/json")

        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as error:
            raw = error.read()
            parsed: Optional[Any] = None
            if raw:
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = None
            message = (parsed or {}).get("message") if isinstance(parsed, dict) else None
            message = message or f"The server answered {error.code}."
            if error.code == 401:
                raise AuthError(message) from None
            if error.code == 403:
                raise ScopeError(message) from None
            if error.code == 422:
                raise ShapeError(message) from None
            raise BuildBoxError(message) from None

    # ---------------------------------------------------------------- #
    # Identity                                                         #
    # ---------------------------------------------------------------- #

    def whoami(self) -> Dict[str, Any]:
        """The device's label and the modules it may report on."""
        _, body = self._request("GET", "/api/device/whoami")
        return body or {}

    def module(self) -> str:
        """The module to report on, when the caller did not name one.

        A device scoped to exactly one module is the common case and naming it on
        every call is noise, so it is resolved once and remembered. A device
        scoped to several is asked to be explicit rather than guessed at, because
        reporting a temperature against the wrong module is worse than an error.
        """
        if self._module:
            return self._module
        modules = self.whoami().get("modules") or []
        if len(modules) == 1:
            self._module = modules[0]["id"]
            return self._module
        if not modules:
            raise ScopeError("This device is not scoped to any module.")
        raise ScopeError(
            "This device is scoped to "
            + ", ".join(f"{m['name']} ({m['id']})" for m in modules)
            + " — pass module= to say which one."
        )

    # ---------------------------------------------------------------- #
    # Reporting                                                        #
    # ---------------------------------------------------------------- #

    def send(
        self,
        events: Union[Dict[str, Any], Sequence[Dict[str, Any]]],
        module: Optional[str] = None,
    ) -> int:
        """Send one event, or a batch. Returns how many the server accepted.

        Both are accepted because `buildbox.data` returns lists, and a caller
        holding a single hand-built event should not have to wrap it.
        """
        # A dict is not a Sequence, and `list(dict)` would send its keys.
        events = [events] if isinstance(events, dict) else list(events)
        if not events:
            return 0
        _, body = self._request(
            "POST", "/api/device/ingest", protocol.envelope(module or self.module(), events)
        )
        return int((body or {}).get("accepted", 0))

    def send_data(
        self,
        key: str,
        value: float,
        unit: Optional[str] = None,
        *,
        module: Optional[str] = None,
    ) -> int:
        """Report one numeric reading.

        ``bb.send_data("temperature", 21.5, unit="C")``
        """
        return self.send([protocol.sample(key, value, unit)], module=module)

    def send_many(self, readings: Dict[str, float], *, module: Optional[str] = None) -> int:
        """Report several readings at once, as one request.

        ``bb.send_many({"temperature": 21.5, "humidity": 44})``
        """
        return self.send(
            [protocol.sample(key, value) for key, value in readings.items()], module=module
        )

    def send_log(self, message: str, severity: str = "info", **kwargs: Any) -> int:
        return self.send([protocol.log(message, severity)], **kwargs)

    def send_status(self, value: str, **kwargs: Any) -> int:
        return self.send([protocol.status(value)], **kwargs)

    def send_shape(self, kind: str, points: Optional[List[float]] = None, **kwargs: Any) -> int:
        module = kwargs.pop("module", None)
        return self.send([protocol.shape(kind, points or [], **kwargs)], module=module)

    # ---------------------------------------------------------------- #
    # Commands                                                         #
    # ---------------------------------------------------------------- #

    def on_command(
        self,
        fn: Optional[Callable[[Command], Outcome]] = None,
        *,
        action: Optional[str] = None,
        match: Optional[str] = None,
    ) -> Any:
        """Register a handler.

            @bb.on_command                       # every command
            @bb.on_command(action="run")         # only writes
            @bb.on_command(match="kill_switch")  # only commands naming it

        Handlers are tried most-specific-first: more constraints win, then the
        longer ``match`` in words. ``match`` is by whole word, so
        ``match="home"`` does not catch ``stop homebridge``. A command left
        matching two equally specific handlers is ambiguous and is refused,
        not given to whichever was declared first. Registering the same
        ``action`` and ``match`` twice is a ValueError.
        """
        if match is not None and not words(match):
            raise ValueError(f"match={match!r} has no words to match on.")
        key = (action, words(match) if match is not None else None)
        if any(entry[1] == key for entry in self._handlers):
            raise ValueError(
                f"A handler for action={action!r}, match={match!r} is already registered."
            )

        def register(handler: Callable[[Command], Outcome]) -> Callable[[Command], Outcome]:
            def predicate(command: Command) -> bool:
                if action is not None and command.action != action:
                    return False
                if match is not None and not command.matches(match):
                    return False
                return True

            rank = (
                (1 if action is not None else 0) + (1 if match is not None else 0),
                len(key[1] or ()),
            )
            self._handlers.append((rank, key, predicate, handler))
            return handler

        return register(fn) if fn is not None else register

    def dispatch(self, command: Command) -> Result:
        """Find the handler for a command and run it, or say there is none."""
        candidates = [entry for entry in self._handlers if entry[2](command)]
        if not candidates:
            # No handler is a refusal, not a success. A command that silently
            # reported ok would tell the interface something happened that did not.
            return Result(
                ok=False,
                reason=f"This device has no handler for “{command.action}”. Nothing was done.",
            )
        best = max(entry[0] for entry in candidates)
        winners = [entry for entry in candidates if entry[0] == best]
        if len(winners) > 1:
            # Guessing between, say, a stop and a homing handler is worse than
            # doing nothing and saying why.
            named = ", ".join(" ".join(entry[1][1] or ()) or "*" for entry in winners)
            return Result(
                ok=False,
                reason=f"The command is ambiguous: it matches {named} equally. Nothing was done.",
            )
        try:
            result = interpret(winners[0][3](command))
            if not result.ok and not result.reason:
                # The protocol requires a reason with every failure; a bare exit
                # code is still worth naming rather than leaving the operator blank.
                code = f" with exit code {result.code}" if result.code is not None else ""
                result.reason = f"The handler reported failure{code} and gave no reason."
            return result
        except Exception as error:  # noqa: BLE001 - reported, not swallowed
            # A handler that raised did not do the thing. Saying so is the
            # whole point; the traceback is the operator's, in their log.
            return Result(ok=False, reason=f"{type(error).__name__}: {error}")

    def check(self, wait: float = 0.0) -> bool:
        """Answer one pending command if there is one. True if one was handled.

        With the default wait of zero this returns immediately, which is what a
        device with its own loop wants: do a reading, answer whatever is waiting,
        go round again.
        """
        command = self.poll(wait=wait)
        if command is None:
            return False
        self.apply(command)
        return True

    def apply(self, command: Command) -> Result:
        """Run a command and report what happened, exactly once."""
        result = self._applied.get(command.cmd_id)
        if result is None:
            result = self.dispatch(command)
            self._remember(command.cmd_id, result)
        # Otherwise it already ran and the server is asking again because it did
        # not hear the answer. Re-running would fire a relay twice, and answering
        # success would hide a failure, so the original result is replayed.

        self._request(
            "POST",
            "/api/device/results",
            {
                "v": protocol.VERSION,
                "type": "result",
                "cmdId": command.cmd_id,
                "moduleId": command.module_id,
                "ok": result.ok,
                "output": list(result.output),
                **({"code": result.code} if result.code is not None else {}),
                **({"reason": result.reason} if result.reason else {}),
            },
        )
        return result

    def _remember(self, cmd_id: str, result: Result) -> None:
        self._applied[cmd_id] = result
        while len(self._applied) > self._applied_limit:
            self._applied.popitem(last=False)

    def poll(self, wait: Optional[float] = None) -> Optional[Command]:
        """Wait for the next command, or return None when the wait elapses."""
        seconds = self.poll_wait if wait is None else wait
        status, body = self._request(
            "GET",
            f"/api/device/commands?wait={int(max(seconds, 0) * 1000)}",
            timeout=seconds + POLL_SLACK_SECONDS,
        )
        if status == 204 or not body:
            return None
        return Command(
            cmd_id=body["cmdId"],
            module_id=body["moduleId"],
            action=body["action"],
            cmd=body.get("cmd", ""),
            target=body.get("target"),
            timeout_ms=int(body.get("timeoutMs", 30_000)),
        )

    # ---------------------------------------------------------------- #
    # Reading                                                          #
    # ---------------------------------------------------------------- #

    def sensor(
        self,
        key: str,
        fn: Optional[Callable[[], Reading]] = None,
        *,
        unit: Optional[str] = None,
        every: float = 1.0,
        module: Optional[str] = None,
    ) -> Any:
        """Declare something this device reads.

            @bb.sensor("temperature", unit="C", every=2.0)
            def temperature():
                return sensor.read()

        Usable with or without arguments:

            bb.sensor("cpu", read_cpu, unit="%", every=5.0)

        The function is called on its interval by `run`, and may return a
        number, a mapping of readings, or None to report nothing this time.
        """

        def register(reader: Callable[[], Reading]) -> Callable[[], Reading]:
            self._sensors.append(
                Sensor(key=key, read=reader, unit=unit, every=max(every, 0.01), module=module)
            )
            return reader

        return register(fn) if fn is not None else register

    def sensors(self) -> List[Sensor]:
        """Everything registered, so a caller can inspect or drive them itself."""
        return list(self._sensors)

    def sample_once(self) -> int:
        """Take one reading from every sensor. Returns how many events were sent.

        For a device that owns its loop rather than handing it to `run`: read,
        then `check()`, then decide when to go round again.
        """
        return sum(self._sample(sensor) for sensor in self._sensors)

    def _sample(self, sensor: Sensor) -> int:
        """Read one sensor and report it. Returns events sent."""
        try:
            value = sensor.read()
        except Exception as error:  # noqa: BLE001 - reported, not swallowed
            # A reader that raised has not produced a reading. Sending a
            # plausible number instead would be inventing a measurement, so the
            # failure goes out as a log line and nothing is plotted.
            sensor.last_error = f"{type(error).__name__}: {error}"
            self.send(
                [
                    protocol.log(
                        f"{sensor.key} could not be read: {sensor.last_error}", "error"
                    )
                ],
                module=sensor.module,
            )
            return 0

        sensor.last_error = None

        if value is None:
            # Nothing to report yet. A sensor that has no fix is not a sensor
            # that is broken, and the two must not look alike.
            return 0

        if isinstance(value, Mapping):
            events = [
                protocol.sample(str(name), float(reading))
                for name, reading in value.items()
                if reading is not None
            ]
            return self.send(events, module=sensor.module) if events else 0

        if isinstance(value, (list, tuple)):
            # A structural reading, already built — a laser sweep is not a series
            # of samples, so a reader hands over the events `buildbox.data` makes
            # rather than a number this could wrap. A list of anything else is a
            # mistake, and sending it would put nonsense on the wire.
            events = list(value)
            if not all(isinstance(event, dict) for event in events):
                raise TypeError(
                    "A reader returned a list that is not events. Return a number, "
                    "a mapping of numbers, None, or a list built by buildbox.data."
                )
            return self.send(events, module=sensor.module) if events else 0

        return self.send(
            [protocol.sample(sensor.key, float(value), sensor.unit)], module=sensor.module
        )

    # ---------------------------------------------------------------- #
    # Running                                                          #
    # ---------------------------------------------------------------- #

    def run(self, poll_wait: Optional[float] = None) -> None:
        """Read the sensors and answer commands, until interrupted.

        Sampling runs on its own thread so a long poll cannot hold up a reading,
        and so a slow reader cannot hold up the command channel. With no sensors
        registered this is exactly the command loop it has always been.
        """
        if not self._sensors:
            self.run_forever(poll_wait)
            return

        stop = threading.Event()
        sampler = threading.Thread(target=self._sample_loop, args=(stop,), daemon=True)
        sampler.start()
        try:
            self.run_forever(poll_wait)
        finally:
            stop.set()
            sampler.join(timeout=5.0)

    def _sample_loop(self, stop: threading.Event) -> None:
        """Call each sensor when it is due, independently of the others."""
        due = {id(sensor): time.monotonic() for sensor in self._sensors}

        while not stop.is_set():
            now = time.monotonic()
            # Bounded at a second so a stop is noticed promptly even when every
            # sensor is on a long interval.
            wait = 1.0
            for sensor in self._sensors:
                marker = id(sensor)
                remaining = due[marker] - now
                if remaining <= 0:
                    try:
                        self._sample(sensor)
                    except Exception as error:  # noqa: BLE001 - the loop outlives it
                        # A send that failed is not a reason to stop reading: the
                        # network comes back, and the next tick will report again.
                        sensor.last_error = f"{type(error).__name__}: {error}"
                        if self.on_error:
                            self.on_error(error)
                    due[marker] = time.monotonic() + sensor.every
                    remaining = due[marker] - time.monotonic()
                wait = min(wait, max(remaining, 0.0))
            stop.wait(max(wait, 0.01))

    def run_forever(self, poll_wait: Optional[float] = None) -> None:
        """Answer commands until interrupted.

        Commands only. If the device has sensors registered, call `run` instead:
        it reads them while doing this, and degrades to exactly this when there
        are none.

        Connection failures are retried with a widening backoff rather than
        raised, because a device on a robot is expected to outlive the network.
        Nothing is retried that could double-apply an action — that is `apply`'s
        job, and it holds the list of what has already run.
        """
        delay = 1.0
        while True:
            try:
                command = self.poll(wait=poll_wait)
                delay = 1.0
                if command is not None:
                    self.apply(command)
            except KeyboardInterrupt:
                raise
            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as error:
                if self.on_error:
                    self.on_error(error)
                time.sleep(delay)
                delay = min(delay * 2, 30.0)

    def close(self) -> None:  # pragma: no cover - symmetry with context managers
        """Nothing to release; the transport is request-per-call."""

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
