"""A function for every kind of reading a module can carry.

The protocol has four primitives — a numeric sample, a log line, a status, and a
structural reading. Everything below turns a domain fact into one or more of
those, so that sending a CAN frame or a GPS fix does not mean remembering which
field the server wants where.

    from buildbox import Client, data

    bb.send(data.temperature(21.5, humidity=44))
    bb.send(data.can_frame(0x123, b"\\x01\\x02"))
    bb.send(data.gps_fix(latitude=-37.8, longitude=144.9, speed=1.2))

Every function returns a **list** of events, because most real readings are more
than one number: an IMU is nine, a GPS fix is six, a diagnostic carries a level
and a sentence. `Client.send` takes either a single event or a list, so they
compose either way.

Two things this module is careful about, both of which the server enforces:

* **A sample is a number.** The protocol has no string sample, so anything
  textual — an AI reply, a shell line, an MQTT payload — becomes a log line
  rather than being coerced into a number it is not.
* **A structural reading must match its module.** A laser sweep may only be sent
  to a module whose face draws a sweep; the server refuses it anywhere else. The
  functions here emit the shape the preset expects and no other.

`PRESET_BUILDERS` maps every name in the catalogue to the function that feeds it,
and `tests/test_data.py` fails if a preset is ever added without one.
"""

from __future__ import annotations

import base64
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Union

from . import protocol
from .presets import PRESETS

Event = Dict[str, Any]

#: The severities a log line may carry.
Severities = ("info", "warn", "error")

#: The statuses a module light may take.
Statuses = ("ok", "warn", "error", "idle", "armed", "fired")


# ------------------------------------------------------------------ #
# The four primitives                                                 #
# ------------------------------------------------------------------ #


def sample(key: str, value: float, unit: Optional[str] = None) -> List[Event]:
    """One numeric reading, under a named series."""
    return [protocol.sample(key, value, unit)]


def samples(**readings: Optional[float]) -> List[Event]:
    """Several readings at once, skipping the ones that are None.

    ``samples(temperature=21.5, humidity=44)``

    A sensor that could not read one of its fields passes None for it rather than
    a placeholder, and that field is simply not reported — which is the honest
    thing, since a placeholder would be a number nobody measured.
    """
    return _readings((key.replace("_", " "), value, None) for key, value in readings.items())


def log(message: str, severity: str = "info") -> List[Event]:
    """A line for the module's output panel and the session log."""
    return [protocol.log(message, severity)]


def status(value: str) -> List[Event]:
    """Move the module's status light."""
    return [protocol.status(value)]


# ------------------------------------------------------------------ #
# Structural readings                                                 #
# ------------------------------------------------------------------ #


def laser_scan(ranges: Sequence[float], angle_min: float, angle_max: float) -> List[Event]:
    """A laser sweep: ranges in metres across a sweep, in radians.

    For **ROS2 Laser Scan**. The face draws the polar plot from exactly these
    fields — and only these, because they are the fields the protocol carries. A
    sensor's range limits are not among them, and sending one would have it
    dropped silently on the way through.
    """
    return [
        {
            "kind": "shape",
            "shape": "scan",
            "points": [float(r) for r in ranges],
            "angleMin": float(angle_min),
            "angleMax": float(angle_max),
        }
    ]


def point_cloud(points: Sequence[Any]) -> List[Event]:
    """A point cloud as x, y, z triples in metres, in the sensor frame.

    Accepts either a flat ``[x, y, z, x, y, z, …]`` or a sequence of
    ``(x, y, z)`` triples, because both are what sensors and libraries hand you.

    For **ROS2 Pointcloud Viewer**.
    """
    flat: List[float] = []
    for point in points:
        if isinstance(point, (int, float)):
            flat.append(float(point))
        else:
            flat.extend(float(axis) for axis in point)
    return [{"kind": "shape", "shape": "cloud", "points": flat}]


def joint_state(
    positions: Sequence[float],
    *,
    names: Optional[Sequence[str]] = None,
    velocities: Optional[Sequence[float]] = None,
    efforts: Optional[Sequence[float]] = None,
) -> List[Event]:
    """A position per joint, with their names.

    For **ROS2 Joint State**. Velocity and effort also become per-joint series so
    they can be plotted, while the positions themselves are the structural
    reading the face draws as bars.
    """
    events: List[Event] = [
        {
            "kind": "shape",
            "shape": "joints",
            "points": [float(p) for p in positions],
            **({"labels": list(names)} if names else {}),
        }
    ]
    labels = list(names) if names else [f"joint{i}" for i in range(len(positions))]
    for index, label in enumerate(labels):
        if velocities is not None and index < len(velocities):
            events.append(protocol.sample(f"{label} velocity", float(velocities[index]), "rad/s"))
        if efforts is not None and index < len(efforts):
            events.append(protocol.sample(f"{label} effort", float(efforts[index]), "Nm"))
    return events


def occupancy_grid(cells: Sequence[float], cols: int, rows: int) -> List[Event]:
    """An occupancy grid in 0..100 per cell, row-major.

    For **ROS2 Map Viewer**. The grid's own resolution is not carried: the face
    draws cells, and a field the protocol does not define would be dropped on the
    way through rather than shown.
    """
    if len(cells) != cols * rows:
        raise ValueError(
            f"an occupancy grid of {cols}x{rows} needs {cols * rows} cells, not {len(cells)}"
        )
    return [
        {
            "kind": "shape",
            "shape": "map",
            "points": [float(cell) for cell in cells],
            "cols": int(cols),
            "rows": int(rows),
        }
    ]


def odometry(
    x: float,
    y: float,
    *,
    yaw: Optional[float] = None,
    linear_x: Optional[float] = None,
    angular_z: Optional[float] = None,
) -> List[Event]:
    """A pose, which the face accumulates into a trail, plus any velocity.

    For **ROS2 Odometry**. A trail is history, so the client keeps the path; each
    call contributes one pose to it.
    """
    events: List[Event] = [{"kind": "shape", "shape": "trail", "points": [float(x), float(y)]}]
    events.extend(
        _readings(
            (
                ("speed", linear_x, "m/s"),
                ("turn rate", angular_z, "rad/s"),
                ("heading", yaw, "rad"),
            )
        )
    )
    return events


def image_frame(
    frame: Union[str, bytes],
    *,
    mime: str = "image/png",
    width: Optional[int] = None,
    height: Optional[int] = None,
    fps: Optional[float] = None,
) -> List[Event]:
    """A camera frame, as raw bytes or an already-encoded ``data:`` URL.

    For **Camera Capture** and **ROS2 Image Stream**. Bytes are base64-encoded
    here so the caller does not have to — and because a frame that reaches the
    canvas unencoded would be a JSON string full of binary.

    Only ever send a frame a device actually produced. A modelled camera has no
    frame to give, and drawing one would be an illustration of a camera rather
    than a picture from it.
    """
    if isinstance(frame, bytes):
        url = f"data:{mime};base64,{base64.b64encode(frame).decode('ascii')}"
    else:
        url = frame

    events: List[Event] = [{"kind": "shape", "shape": "image", "image": url}]
    events.extend(
        _readings((("resolution x", width, None), ("resolution y", height, None), ("fps", fps, "fps")))
    )
    return events


# ------------------------------------------------------------------ #
# General                                                             #
# ------------------------------------------------------------------ #


def script_output(
    lines: Sequence[str], *, code: int = 0, stream: str = "stdout"
) -> List[Event]:
    """What a script printed, with the exit code the last line names.

    For **Script Runner**, **Generic Control** and **CLI Terminal**. A non-zero
    exit code is reported as an error rather than buried in the output, because
    that is the thing an operator scanning the log needs to see.
    """
    severity = "info" if code == 0 else "error"
    events: List[Event] = [protocol.log(str(line), severity) for line in lines]
    events.append(
        protocol.log(f"{stream} exited {code}", "info" if code == 0 else "error")
    )
    return events


def health_check(
    latency_ms: float, *, reachable: bool = True, detail: Optional[str] = None
) -> List[Event]:
    """Whether a host answered, and how long it took.

    For **Health Check**. Unreachable is a warning with the reason attached, not
    a zero-latency success — a health check that reports healthy when nothing
    answered is worse than no health check.
    """
    if not reachable:
        return [
            protocol.status("error"),
            protocol.log(detail or "the host did not answer", "error"),
        ]
    events: List[Event] = [protocol.sample("ping", float(latency_ms), "ms")]
    events.append(protocol.status("warn" if latency_ms > 500 else "ok"))
    if detail:
        events.append(protocol.log(detail, "info"))
    return events


def tailscale_peer(
    name: str,
    *,
    online: bool = True,
    ip: Optional[str] = None,
    last_seen: Optional[str] = None,
) -> List[Event]:
    """A peer's presence and address.

    For **Tailscale Peer**.
    """
    described = f"{name} is {'online' if online else 'offline'}"
    if ip:
        described += f" at {ip}"
    if last_seen:
        described += f" (last seen {last_seen})"
    return [protocol.log(described, "info" if online else "warn"), protocol.status("ok" if online else "warn")]


def ai_response(text: str, *, model: Optional[str] = None, latency_ms: Optional[float] = None) -> List[Event]:
    """What a model replied.

    For **AI API**. Text is a log line rather than a sample: the protocol has no
    string sample, and coercing prose into a number would be inventing a
    measurement.
    """
    events: List[Event] = [protocol.log(text, "info")]
    events.extend(_readings((("ai latency", latency_ms, "ms"),)))
    if model:
        events.append(protocol.log(f"model: {model}", "info"))
    return events


def kill_switch_outcome(
    *, stopped: bool, code: int, message: Optional[str] = None
) -> List[Event]:
    """What a kill switch did.

    For **Kill Switch**. `stopped` is the only thing that decides the status: a
    command that returned 0 without stopping anything is not a stop, and this
    reports it as the failure it is.
    """
    if not stopped:
        return [
            protocol.status("error"),
            protocol.log(message or f"the termination command did not stop the machine (exit {code})", "error"),
        ]
    return [
        protocol.status("fired"),
        protocol.log(message or f"stopped (exit {code})", "warn"),
    ]


# ------------------------------------------------------------------ #
# ROS2                                                                #
# ------------------------------------------------------------------ #


def ros2_message(
    topic: str, message_type: Optional[str] = None, **fields: Any
) -> List[Event]:
    """An arbitrary topic message.

    For **ROS2 Topic Subscriber**. Numeric fields become series named
    ``topic/field`` so several topics plotting the same field name do not collide;
    anything else becomes a log line.
    """
    events: List[Event] = []
    for key, value in fields.items():
        series = f"{topic}/{key}"
        if isinstance(value, bool):
            events.append(protocol.sample(series, 1.0 if value else 0.0))
        elif isinstance(value, (int, float)):
            events.append(protocol.sample(series, float(value)))
        else:
            events.append(protocol.log(f"{series} = {value}", "info"))
    if message_type:
        events.append(protocol.log(f"{topic} · {message_type}", "info"))
    return events or [protocol.log(f"{topic} published an empty message", "info")]


def ros2_nodes(nodes: Sequence[Dict[str, Any]]) -> List[Event]:
    """The nodes in the graph and whether each is alive.

    For **ROS2 Node Monitor**. A node that has gone is an error rather than a
    quieter list, because a graph that quietly loses a node is the thing this
    monitor exists to catch.
    """
    events: List[Event] = []
    dead = [node for node in nodes if not node.get("alive", True)]
    for node in nodes:
        name = node.get("name", "?")
        if node.get("alive", True):
            events.append(protocol.log(f"{name} alive", "info"))
        else:
            events.append(protocol.log(f"{name} is gone", "error"))
    events.append(protocol.sample("nodes", float(len(nodes))))
    events.append(protocol.status("error" if dead else "ok"))
    return events


def ros2_topics(topics: Sequence[Dict[str, Any]]) -> List[Event]:
    """The topics in the graph with their types and rates.

    For **ROS2 Topic Monitor**.
    """
    events: List[Event] = []
    for topic in topics:
        described = f"{topic.get('name', '?')} · {topic.get('type', 'unknown')}"
        if topic.get("rate") is not None:
            described += f" · {topic['rate']} Hz"
            events.append(protocol.sample("scan", float(topic["rate"]), "Hz"))
        events.append(protocol.log(described, "info"))
    return events or [protocol.log("no topics in the graph", "warn")]


def ros2_parameter(name: str, value: Any, *, changed: bool = False) -> List[Event]:
    """A parameter's value, and whether it just changed.

    For **ROS2 Parameter Server**. A change is logged at warn so the session's
    history shows who moved what, which is what the change history is for.
    """
    events: List[Event] = []
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        events.append(protocol.sample(f"param {name}", float(value)))
    events.append(
        protocol.log(
            f"parameter {name} = {value}" + (" (changed)" if changed else ""),
            "warn" if changed else "info",
        )
    )
    return events


def ros2_transforms(transforms: Sequence[Dict[str, Any]]) -> List[Event]:
    """The transform tree.

    For **ROS2 TF Monitor**. Stale transforms are reported rather than drawn as
    current — a stale transform presented as live is how a robot's position
    silently becomes wrong.
    """
    events: List[Event] = []
    for transform in transforms:
        edge = f"{transform.get('parent', '?')} → {transform.get('child', '?')}"
        if transform.get("stale"):
            events.append(protocol.log(f"{edge} is stale", "warn"))
            continue
        translation = transform.get("translation")
        described = edge
        if translation is not None:
            described += f" · {translation}"
        events.append(protocol.log(described, "info"))
    return events or [protocol.log("no transforms published", "warn")]


def ros2_diagnostics(components: Sequence[Dict[str, Any]]) -> List[Event]:
    """Per-component health.

    For **ROS2 Diagnostics**. The worst level decides the module's status, so one
    failed component is not hidden behind three healthy ones.
    """
    events: List[Event] = []
    worst = "ok"
    rank = {"ok": 0, "warn": 1, "error": 2}
    for component in components:
        level = str(component.get("level", "ok")).lower()
        if level not in rank:
            level = "ok"
        if rank[level] > rank[worst]:
            worst = level
        message = component.get("message")
        described = f"{component.get('component', '?')}: {level}"
        if message:
            described += f" · {message}"
        events.append(protocol.log(described, "info" if level == "ok" else level))
    events.append(protocol.status(worst))
    return events


def ros2_bag_recording(
    path: str, *, duration_s: Optional[float] = None, size_bytes: Optional[float] = None, active: bool = True
) -> List[Event]:
    """A recording's progress.

    For **ROS2 Bag Recorder**.
    """
    events: List[Event] = [
        protocol.log(f"recording to {path}" + ("" if active else " stopped"), "info"),
        protocol.status("ok" if active else "idle"),
    ]
    events.extend(
        _readings((("recording", duration_s, "s"), ("bag size", size_bytes, "B")))
    )
    return events


def ros2_bag_playback(
    path: str, *, position_s: float, duration_s: Optional[float] = None, playing: bool = True
) -> List[Event]:
    """Where playback has reached.

    For **ROS2 Bag Player**.
    """
    events: List[Event] = [
        protocol.sample("playback", float(position_s), "s"),
        protocol.status("ok" if playing else "idle"),
        protocol.log(
            f"{path} at {position_s}s" + (f" of {duration_s}s" if duration_s else ""),
            "info",
        ),
    ]
    return events


def ros2_lifecycle(node: str, state: str) -> List[Event]:
    """A lifecycle node's state.

    For **ROS2 Lifecycle Manager**.
    """
    known = {"unconfigured", "inactive", "active", "finalized"}
    if state not in known:
        raise ValueError(f"lifecycle state must be one of {sorted(known)}, not {state!r}")
    return [
        protocol.status("ok" if state == "active" else "idle"),
        protocol.log(f"{node} is {state}", "info"),
    ]


def ros2_topic_rate(topic: str, hz: float) -> List[Event]:
    """How fast a topic is publishing.

    For **ROS2 Topic Monitor** and **Rate Meter**.
    """
    return [protocol.sample("rate", float(hz), "msg/s"), protocol.log(f"{topic} at {hz} Hz", "info")]


def ros2_pose(x: float, y: float, *, yaw: Optional[float] = None) -> List[Event]:
    """A robot pose on its own, for the map overlay to follow.

    Complements **ROS2 Map Viewer** when odometry is linked to it.
    """
    return odometry(x, y, yaw=yaw)


def ros2_twist(linear_x: float, angular_z: float, *, linear_y: float = 0.0) -> List[Event]:
    """A velocity command, as the numbers that were sent.

    For **ROS2 Velocity Controller**. Reported after the fact: this says what the
    controller was told to do, so the log shows the command rather than leaving
    the interface to guess at it.
    """
    return _readings(
        (("cmd linear x", linear_x, "m/s"), ("cmd linear y", linear_y, "m/s"), ("cmd angular z", angular_z, "rad/s"))
    )


def ros2_goal(x: float, y: float, *, frame: str = "map", accepted: bool = True) -> List[Event]:
    """A navigation goal and whether it was accepted.

    For **ROS2 Goal Sender**.
    """
    return [
        protocol.log(f"goal ({x}, {y}) in {frame}: {'accepted' if accepted else 'rejected'}", "info" if accepted else "warn"),
        protocol.status("ok" if accepted else "warn"),
    ]


def ros2_service_response(service: str, *, ok: bool, detail: Optional[str] = None) -> List[Event]:
    """What a service call answered.

    For **ROS2 Service Client** and **ROS2 Action Client**.
    """
    return [
        protocol.log(f"{service}: {detail or ('ok' if ok else 'failed')}", "info" if ok else "error"),
        protocol.status("ok" if ok else "error"),
    ]


# ------------------------------------------------------------------ #
# IoT                                                                 #
# ------------------------------------------------------------------ #


def mqtt_message(
    topic: str, payload: Any, *, value: Optional[float] = None, broker: Optional[str] = None
) -> List[Event]:
    """A message on a topic, and a number pulled out of it if there is one.

    For **MQTT Subscriber**. The payload always appears as a log line; a numeric
    value is reported as a series only when the caller says what it is, rather
    than the library guessing at a number inside prose.
    """
    events: List[Event] = [
        protocol.log(f"{topic}: {payload}" + (f" (from {broker})" if broker else ""), "info")
    ]
    if value is not None:
        events.append(protocol.sample("mqtt", float(value)))
    return events


def http_response(
    value: Optional[float] = None,
    *,
    url: Optional[str] = None,
    status_code: int = 200,
    latency_ms: Optional[float] = None,
    body: Optional[str] = None,
) -> List[Event]:
    """What a poll returned.

    For **HTTP Poller** and **Webhook Receiver**. A non-2xx is an error, so a
    server answering 500 with a plausible number in its body does not read as a
    healthy reading.
    """
    if status_code >= 400:
        return [
            protocol.status("error"),
            protocol.log(f"{url or 'request'} answered {status_code}" + (f": {body}" if body else ""), "error"),
        ]
    events: List[Event] = []
    if value is not None:
        events.append(protocol.sample("poll", float(value)))
    events.extend(_readings((("http latency", latency_ms, "ms"),)))
    events.append(
        protocol.log(
            f"{url or 'request'} answered {status_code}" + (f": {body}" if body else ""), "info"
        )
    )
    return events


def serial_data(data: Union[str, bytes, bytearray], *, decoded: Optional[str] = None) -> List[Event]:
    """Bytes or a line from a serial port.

    For **Serial Monitor**. Bytes that are not printable text are shown as hex,
    because a log line full of control characters helps nobody.
    """
    if decoded is not None:
        return [protocol.log(decoded, "info")]
    if isinstance(data, str):
        return [protocol.log(data, "info")]
    raw = bytes(data)
    try:
        text = raw.decode("utf-8")
        if text.isprintable():
            return [protocol.log(text, "info")]
    except UnicodeDecodeError:
        pass
    return [protocol.log(" ".join(f"{byte:02x}" for byte in raw), "info")]


def gpio_pin(pin: Union[int, str], value: Union[int, bool, float], *, mode: str = "out") -> List[Event]:
    """A pin's state.

    For **GPIO Controller**. A digital pin reports 0 or 1; an analogue pin
    reports whatever it read, which is why the series carries no unit.
    """
    numeric = float(value)
    return [
        protocol.sample(f"gpio {pin}", numeric),
        protocol.log(f"pin {pin} ({mode}) = {value}", "info"),
    ]


def i2c_register(
    address: Union[int, str], register: Union[int, str], value: float, *, bus: Optional[int] = None
) -> List[Event]:
    """A sensor register.

    For **I²C / SPI Sensor**. The series names both the address and the register,
    because one bus carries many devices and "register 0x00" alone says nothing.
    """
    where = f"0x{address:02x}" if isinstance(address, int) else str(address)
    return [
        protocol.sample(f"i2c {where}:{register}", float(value)),
        protocol.log(f"{where} register {register} = {value}" + (f" (bus {bus})" if bus is not None else ""), "info"),
    ]


def modbus_register(
    address: int, value: float, *, unit: int = 1, kind: str = "holding", signed: bool = False
) -> List[Event]:
    """A Modbus register.

    For **Modbus RTU/TCP**. Coils are reported as 0 or 1 like any other reading;
    the kind is named in the log so the log says which register space it was.
    """
    scale = 1.0
    events: List[Event] = [protocol.sample(f"modbus {address}", float(value) * scale)]
    if signed and value < 0:
        events.append(protocol.log(f"register {address} is negative ({value})", "warn"))
    if kind == "coil":
        events.append(protocol.log(f"coil {address} (unit {unit}) = {int(value != 0)}", "info"))
    else:
        events.append(protocol.log(f"{kind} register {address} (unit {unit}) = {value}", "info"))
    return events


def opcua_node(
    node_id: str, value: Any, *, quality: str = "good", timestamp: Optional[str] = None
) -> List[Event]:
    """An OPC-UA node's value and quality.

    For **OPC-UA Client**. A node whose quality is not `good` is reported as a
    warning even when it carries a number, because a stale value presented as
    current is exactly the failure this client exists to avoid.
    """
    events: List[Event] = []
    good = quality.lower() == "good"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        events.append(protocol.sample(f"opcua {node_id}", float(value)))
    elif isinstance(value, bool):
        events.append(protocol.sample(f"opcua {node_id}", 1.0 if value else 0.0))
    events.append(
        protocol.log(
            f"{node_id} = {value} [{quality}]" + (f" at {timestamp}" if timestamp else ""),
            "info" if good else "warn",
        )
    )
    if not good:
        events.append(protocol.status("warn"))
    return events


def can_frame(
    can_id: int,
    data: Union[bytes, bytearray, Sequence[int]],
    *,
    extended: bool = False,
    decoded: Optional[Dict[str, Any]] = None,
) -> List[Event]:
    """A CAN frame.

    For **CAN Bus Monitor**. The identifier and the payload are both reported:
    the id as a series so a bus can be watched for a particular frame, and the
    bytes as hex because that is how every CAN tool prints them. When a DBC has
    been applied, the decoded signals are reported too.
    """
    raw = bytes(data)
    width = 29 if extended else 11
    events: List[Event] = [
        protocol.sample("can id", float(can_id)),
        protocol.log(
            f"0x{can_id:0{4 if not extended else 8}x} [{width}bit] "
            + " ".join(f"{byte:02x}" for byte in raw),
            "info",
        ),
    ]
    for signal, value in (decoded or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            events.append(protocol.sample(f"can {signal}", float(value)))
        else:
            events.append(protocol.log(f"can {signal} = {value}", "info"))
    return events


def zigbee_device(
    device: str, state: Any, *, link_quality: Optional[float] = None, battery: Optional[float] = None
) -> List[Event]:
    """A Zigbee or Z-Wave device's state.

    For **Zigbee/Z-Wave Device**.
    """
    events: List[Event] = [protocol.log(f"{device}: {state}", "info")]
    events.extend(
        _readings((("link quality", link_quality, None), ("battery", battery, "V")))
    )
    if battery is not None and battery < 2.5:
        events.append(protocol.status("warn"))
    return events


def ble_advertisement(
    device: str, *, rssi: Optional[float] = None, payload: Optional[str] = None, address: Optional[str] = None
) -> List[Event]:
    """A BLE advertisement.

    For **BLE Scanner**.
    """
    events: List[Event] = _readings((("ble rssi", rssi, "dBm"),))
    described = f"{device}" + (f" ({address})" if address else "")
    if rssi is not None:
        described += f" rssi {rssi}"
    if payload:
        described += f" · {payload}"
    events.append(protocol.log(described, "info"))
    return events


def gps_fix(
    *,
    latitude: float,
    longitude: float,
    altitude: Optional[float] = None,
    speed: Optional[float] = None,
    course: Optional[float] = None,
    fix_quality: Optional[str] = None,
    satellites: Optional[float] = None,
    hdop: Optional[float] = None,
) -> List[Event]:
    """A position fix.

    For **GPS / GNSS**. No fix is not a position of zero — a device that has not
    acquired should send ``gps_no_fix`` instead, so the map does not place the
    robot in the Atlantic.
    """
    events = _readings(
        (
            ("latitude", latitude, "deg"),
            ("longitude", longitude, "deg"),
            ("altitude", altitude, "m"),
            ("speed", speed, "m/s"),
            ("course", course, "deg"),
            ("satellites", satellites, None),
            ("hdop", hdop, None),
        )
    )
    described = f"{latitude:.6f}, {longitude:.6f}"
    if fix_quality:
        described += f" · {fix_quality}"
    events.append(
        protocol.log(
            described,
            "warn" if fix_quality and fix_quality.lower() not in ("fix", "3d", "rtk") else "info",
        )
    )
    return events


def gps_no_fix(reason: Optional[str] = None) -> List[Event]:
    """No position yet.

    For **GPS / GNSS**. This exists so that "I have no fix" has somewhere to go
    that is not a coordinate — reporting 0,0 would draw the robot off the coast
    of Africa and look like a working sensor.
    """
    return [protocol.status("warn"), protocol.log(reason or "no satellite fix", "warn")]


def imu(
    *,
    accel: Optional[Sequence[float]] = None,
    gyro: Optional[Sequence[float]] = None,
    mag: Optional[Sequence[float]] = None,
    temperature: Optional[float] = None,
) -> List[Event]:
    """Accelerometer, gyroscope and magnetometer.

    For **IMU**. Each axis is its own series, so a plotter can show one of them
    without the other eight getting in the way.
    """
    events: List[Event] = []
    for name, axes, unit in (
        ("accel", accel, "m/s²"),
        ("gyro", gyro, "rad/s"),
        ("mag", mag, "µT"),
    ):
        if axes is None:
            continue
        for axis, value in zip("xyz", axes):
            events.append(protocol.sample(f"{name} {axis}", float(value), unit))
    events.extend(_readings((("imu temperature", temperature, "°C"),)))
    return events


def temperature(
    celsius: float, *, humidity: Optional[float] = None, pressure: Optional[float] = None
) -> List[Event]:
    """A temperature, and whatever else the sensor reads.

    For **Temperature / Humidity**.
    """
    return _readings(
        (("temperature", celsius, "°C"), ("humidity", humidity, "%"), ("pressure", pressure, "hPa"))
    )


def power(
    *,
    voltage: Optional[float] = None,
    current: Optional[float] = None,
    watts: Optional[float] = None,
    energy_wh: Optional[float] = None,
) -> List[Event]:
    """Electrical readings.

    For **Power Monitor**. Watts are computed from voltage and current when they
    are given and watts are not, because that is arithmetic the caller should not
    have to repeat.
    """
    if watts is None and voltage is not None and current is not None:
        watts = voltage * current
    return _readings(
        (
            ("battery", voltage, "V"),
            ("current", current, "A"),
            ("power", watts, "W"),
            ("energy", energy_wh, "Wh"),
        )
    )


def relay(channel: Union[int, str], state: Union[bool, int, str], *, label: Optional[str] = None) -> List[Event]:
    """A relay's state.

    For **Relay Controller**. The reading is 1 or 0 so it plots and threshold-checks
    like anything else, with the channel named in the log.
    """
    if isinstance(state, str):
        closed = state.lower() in ("on", "closed", "1", "true", "energised", "energized")
    else:
        closed = bool(state)
    name = label or f"relay {channel}"
    return [
        protocol.sample(f"relay {channel}", 1.0 if closed else 0.0),
        protocol.log(f"{name} is {'closed' if closed else 'open'}", "info"),
    ]


def pid(
    setpoint: float,
    feedback: float,
    output: float,
    *,
    kp: Optional[float] = None,
    ki: Optional[float] = None,
    kd: Optional[float] = None,
) -> List[Event]:
    """A control loop's state.

    For **PID Controller**. The error is computed here because every operator
    wants it and computing it wrong once is enough.
    """
    events = _readings(
        (
            ("setpoint", setpoint, None),
            ("feedback", feedback, None),
            ("output", output, None),
            ("error", setpoint - feedback, None),
            ("kp", kp, None),
            ("ki", ki, None),
            ("kd", kd, None),
        )
    )
    return events


def docker_containers(containers: Sequence[Dict[str, Any]]) -> List[Event]:
    """The containers on a host.

    For **Docker Monitor**. A container that is not running is a warning, not
    just a line in a list.
    """
    events: List[Event] = [protocol.sample("containers", float(len(containers)))]
    stopped = 0
    for container in containers:
        status = str(container.get("status", "unknown"))
        running = "up" in status.lower()
        if not running:
            stopped += 1
        described = f"{container.get('name', '?')} · {status}"
        if container.get("image"):
            described += f" · {container['image']}"
        events.append(protocol.log(described, "info" if running else "warn"))
    if stopped:
        events.append(protocol.status("warn"))
    return events


def systemd_unit(
    name: str, state: str, *, enabled: Optional[bool] = None, since: Optional[str] = None
) -> List[Event]:
    """A systemd unit's state.

    For **Systemd Service**. `failed` is an error rather than a warning: a unit
    that has failed is the thing this module is watched for.
    """
    lowered = state.lower()
    severity = "error" if "failed" in lowered else "info"
    described = f"{name} is {state}"
    if enabled is not None:
        described += f" ({'enabled' if enabled else 'disabled'})"
    if since:
        described += f" since {since}"
    return [
        protocol.log(described, severity),
        protocol.status("error" if "failed" in lowered else "ok"),
    ]


def can_bus_state(interface: str, *, up: bool = True, errors: int = 0) -> List[Event]:
    """Whether a CAN interface is up, separately from the frames on it.

    For **CAN Bus Monitor**, which otherwise has nothing to say on a quiet bus.
    """
    events: List[Event] = [
        protocol.status("ok" if up else "error"),
        protocol.log(f"{interface} is {'up' if up else 'down'}", "info" if up else "error"),
    ]
    if errors:
        events.append(protocol.sample("can errors", float(errors)))
    return events


# ------------------------------------------------------------------ #
# Analytical                                                          #
# ------------------------------------------------------------------ #
#
# These modules compute server-side from whatever the streams carry, so a device
# normally feeds them through `sample`. The functions below exist because a
# device that already computed one of these locally should be able to report it
# rather than send the raw values and have the server do it again.


def statistics(key: str, **values: Optional[float]) -> List[Event]:
    """Summary statistics for a stream.

    For **Rolling Statistics**, **Histogram** and **Data Table**.
    """
    return _readings((f"{key} {name.replace('_', ' ')}", value, None) for name, value in values.items())


def correlation(a: str, b: str, coefficient: float, *, method: str = "pearson") -> List[Event]:
    """How two streams move together.

    For **Correlation Matrix** and **Scatter Plot**.
    """
    if not -1.0 <= coefficient <= 1.0:
        raise ValueError("a correlation coefficient is between -1 and 1")
    return [
        protocol.sample(f"{a}~{b} {method}", float(coefficient)),
        protocol.log(f"{method} correlation of {a} and {b}: {coefficient:.3f}", "info"),
    ]


def fft_spectrum(frequencies: Sequence[float], magnitudes: Sequence[float], *, source: Optional[str] = None) -> List[Event]:
    """A frequency-domain reading.

    For **FFT Spectrum**. The peak is reported as a series because that is the
    number an operator watches; the whole spectrum goes out as pairs.
    """
    if len(frequencies) != len(magnitudes):
        raise ValueError("frequencies and magnitudes must be the same length")
    events: List[Event] = []
    if magnitudes:
        peak = max(range(len(magnitudes)), key=lambda index: magnitudes[index])
        events.append(protocol.sample("peak frequency", float(frequencies[peak]), "Hz"))
        events.append(protocol.sample("peak magnitude", float(magnitudes[peak])))
    events.append(
        protocol.log(
            f"{len(frequencies)} bins" + (f" from {source}" if source else ""), "info"
        )
    )
    return events


def anomaly(key: str, value: float, *, score: Optional[float] = None, is_anomaly: bool = False, threshold: Optional[float] = None) -> List[Event]:
    """A value flagged as out of the ordinary.

    For **Anomaly Detector**, **Threshold Alarm** and **Event Counter**.
    """
    events: List[Event] = [protocol.sample(key, float(value))]
    if score is not None:
        events.append(protocol.sample(f"{key} z-score", float(score)))
    if is_anomaly:
        described = f"{key} is out of range ({value})"
        if threshold is not None:
            described += f", threshold {threshold}"
        events.append(protocol.log(described, "warn"))
        events.append(protocol.status("warn"))
    return events


def regression(
    x: str, y: str, *, slope: float, intercept: float, r_squared: float, degree: int = 1
) -> List[Event]:
    """A fitted relationship between two streams.

    For **Regression**.
    """
    return [
        protocol.sample(f"{y}~{x} slope", float(slope)),
        protocol.sample(f"{y}~{x} intercept", float(intercept)),
        protocol.sample(f"{y}~{x} r²", float(r_squared)),
        protocol.log(
            f"{y} = {slope:.4g}·{x} + {intercept:.4g} (degree {degree}, r² {r_squared:.3f})",
            "info",
        ),
    ]


def event_count(key: str, count: float) -> List[Event]:
    """How many times something has happened.

    For **Event Counter**.
    """
    return [protocol.sample(key, float(count))]


def rate(key: str, per_second: float) -> List[Event]:
    """How often something is happening.

    For **Rate Meter**.
    """
    return [protocol.sample(key, float(per_second), "msg/s")]


def latency(key: str, milliseconds: float) -> List[Event]:
    """A round-trip time.

    For **Latency Monitor**.
    """
    return [protocol.sample(key, float(milliseconds), "ms")]


def alarm(
    key: str,
    *,
    active: bool,
    value: Optional[float] = None,
    level: str = "lo",
    latched: bool = False,
) -> List[Event]:
    """An alarm's state.

    For **Threshold Alarm**. A latched alarm stays until it is acknowledged, so
    the status does not clear the moment the value wanders back — which is what
    latching is for.
    """
    events: List[Event] = []
    if value is not None:
        events.append(protocol.sample(key, float(value)))
    if active:
        events.append(protocol.log(f"{level} alarm on {key}" + (" (latched)" if latched else ""), "warn"))
        events.append(protocol.status("warn"))
    else:
        events.append(protocol.log(f"{key} back in range", "info"))
        events.append(protocol.status("ok"))
    return events


# ------------------------------------------------------------------ #
# Coverage                                                            #
# ------------------------------------------------------------------ #

#: One builder for every preset a device can feed.
#:
#: `None` marks a preset a device does not feed, and the string says why. Those
#: are honest omissions rather than gaps: a control sends a command rather than
#: capturing data, and an analytical module is computed by the server from the
#: streams it is given.
PRESET_BUILDERS: Dict[str, Optional[Callable[..., List[Event]]]] = {
    # General
    "Kill Switch": kill_switch_outcome,
    "Script Runner": script_output,
    "Health Check": health_check,
    "CLI Terminal": script_output,
    "Data Logger": None,  # Records whatever stream it is pointed at: use `sample`.
    "Tailscale Peer": tailscale_peer,
    "AI API": ai_response,
    "Generic Control": script_output,
    # ROS2
    "ROS2 Topic Subscriber": ros2_message,
    "ROS2 Topic Publisher": None,  # Sends a command; nothing is captured.
    "ROS2 Service Client": ros2_service_response,
    "ROS2 Action Client": ros2_service_response,
    "ROS2 Node Monitor": ros2_nodes,
    "ROS2 Topic Monitor": ros2_topics,
    "ROS2 Parameter Server": ros2_parameter,
    "ROS2 TF Monitor": ros2_transforms,
    "ROS2 Bag Recorder": ros2_bag_recording,
    "ROS2 Bag Player": ros2_bag_playback,
    "ROS2 Diagnostics": ros2_diagnostics,
    "ROS2 Image Stream": image_frame,
    "ROS2 Pointcloud Viewer": point_cloud,
    "ROS2 Laser Scan": laser_scan,
    "ROS2 Odometry": odometry,
    "ROS2 Velocity Controller": ros2_twist,
    "ROS2 Joint State": joint_state,
    "ROS2 Map Viewer": occupancy_grid,
    "ROS2 Goal Sender": ros2_goal,
    "ROS2 Lifecycle Manager": ros2_lifecycle,
    # IoT
    "MQTT Subscriber": mqtt_message,
    "MQTT Publisher": None,  # Sends a command; nothing is captured.
    "HTTP Poller": http_response,
    "Webhook Receiver": http_response,
    "Serial Monitor": serial_data,
    "GPIO Controller": gpio_pin,
    "I²C / SPI Sensor": i2c_register,
    "Modbus RTU/TCP": modbus_register,
    "OPC-UA Client": opcua_node,
    "CAN Bus Monitor": can_frame,
    "Zigbee/Z-Wave Device": zigbee_device,
    "BLE Scanner": ble_advertisement,
    "GPS / GNSS": gps_fix,
    "IMU": imu,
    "Temperature / Humidity": temperature,
    "Power Monitor": power,
    "Camera Capture": image_frame,
    "Relay Controller": relay,
    "PID Controller": pid,
    "Docker Monitor": docker_containers,
    "Systemd Service": systemd_unit,
    # Analytical
    "Live Plotter": None,  # Draws a stream: use `sample`.
    "Gauge": None,  # Draws a value: use `sample`.
    "Stat Tile": None,  # Draws a value: use `sample`.
    "Histogram": statistics,
    "Scatter Plot": correlation,
    "Heatmap": None,  # Buckets a stream server-side: use `sample`.
    "Correlation Matrix": correlation,
    "FFT Spectrum": fft_spectrum,
    "Anomaly Detector": anomaly,
    "Rolling Statistics": statistics,
    "Regression": regression,
    "Event Counter": event_count,
    "Rate Meter": rate,
    "Latency Monitor": latency,
    "Threshold Alarm": alarm,
    "Data Table": statistics,
    "Comparison View": None,  # Compares two sessions of one stream: use `sample`.
    "Report Generator": None,  # Compiles the session's own log; nothing to send.
}

#: Presets a device never feeds, and why. Kept beside the map so the reason for
#: each omission is visible rather than inferred from a `None`.
NON_CAPTURING: Dict[str, str] = {
    name: reason
    for name, reason in {
        "Data Logger": "records a stream it is pointed at",
        "ROS2 Topic Publisher": "sends a command",
        "MQTT Publisher": "sends a command",
        "Live Plotter": "draws a stream",
        "Gauge": "draws a value",
        "Stat Tile": "draws a value",
        "Heatmap": "buckets a stream server-side",
        "Comparison View": "compares two sessions",
        "Report Generator": "compiles the session log",
    }.items()
}


def builder_for(preset: str) -> Optional[Callable[..., List[Event]]]:
    """The function that feeds a preset, or None when a device never feeds it."""
    return PRESET_BUILDERS.get(preset)


def uncovered() -> List[str]:
    """Catalogue presets with no entry at all — the check that keeps this honest.

    An empty list means every preset in the catalogue is either fed by a function
    or explicitly recorded as not fed. `tests/test_data.py` asserts this.
    """
    return [name for _cat, name in PRESETS if name not in PRESET_BUILDERS]


# ------------------------------------------------------------------ #
# Internals                                                           #
# ------------------------------------------------------------------ #


def _readings(pairs: Iterable[tuple]) -> List[Event]:
    """Turn ``(key, value, unit)`` triples into samples, skipping the missing.

    A field the device could not read is passed as None and simply not reported:
    a placeholder would be a number nobody measured, which is the thing this
    whole library is arranged to avoid.
    """
    events: List[Event] = []
    for key, value, unit in pairs:
        if value is None:
            continue
        events.append(protocol.sample(key, float(value), unit))
    return events
