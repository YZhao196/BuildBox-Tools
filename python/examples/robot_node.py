"""A device on a robot: read a real sensor, report it, and stop when told to.

    export BUILDBOX_URL=http://127.0.0.1:8787
    export BUILDBOX_TOKEN=<the token the server gave you>
    python examples/robot_node.py

The token is shown once when you mint it — `POST /api/projects/<id>/devices`, or
the interface. Scoping it to a module is what lets it report against that module
and nothing else.

By default this reads the SoC temperature on a Raspberry Pi or a Jetson, which
the kernel exposes as a file:

    /sys/class/thermal/thermal_zone0/temp       21500        (millidegrees C)

That is how a great deal of hardware is actually read on a Pi — temperature,
voltage, current, fan speed are all files under `/sys`. Point it somewhere else
if your sensor is elsewhere:

    BUILDBOX_SENSOR=/sys/class/power_supply/BAT0/voltage_now \
    BUILDBOX_SENSOR_SCALE=0.000001 python examples/robot_node.py

and if the file cannot be read the device says so rather than reporting a
number, which is the point.
"""

from __future__ import annotations

import os
import sys
import time

from buildbox import Client, Command, Result, sensors

SENSOR_PATH = os.environ.get("BUILDBOX_SENSOR", "/sys/class/thermal/thermal_zone0/temp")
SENSOR_SCALE = float(os.environ.get("BUILDBOX_SENSOR_SCALE", "0.000001"))

# The Client reads BUILDBOX_URL and BUILDBOX_TOKEN from the environment, which is
# how a service on a robot usually gets its configuration.
bb = Client()

STOPPED = False


# Reading is a registration, not a loop: the client calls this on its interval
# and sends whatever comes back. It raises if the file cannot be read, and the
# failure is logged rather than plotted.
bb.sensor("temperature", sensors.file_number(SENSOR_PATH, scale=SENSOR_SCALE), unit="C", every=2.0)


# A second reading from the same device, off a plain Python value. Anything
# callable works — a library call, an attribute, a computation.
_uptime = time.monotonic()
bb.sensor("uptime", lambda: time.monotonic() - _uptime, unit="s", every=5.0)


@bb.on_command(match="kill_switch")
def kill_switch(command: Command) -> "int | Result":
    """Stop the machine. Returns an exit code, like the shell command it replaces.

    0 means it stopped; anything else means it did not, and the server reports
    that back to whoever pressed the button. This is the one answer in the whole
    system that must never be optimistic: a kill switch that reports having
    stopped something it did not is the most dangerous thing this could do.
    """
    global STOPPED
    if command.matches("1"):
        STOPPED = True
        bb.send_status("idle")
        bb.send_log("stopped on request", "warn")
        return 0
    return Result(ok=False, code=1, reason="Not stopped: expected “kill_switch 1”.")


def home_axes() -> bool:
    """Drive every axis to its limit switch; True only once they are all there.

    Replace this with your motion controller's homing routine. This example has
    no axes, so it cannot home anything, and says so.
    """
    return False


@bb.on_command(match="home")
def home(command: Command) -> Result:
    """Home the machine. Success only when the axes report they are home."""
    if home_axes():
        bb.send_status("homed")
        return Result(ok=True, code=0, output=["homed"])
    return Result(
        ok=False,
        code=1,
        reason="Not homed: home_axes() is not wired to a motion controller on this device.",
    )


@bb.on_command
def anything_else(command: Command) -> Result:
    """Everything else is logged and refused, never acknowledged as done.

    Answering success here would tell whoever pressed the button that something
    happened when nothing did.
    """
    bb.send_log(f"asked to {command.action}: {command.cmd}", "info")
    return Result(
        ok=False,
        code=1,
        reason=f"This device has no handler for “{command.action}: {command.cmd}”. Nothing was done.",
    )


def main() -> int:
    who = bb.whoami()
    print(f"connected as {who['device']['label']} ({who['device']['id']})")
    for module in who["modules"]:
        print(f"  reporting on {module['name']} ({module['id']})")

    try:
        reading = sensors.file_number(SENSOR_PATH, scale=SENSOR_SCALE)()
        print(f"  reading {SENSOR_PATH} → {reading}")
    except sensors.SensorError as error:
        # Worth saying now rather than watching nothing appear: the device will
        # keep running and keep reporting the failure, but the operator should
        # know before they start waiting.
        print(f"  cannot read {SENSOR_PATH}: {error}", file=sys.stderr)
        print("  set BUILDBOX_SENSOR to a file holding a number", file=sys.stderr)

    print("reading the sensor and answering commands; Ctrl-C to stop")
    try:
        bb.run()
    except KeyboardInterrupt:
        print("\ninterrupted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
