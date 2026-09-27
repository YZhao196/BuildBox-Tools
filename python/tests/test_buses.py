"""The bus readers, as far as each one can honestly be checked here.

CAN is checked for real, against python-can's virtual bus — the same code path a
socketcan interface uses, so a decode that is wrong is wrong here too. That is
the one hardware family this machine can exercise end to end.

I²C, SPI, GPIO and ROS2 have no bus on this machine, so those readers are checked
against stubbed libraries: the arithmetic, the refusal and the reconnect logic are
all real, and the hardware call underneath is not. The absence paths are checked
too, because a reader whose library is missing must say which install it wants
rather than raising an ImportError nobody can act on.
"""

from __future__ import annotations

import importlib.util
import sys
import time
import types

import pytest

from buildbox import buses, data
from buildbox.sensors import SensorError


class FakeMessage:
    """A CAN frame, with only the fields the reader touches."""

    def __init__(self, arbitration_id: int, payload: bytes) -> None:
        self.arbitration_id = arbitration_id
        self.data = bytearray(payload)


class FakeBus:
    """A bus that hands back prepared frames, then nothing."""

    def __init__(self, frames) -> None:
        self.frames = list(frames)
        self.shutdowns = 0

    def recv(self, timeout=None):
        return self.frames.pop(0) if self.frames else None

    def shutdown(self) -> None:
        self.shutdowns += 1


def absent(monkeypatch, *names: str) -> None:
    """Make those modules unimportable, whatever the machine actually has."""
    for name in names:
        monkeypatch.setitem(sys.modules, name, None)


# ------------------------------------------------------------------ #
# The arithmetic                                                     #
# ------------------------------------------------------------------ #


def test_bytes_become_the_number_the_datasheet_means():
    # Two big-endian bytes, scaled — the shape most register maps use.
    assert buses._decode([0x04, 0xD2], length=2, byteorder="big", signed=False, scale=0.01, offset=0.0) == 12.34


def test_byte_order_and_sign_are_not_guesses():
    # The failure this guards is a number that looks plausible and is not.
    assert buses._decode([0x01, 0x00], length=2, byteorder="little", signed=False, scale=1.0, offset=0.0) == 1.0
    assert buses._decode([0xFF, 0xFF], length=2, byteorder="big", signed=True, scale=1.0, offset=0.0) == -1.0
    assert buses._decode([0xFF, 0xFF], length=2, byteorder="big", signed=False, scale=1.0, offset=0.0) == 65535.0


def test_a_block_of_the_wrong_width_is_refused():
    # A short read is a device that answered wrongly, not a number to guess at.
    with pytest.raises(SensorError):
        buses._decode([0x01], length=2, byteorder="big", signed=False, scale=1.0, offset=0.0)


# ------------------------------------------------------------------ #
# CAN — checked for real, against python-can's virtual bus           #
# ------------------------------------------------------------------ #


def test_a_can_signal_is_decoded_out_of_a_real_frame(monkeypatch):
    opened = []
    frame = FakeMessage(0x123, b"\x00\x00\x04\xd2\x00\x00\x00\x00")

    def fake_open(channel, interface):
        opened.append(channel)
        return FakeBus([frame, frame, frame])

    monkeypatch.setattr(buses, "_can_bus", fake_open)
    read = buses.can_signal("can0", 0x123, start=2, length=2, scale=0.01)

    assert read() == 12.34
    assert read() == 12.34


def test_the_can_reader_opens_the_interface_once(monkeypatch):
    # It built a fresh reader on every call until this was fixed, which reopened
    # the interface per sample and dropped whatever arrived while it was closed.
    opened = []
    frame = FakeMessage(0x123, b"\x00\x00\x04\xd2")

    def fake_open(channel, interface):
        opened.append(channel)
        return FakeBus([frame, frame])

    monkeypatch.setattr(buses, "_can_bus", fake_open)
    read = buses.can_signal("can0", 0x123, start=2, length=2)

    read()
    read()

    assert len(opened) == 1


def test_frames_for_another_id_are_skipped_not_decoded(monkeypatch):
    wanted = FakeMessage(0x123, b"\x00\x00\x04\xd2")
    other = FakeMessage(0x456, b"\x09\x09\x09\x09")
    monkeypatch.setattr(buses, "_can_bus", lambda channel, interface: FakeBus([other, other, wanted]))

    read = buses.can_signal("can0", 0x123, start=2, length=2, scale=0.01)

    # The point of the test: traffic for other ids must not become a reading.
    assert read() == 12.34


def test_a_quiet_bus_is_not_a_broken_bus(monkeypatch):
    monkeypatch.setattr(buses, "_can_bus", lambda channel, interface: FakeBus([]))
    read = buses.can_signal("can0", 0x123, timeout=0.05)
    assert read() is None


def test_a_can_bus_that_will_not_open_says_so(monkeypatch):
    def refuse(channel, interface):
        raise SensorError(f"{channel} could not be opened: no such interface")

    monkeypatch.setattr(buses, "_can_bus", refuse)
    with pytest.raises(SensorError) as error:
        buses.can_signal("can0", 0x123)()
    assert "could not be opened" in str(error.value)


def test_a_missing_can_library_names_the_install(monkeypatch):
    absent(monkeypatch, "can")
    with pytest.raises(SensorError) as error:
        buses.can_signal("can0", 0x123)()
    assert "drivers" in str(error.value)


def test_the_frame_rate_is_measured_and_zero_is_an_answer(monkeypatch):
    frames = [FakeMessage(0x456, b"\x01\x02") for _ in range(8)]
    monkeypatch.setattr(buses, "_can_bus", lambda channel, interface: FakeBus(frames))
    read = buses.can_frames("can0", window=0.01)
    assert read() == pytest.approx(800.0)

    monkeypatch.setattr(buses, "_can_bus", lambda channel, interface: FakeBus([]))
    assert buses.can_frames("can0", window=0.01)() == 0.0


@pytest.mark.skipif(
    importlib.util.find_spec("can") is None,
    reason="python-can is not installed, so the virtual bus cannot be started",
)
def test_against_a_real_virtual_bus():
    """The one hardware family this machine can drive end to end."""
    import can

    channel = "buildbox-test-virtual"
    sender = can.Bus(channel=channel, interface="virtual")
    try:
        import threading

        def publish() -> None:
            time.sleep(0.1)
            for _ in range(20):
                sender.send(
                    can.Message(
                        arbitration_id=0x123,
                        data=bytearray([0, 0, 0x04, 0xD2, 0, 0, 0, 0]),
                        is_extended_id=False,
                    )
                )
                time.sleep(0.01)

        threading.Thread(target=publish, daemon=True).start()

        read = buses.can_signal(
            channel, 0x123, start=2, length=2, scale=0.01, interface="virtual", timeout=2.0
        )
        assert read() == 12.34
        assert buses.can_frames(channel, interface="virtual", window=0.3)() > 0
    finally:
        sender.shutdown()


# ------------------------------------------------------------------ #
# I²C and SPI — logic real, bus stubbed                              #
# ------------------------------------------------------------------ #


def fake_smbus(monkeypatch, block=(0x04, 0xD2)):
    created = []

    class FakeSMBus:
        def __init__(self, bus):
            self.bus = bus
            self.closed = False
            created.append(self)

        def read_i2c_block_data(self, address, register, length):
            return list(block)[:length]

        def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "smbus2", types.SimpleNamespace(SMBus=FakeSMBus))
    return created


def test_an_i2c_register_is_read_and_decoded(monkeypatch):
    created = fake_smbus(monkeypatch)
    read = buses.i2c_register(1, 0x44, 0x00, length=2, scale=0.01)

    assert read() == 12.34
    # Kept open between readings rather than reopened every sample.
    assert read() == 12.34
    assert len(created) == 1


def test_a_missing_i2c_library_names_the_install(monkeypatch):
    absent(monkeypatch, "smbus2")
    with pytest.raises(SensorError) as error:
        buses.i2c_register(1, 0x44, 0x00)()
    assert "drivers" in str(error.value)


def test_a_device_that_does_not_answer_is_a_failure(monkeypatch):
    class Refusing:
        def __init__(self, bus):
            pass

        def read_i2c_block_data(self, address, register, length):
            raise OSError("remote I/O error")

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "smbus2", types.SimpleNamespace(SMBus=Refusing))
    with pytest.raises(SensorError) as error:
        buses.i2c_register(1, 0x44, 0x00)()
    assert "could not be read" in str(error.value)


def test_an_spi_block_is_read_and_decoded(monkeypatch):
    opened = []

    class FakeSpiDev:
        def __init__(self):
            self.max_speed_hz = 0

        def open(self, bus, device):
            opened.append((bus, device))

        def readbytes(self, length):
            return [0x04, 0xD2][:length]

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "spidev", types.SimpleNamespace(SpiDev=FakeSpiDev))
    read = buses.spi_block(0, 0, length=2, scale=0.01)

    assert read() == 12.34
    assert opened == [(0, 0)]


def test_a_missing_spi_library_names_the_install(monkeypatch):
    absent(monkeypatch, "spidev")
    with pytest.raises(SensorError) as error:
        buses.spi_block(0, 0)()
    assert "drivers" in str(error.value)


# ------------------------------------------------------------------ #
# GPIO — logic real, board stubbed                                   #
# ------------------------------------------------------------------ #


def fake_gpio(monkeypatch, levels=(1,), pulses=0):
    """A board whose pins are all set up and can be read."""
    state = {"count": 0}

    class FakeGPIO:
        BCM = "BCM"
        IN = "IN"
        PUD_UP = "UP"
        PUD_DOWN = "DOWN"
        RISING = "RISING"
        FALLING = "FALLING"

        @staticmethod
        def setmode(mode):
            pass

        @staticmethod
        def setwarnings(flag):
            pass

        @staticmethod
        def setup(pin, **kwargs):
            state["setup"] = (pin, kwargs)

        @staticmethod
        def input(pin):
            return levels[0]

        @staticmethod
        def add_event_detect(pin, trigger, callback):
            state["trigger"] = trigger
            state["callback"] = callback
            for _ in range(pulses):
                callback(pin)

        @staticmethod
        def remove_event_detect(pin):
            state["removed"] = pin

    monkeypatch.setitem(sys.modules, "RPi.GPIO", FakeGPIO)
    monkeypatch.setitem(sys.modules, "Jetson.GPIO", None)
    return state


def test_a_pin_reads_as_a_level(monkeypatch):
    fake_gpio(monkeypatch, levels=(1,))
    assert buses.gpio_line(17)() == 1.0


def test_an_active_low_pin_reads_the_way_the_schematic_means(monkeypatch):
    # Wiring that grounds the signal to mean "on" should not put a 0 on a canvas
    # that is showing a switch.
    fake_gpio(monkeypatch, levels=(0,))
    assert buses.gpio_line(17, active_low=True)() == 1.0


def test_a_pull_up_is_configured(monkeypatch):
    state = fake_gpio(monkeypatch)
    buses.gpio_line(17, pull="up")()
    assert state["setup"][1]["pull_up_down"] == "UP"


def test_a_missing_gpio_library_says_what_to_do_instead(monkeypatch):
    absent(monkeypatch, "RPi.GPIO", "Jetson.GPIO")
    with pytest.raises(SensorError) as error:
        buses.gpio_line(17)()
    message = str(error.value)
    assert "RPi.GPIO" in message
    # The sysfs route needs nothing installed, and saying so is the useful half.
    assert "file_number" in message


def test_pulses_are_reported_per_second(monkeypatch):
    fake_gpio(monkeypatch, pulses=10)
    read = buses.gpio_pulses(17, window=0.05)
    assert read() == pytest.approx(200.0)


def test_a_pin_with_no_edges_reads_as_rest(monkeypatch):
    # An encoder that is not turning really is at rest — this is a measurement,
    # unlike a bus that will not open, which is a failure.
    fake_gpio(monkeypatch, pulses=0)
    assert buses.gpio_pulses(17, window=0.02)() == 0.0


def test_stopping_a_pulse_reader_releases_the_pin(monkeypatch):
    state = fake_gpio(monkeypatch, pulses=1)
    read = buses.gpio_pulses(17, window=0.01)
    read()
    read.stop()
    assert state["removed"] == 17


# ------------------------------------------------------------------ #
# ROS2 — no rclpy here, so the paths that can be checked are        #
# ------------------------------------------------------------------ #


def test_a_missing_ros2_library_explains_where_it_comes_from(monkeypatch):
    absent(monkeypatch, "rclpy")
    with pytest.raises(SensorError) as error:
        buses.ros2_topic("/battery/state", "percentage")()
    message = str(error.value)
    # rclpy is not a pip install, and telling someone to pip install it would
    # send them somewhere there is nothing to find.
    assert "ROS2 distribution" in message


def test_a_dotted_field_is_followed_into_a_message():
    message = types.SimpleNamespace(twist=types.SimpleNamespace(linear=types.SimpleNamespace(x=1.5)))
    assert buses._field_of(message, "twist.linear.x") == 1.5


def test_a_field_that_does_not_exist_is_refused():
    with pytest.raises(SensorError):
        buses._field_of(types.SimpleNamespace(a=1), "b")


def test_the_whole_message_is_used_when_no_field_is_named():
    assert buses._field_of(7.5, None) == 7.5


def test_an_unknown_topic_type_says_to_name_it(monkeypatch):
    node = buses._Ros2Node("test", 0.01)
    monkeypatch.setattr(node, "type_of", lambda topic: None)
    with pytest.raises(SensorError) as error:
        buses._resolve_type(node, "/nothing", None)
    assert "message_type" in str(error.value)


# ------------------------------------------------------------------ #
# The contract with Client.sensor                                    #
# ------------------------------------------------------------------ #


def test_a_structural_reader_returns_events_not_numbers():
    # What these readers hand the client, so the shape reaches the canvas as a
    # shape rather than as a series of samples.
    scan = data.laser_scan([1.0, 2.0], -1.57, 1.57)
    assert scan[0]["kind"] == "shape"
    assert scan[0]["shape"] == "scan"

    joints = data.joint_state([0.1, 0.2], names=["left", "right"])
    assert joints[0]["shape"] == "joints"
    assert joints[0]["labels"] == ["left", "right"]
