"""The receiving end: a program that devices report to and are commanded by.

    from buildbox_management import Management

    mgmt = Management()
    mgmt.add_module("proj-1", "mod-temp", name="Temperature")
    device, token = mgmt.mint("proj-1", "rover", ["mod-temp"])
    # hand `token` to the device, then

    @mgmt.on_events
    def received(reading):
        for event in reading.events:
            print(reading.module_id, event)

    mgmt.serve("127.0.0.1", 8787)

The HTTP surface is only one way to reach this. `Management.handle` is the whole
protocol as a function from a request to a response, so it can be mounted in
whatever server the embedding program already runs; `server.py` is a standard
library adapter for programs that do not have one.

What this deliberately is not: it is not BuildBox. It has no accounts, no
projects on a canvas, no interface, and no way to mint a device over HTTP. Those
belong to the program embedding it, which already has its own idea of who its
operators are. What it does have is the part the protocol defines — identity,
scope, validation, and the command channel.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs

from . import protocol
from .channel import (
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_POLL_WAIT,
    Channel,
    Result,
    new_command,
)
from .registry import DEFAULT_ONLINE_WINDOW, Device, DeviceSummary, Registry

#: A reading that arrived, as the calling program sees it.
@dataclass
class Reading:
    """One accepted batch, handed to every `on_events` callback.

    The events are passed through as they arrived, with `source` set to
    ``"device"`` by *this* code rather than read from the wire — a value a
    device reported can therefore never be mistaken for one a model produced.
    """

    module_id: str
    events: List[Dict[str, Any]]
    device_id: str
    device_label: str
    at: float = field(default_factory=time.time)


@dataclass
class Response:
    """A reply, transport-free so `handle` can be mounted anywhere."""

    status: int
    body: Optional[Dict[str, Any]] = None
    headers: Dict[str, str] = field(default_factory=dict)


def _error(status: int, code: str, message: str) -> Response:
    return Response(status, {"error": code, "message": message})


def bearer(headers: Optional[Dict[str, str]]) -> Optional[str]:
    """The token on a request, or None.

    Header lookup is case-insensitive because HTTP says so and because a
    library that only accepted one spelling would fail in the least helpful way.
    """
    if not headers:
        return None
    for name, value in headers.items():
        if name.lower() != "authorization":
            continue
        if not value.lower().startswith("bearer "):
            return None
        token = value[len("bearer ") :].strip()
        return token or None
    return None


class Management:
    """Devices, the readings they report, and the commands they answer."""

    def __init__(
        self,
        *,
        online_window: float = DEFAULT_ONLINE_WINDOW,
        poll_wait: float = DEFAULT_POLL_WAIT,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> None:
        self.registry = Registry(online_window=online_window)
        self.channel = Channel(poll_wait=poll_wait, command_timeout=command_timeout)
        self._on_events: List[Callable[[Reading], None]] = []
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- #
    # Setting the stage                                                 #
    # ---------------------------------------------------------------- #

    def add_module(
        self,
        project_id: str,
        module_id: str,
        *,
        name: Optional[str] = None,
        shapes: Optional[List[str]] = None,
    ) -> None:
        """Declare something a device can be scoped to.

        `shapes` names the structural readings this module displays — ``["scan"]``
        for a lidar, ``["cloud"]`` for a depth camera, ``["image"]`` for a
        camera. A module declared with none carries numbers only, and a device
        that sends it a structural reading is refused rather than believed.
        """
        self.registry.add_module(project_id, module_id, name=name, shapes=shapes)

    def mint(
        self, project_id: str, label: str, module_ids: List[str]
    ) -> tuple:
        """Register a device. Returns ``(device, token)``; the token is shown once."""
        return self.registry.mint(project_id, label, module_ids)

    def revoke(self, project_id: str, device_id: str) -> bool:
        """Cut a device off. Takes effect on its next request, not its next reconnect."""
        revoked = self.registry.revoke(project_id, device_id)
        if revoked:
            self.channel.forget(device_id)
        return revoked

    def devices(self, project_id: str) -> List[DeviceSummary]:
        return [self.registry.summary(device) for device in self.registry.devices(project_id)]

    def device(self, device_id: str) -> Optional[Device]:
        return self.registry.get(device_id)

    # ---------------------------------------------------------------- #
    # Gathering                                                         #
    # ---------------------------------------------------------------- #

    def on_events(self, fn: Optional[Callable[[Reading], None]] = None) -> Any:
        """Register a callback for accepted readings.

            @mgmt.on_events
            def received(reading):
                store(reading)

        Callbacks run on the thread that handled the request, so a slow one
        holds that device's request open. Batch into a queue if that matters.

        A callback that raises is reported to the device as a server error. That
        is deliberate: a reading the program did not actually take delivery of
        must not be acknowledged as accepted, or the device would drop it.
        """

        def register(callback: Callable[[Reading], None]) -> Callable[[Reading], None]:
            with self._lock:
                self._on_events.append(callback)
            return callback

        return register(fn) if fn is not None else register

    def _publish(self, reading: Reading) -> None:
        with self._lock:
            callbacks = list(self._on_events)
        for callback in callbacks:
            callback(reading)

    # ---------------------------------------------------------------- #
    # Commanding                                                        #
    # ---------------------------------------------------------------- #

    def command(
        self,
        device_id: str,
        module_id: str,
        action: str,
        *,
        cmd: str = "",
        target: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Result:
        """Ask a device to do something, and wait for what it did.

        Refuses before sending when the device is not scoped to the module, when
        the module is unknown, or when the device has not been heard from
        recently. The last of those is a courtesy rather than a rule of the
        protocol — the alternative is holding the caller for the whole timeout to
        be told what was already true when they asked.
        """
        device = self.registry.get(device_id)
        if device is None:
            return Result(
                cmd_id="",
                module_id=module_id,
                ok=False,
                reason=f"No device with id {device_id}.",
            )
        if not device.may_report_on(module_id):
            return Result(
                cmd_id="",
                module_id=module_id,
                ok=False,
                reason=f"{device.label} is not scoped to {module_id}.",
            )
        if self.registry.module(device.project_id, module_id) is None:
            return Result(
                cmd_id="",
                module_id=module_id,
                ok=False,
                reason=f"{module_id} is not a module on {device.project_id}.",
            )
        if not self.registry.online(device):
            heard = (
                "never"
                if device.last_seen_at is None
                else f"{round(time.time() - device.last_seen_at)} s ago"
            )
            return Result(
                cmd_id="",
                module_id=module_id,
                ok=False,
                reason=(
                    f"{device.label} last spoke {heard}, so nothing was sent. "
                    "A command is only delivered to a device that is polling."
                ),
            )

        # The timeout the device is told and the one the caller waits on must be
        # the same number, or the device is still being asked to keep working on
        # something the caller has already given up on.
        seconds = self.channel.command_timeout if timeout is None else float(timeout)
        command = new_command(
            module_id, action, cmd=cmd, target=target, timeout=seconds
        )
        return self.channel.dispatch(device_id, command, timeout=seconds)

    # ---------------------------------------------------------------- #
    # The protocol, as a function                                        #
    # ---------------------------------------------------------------- #

    def handle(
        self,
        method: str,
        path: str,
        *,
        query: str = "",
        headers: Optional[Dict[str, str]] = None,
        body: Optional[bytes] = None,
    ) -> Response:
        """Answer one device-facing request.

        `path` is the path alone and `query` the raw query string, so this can
        be mounted in any server without this module knowing what a router is.
        """
        if method.upper() == "GET" and path == "/api/device/whoami":
            return self._whoami(headers)
        if method.upper() == "GET" and path == "/api/device/commands":
            return self._commands(query, headers)
        if method.upper() == "POST" and path == "/api/device/ingest":
            return self._ingest(headers, body)
        if method.upper() == "POST" and path == "/api/device/results":
            return self._results(headers, body)
        return _error(404, "not-found", f"No device route at {method.upper()} {path}.")

    # ---------------------------------------------------------------- #
    # Device-facing routes                                              #
    # ---------------------------------------------------------------- #

    def _authenticate(self, headers: Optional[Dict[str, str]]) -> Any:
        """The device a request is from, or a Response to send instead."""
        device = self.registry.resolve(bearer(headers))
        if device is None:
            return _error(
                401,
                "device-unauthorised",
                "That device token is missing, unknown, or revoked.",
            )
        return device

    def _whoami(self, headers: Optional[Dict[str, str]]) -> Response:
        device = self._authenticate(headers)
        if isinstance(device, Response):
            return device

        # The summary is built from the timestamp this request just wrote, not
        # the one the record held when it was read, so a device identifying
        # itself is never reported as offline in the same breath.
        now = time.time()
        self.registry.touch(device.id, now)
        names = {
            module.id: module.name for module in self.registry.modules(device.project_id)
        }
        # Only what the device could already act on, so this tells a device
        # nothing about the project it should not have.
        return Response(
            200,
            {
                "device": self.registry.summary(device, now).as_dict(),
                "modules": [
                    {"id": mid, "name": names.get(mid, mid)} for mid in device.module_ids
                ],
            },
        )

    def _commands(self, query: str, headers: Optional[Dict[str, str]]) -> Response:
        device = self._authenticate(headers)
        if isinstance(device, Response):
            return device

        requested = parse_qs(query).get("wait", [None])[0]
        try:
            seconds = self.channel.poll_wait if requested is None else float(requested) / 1000.0
        except (TypeError, ValueError):
            seconds = self.channel.poll_wait
        # Capped at the channel's own wait: a caller cannot hold a worker open
        # for longer than the protocol allows by asking nicely.
        seconds = min(max(seconds, 0.0), self.channel.poll_wait)

        command = self.channel.next_command(device.id, seconds)
        self.registry.touch(device.id)
        if command is None:
            return Response(204)
        return Response(200, command.as_dict())

    def _ingest(self, headers: Optional[Dict[str, str]], body: Optional[bytes]) -> Response:
        device = self._authenticate(headers)
        if isinstance(device, Response):
            return device

        parsed = _parse(body)
        if isinstance(parsed, Response):
            return parsed

        if parsed.get("v") != protocol.VERSION:
            return _error(400, "malformed", f"Expected a {protocol.VERSION} message.")

        if parsed.get("type") == "hello":
            # Optional in the protocol, and it says what a device *is* rather
            # than anything it measured, so there is nothing to publish. It is
            # still the device speaking, so it counts as the heartbeat.
            self.registry.touch(device.id)
            return Response(200, {"accepted": 0})

        if parsed.get("type") != "events":
            return _error(
                400, "malformed", f"Expected a {protocol.VERSION} events batch."
            )
        # A device is the thing that measured these numbers. Anything else is a
        # device claiming to be a model, which is the distinction the field exists
        # for — and this end sets the value it passes on, so a device cannot.
        if parsed.get("source") != "device":
            return _error(
                400, "malformed", "A device must send source: \"device\"."
            )

        module_id = parsed.get("moduleId")
        events = parsed.get("events")
        if not isinstance(module_id, str) or module_id == "":
            return _error(400, "malformed", "A batch must name a module.")
        if not isinstance(events, list) or not events:
            return _error(400, "malformed", "A batch must carry at least one reading.")
        if len(events) > protocol.MAX_BATCH:
            return _error(
                400,
                "malformed",
                f"A batch may carry at most {protocol.MAX_BATCH} readings, not {len(events)}.",
            )

        if not device.may_report_on(module_id):
            return _error(
                403,
                "module-out-of-scope",
                f"{device.label} is not scoped to {module_id}.",
            )

        module = self.registry.module(device.project_id, module_id)
        if module is None:
            return _error(
                404,
                "unknown-module",
                f"{module_id} is not a module on this device's project.",
            )

        for event in events:
            refusal = protocol.validate_event(event)
            if refusal is not None:
                return _error(422, "invalid-reading", refusal)
            if event.get("kind") == "shape":
                refusal = protocol.refusal_for_shape(module.shapes, event)
                if refusal is not None:
                    return _error(422, "shape-mismatch", refusal)

        # The batch is all or nothing. Half of it would leave the program
        # holding readings the device never sent as a set, and a device is
        # entitled to assume the batch it sent is the batch that arrived.
        reading = Reading(
            module_id=module_id,
            events=[{**event, "source": "device"} for event in events],
            device_id=device.id,
            device_label=device.label,
        )
        self._publish(reading)
        self.registry.touch(device.id)
        return Response(200, {"accepted": len(events)})

    def _results(self, headers: Optional[Dict[str, str]], body: Optional[bytes]) -> Response:
        device = self._authenticate(headers)
        if isinstance(device, Response):
            return device

        parsed = _parse(body)
        if isinstance(parsed, Response):
            return parsed

        if parsed.get("v") != protocol.VERSION or parsed.get("type") != "result":
            return _error(400, "malformed", f"Expected a {protocol.VERSION} result.")
        if not isinstance(parsed.get("cmdId"), str) or parsed.get("cmdId") == "":
            return _error(400, "malformed", "A result must name the command it answers.")
        if not isinstance(parsed.get("ok"), bool):
            return _error(400, "malformed", "A result must say whether it worked.")
        if not parsed.get("ok") and not parsed.get("reason"):
            # The protocol says a failure must carry a reason. Enforcing it here
            # means an operator is never shown "it failed" with nothing to act on.
            return _error(
                400, "malformed", "A failed result must say what went wrong."
            )

        self.registry.touch(device.id)
        matched = self.channel.complete(Result.from_wire(parsed), device_id=device.id)
        if not matched:
            # Not an error: the caller may already have given up, and a late
            # answer to a command nobody is waiting for is not the device's fault.
            return Response(
                200, {"accepted": False, "reason": "No command with that id was waiting."}
            )
        return Response(200, {"accepted": True})

    # ---------------------------------------------------------------- #
    # Running                                                           #
    # ---------------------------------------------------------------- #

    def serve(
        self,
        host: str = "127.0.0.1",
        port: int = 8787,
        *,
        block: bool = True,
    ) -> Any:
        """Serve the protocol over HTTP with the standard library.

        Returns the server object so a caller can `shutdown()` it; blocks unless
        `block=False`.
        """
        from .server import serve

        return serve(self, host, port, block=block)


def _parse(body: Optional[bytes]) -> Any:
    """A JSON object from a request body, or a Response explaining why not."""
    if not body:
        return _error(400, "malformed", "Expected a JSON body.")
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _error(400, "malformed", "That body is not valid JSON.")
    if not isinstance(parsed, dict):
        return _error(400, "malformed", "Expected a JSON object.")
    return parsed
