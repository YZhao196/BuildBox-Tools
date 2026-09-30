"""Every preset in the mirrored list has a function, and every function produces a legal reading.

The first test is the guarantee, and it is narrower than it sounds: it fails if a
preset in `buildbox.presets.PRESETS` has no builder. That list is generated from
the product's catalogue and then committed, so a preset added to the product's
`catalog.ts` is absent from both sides here and this file stays green until
`scripts/sync_presets.py` is run against that checkout and its output committed.
The rest of the file checks that what the builders produce is something the
server will actually accept — a sample is a finite number, a shape names a shape
the client can draw, a log line carries a severity it knows.
"""

from __future__ import annotations

import math

import pytest

from buildbox import data, presets, protocol


def assert_valid(events, where: str) -> None:
    assert isinstance(events, list), f"{where} did not return a list"
    assert events, f"{where} returned nothing at all"
    for event in events:
        assert isinstance(event, dict), f"{where} produced a non-event"
        kind = event.get("kind")
        assert kind in protocol.KINDS, f"{where} produced kind {kind!r}"

        if kind == "sample":
            assert isinstance(event.get("key"), str) and event["key"], f"{where}: sample needs a key"
            value = event.get("value")
            # A bool is an int in Python, and a sample that plots as 0/1 by
            # accident is not what anyone meant.
            assert isinstance(value, (int, float)) and not isinstance(value, bool), (
                f"{where}: sample value must be a number, got {value!r}"
            )
            assert math.isfinite(value), f"{where}: sample value must be finite"
        elif kind == "log":
            assert isinstance(event.get("message"), str), f"{where}: log needs a message"
            assert event.get("severity") in protocol.SEVERITIES, f"{where}: bad severity"
        elif kind == "status":
            assert event.get("status") in protocol.STATUSES, f"{where}: bad status"
        elif kind == "shape":
            assert event.get("shape") in protocol.SHAPES, f"{where}: bad shape"
            if event["shape"] == "image":
                assert event.get("image"), f"{where}: an image shape needs image data"
            else:
                assert isinstance(event.get("points"), list) and event["points"], (
                    f"{where}: a {event['shape']} shape needs points"
                )
            if event["shape"] == "map":
                assert event.get("cols") and event.get("rows"), f"{where}: a map needs dimensions"
                assert len(event["points"]) == event["cols"] * event["rows"], (
                    f"{where}: map points must fill the grid"
                )


# ------------------------------------------------------------------ #
# The guarantee                                                       #
# ------------------------------------------------------------------ #


def test_every_preset_in_the_catalogue_is_accounted_for():
    assert data.uncovered() == [], (
        "these presets have no builder and are not recorded as non-capturing: "
        + ", ".join(data.uncovered())
    )


def test_the_registry_covers_the_catalogue_exactly():
    # Both directions: nothing missing, and nothing invented for a preset that
    # does not exist.
    assert set(data.PRESET_BUILDERS) == set(presets.PRESET_NAMES)


def test_a_non_capturing_preset_says_why():
    for preset, builder in data.PRESET_BUILDERS.items():
        if builder is None:
            assert preset in data.NON_CAPTURING, (
                f"{preset} has no builder but no reason given for why"
            )


def test_every_builder_is_reachable_by_name():
    for preset, builder in data.PRESET_BUILDERS.items():
        assert data.builder_for(preset) is builder


# ------------------------------------------------------------------ #
# What each builder produces                                          #
# ------------------------------------------------------------------ #

EXAMPLES = {
    # General
    "Kill Switch": lambda: data.kill_switch_outcome(stopped=True, code=0),
    "Script Runner": lambda: data.script_output(["line one", "line two"], code=0),
    "Health Check": lambda: data.health_check(12.5),
    "CLI Terminal": lambda: data.script_output(["$ uptime"], code=0),
    "Tailscale Peer": lambda: data.tailscale_peer("robot-01", online=True, ip="100.64.0.5"),
    "AI API": lambda: data.ai_response("The system is nominal.", model="claude-haiku-4-5"),
    "Generic Control": lambda: data.script_output(["done"], code=0),
    # ROS2
    "ROS2 Topic Subscriber": lambda: data.ros2_message("/battery", "sensor_msgs/BatteryState", voltage=12.4),
    "ROS2 Service Client": lambda: data.ros2_service_response("/calibrate", ok=True),
    "ROS2 Action Client": lambda: data.ros2_service_response("/navigate", ok=False, detail="rejected"),
    "ROS2 Node Monitor": lambda: data.ros2_nodes([{"name": "/amcl", "alive": True}, {"name": "/scan", "alive": False}]),
    "ROS2 Topic Monitor": lambda: data.ros2_topics([{"name": "/scan", "type": "LaserScan", "rate": 12}]),
    "ROS2 Parameter Server": lambda: data.ros2_parameter("max_speed", 1.5, changed=True),
    "ROS2 TF Monitor": lambda: data.ros2_transforms([{"parent": "map", "child": "odom"}]),
    "ROS2 Bag Recorder": lambda: data.ros2_bag_recording("/bags/run1", duration_s=12, size_bytes=4096),
    "ROS2 Bag Player": lambda: data.ros2_bag_playback("/bags/run1", position_s=3.5, duration_s=60),
    "ROS2 Diagnostics": lambda: data.ros2_diagnostics([{"component": "imu", "level": "warn", "message": "drift"}]),
    "ROS2 Image Stream": lambda: data.image_frame(b"\x89PNG", width=640, height=480),
    "ROS2 Pointcloud Viewer": lambda: data.point_cloud([(0.0, 0.0, 1.0), (1.0, 0.0, 1.0)]),
    "ROS2 Laser Scan": lambda: data.laser_scan([1.0, 1.2, 1.1], -1.57, 1.57),
    "ROS2 Odometry": lambda: data.odometry(1.0, 2.0, yaw=0.3, linear_x=0.5),
    "ROS2 Velocity Controller": lambda: data.ros2_twist(0.5, 0.1),
    "ROS2 Joint State": lambda: data.joint_state([0.1, 0.2], names=["left", "right"], velocities=[0.0, 0.1]),
    "ROS2 Map Viewer": lambda: data.occupancy_grid([0, 50, 100, 0], cols=2, rows=2),
    "ROS2 Goal Sender": lambda: data.ros2_goal(3.0, 4.0, accepted=True),
    "ROS2 Lifecycle Manager": lambda: data.ros2_lifecycle("/camera", "active"),
    # IoT
    "MQTT Subscriber": lambda: data.mqtt_message("home/temp", "21.5", value=21.5),
    "HTTP Poller": lambda: data.http_response(21.5, url="http://host/state", latency_ms=8.0),
    "Webhook Receiver": lambda: data.http_response(url="http://host/hook", body='{"ok":true}'),
    "Serial Monitor": lambda: data.serial_data(b"\x01\x02\xff"),
    "GPIO Controller": lambda: data.gpio_pin(17, 1, mode="out"),
    "I²C / SPI Sensor": lambda: data.i2c_register(0x44, "0x00", 21.5, bus=1),
    "Modbus RTU/TCP": lambda: data.modbus_register(40001, 23.5, unit=1, kind="holding"),
    "OPC-UA Client": lambda: data.opcua_node("ns=2;s=Temp", 21.5, quality="good"),
    "CAN Bus Monitor": lambda: data.can_frame(0x123, b"\x01\x02", decoded={"rpm": 1500}),
    "Zigbee/Z-Wave Device": lambda: data.zigbee_device("lamp", "on", link_quality=180, battery=3.0),
    "BLE Scanner": lambda: data.ble_advertisement("Beacon", rssi=-62, payload="aa:bb"),
    "GPS / GNSS": lambda: data.gps_fix(latitude=-37.8, longitude=144.9, speed=1.2, satellites=9),
    "IMU": lambda: data.imu(accel=(0.0, 0.0, 9.81), gyro=(0.0, 0.0, 0.0), mag=(1.0, 2.0, 3.0)),
    "Temperature / Humidity": lambda: data.temperature(21.5, humidity=44.0),
    "Power Monitor": lambda: data.power(voltage=12.4, current=1.2),
    "Camera Capture": lambda: data.image_frame(b"\x00\x01", mime="image/jpeg"),
    "Relay Controller": lambda: data.relay(1, "closed", label="pump"),
    "PID Controller": lambda: data.pid(1.0, 0.9, 0.2, kp=0.5, ki=0.1, kd=0.05),
    "Docker Monitor": lambda: data.docker_containers([{"name": "web", "status": "Up 3 hours", "image": "nginx"}]),
    "Systemd Service": lambda: data.systemd_unit("robot.service", "active", enabled=True),
    # Analytical
    "Histogram": lambda: data.statistics("temp", min=20.0, max=23.0, mean=21.5),
    "Scatter Plot": lambda: data.correlation("speed", "current", 0.82),
    "Correlation Matrix": lambda: data.correlation("a", "b", -0.4, method="spearman"),
    "FFT Spectrum": lambda: data.fft_spectrum([1.0, 2.0, 3.0], [0.1, 0.9, 0.2]),
    "Anomaly Detector": lambda: data.anomaly("temp", 99.0, score=4.2, is_anomaly=True),
    "Rolling Statistics": lambda: data.statistics("temp", mean=21.5, std=0.4, p95=22.1),
    "Regression": lambda: data.regression("x", "y", slope=2.0, intercept=1.0, r_squared=0.98),
    "Event Counter": lambda: data.event_count("faults", 3),
    "Rate Meter": lambda: data.rate("messages", 42.0),
    "Latency Monitor": lambda: data.latency("round trip", 12.5),
    "Threshold Alarm": lambda: data.alarm("temp", active=True, value=91.0, level="hi", latched=True),
    "Data Table": lambda: data.statistics("temp", min=20.0, max=22.0, count=10),
}


def test_every_builder_has_an_example():
    # A builder with no example is one nothing has ever called.
    missing = [
        preset
        for preset, builder in data.PRESET_BUILDERS.items()
        if builder is not None and preset not in EXAMPLES
    ]
    assert missing == [], "these builders are never exercised: " + ", ".join(missing)


@pytest.mark.parametrize("preset", sorted(EXAMPLES))
def test_each_builder_produces_legal_events(preset: str) -> None:
    assert_valid(EXAMPLES[preset](), preset)


def test_every_shape_the_face_can_draw_is_produced_by_something():
    produced = set()
    for make in EXAMPLES.values():
        for event in make():
            if event["kind"] == "shape":
                produced.add(event["shape"])
    # trail and joints come from odometry and joint_state; all six must appear.
    assert produced == set(protocol.SHAPES), f"never produced: {set(protocol.SHAPES) - produced}"


# ------------------------------------------------------------------ #
# The rules the protocol lays down                                    #
# ------------------------------------------------------------------ #


def test_text_is_a_log_line_and_never_a_sample():
    # The protocol has no string sample, so prose must not be coerced into a
    # number. This is the rule that stops prose becoming a fake measurement.
    prose = data.ai_response("the answer is 42")
    assert all(event["kind"] == "log" for event in prose)

    serial = data.serial_data("boot complete")
    assert all(event["kind"] == "log" for event in serial)


def test_a_missing_field_is_omitted_rather_than_guessed():
    # No humidity sensor, so no humidity series — not a zero pretending to be one.
    events = data.temperature(21.5)
    keys = [event["key"] for event in events if event["kind"] == "sample"]
    assert keys == ["temperature"]


def test_no_fix_is_not_a_position():
    # Reporting a position of zero would put the robot in the Atlantic.
    events = data.gps_no_fix()
    assert all(event["kind"] != "shape" for event in events)
    assert all(
        event.get("key") not in ("latitude", "longitude")
        for event in events
        if event["kind"] == "sample"
    )


def test_an_unreachable_health_check_is_not_a_zero_latency_success():
    events = data.health_check(0.0, reachable=False, detail="no route")
    assert not any(event["kind"] == "sample" for event in events)
    assert any(event.get("status") == "error" for event in events)


def test_a_failed_service_is_reported_as_a_failure():
    events = data.ros2_service_response("/nav", ok=False, detail="rejected")
    assert any(event.get("status") == "error" for event in events)


def test_power_computes_watts_when_it_is_not_given():
    events = data.power(voltage=12.0, current=2.0)
    watts = [e for e in events if e["kind"] == "sample" and e["key"] == "power"]
    assert watts and watts[0]["value"] == 24.0


def test_pid_computes_the_error():
    events = data.pid(1.0, 0.75, 0.1)
    error = [e for e in events if e["kind"] == "sample" and e["key"] == "error"]
    assert error and abs(error[0]["value"] - 0.25) < 1e-9


def test_a_map_must_fill_its_grid():
    with pytest.raises(ValueError):
        data.occupancy_grid([1, 2, 3], cols=2, rows=2)


def test_a_correlation_outside_minus_one_to_one_is_refused():
    with pytest.raises(ValueError):
        data.correlation("a", "b", 1.5)


def test_an_unknown_lifecycle_state_is_refused():
    with pytest.raises(ValueError):
        data.ros2_lifecycle("/node", "exploded")


def test_bytes_that_are_not_text_are_shown_as_hex():
    events = data.serial_data(b"\x00\x01\x02")
    assert "00 01 02" in events[0]["message"]


def test_a_frame_is_encoded_as_a_data_url():
    events = data.image_frame(b"\x89PNG\r\n", mime="image/png")
    shape = next(e for e in events if e["kind"] == "shape")
    assert shape["image"].startswith("data:image/png;base64,")


def test_a_frame_the_server_would_refuse_is_refused_here_with_the_fix():
    # The server's cap (routes/devices.ts, IMAGE_MAX_CHARS) and its four types.
    # Refusing here names the fix; sending would only earn a bare 422.
    with pytest.raises(ValueError, match="JPEG|resolution"):
        data.image_frame(b"\x00" * (400 * 1024), mime="image/png")
    with pytest.raises(ValueError, match="png, jpeg, gif or webp"):
        data.image_frame(b"\x00", mime="image/bmp")
    with pytest.raises(ValueError, match="base64 data URL"):
        data.image_frame("https://example.com/frame.png")
    # Just under the cap still goes.
    assert data.image_frame(b"\x00" * (380 * 1024), mime="image/jpeg")
