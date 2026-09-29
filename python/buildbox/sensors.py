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

`camera_frame` is the exception to both, and deliberately: a frame is a
*structural* reading rather than a number, so it returns the events
`data.image_frame` builds, and a camera that is open but hands back no frame is
unplugged rather than idle — a fault, and it raises.

Ordinary Python covers most cases without any of this — a value off an object, a
library call, a computation:

    bb.sensor("level", lambda: tank.level, unit="m")
    bb.sensor("cpu", lambda: psutil.cpu_percent(), unit="%")
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

from . import data

__all__ = ["SensorError", "file_number", "serial_line", "camera_frame"]

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


#: The encodings a frame can be handed over in, and the extension that names each
#: to OpenCV. A mime outside this table is refused rather than guessed at, because
#: a wrong extension produces bytes labelled as something they are not.
_ENCODINGS = {"image/png": ".png", "image/jpeg": ".jpg"}


class CameraReader:
    """A camera captured from, opened on the first read and kept open.

    Same shape as `SerialReader`, for the same reasons: opening a capture device
    per frame is slow enough to drop frames and, on some drivers, to make the
    exposure wander, so it is opened once and held. A read that fails closes it so
    the next one reopens, and a camera unplugged and plugged back in recovers
    without restarting the device.

    This is the one reader here that produces a *structural* reading rather than
    a number: a frame reaches the canvas as the `image` shape the protocol
    defines, built by `data.image_frame`, not as a series of samples.

    There is no "nothing to report yet" state. A camera that is open but hands
    back no frame is unplugged or has failed — that is a fault, not silence, so it
    raises rather than returning anything. A fabricated frame would be an
    illustration of a camera rather than a picture from one.
    """

    def __init__(
        self,
        index: int,
        *,
        mime: str,
        width: Optional[int],
        height: Optional[int],
        fps: Optional[float],
    ) -> None:
        if mime not in _ENCODINGS:
            raise SensorError(
                f"{mime!r} is not a frame encoding this reader can produce; "
                f"use one of {sorted(_ENCODINGS)}."
            )
        self.index = index
        self.mime = mime
        self.width = width
        self.height = height
        self.fps = fps
        self._handle = None
        self._cv2 = None

    def open(self) -> None:
        try:
            import cv2  # type: ignore[import-not-found]
        except ImportError:
            raise SensorError(
                "opencv-python is not installed, so camera "
                f"{self.index} cannot be read. Install it with: "
                "pip install opencv-python"
            ) from None
        try:
            handle = cv2.VideoCapture(self.index)
        except Exception as error:  # noqa: BLE001 - the backend raises a wide range
            raise SensorError(f"camera {self.index} could not be opened: {error}") from None
        if not handle.isOpened():
            handle.release()
            raise SensorError(
                f"camera {self.index} is not present: no capture device answered. "
                "This machine has no camera at this index."
            )
        # The requested size is a hint to the driver; the frame's own size is what
        # gets reported, because that is what the device actually produced.
        if self.width is not None:
            handle.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        if self.height is not None:
            handle.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        if self.fps is not None:
            handle.set(cv2.CAP_PROP_FPS, self.fps)
        self._cv2 = cv2
        self._handle = handle

    def close(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.release()
        except Exception:  # noqa: BLE001 - releasing a camera that has gone away
            pass
        self._handle = None
        self._cv2 = None

    def __call__(self) -> list:
        if self._handle is None:
            self.open()

        try:
            ok, frame = self._handle.read()
        except Exception as error:  # noqa: BLE001
            # Drop the handle so the next read reopens rather than failing forever
            # on a device that has gone away.
            self.close()
            raise SensorError(f"camera {self.index} could not be read: {error}") from None

        if not ok or frame is None:
            # The device answered and had no frame to give. A camera has no
            # "nothing to report yet" state, so this is a fault and is reported as
            # one — never filled in with a frame nobody captured.
            self.close()
            raise SensorError(f"camera {self.index} returned no frame")

        try:
            encoded, buffer = self._cv2.imencode(_ENCODINGS[self.mime], frame)
        except Exception as error:  # noqa: BLE001
            self.close()
            raise SensorError(f"camera {self.index} frame could not be encoded: {error}") from None
        if not encoded:
            raise SensorError(f"camera {self.index} frame could not be encoded as {self.mime}")

        shape = getattr(frame, "shape", None)
        height, width = (shape[0], shape[1]) if shape and len(shape) >= 2 else (None, None)
        return data.image_frame(
            buffer.tobytes(), mime=self.mime, width=width, height=height
        )

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<CameraReader {self.index} {self.mime}>"


def camera_frame(
    index: int = 0,
    *,
    mime: str = "image/png",
    width: Optional[int] = None,
    height: Optional[int] = None,
    fps: Optional[float] = None,
) -> Reader:
    """A reader for a camera, capturing one frame per call.

    A camera is the one device here whose reading is a picture rather than a
    number, so it returns the `image` shape `data.image_frame` builds and the
    client sends it as a structural reading. `index` is OpenCV's capture-device
    index — `0` is the first camera — and `width`/`height`/`fps` are hints to the
    driver; the resolution reported is always the frame's own.

    Needs OpenCV (`pip install opencv-python`), imported on first use so a device
    that never reads a camera does not need it. With no camera at `index`, or with
    OpenCV absent, the reader raises `SensorError` naming which — it never returns
    a placeholder frame.

        from buildbox import Client, sensors

        bb = Client()
        bb.sensor("camera", sensors.camera_frame(0), every=1.0)
        bb.run()
    """
    return CameraReader(index, mime=mime, width=width, height=height, fps=fps)
