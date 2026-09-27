"""Report readings from a device, and answer the commands it is sent.

    from buildbox import Client

    bb = Client("http://127.0.0.1:8787", token=TOKEN)
    bb.send_data("temperature", 21.5, unit="C")

The server side of this is the bridge protocol; ``buildbox.protocol`` is the
Python port of it and ``docs/bridge-protocol.md`` is the written form.

Requires nothing but the standard library, so it installs on a robot with no
wheel for its architecture and nothing to compile.
"""

from . import data, presets, protocol, sensors
from ._version import __version__
from .client import (
    AuthError,
    BuildBoxError,
    Client,
    Command,
    Result,
    ScopeError,
    Sensor,
    ShapeError,
    interpret,
)
from .protocol import AGENT, VERSION, log, sample, shape, status

__all__ = [
    "AGENT",
    "AuthError",
    "BuildBoxError",
    "Client",
    "Command",
    "Result",
    "ScopeError",
    "Sensor",
    "ShapeError",
    "VERSION",
    "__version__",
    "data",
    "interpret",
    "log",
    "presets",
    "protocol",
    "sample",
    "sensors",
    "shape",
    "status",
]
