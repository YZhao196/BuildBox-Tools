"""Readers for things a device actually measures.

Each function here returns a **reader** — a callable with no arguments — which
you hand to `Client.sensor`:

    from buildbox import Client, sensors

    bb = Client()
    bb.sensor("temperature", sensors.file_number(THERMAL, scale=1e-3), unit="C")
    bb.sensor("pressure", sensors.serial_line("/dev/ttyUSB0", pattern=r"P=([\\d.]+)"), unit="hPa")
    bb.run()

The two rules the rest of this library is built on apply here, and they are the
reason these exist rather than leaving you to write the plumbing:

* **A reader that cannot read raises.** It does not return a plausible number.
  The sampler turns the exception into a log line naming what went wrong, so a
  sensor that is unplugged shows as unplugged rather than as a steady zero.
* **A reader with nothing to report returns None.** A sensor waiting for a
  satellite fix and a sensor that is broken are different, and only one of them
  is a problem.

Ordinary Python covers most cases without any of this — a value off an object, a
library call, a computation:

    bb.sensor("level", lambda: tank.level, unit="m")
    bb.sensor("cpu", lambda: psutil.cpu_percent(), unit="%")
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

__all__ = ["SensorError", "file_number", "serial_line"]

#: A callable the sampler can call to get a reading.
Reader = Callable[[], Optional[float]]


class SensorError(RuntimeError):
    """A reader could not produce a reading, with a reason worth reading."""


def file_number(
    path: str,
    *,
    scale: float = 1.0,
    offset: float = 0.0,
    pattern: Optional[str] = None,
    encoding: str = "utf-8",
) -> Reader:
    """Read a number out of a file.

    This is not a contrivance — on a Raspberry Pi or a Jetson it is how a great
    deal of hardware is read. The kernel exposes temperature, voltage and
    current as plain files under `/sys`:

        /sys/class/thermal/thermal_zone0/temp        millidegrees C
        /sys/class/power_supply/BAT0/voltage_now     microvolts
        /sys/class/hwmon/hwmon0/fan1_input           RPM

    So a temperature sensor on a Pi is:

        sensors.file_number("/sys/class/thermal/thermal_zone0/temp", scale=1e-3)

    `scale` and `offset` are applied as `value * scale + offset`, because those
    files report in the smallest unit rather than the useful one.

    `pattern` takes the first capturing group out of the contents when the file
    has text around the number. Without it, the whole file must be a number.
    """

    def read() -> float:
        try:
            text = Path(path).read_text(encoding=encoding).strip()
        except OSError as error:
            raise SensorError(f"{path} could not be read: {error}") from None

        if pattern is not None:
            found = re.search(pattern, text)
            if found is None:
                raise SensorError(f"{path} did not match {pattern!r}: {text!r}")
            text = found.group(1) if found.groups() else found.group(0)

        try:
            return float(text) * scale + offset
        except ValueError:
            raise SensorError(f"{path} did not contain a number: {text!r}") from None

    read.__name__ = f"file_number({path})"
    return read


class SerialReader:
    """Reads one line at a time from a serial port.

    The port is opened on the first read and kept open, because reopening it
    every second is how you lose the first byte of every line. A read that fails
    closes the port so the next one reconnects — a USB adapter that was
    unplugged and plugged back in recovers without restarting the device.

    A read that times out with nothing to say returns `None`. An idle port is
    not a broken port, and reporting a zero for silence would put a number on
    the canvas that nothing measured.
    """

    def __init__(
        self,
        port: str,
        *,
        baudrate: int = 9600,
        timeout: float = 1.0,
        pattern: Optional[str] = None,
        encoding: str = "utf-8",
    ) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.pattern = pattern
        self.encoding = encoding
        self._handle = None

    def open(self) -> None:
        try:
            import serial  # type: ignore[import-not-found]
        except ImportError:
            raise SensorError(
                "pyserial is not installed. Install it with: pip install \"./python[drivers]\""
            ) from None
        try:
            self._handle = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
        except Exception as error:  # noqa: BLE001 - serial raises a wide range
            raise SensorError(f"{self.port} could not be opened: {error}") from None

    def close(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.close()
        except Exception:  # noqa: BLE001 - closing a dead port
            pass
        self._handle = None

    def __call__(self) -> Optional[float]:
        if self._handle is None:
            self.open()
        try:
            raw = self._handle.readline()
        except Exception as error:  # noqa: BLE001
            # Drop the handle so the next read reconnects rather than failing
            # forever on a port that has gone away.
            self.close()
            raise SensorError(f"{self.port} could not be read: {error}") from None

        if not raw:
            return None

        text = raw.decode(self.encoding, errors="replace").strip()
        if self.pattern is not None:
            found = re.search(self.pattern, text)
            if found is None:
                raise SensorError(f"{self.port} line did not match {self.pattern!r}: {text!r}")
            text = found.group(1) if found.groups() else found.group(0)

        try:
            return float(text)
        except ValueError:
            raise SensorError(f"{self.port} line was not a number: {text!r}") from None

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<SerialReader {self.port} at {self.baudrate}>"


def serial_line(
    port: str,
    *,
    baudrate: int = 9600,
    timeout: float = 1.0,
    pattern: Optional[str] = None,
) -> Reader:
    """A reader for a sensor that prints a line per reading over serial.

    Most hobby sensors do: a GPS speaks NMEA sentences, a scale prints a weight,
    an Arduino prints whatever it was told to. `pattern` picks the number out of
    the line when it is not the whole thing.
    """
    return SerialReader(port, baudrate=baudrate, timeout=timeout, pattern=pattern)
