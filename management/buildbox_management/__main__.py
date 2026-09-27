"""Run a receiver from the command line, for trying the library out.

    buildbox-management serve --project demo \\
        --module mod-temp --module mod-lidar:scan --label rover

It mints a device, prints the token once, serves the protocol, and prints every
accepted batch as one JSON line on stdout — so a shell can pipe readings
somewhere without writing any Python at all.

This is a convenience, not the interface. A program that wants to keep the
readings should embed `Management` and register `on_events`; this only prints.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional, Tuple

from . import __version__
from .management import Management, Reading


def _module(argument: str) -> Tuple[str, List[str]]:
    """`mod-lidar:scan,trail` → ("mod-lidar", ["scan", "trail"])."""
    name, _, shapes = argument.partition(":")
    name = name.strip()
    if not name:
        raise argparse.ArgumentTypeError("a module needs an id")
    return name, [shape.strip() for shape in shapes.split(",") if shape.strip()]


def _parse(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="buildbox-management",
        description="Receive readings from devices and command them.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run a receiver")
    serve.add_argument("--project", required=True, help="the project id devices belong to")
    serve.add_argument(
        "--module",
        required=True,
        action="append",
        type=_module,
        metavar="ID[:SHAPE,SHAPE]",
        help="a module a device may report on; repeatable. Shapes it displays: "
        "scan, cloud, joints, map, trail, image",
    )
    serve.add_argument("--label", default="cli", help="what the minted device is called")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    arguments = _parse(argv)
    if arguments.command != "serve":  # pragma: no cover - argparse enforces this
        return 2

    management = Management()
    for module_id, shapes in arguments.module:
        management.add_module(arguments.project, module_id, shapes=shapes)

    device, token = management.mint(
        arguments.project, arguments.label, [mid for mid, _ in arguments.module]
    )

    def report(reading: Reading) -> None:
        # One line per batch, so `buildbox-management serve … | jq` works.
        print(
            json.dumps(
                {
                    "at": reading.at,
                    "moduleId": reading.module_id,
                    "device": reading.device_label,
                    "events": reading.events,
                }
            ),
            flush=True,
        )

    management.on_events(report)

    print(f"url    http://{arguments.host}:{arguments.port}", file=sys.stderr)
    print(f"device {device.label} ({device.id})", file=sys.stderr)
    print(f"scope  {', '.join(device.module_ids)}", file=sys.stderr)
    # Shown once, and never stored anywhere it could be read back — the same
    # bargain the protocol makes. A lost token is replaced, not recovered.
    print(f"token  {token}", file=sys.stderr)
    print("waiting for readings…", file=sys.stderr)

    server = management.serve(arguments.host, arguments.port, block=False)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - the entry point is the script
    sys.exit(main())
