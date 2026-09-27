"""Readers for the buses a robot actually keeps its readings on.

`sensors.py` covers what a machine reads without help — a file the kernel
publishes, a line a sensor prints. This module covers the buses: CAN, I²C, SPI,
GPIO and ROS2. On a robot that is where most of the numbers live, and it is the
part a device author would otherwise have to write themselves.

Every reader follows the contract `sensors.py` sets out, and it matters more
rather than less out here:

* **A reader that cannot read raises.** An interface that is down, a device that
  does not answer, a bus that will not open — each is a `SensorError` naming what
  went wrong. Nothing here returns a plausible number to fill the gap.
* **A reader with nothing to report returns None.** A CAN bus carrying no traffic
  and a CAN bus with no interface are different things, and only the second is a
  fault. Silence is reported as silence.

No hardware library is imported when this module is imported. Each is imported on
first use, so a device that only reads a file never needs `python-can`, and a
missing one produces the install command rather than a traceback.

    from buildbox import Client, buses

    bb = Client()
    bb.sensor("wheel", buses.can_signal("can0", 0x123, start=2, length=2, scale=0.01),
              unit="km/h", every=0.1)
    bb.sensor("flow", buses.gpio_pulses(17), unit="Hz", every=1.0)
    bb.sensor("battery", buses.ros2_topic("/battery/state", "percentage"), unit="%")
    bb.run()

Two of these return a *structural* reading rather than a number —
`ros2_laser_scan` and `ros2_joint_state` — because a laser sweep is not a series
of samples. See `Client.sensor` for what a reader may return.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import data
from .sensors import Reader, SensorError

__all__ = [
    "can_signal",
    "can_frames",
    "i2c_register",
    "spi_block",
    "gpio_line",
    "gpio_pulses",
    "ros2_topic",
    "ros2_laser_scan",
    "ros2_joint_state",
]

#: What every reader says when the library it needs is not installed.
#:
#: The extra is named rather than the individual package, because a device that
#: wants one bus usually wants its neighbours too and the extra installs all of
#: them. The package name follows, so the message is still actionable when the
#: device is on an architecture the extra does not cover.
_INSTALL = 'pip install "./python[drivers]"'


def _decode(
    block: Sequence[int],
    *,
    length: int,
    byteorder: str,
    signed: bool,
    scale: float,
    offset: float,
) -> float:
    """Turn a block of bytes into the number the device meant.

    Separate from the readers so it can be tested without a bus: this is where a
    wrong byte order or a wrong width turns into a number that looks plausible
    and is not, which is the failure worth a test rather than a comment.
    """
    if len(block) != length:
        raise SensorError(f"Expected {length} bytes, got {len(block)}.")
    return int.from_bytes(bytes(block), byteorder, signed=signed) * scale + offset


class _Handle:
    """A connection opened on first use, kept open, and dropped when it breaks.

    Reopening a bus for every reading is how you lose the first frame of every
    batch, and a board that was unplugged and plugged back in should recover
    without restarting the device — so the handle is kept, and a failed read
    closes it so the next one reconnects.
    """

    def __init__(self) -> None:
        self._open: Any = None

    def _connect(self) -> Any:  # pragma: no cover - each subclass overrides
        raise NotImplementedError

    def _disconnect(self, handle: Any) -> None:  # pragma: no cover - optional
        pass

    def handle(self) -> Any:
        if self._open is None:
            self._open = self._connect()
        return self._open

    def drop(self) -> None:
        handle, self._open = self._open, None
        if handle is None:
            return
        try:
            self._disconnect(handle)
        except Exception:  # noqa: BLE001 - closing a bus that has gone away
            pass


# ------------------------------------------------------------------ #
# CAN                                                                 #
# ------------------------------------------------------------------ #


def _can_bus(channel: str, interface: str) -> Any:
    try:
        import can  # type: ignore[import-not-found]
    except ImportError:
        raise SensorError(
            f"python-can is not installed, so {channel} cannot be read. "
            f"Install it with: {_INSTALL}"
        ) from None
    try:
        return can.Bus(channel=channel, interface=interface)
    except Exception as error:  # noqa: BLE001 - python-can raises a wide range
        raise SensorError(f"{channel} could not be opened: {error}") from None


class _CanSignal(_Handle):
    """One signal, decoded out of the frames on a CAN bus."""

    def __init__(
        self,
        channel: str,
        can_id: int,
        *,
        start: int,
        length: int,
        scale: float,
        offset: float,
        byteorder: str,
        signed: bool,
        interface: str,
        timeout: float,
    ) -> None:
        super().__init__()
        self.channel = channel
        self.can_id = can_id
        self.start = start
        self.length = length
        self.scale = scale
        self.offset = offset
        self.byteorder = byteorder
        self.signed = signed
        self.interface = interface
        self.timeout = timeout

    def _connect(self) -> Any:
        return _can_bus(self.channel, self.interface)

    def _disconnect(self, handle: Any) -> None:
        handle.shutdown()

    def __call__(self) -> Optional[float]:
        bus = self.handle()
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Nothing arrived in the window. The bus is idle, which is not a
                # fault — a robot between moves sends no frames, and reporting a
                # zero for that would put a number on the canvas nothing measured.
                return None
            try:
                message = bus.recv(timeout=remaining)
            except Exception as error:  # noqa: BLE001
                self.drop()
                raise SensorError(f"{self.channel} could not be read: {error}") from None
            if message is None:
                return None
            if message.arbitration_id != self.can_id:
                continue
            return _decode(
                bytes(message.data)[self.start : self.start + self.length],
                length=self.length,
                byteorder=self.byteorder,
                signed=self.signed,
                scale=self.scale,
                offset=self.offset,
            )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<can_signal {self.channel} id={self.can_id:#x}>"


class _CanFrames(_Handle):
    """How busy the bus is: frames seen per second over a window."""

    def __init__(self, channel: str, *, interface: str, window: float) -> None:
        super().__init__()
        self.channel = channel
        self.interface = interface
        self.window = window

    def _connect(self) -> Any:
        return _can_bus(self.channel, self.interface)

    def _disconnect(self, handle: Any) -> None:
        handle.shutdown()

    def __call__(self) -> float:
        bus = self.handle()
        deadline = time.monotonic() + self.window
        count = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                if bus.recv(timeout=remaining) is None:
                    break
            except Exception as error:  # noqa: BLE001
                self.drop()
                raise SensorError(f"{self.channel} could not be read: {error}") from None
            count += 1
        # Zero is a measurement here, unlike the reader above: the window was
        # watched and nothing arrived, so the bus really is quiet.
        return count / self.window


def can_signal(
    channel: str,
    can_id: int,
    *,
    start: int = 0,
    length: int = 2,
    scale: float = 1.0,
    offset: float = 0.0,
    byteorder: str = "big",
    signed: bool = False,
    interface: str = "socketcan",
    timeout: float = 2.0,
) -> Reader:
    """One signal out of the frames carrying a given arbitration id.

    This is the reading a CAN integration actually wants, and it is the part that
    is easy to get quietly wrong: a signal is a run of bytes at an offset, and
    the wrong byte order or width produces a number that looks plausible and is
    not. `scale` and `offset` are applied as `value * scale + offset`, which is
    how a raw count becomes the unit on the datasheet.

    Frames with any other arbitration id are skipped rather than counted, so a
    busy bus does not turn into a wrong reading. A window with no matching frame
    returns `None` — see `_CanSignal.__call__`.

    `interface` is python-can's, so `"socketcan"` on Linux and `"virtual"` in a
    test; the same reader drives both, which is what makes this one testable
    without a vehicle.
    """
    # Built once, not per read: the bus is opened on the first reading and kept,
    # and a reader that rebuilt this each time would reopen the interface every
    # sample and drop whatever arrived while it was closed.
    reader = _CanSignal(
        channel,
        can_id,
        start=start,
        length=length,
        scale=scale,
        offset=offset,
        byteorder=byteorder,
        signed=signed,
        interface=interface,
        timeout=timeout,
    )

    def read() -> Optional[float]:
        return reader()

    read.__name__ = f"can_signal({channel}, {can_id:#x})"
    return read


def can_frames(
    channel: str,
    *,
    interface: str = "socketcan",
    window: float = 1.0,
) -> Reader:
    """Frames per second on a bus — how loaded it is, or whether it is alive.

    Counts everything, whatever the arbitration id, because the question here is
    about the bus rather than any one signal on it.
    """
    reader = _CanFrames(channel, interface=interface, window=max(window, 0.01))

    def read() -> float:
        return reader()

    read.__name__ = f"can_frames({channel})"
    return read


# ------------------------------------------------------------------ #
# I²C and SPI                                                         #
# ------------------------------------------------------------------ #


class _I2cRegister(_Handle):
    """A register block read from an I²C sensor."""

    def __init__(
        self,
        bus: int,
        address: int,
        register: int,
        *,
        length: int,
        scale: float,
        offset: float,
        byteorder: str,
        signed: bool,
    ) -> None:
        super().__init__()
        self.bus_number = bus
        self.address = address
        self.register = register
        self.length = length
        self.scale = scale
        self.offset = offset
        self.byteorder = byteorder
        self.signed = signed

    def _connect(self) -> Any:
        try:
            import smbus2  # type: ignore[import-not-found]
        except ImportError:
            raise SensorError(
                "smbus2 is not installed, so the I²C bus cannot be read. "
                f"Install it with: {_INSTALL}"
            ) from None
        try:
            return smbus2.SMBus(self.bus_number)
        except Exception as error:  # noqa: BLE001
            raise SensorError(f"i2c-{self.bus_number} could not be opened: {error}") from None

    def _disconnect(self, handle: Any) -> None:
        handle.close()

    def __call__(self) -> float:
        handle = self.handle()
        try:
            block = handle.read_i2c_block_data(self.address, self.register, self.length)
        except Exception as error:  # noqa: BLE001
            self.drop()
            raise SensorError(
                f"register {self.register:#x} at {self.address:#x} on i2c-{self.bus_number} "
                f"could not be read: {error}"
            ) from None
        return _decode(
            block,
            length=self.length,
            byteorder=self.byteorder,
            signed=self.signed,
            scale=self.scale,
            offset=self.offset,
        )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<i2c_register {self.address:#x}/{self.register:#x}>"


class _SpiBlock(_Handle):
    """A block read from an SPI device."""

    def __init__(
        self,
        bus: int,
        device: int,
        *,
        length: int,
        speed_hz: int,
        scale: float,
        offset: float,
        byteorder: str,
        signed: bool,
    ) -> None:
        super().__init__()
        self.bus_number = bus
        self.device = device
        self.length = length
        self.speed_hz = speed_hz
        self.scale = scale
        self.offset = offset
        self.byteorder = byteorder
        self.signed = signed

    def _connect(self) -> Any:
        try:
            import spidev  # type: ignore[import-not-found]
        except ImportError:
            raise SensorError(
                "spidev is not installed, so the SPI bus cannot be read. "
                f"Install it with: {_INSTALL}"
            ) from None
        handle = spidev.SpiDev()
        try:
            handle.open(self.bus_number, self.device)
            handle.max_speed_hz = self.speed_hz
        except Exception as error:  # noqa: BLE001
            handle.close()
            raise SensorError(
                f"spi{self.bus_number}.{self.device} could not be opened: {error}"
            ) from None
        return handle

    def _disconnect(self, handle: Any) -> None:
        handle.close()

    def __call__(self) -> float:
        handle = self.handle()
        try:
            block = handle.readbytes(self.length)
        except Exception as error:  # noqa: BLE001
            self.drop()
            raise SensorError(
                f"spi{self.bus_number}.{self.device} could not be read: {error}"
            ) from None
        return _decode(
            block,
            length=self.length,
            byteorder=self.byteorder,
            signed=self.signed,
            scale=self.scale,
            offset=self.offset,
        )


def i2c_register(
    bus: int,
    address: int,
    register: int,
    *,
    length: int = 2,
    scale: float = 1.0,
    offset: float = 0.0,
    byteorder: str = "big",
    signed: bool = False,
) -> Reader:
    """A register block read from an I²C sensor.

    The shape every datasheet describes: an address on the bus, a register
    inside the device, and a run of bytes that means a number once you know its
    width and order. `SHT31`'s temperature, for instance, is two big-endian bytes
    at register `0x00` that need the scale the datasheet supplies.
    """
    reader = _I2cRegister(
        bus,
        address,
        register,
        length=length,
        scale=scale,
        offset=offset,
        byteorder=byteorder,
        signed=signed,
    )

    def read() -> float:
        return reader()

    read.__name__ = f"i2c_register(i2c-{bus}, {address:#x}, {register:#x})"
    return read


def spi_block(
    bus: int,
    device: int,
    *,
    length: int = 2,
    speed_hz: int = 1_000_000,
    scale: float = 1.0,
    offset: float = 0.0,
    byteorder: str = "big",
    signed: bool = False,
) -> Reader:
    """A block read from an SPI device.

    SPI has no register addressing of its own — a device defines its own protocol
    on the wire, and the common case is a status byte followed by the reading.
    This reads `length` bytes as one number, which covers the sensors that simply
    stream their value.
    """
    reader = _SpiBlock(
        bus,
        device,
        length=length,
        speed_hz=speed_hz,
        scale=scale,
        offset=offset,
        byteorder=byteorder,
        signed=signed,
    )

    def read() -> float:
        return reader()

    read.__name__ = f"spi_block(spi{bus}.{device})"
    return read


# ------------------------------------------------------------------ #
# GPIO                                                                #
# ------------------------------------------------------------------ #


def _gpio_module() -> Any:
    """The GPIO library for the board this is running on, or a reason there is none.

    Two names because two boards: `RPi.GPIO` on a Raspberry Pi, `Jetson.GPIO` on
    a Jetson, and they share an API. Neither is installed on a machine that has
    no header, which is the common case and not an error worth a traceback.

    `importlib.import_module` rather than `__import__`, which for a dotted name
    hands back the top-level package — so the constants and functions would be
    missing from the object returned, and every pin would fail with an
    AttributeError instead of a reading.
    """
    import importlib

    for name in ("RPi.GPIO", "Jetson.GPIO"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise SensorError(
        "No GPIO library is installed, so this pin cannot be read. Install "
        f"RPi.GPIO or Jetson.GPIO, or read the kernel's own "
        f'"/sys/class/gpio/gpioN/value" with sensors.file_number instead.'
    )


class _GpioLine:
    """One pin, read as a level."""

    def __init__(self, pin: int, *, pull: Optional[str], active_low: bool, bounce_ms: int) -> None:
        self.pin = pin
        self.pull = pull
        self.active_low = active_low
        self.bounce_ms = bounce_ms
        self._ready = False

    def _prepare(self, gpio: Any) -> None:
        gpio.setmode(gpio.BCM)
        gpio.setwarnings(False)
        args: Dict[str, Any] = {"direction": gpio.IN}
        if self.pull is not None:
            args["pull_up_down"] = {"up": gpio.PUD_UP, "down": gpio.PUD_DOWN}[self.pull]
        if self.bounce_ms:
            args["bouncetime"] = self.bounce_ms
        gpio.setup(self.pin, **args)
        self._ready = True

    def __call__(self) -> float:
        gpio = _gpio_module()
        if not self._ready:
            try:
                self._prepare(gpio)
            except Exception as error:  # noqa: BLE001
                raise SensorError(f"GPIO {self.pin} could not be set up: {error}") from None
        try:
            level = gpio.input(self.pin)
        except Exception as error:  # noqa: BLE001
            self._ready = False
            raise SensorError(f"GPIO {self.pin} could not be read: {error}") from None
        if self.active_low:
            level = not level
        return float(level)


class _GpioPulses:
    """One pin, counted as a rate — what an encoder or a flow meter sends."""

    def __init__(self, pin: int, *, pull: Optional[str], window: float, edge: str) -> None:
        self.pin = pin
        self.pull = pull
        self.window = window
        self.edge = edge
        self._count = 0
        self._ready = False
        self._lock = threading.Lock()

    def _bump(self, _channel: Any) -> None:
        with self._lock:
            self._count += 1

    def _prepare(self, gpio: Any) -> None:
        gpio.setmode(gpio.BCM)
        gpio.setwarnings(False)
        args: Dict[str, Any] = {"direction": gpio.IN}
        if self.pull is not None:
            args["pull_up_down"] = {"up": gpio.PUD_UP, "down": gpio.PUD_DOWN}[self.pull]
        gpio.setup(self.pin, **args)
        trigger = gpio.RISING if self.edge == "rising" else gpio.FALLING
        gpio.add_event_detect(self.pin, trigger, callback=self._bump)
        self._ready = True

    def __call__(self) -> float:
        gpio = _gpio_module()
        # Zeroed before the pin is set up, not after: edges that arrive while it
        # is being configured are part of this window, and resetting afterwards
        # would count them and then throw them away.
        with self._lock:
            self._count = 0

        if not self._ready:
            try:
                self._prepare(gpio)
            except Exception as error:  # noqa: BLE001
                # A pin that cannot be watched is reported once, and the next
                # reading tries again rather than counting nothing forever.
                self._ready = False
                raise SensorError(
                    f"GPIO {self.pin} could not be watched for edges: {error}"
                ) from None

        time.sleep(self.window)
        with self._lock:
            counted = self._count
        # Zero is a real answer: an encoder that is not turning really is at rest.
        return counted / self.window

    def stop(self) -> None:
        """Stop watching the pin. Worth calling before a device shuts down."""
        if not self._ready:
            return
        try:
            _gpio_module().remove_event_detect(self.pin)
        except Exception:  # noqa: BLE001 - already gone
            pass
        self._ready = False


def gpio_line(
    pin: int,
    *,
    pull: Optional[str] = None,
    active_low: bool = False,
    bounce_ms: int = 0,
) -> Reader:
    """A GPIO pin read as a level: `0.0` or `1.0`.

    `pull` is `"up"` or `"down"` for a pin that would otherwise float, which is
    most of them. `active_low` inverts the reading for wiring that grounds the
    signal to mean "on", so the number on the canvas means what the schematic
    means rather than what the pin happens to do.

    On a board where the kernel already exposes the pin, `sensors.file_number`
    needs none of this — this exists for the export, direction and pull-up that
    would otherwise be done by hand.
    """
    reader = _GpioLine(pin, pull=pull, active_low=active_low, bounce_ms=bounce_ms)

    def read() -> float:
        return reader()

    read.__name__ = f"gpio_line({pin})"
    return read


def gpio_pulses(
    pin: int,
    *,
    pull: Optional[str] = "up",
    window: float = 1.0,
    edge: str = "rising",
) -> Reader:
    """Count edges on a pin and report them per second.

    This is how a robot reads a wheel encoder, a flow meter or an anemometer —
    things that do not measure a level but produce a frequency. `window` is how
    long each reading counts for, so it sets both the resolution and the lag.

    A pin that is not receiving edges reports `0.0`, which is a measurement: an
    encoder at rest really is at rest.
    """
    reader = _GpioPulses(pin, pull=pull, window=max(window, 0.01), edge=edge)

    def read() -> float:
        return reader()

    read.__name__ = f"gpio_pulses({pin})"
    read.stop = reader.stop  # type: ignore[attr-defined]
    return read


# ------------------------------------------------------------------ #
# ROS2                                                                #
# ------------------------------------------------------------------ #


class _Ros2Node:
    """One rclpy node, spun on its own thread, holding the latest message per topic.

    rclpy wants a node and a spinning executor before any subscription delivers
    anything, and a reader that called `rclpy.init()` on every sample would
    re-initialise the runtime once a second. So the node is built once, on first
    use, and lives for the life of the process.
    """

    def __init__(self, node_name: str, timeout: float) -> None:
        self.node_name = node_name
        self.timeout = timeout
        self._node: Any = None
        self._executor: Any = None
        self._latest: Dict[str, Any] = {}
        self._types: Dict[str, str] = {}
        self._lock = threading.Lock()

    def _rclpy(self) -> Any:
        try:
            import rclpy  # type: ignore[import-not-found]
        except ImportError:
            raise SensorError(
                "rclpy is not installed, so no ROS2 topic can be read. It comes "
                "with a ROS2 distribution rather than from pip — source your "
                "ROS2 setup, then run this again."
            ) from None
        return rclpy

    def _start(self) -> None:
        rclpy = self._rclpy()
        if not rclpy.ok():
            rclpy.init()
        self._node = rclpy.create_node(self.node_name)

        from rclpy.executors import SingleThreadedExecutor  # type: ignore[import-not-found]

        self._executor = SingleThreadedExecutor()
        self._executor.add_node(self._node)
        thread = threading.Thread(target=self._executor.spin, daemon=True)
        thread.start()

    def node(self) -> Any:
        if self._node is None:
            self._start()
        return self._node

    def subscribe(self, topic: str, message_type: Any, key: str) -> None:
        node = self.node()

        def remember(message: Any) -> None:
            with self._lock:
                self._latest[key] = message

        node.create_subscription(message_type, topic, remember, 10)

    def type_of(self, topic: str) -> Optional[str]:
        """The message type published on a topic, or None if nothing is on it."""
        node = self.node()
        for name, types in node.get_topic_names_and_types():
            if name == topic:
                return types[0] if types else None
        return None

    def latest(self, key: str) -> Any:
        with self._lock:
            return self._latest.get(key)

    def wait_for_first(self, key: str) -> bool:
        """Block until a message has arrived, up to the timeout."""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.latest(key) is not None:
                return True
            time.sleep(0.02)
        return False


def _resolve_type(node: _Ros2Node, topic: str, given: Optional[str]) -> Any:
    """The Python class for a message type, imported from its ROS2 name."""
    name = given or node.type_of(topic)
    if not name:
        raise SensorError(
            f"Nothing is publishing on {topic} yet, so its message type is "
            "unknown. Pass message_type= to name it."
        )
    try:
        package, _, kind = name.partition("/")
        module_name = "".join(
            f"_{character.lower()}" if character.isupper() else character
            for character in kind
        ).lstrip("_")
        module = __import__(f"{package}.msg", fromlist=[kind])
        return getattr(module, kind)
    except Exception as error:  # noqa: BLE001
        raise SensorError(f"{name} could not be imported: {error}") from None


def _field_of(message: Any, field: Optional[str]) -> Any:
    """Follow a dotted path into a message, the way a ROS2 field is named."""
    value = message
    for part in (field or "").split("."):
        if not part:
            continue
        try:
            value = getattr(value, part)
        except AttributeError:
            raise SensorError(f"{part!r} is not a field of this message.") from None
    return value


def ros2_topic(
    topic: str,
    field: Optional[str] = None,
    *,
    message_type: Optional[str] = None,
    node_name: str = "buildbox_sensor",
    timeout: float = 5.0,
) -> Reader:
    """Read a numeric field from the latest message on a ROS2 topic.

    `field` is a dotted path into the message — `"temperature"`,
    `"twist.linear.x"` — and leaving it off reads the message itself, which works
    for the plain std_msgs types.

    Returns `None` until the first message arrives: a topic with no publisher yet
    is a topic waiting for one, not a broken sensor, and the two must not look
    alike. After that it returns the most recent value, which is what a topic
    carrying state rather than a stream actually means.

    `message_type` is unnecessary when something is already publishing — the
    type is read from the graph — and required when this reader starts before the
    publisher does.
    """
    node = _Ros2Node(node_name, timeout)
    key = f"{topic}|{field or ''}"
    subscribed = False

    def read() -> Optional[float]:
        nonlocal subscribed
        if not subscribed:
            message_class = _resolve_type(node, topic, message_type)
            node.subscribe(topic, message_class, key)
            subscribed = True
            if not node.wait_for_first(key):
                # Nothing published within the timeout. Not a failure: the
                # publisher may simply not be up yet, and the next tick asks
                # again rather than reporting a number nobody sent.
                return None
        message = node.latest(key)
        if message is None:
            return None
        value = _field_of(message, field)
        try:
            return float(value)
        except (TypeError, ValueError):
            raise SensorError(
                f"{topic}{'.' + field if field else ''} is not a number "
                f"({type(value).__name__}); use ros2_laser_scan or ros2_joint_state "
                "for the structural messages."
            ) from None

    read.__name__ = f"ros2_topic({topic})"
    return read


class _Ros2Shape(_Handle):
    """A structural ROS2 message, turned into the events the canvas draws."""

    def __init__(
        self,
        topic: str,
        build: Callable[[Any], Sequence[Dict[str, Any]]],
        *,
        message_type: Optional[str],
        node_name: str,
        timeout: float,
    ) -> None:
        super().__init__()
        self.topic = topic
        self.build = build
        self.message_type = message_type
        self._node = _Ros2Node(node_name, timeout)
        self._key = topic

    def _connect(self) -> Any:
        message_class = _resolve_type(self._node, self.topic, self.message_type)
        self._node.subscribe(self.topic, message_class, self._key)
        if not self._node.wait_for_first(self._key):
            return None
        return self._node

    def __call__(self) -> Sequence[Dict[str, Any]]:
        if self._open is None:
            self._open = self._connect()
        if self._open is None:
            return []
        message = self._node.latest(self._key)
        if message is None:
            return []
        return list(self.build(message))


def ros2_laser_scan(
    topic: str,
    *,
    message_type: str = "sensor_msgs/LaserScan",
    node_name: str = "buildbox_scan",
    timeout: float = 5.0,
) -> Reader:
    """A `sensor_msgs/LaserScan` as the structural reading the canvas draws.

    A sweep is not a series of samples — the order of the ranges and the angles
    they span are the reading, and a plot of them as separate series would be a
    different and worse thing. So this returns the shape `data.laser_scan` builds,
    which `Client.sensor` sends as a structural reading.
    """
    reader = _Ros2Shape(
        topic,
        lambda message: data.laser_scan(
            list(message.ranges),
            float(message.angle_min),
            float(message.angle_max),
        ),
        message_type=message_type,
        node_name=node_name,
        timeout=timeout,
    )

    def read() -> Any:
        return reader()

    read.__name__ = f"ros2_laser_scan({topic})"
    return read


def ros2_joint_state(
    topic: str,
    *,
    message_type: str = "sensor_msgs/JointState",
    node_name: str = "buildbox_joints",
    timeout: float = 5.0,
) -> Reader:
    """A `sensor_msgs/JointState` as per-joint positions with their names.

    The names matter: the canvas draws one bar per joint, and a position with no
    name beside it is a number an operator cannot act on.
    """
    reader = _Ros2Shape(
        topic,
        lambda message: data.joint_state(
            [float(position) for position in message.position],
            names=list(message.name),
        ),
        message_type=message_type,
        node_name=node_name,
        timeout=timeout,
    )

    def read() -> Any:
        return reader()

    read.__name__ = f"ros2_joint_state({topic})"
    return read
