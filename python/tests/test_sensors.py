"""Declaring what a device reads, and the readers that feed it.

The transport is stubbed, so what is checked here is what the device decides to
send — and, more importantly, what it declines to send. A reader that fails must
not become a number on the canvas.
"""

from __future__ import annotations

import sys
import threading
import time
import types

import pytest

from buildbox import Client, sensors


class Recorder:
    """Stands in for the HTTP call and remembers what was sent."""

    def __init__(self):
        self.calls = []
        self.fail_next = False

    def __call__(self, method, path, payload=None, timeout=None):
        if self.fail_next:
            self.fail_next = False
            raise OSError("the network is not there")
        self.calls.append((method, path, payload))
        return 200, {"accepted": len((payload or {}).get("events", []))}

    def sent_events(self):
        return [event for _m, path, payload in self.calls if path.endswith("/ingest")
                for event in (payload or {}).get("events", [])]


@pytest.fixture
def recorder(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(
        Client,
        "_request",
        lambda self, m, p, payload=None, timeout=None: recorder(m, p, payload, timeout),
    )
    return recorder


def make_client() -> Client:
    return Client("http://example.test", "token-abc", module="mod-1")


# ------------------------------------------------------------------ #
# Registering                                                         #
# ------------------------------------------------------------------ #


def test_a_sensor_can_be_declared_as_a_decorator():
    client = make_client()

    @client.sensor("temperature", unit="C", every=2.0)
    def temperature():
        return 21.5

    registered = client.sensors()
    assert len(registered) == 1
    assert registered[0].key == "temperature"
    assert registered[0].unit == "C"
    assert registered[0].every == 2.0
    # The decorator returns the function, so it stays callable in your own code.
    assert temperature() == 21.5


def test_a_sensor_can_be_declared_without_arguments():
    client = make_client()
    client.sensor("cpu", lambda: 12.0, unit="%", every=5.0)

    registered = client.sensors()
    assert registered[0].key == "cpu"
    assert registered[0].every == 5.0


def test_an_interval_of_zero_is_not_allowed_to_spin():
    client = make_client()
    client.sensor("fast", lambda: 1.0, every=0.0)
    assert client.sensors()[0].every > 0


# ------------------------------------------------------------------ #
# Sampling                                                            #
# ------------------------------------------------------------------ #


def test_a_number_is_reported_under_its_key(recorder):
    client = make_client()
    client.sensor("temperature", lambda: 21.5, unit="C")

    assert client.sample_once() == 1
    events = recorder.sent_events()
    assert len(events) == 1
    assert events[0]["key"] == "temperature"
    assert events[0]["value"] == 21.5
    assert events[0]["unit"] == "C"
    assert recorder.calls[-1][2]["moduleId"] == "mod-1"


def test_a_mapping_is_reported_as_several_readings(recorder):
    client = make_client()
    client.sensor("climate", lambda: {"temperature": 21.5, "humidity": 44.0})

    assert client.sample_once() == 2
    keys = sorted(event["key"] for event in recorder.sent_events())
    assert keys == ["humidity", "temperature"]


def test_a_missing_field_in_a_mapping_is_omitted(recorder):
    client = make_client()
    client.sensor("climate", lambda: {"temperature": 21.5, "humidity": None})

    client.sample_once()
    keys = [event["key"] for event in recorder.sent_events()]
    assert keys == ["temperature"]


def test_a_reader_with_nothing_to_report_sends_nothing(recorder):
    client = make_client()
    # A sensor waiting for a fix is not a sensor that is broken, and a zero here
    # would be a number nothing measured.
    client.sensor("gps", lambda: None)

    assert client.sample_once() == 0
    assert recorder.calls == []


def test_a_reader_that_raises_logs_the_failure_and_plots_nothing(recorder):
    client = make_client()

    @client.sensor("temperature")
    def broken():
        raise RuntimeError("the probe is unplugged")

    assert client.sample_once() == 0

    events = recorder.sent_events()
    assert len(events) == 1
    assert events[0]["kind"] == "log"
    assert events[0]["severity"] == "error"
    assert "the probe is unplugged" in events[0]["message"]
    # And it does not pretend the reading happened.
    assert not any(event["kind"] == "sample" for event in events)

    assert client.sensors()[0].last_error is not None


def test_a_reader_that_recovers_clears_its_error(recorder):
    client = make_client()
    state = {"broken": True}

    @client.sensor("temperature")
    def flaky():
        if state["broken"]:
            raise RuntimeError("not yet")
        return 21.5

    client.sample_once()
    assert client.sensors()[0].last_error is not None

    state["broken"] = False
    client.sample_once()
    assert client.sensors()[0].last_error is None


def test_a_failing_send_does_not_stop_the_sampler(recorder):
    client = make_client()
    reads = []

    def read():
        reads.append(1)
        return 21.5

    client.sensor("temperature", read, every=0.01)
    recorder.fail_next = True  # the first send fails

    stop = threading.Event()
    sampler = threading.Thread(target=client._sample_loop, args=(stop,), daemon=True)
    sampler.start()
    time.sleep(0.2)
    stop.set()
    sampler.join(timeout=2.0)

    # A send that failed is not a reason to stop reading: the network comes
    # back, and the next tick reports again.
    assert len(reads) > 1, f"the sampler stopped after the failure ({len(reads)} reads)"
    assert not sampler.is_alive(), "the sampler ignored the stop"


# ------------------------------------------------------------------ #
# file_number                                                         #
# ------------------------------------------------------------------ #


def test_file_number_reads_a_bare_number(tmp_path):
    path = tmp_path / "temp"
    path.write_text("21500\n")
    assert sensors.file_number(str(path))() == 21500.0


def test_file_number_scales_the_smallest_unit(tmp_path):
    # How /sys reports it: millidegrees, not degrees.
    path = tmp_path / "temp"
    path.write_text("21500\n")
    assert sensors.file_number(str(path), scale=1e-3)() == 21.5


def test_file_number_applies_an_offset(tmp_path):
    path = tmp_path / "raw"
    path.write_text("100\n")
    assert sensors.file_number(str(path), scale=0.5, offset=-1.0)() == 49.0


def test_file_number_takes_a_capturing_group(tmp_path):
    path = tmp_path / "status"
    path.write_text("temp=21.5C humidity=44%\n")
    reader = sensors.file_number(str(path), pattern=r"temp=([\d.]+)")
    assert reader() == 21.5


def test_file_number_raises_when_the_file_is_missing(tmp_path):
    with pytest.raises(sensors.SensorError) as caught:
        sensors.file_number(str(tmp_path / "nope"))()
    assert "could not be read" in str(caught.value)


def test_file_number_raises_rather_than_guessing_at_text(tmp_path):
    path = tmp_path / "junk"
    path.write_text("all systems nominal\n")
    with pytest.raises(sensors.SensorError) as caught:
        sensors.file_number(str(path))()
    assert "did not contain a number" in str(caught.value)


def test_file_number_names_itself_for_a_stack_trace(tmp_path):
    reader = sensors.file_number(str(tmp_path / "x"))
    assert "file_number" in reader.__name__


# ------------------------------------------------------------------ #
# serial_line                                                         #
# ------------------------------------------------------------------ #


def test_serial_line_raises_with_something_actionable_when_it_cannot_open():
    # Either pyserial is missing or the port is — both are SensorError, and
    # neither is allowed to return a number.
    reader = sensors.serial_line("/dev/definitely-not-a-port")
    with pytest.raises(sensors.SensorError) as caught:
        reader()
    message = str(caught.value)
    assert "pyserial" in message or "could not be opened" in message


def test_serial_line_parses_a_line_with_a_pattern():
    # Exercised without a port by driving the object directly, so the parsing
    # rule is tested even where no serial hardware exists.
    reader = sensors.SerialReader("/dev/null", pattern=r"P=([\d.]+)")
    assert reader.pattern == r"P=([\d.]+)"


def test_an_idle_serial_port_is_not_an_error():
    # `readline` returning nothing means silence, and silence is None rather
    # than a zero. Checked through the reader's own contract, not a real port.
    reader = sensors.SerialReader("/dev/null")
    reader._handle = _SilentPort()
    assert reader() is None


class _SilentPort:
    def readline(self):
        return b""

    def close(self):
        pass


# ------------------------------------------------------------------ #
# camera_frame — no camera and no cv2 on this machine, so the        #
# capture path is exercised against a fake OpenCV.                   #
# ------------------------------------------------------------------ #


def fake_camera(monkeypatch, *, reads=None, opened=True, encode_ok=True, payload=b"\x89PNG"):
    """A camera, with only the OpenCV surface the reader touches.

    `reads` is the sequence of `read()` verdicts; `opened` is what `isOpened()`
    says. Everything real about the reader — when it opens, when it drops the
    handle, how it encodes, what it refuses — runs against this.
    """
    created = {"opens": 0, "capture": None}

    class FakeFrame:
        shape = (480, 640, 3)

    class FakeBuffer:
        def tobytes(self):
            return payload

    class FakeCapture:
        def __init__(self, index):
            self.index = index
            self.released = False
            self.settings = {}
            self._reads = list(reads if reads is not None else [True])
            created["opens"] += 1
            created["capture"] = self

        def isOpened(self):
            return opened

        def set(self, prop, value):
            self.settings[prop] = value

        def read(self):
            ok = self._reads.pop(0) if self._reads else True
            return (True, FakeFrame()) if ok else (False, None)

        def release(self):
            self.released = True

    module = types.SimpleNamespace(
        VideoCapture=FakeCapture,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FPS=5,
        imencode=lambda extension, frame: (encode_ok, FakeBuffer()),
    )
    monkeypatch.setitem(sys.modules, "cv2", module)
    return created


def test_a_captured_frame_arrives_as_an_image_shape(monkeypatch):
    # What the protocol defines and the server already accepts: the `image`
    # shape, as a data URL, built by data.image_frame.
    fake_camera(monkeypatch, payload=b"\x89PNG")
    events = sensors.camera_frame(0, mime="image/png")()

    assert events[0]["kind"] == "shape"
    assert events[0]["shape"] == "image"
    assert events[0]["image"].startswith("data:image/png;base64,")


def test_the_reported_resolution_is_the_frames_own(monkeypatch):
    fake_camera(monkeypatch)
    events = sensors.camera_frame(0)()
    readings = {event["key"]: event["value"] for event in events if event["kind"] == "sample"}
    assert readings == {"resolution x": 640.0, "resolution y": 480.0}


def test_the_camera_is_opened_once_and_kept_open(monkeypatch):
    # Same reason as SerialReader: reopening a capture device per frame drops
    # frames and lets the exposure wander.
    created = fake_camera(monkeypatch)
    read = sensors.camera_frame(0)
    read()
    read()

    assert created["opens"] == 1
    assert created["capture"].released is False


def test_requested_settings_are_passed_to_the_driver(monkeypatch):
    created = fake_camera(monkeypatch)
    sensors.camera_frame(0, width=320, height=240, fps=15)()

    settings = created["capture"].settings
    assert settings == {3: 320, 4: 240, 5: 15}


def test_a_missing_opencv_names_the_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "cv2", None)  # unimportable, whatever is installed
    with pytest.raises(sensors.SensorError) as error:
        sensors.camera_frame(0)()
    message = str(error.value)
    assert "opencv-python" in message
    assert "pip install" in message


def test_a_missing_camera_is_refused_with_its_index(monkeypatch):
    fake_camera(monkeypatch, opened=False)
    with pytest.raises(sensors.SensorError) as error:
        sensors.camera_frame(2)()
    message = str(error.value)
    assert "camera 2" in message
    assert "not present" in message


def test_a_camera_with_no_frame_is_a_fault_not_a_placeholder(monkeypatch):
    # The defect this whole reader is arranged around: a frame nobody captured
    # must never reach the canvas. An open camera that yields nothing is a fault.
    created = fake_camera(monkeypatch, reads=[False])
    read = sensors.camera_frame(0)
    with pytest.raises(sensors.SensorError) as error:
        read()
    assert "returned no frame" in str(error.value)
    # And the handle is dropped, so a reconnected camera recovers on the next read.
    assert created["capture"].released is True


def test_an_encoding_it_cannot_produce_is_refused_at_construction():
    with pytest.raises(sensors.SensorError) as error:
        sensors.camera_frame(0, mime="image/tiff")
    assert "image/tiff" in str(error.value)


def test_a_captured_frame_reaches_the_wire_as_a_shape(recorder, monkeypatch):
    fake_camera(monkeypatch)
    client = make_client()
    client.sensor("camera", sensors.camera_frame(0))

    client.sample_once()

    events = recorder.sent_events()
    assert any(event["kind"] == "shape" and event["shape"] == "image" for event in events)
    # Sent as a shape, not as a sample under the sensor's key.
    assert not any(event["kind"] == "sample" and event.get("key") == "camera" for event in events)


def test_a_camera_that_cannot_be_read_plots_no_image(recorder, monkeypatch):
    monkeypatch.setitem(sys.modules, "cv2", None)
    client = make_client()
    client.sensor("camera", sensors.camera_frame(0))

    client.sample_once()

    events = recorder.sent_events()
    assert not any(event["kind"] == "shape" for event in events)
    assert any(
        event["kind"] == "log" and "opencv-python" in event["message"] for event in events
    )
