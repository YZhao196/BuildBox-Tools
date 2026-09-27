"""Receive from devices, and command them — the other end of the bridge.

    from buildbox_management import Management

    mgmt = Management()
    mgmt.add_module("proj-1", "mod-temp", name="Temperature")

    device, token = mgmt.mint("proj-1", "rover", ["mod-temp"])

    @mgmt.on_events
    def received(reading):
        print(reading.module_id, reading.events)

    mgmt.serve("127.0.0.1", 8787)

This is the counterpart to the `buildbox` package a device runs: that one reports
readings and answers commands, this one receives them and sends them. A program
can use this to gather data from devices without running BuildBox, and the
protocol it speaks — ``bbp/1`` — is written down in
``docs/bridge-protocol.md``.

Requires nothing but the standard library.
"""

from . import protocol
from ._version import __version__
from .channel import Channel, Command, Result, new_command
from .management import Management, Reading, Response
from .registry import Device, DeviceSummary, Module, Registry
from .server import serve

__all__ = [
    "Channel",
    "Command",
    "Device",
    "DeviceSummary",
    "Management",
    "Module",
    "Reading",
    "Registry",
    "Response",
    "Result",
    "__version__",
    "new_command",
    "protocol",
    "serve",
]
