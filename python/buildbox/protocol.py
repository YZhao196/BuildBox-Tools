"""The wire format, and the small amount of vocabulary that goes with it.

The shapes here mirror ``docs/bridge-protocol.md`` field for field. That document
is normative and this is a port of it; ``tests/test_protocol.py`` pulls the
example messages out of it so the two cannot drift apart quietly.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ._version import __version__

#: The only protocol version this package speaks.
VERSION = "bbp/1"

#: What a device calls itself in a log line on the server.
#:
#: Built from the package version rather than written out, so a device cannot
#: announce a version it is not running.
AGENT = f"python/{__version__}"

#: Actions that only observe.
#:
#: Anything outside this set changes something on the device, so the server will
#: demand a confirmation token for it before a command is ever sent here. The set
#: exists in the library as well so a handler can ask what it is being asked to
#: do without matching on strings.
READ_ACTIONS = frozenset(
    {"read", "check", "poll", "listen", "scan", "subscribe", "open", "capture"}
)

#: The structural readings a device may report, and what each one carries.
SHAPES = frozenset({"scan", "cloud", "joints", "map", "trail", "image"})

KINDS = frozenset({"sample", "log", "status", "shape"})
SEVERITIES = frozenset({"info", "warn", "error"})
STATUSES = frozenset({"ok", "warn", "error", "idle", "armed", "fired"})


def sample(key: str, value: float, unit: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    """One numeric reading.

    ``key`` is the series name the interface plots it under. Two devices
    reporting the same key on the same module share a series, which is usually
    what you want and occasionally is not — give them different keys if so.
    """
    event: Dict[str, Any] = {"kind": "sample", "key": key, "value": float(value)}
    if unit is not None:
        event["unit"] = unit
    event.update(extra)
    return event


def log(message: str, severity: str = "info", **extra: Any) -> Dict[str, Any]:
    """A line for the module's output panel and the session log."""
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {sorted(SEVERITIES)}, not {severity!r}")
    return {"kind": "log", "message": str(message), "severity": severity, **extra}


def status(value: str, **extra: Any) -> Dict[str, Any]:
    """Move the module's status light."""
    if value not in STATUSES:
        raise ValueError(f"status must be one of {sorted(STATUSES)}, not {value!r}")
    return {"kind": "status", "status": value, **extra}


def shape(kind: str, points: List[float], **extra: Any) -> Dict[str, Any]:
    """A reading with structure rather than a single number.

    ``scan``     ranges in metres, ordered from ``angleMin`` to ``angleMax``.
    ``cloud``    x, y, z triples in metres, in the sensor frame.
    ``joints``   a position per joint, with ``labels`` naming them.
    ``map``      occupancy in 0..100 per cell, row-major, ``cols`` x ``rows``.
    ``trail``    a single x, y pose; the interface accumulates the path.
    ``image``    a data URL, passed as ``image=`` rather than in ``points``.

    The server checks this against the module's preset and refuses a reading the
    module could not have produced — a laser sweep arriving on a temperature
    module is rejected rather than drawn.
    """
    if kind not in SHAPES:
        raise ValueError(f"shape must be one of {sorted(SHAPES)}, not {kind!r}")
    if kind == "image":
        if "image" not in extra:
            raise ValueError("an image reading needs image=<data url>")
        return {"kind": "shape", "shape": kind, **extra}
    return {"kind": "shape", "shape": kind, "points": [float(p) for p in points], **extra}


def envelope(module_id: str, events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Wrap readings for ``POST /api/device/ingest``.

    ``source`` is fixed to ``"device"`` here rather than being a parameter. The
    server refuses anything else, and a client that let you set it would only be
    offering a way to be rejected.
    """
    return {
        "v": VERSION,
        "type": "events",
        "moduleId": module_id,
        "source": "device",
        "events": events,
    }


def hello(capabilities: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "v": VERSION,
        "type": "hello",
        "agent": AGENT,
        "capabilities": list(capabilities or []),
    }
