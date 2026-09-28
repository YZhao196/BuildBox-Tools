"""The command line: ``buildbox doctor``, ``buildbox send``, ``buildbox run``.

``doctor`` is the one that earns its place. A library installed on a robot has to
be able to answer "what can this machine actually do?" in the same words the
interface uses, because the alternative is an operator discovering that a driver
was never going to work by watching nothing arrive. It probes, and it reports
``available``, ``unavailable``, ``unsupported`` or ``misconfigured`` — never a
bare failure, and never a success it did not observe.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from . import protocol
from .client import BuildBoxError, Client

#: (the modules that would satisfy this, what they would let this device do)
#:
#: Almost every entry is one module, and it is the module the reader imports —
#: probing anything else would answer a question the readers do not ask. GPIO is
#: the exception worth naming: the board decides which of `RPi.GPIO` and
#: `Jetson.GPIO` the readers will import, they share an API, and either one is
#: what "available" means here. Naming a single library would report the other
#: board's machine as unable to read a pin it can read, which is the same lie as
#: probing a library no reader uses.
OPTIONAL_LIBRARIES: List[Tuple[Tuple[str, ...], str]] = [
    (("serial",), "serial and UART ports"),
    (("can",), "CAN bus"),
    (("pymodbus",), "Modbus registers"),
    (("RPi.GPIO", "Jetson.GPIO"), "GPIO pins on a Raspberry Pi or Jetson"),
    (("rclpy",), "native ROS2 topics, services and parameters"),
    (("smbus2",), "I2C devices"),
    (("spidev",), "SPI devices"),
    (("pynmea2",), "NMEA sentences from a GPS"),
    (("cv2",), "camera frames"),
]


def _probe_library(names: Tuple[str, ...]) -> Tuple[str, str]:
    """Whether any of these libraries is importable, and why not when none is.

    A machine carries one GPIO library, not both, so the alternatives are tried
    in turn and the first that imports is the answer. A library that is installed
    but raises on import is not the same as one that is absent, and that reason is
    kept in preference to "not installed" when nothing loads.
    """
    refusal = "not installed"
    for name in names:
        try:
            __import__(name)
        except ImportError:
            continue
        except Exception as error:  # noqa: BLE001 - a broken install is a real answer
            refusal = f"installed but failed to load: {error}"
            continue
        return "available", ""
    return "unavailable", refusal


def _serial_ports() -> Tuple[str, str, List[str]]:
    """What serial ports exist, if any, and whether it is even possible to look."""
    try:
        from serial.tools import list_ports  # type: ignore[import-not-found]
    except ImportError:
        return "unavailable", "pyserial is not installed", []

    try:
        ports = [port.device for port in list_ports.comports()]
    except Exception as error:  # noqa: BLE001
        return "unavailable", f"the port list could not be read: {error}", []

    if not ports:
        return "unavailable", "no serial ports on this machine", []
    return "available", "", ports


def _can_interfaces() -> Tuple[str, str, List[str]]:
    """SocketCAN interfaces. Linux only, and unsupported rather than absent elsewhere."""
    if not sys.platform.startswith("linux"):
        return "unsupported", f"SocketCAN does not exist on {sys.platform}", []
    root = "/sys/class/net"
    try:
        names = [n for n in os.listdir(root) if os.path.exists(os.path.join(root, n, "type"))]
    except OSError as error:
        return "unavailable", f"{root} could not be read: {error}", []

    interfaces = []
    for name in names:
        try:
            with open(os.path.join(root, name, "type"), encoding="utf-8") as handle:
                # ARPHRD_CAN is 280. Only those are CAN devices; ethernet and
                # loopback share the same directory.
                if handle.read().strip() == "280":
                    interfaces.append(name)
        except OSError:
            continue

    if not interfaces:
        return "unavailable", "no CAN interfaces are up", []
    return "available", "", interfaces


def _server_probe(client: Optional[Client], url: str) -> Tuple[str, str, Dict[str, Any]]:
    """Whether the server answers, and whether this token is good."""
    if client is None:
        return "misconfigured", "no device token (pass --token or set BUILDBOX_TOKEN)", {}
    try:
        return "available", "", client.whoami()
    except BuildBoxError as error:
        return "unavailable", str(error), {}


def doctor(client: Optional[Client], url: str) -> int:
    """Print what this machine can do. Returns a process exit code."""
    rows: List[Tuple[str, str, str, str]] = []

    server_state, server_reason, identity = _server_probe(client, url)
    rows.append(("server", server_state, url, server_reason))

    if identity:
        device = identity.get("device", {})
        rows.append(
            (
                "device",
                "available",
                f"{device.get('label', '?')} ({device.get('id', '?')})",
                "",
            )
        )
        for module in identity.get("modules", []):
            rows.append(("module", "available", f"{module['name']} ({module['id']})", ""))

    for names, description in OPTIONAL_LIBRARIES:
        state, reason = _probe_library(names)
        rows.append((" / ".join(names), state, description, reason))

    ports_state, ports_reason, ports = _serial_ports()
    rows.append(("serial ports", ports_state, ", ".join(ports), ports_reason))

    can_state, can_reason, interfaces = _can_interfaces()
    rows.append(("CAN", can_state, ", ".join(interfaces), can_reason))

    width = max(len(row[0]) for row in rows)
    for name, state, detail, reason in rows:
        line = f"{name.ljust(width)}  {state.ljust(13)} {detail}"
        if reason:
            line += f"  · {reason}"
        print(line)

    if server_state != "available":
        print("\nThis device cannot report anything until the server answers.", file=sys.stderr)
        return 1
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="buildbox",
        description="Report readings from this device and answer the commands it is sent.",
    )
    parser.add_argument("--url", default=os.environ.get("BUILDBOX_URL", "http://127.0.0.1:8787"))
    parser.add_argument("--token", default=os.environ.get("BUILDBOX_TOKEN"))
    parser.add_argument("--module", default=None, help="the module to report on")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="what this machine can actually do")
    subparsers.add_parser("whoami", help="the device and the modules it may report on")

    send = subparsers.add_parser("send", help="report one reading")
    send.add_argument("key")
    send.add_argument("value", type=float)
    send.add_argument("--unit", default=None)

    run = subparsers.add_parser("run", help="answer commands until interrupted")
    run.add_argument(
        "--log",
        action="store_true",
        help="also print each command and what this device decided",
    )

    args = parser.parse_args(argv)

    client: Optional[Client] = None
    if args.token:
        client = Client(args.url, args.token, module=args.module)

    if args.command == "doctor":
        return doctor(client, args.url)

    if client is None:
        print("No device token. Pass --token or set BUILDBOX_TOKEN.", file=sys.stderr)
        return 2

    try:
        if args.command == "whoami":
            print(json.dumps(client.whoami(), indent=2))
            return 0

        if args.command == "send":
            count = client.send_data(args.key, args.value, args.unit)
            print(f"sent {count} reading(s)")
            return 0

        if args.command == "run":
            if args.log:
                client.on_command(lambda command: print(f"← {command}") or None)
            print(f"{client.url} · waiting for commands (Ctrl-C to stop)")
            client.run_forever()
            return 0
    except BuildBoxError as error:
        print(str(error), file=sys.stderr)
        return 1

    parser.error(f"unknown command {args.command!r}")  # pragma: no cover
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
