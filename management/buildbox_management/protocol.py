"""The wire format, from the receiving end.

`docs/bridge-protocol.md` is normative and `python/buildbox/protocol.py` is the
device's port of it. This is the third port — the same vocabulary, written out
again rather than imported, because a program that only wants to *receive* from
devices should not have to install the package a device runs. Both are ports of
one document, which is the arrangement the C++ library already works under.

The jobs of this module are small and specific:

* name the fields the wire uses, so the rest of the package is not stringly typed;
* validate one reading, because a reading that reaches a consumer is one
  somebody will act on.

Validation here is deliberately about *shape of a message*, not about meaning.
Whether a reading is something a given module could have produced is a question
about that module, and it is answered in `management.py` where modules are known.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

#: The only protocol version this package speaks.
VERSION = "bbp/1"

KINDS = frozenset({"sample", "log", "status", "shape"})
SEVERITIES = frozenset({"info", "warn", "error"})
STATUSES = frozenset({"ok", "warn", "error", "idle", "armed", "fired"})

#: The structural readings a device may report. These carry shape a single
#: number cannot — a laser sweep, a point cloud, a joint pose.
SHAPES = frozenset({"scan", "cloud", "joints", "map", "trail", "image"})

#: Actions that only observe.
#:
#: Anything outside this set changes something on the device. The receiving end
#: holds that distinction so a caller can ask whether it is about to ask a robot
#: to do something, rather than matching on strings.
READ_ACTIONS = frozenset(
    {"read", "check", "poll", "listen", "scan", "subscribe", "open", "capture"}
)

#: How many readings one batch may carry.
#:
#: A device sampling at 50 Hz batches rather than making fifty requests a second,
#: so this has to be generous enough for that and small enough that one device
#: cannot exhaust the host's memory in a single call.
MAX_BATCH = 500


def is_read(action: str) -> bool:
    """Whether an action only observes. Everything else changes something."""
    return isinstance(action, str) and action in READ_ACTIONS


def _one_of(value: Any, allowed: frozenset) -> bool:
    """Whether a JSON value is one of a set of words.

    The `isinstance` is not decoration: these sets are a frozenset, so a value
    that is a list or an object raises `TypeError` from the membership test
    rather than answering. A request body is not a place to let that happen.
    """
    return isinstance(value, str) and value in allowed


def _finite(value: Any) -> bool:
    """A JSON number that is not NaN or an infinity.

    `float('nan')` is a float, so `isinstance(x, float)` is not the check that
    matters — a NaN would serialize to a token the JSON parser on the other end
    is entitled to reject, or worse, plot as a gap nobody explained.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and value not in (
        float("inf"),
        float("-inf"),
    )


def validate_event(event: Any) -> Optional[str]:
    """Why this reading is not one this protocol carries, or None if it is.

    Returns the reason rather than raising, because every caller needs to turn it
    into a refusal with a status code and the wording is the same either way.
    """
    if not isinstance(event, dict):
        return f"A reading must be an object, not {type(event).__name__}."

    kind = event.get("kind")
    if not _one_of(kind, KINDS):
        return f"“{kind}” is not a kind of reading; expected one of {sorted(KINDS)}."

    if kind == "sample":
        if not isinstance(event.get("key"), str) or event.get("key") == "":
            return "A sample must name the series it belongs to."
        if not _finite(event.get("value")):
            return "A sample must carry a finite number."
        return None

    if kind == "log":
        if not isinstance(event.get("message"), str):
            return "A log line must carry its message."
        if not _one_of(event.get("severity"), SEVERITIES):
            return f"A log line's severity must be one of {sorted(SEVERITIES)}."
        return None

    if kind == "status":
        if not _one_of(event.get("status"), STATUSES):
            return f"A status must be one of {sorted(STATUSES)}."
        return None

    # kind == "shape"
    name = event.get("shape")
    if not _one_of(name, SHAPES):
        return f"A structural reading must say which shape it is; {sorted(SHAPES)}."
    if name == "image":
        if not isinstance(event.get("image"), str):
            return "An image reading must carry its frame as a data URL."
        return None
    points = event.get("points")
    if not isinstance(points, list) or not all(_finite(p) for p in points):
        return f"A {name} reading must carry a list of numbers in points."
    if name == "map":
        cols, rows = event.get("cols"), event.get("rows")
        # `bool` is a subclass of `int`, so `True` would otherwise be a 1x1 grid.
        if not _positive_int(cols) or not _positive_int(rows):
            return "A map reading must carry its cols and rows."
        if len(points) != cols * rows:
            return (
                f"A map reading is row-major and must fill its grid: "
                f"{len(points)} cells for {cols}x{rows}."
            )
    return None


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def refusal_for_shape(allowed: Iterable[str], event: Dict[str, Any]) -> Optional[str]:
    """Why this module cannot carry this structural reading, or None if it can.

    `allowed` is the set of shapes the module was declared to display. A module
    declared with none displays no structural reading, which is the common case —
    a temperature module has no business carrying a laser sweep, and the
    alternative to refusing it is drawing something no sensor measured.
    """
    name = event.get("shape")
    permitted = set(allowed)
    if not permitted:
        return "This module does not display a structural reading, so it cannot carry one."
    if name not in permitted:
        return (
            f"This module displays {' or '.join(sorted(permitted))}, "
            f"so it cannot carry a {name} reading."
        )
    return None
