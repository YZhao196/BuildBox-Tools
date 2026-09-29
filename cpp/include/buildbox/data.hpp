// A function for every kind of reading a module can carry.
//
// The protocol has four primitives — a numeric sample, a log line, a status, and
// a structural reading. Everything below turns a domain fact into one or more of
// those, so sending a CAN frame or a GPS fix does not mean remembering which
// field the server wants where.
//
//     #include <buildbox/data.hpp>
//
//     bb.send(buildbox::data::temperature(21.5, 44.0));
//     bb.send(buildbox::data::can_frame(0x123, {0x01, 0x02}));
//     bb.send(buildbox::data::gps_fix(-37.8, 144.9, 1.2));
//
// Every function returns a vector, because most real readings are more than one
// number: an IMU is nine, a GPS fix is six. `Client::send` takes a vector, and
// `Client::send_data` covers the single-number case.
//
// Two rules the server enforces, honoured here:
//
//  * **A sample is a number.** The protocol has no string sample, so anything
//    textual — an AI reply, a shell line, an MQTT payload — becomes a log line
//    rather than being coerced into a number it is not.
//  * **A structural reading must match its module.** A laser sweep may only be
//    sent to a module whose face draws a sweep.
//
// `preset_builder_name` maps every catalogue preset to the function that feeds
// it, and `uncovered()` reports any preset with neither — which the tests assert
// is empty.

#pragma once

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "buildbox/client.hpp"
#include "buildbox/json.hpp"
#include "buildbox/presets.hpp"

namespace buildbox {
namespace data {

using Events = std::vector<Json>;

// ------------------------------------------------------------------ //
// The four primitives                                                 //
// ------------------------------------------------------------------ //

/// One numeric reading, under a named series.
inline Events sample(const std::string& key, double value, const std::string& unit = "") {
  return {event::sample(key, value, unit)};
}

/// Several readings at once. A missing one is simply not reported.
inline Events samples(std::initializer_list<std::pair<std::string, std::optional<double>>> readings) {
  Events events;
  for (const auto& [key, value] : readings) {
    if (value.has_value()) events.push_back(event::sample(key, *value));
  }
  return events;
}

/// A line for the module's output panel and the session log.
inline Events log(const std::string& message, const std::string& severity = "info") {
  return {event::log(message, severity)};
}

/// Move the module's status light.
inline Events status(const std::string& value) { return {event::status(value)}; }

// ------------------------------------------------------------------ //
// Structural readings                                                 //
// ------------------------------------------------------------------ //

/// A laser sweep: ranges in metres across a sweep given in radians.
///
/// These are the fields the protocol carries and the face draws, and only these:
/// a sensor's range limits would be dropped on the way through rather than shown.
/// For **ROS2 Laser Scan**.
inline Events laser_scan(const std::vector<double>& ranges, double angle_min, double angle_max) {
  Json sweep = Json::object();
  sweep.set("kind", "shape");
  sweep.set("shape", "scan");
  Json points = Json::array();
  for (double range : ranges) points.push(Json(range));
  sweep.set("points", points);
  sweep.set("angleMin", angle_min);
  sweep.set("angleMax", angle_max);
  return {sweep};
}

/// A point cloud as x, y, z triples in metres, in the sensor frame.
/// For **ROS2 Pointcloud Viewer**.
inline Events point_cloud(const std::vector<double>& xyz) {
  if (xyz.size() % 3 != 0) {
    throw std::invalid_argument("a point cloud is x, y, z triples, so the count must be a multiple of three");
  }
  return {event::shape("cloud", xyz)};
}

/// A position per joint, with their names.
/// For **ROS2 Joint State**.
inline Events joint_state(const std::vector<double>& positions,
                          const std::vector<std::string>& names = {},
                          const std::vector<double>& velocities = {},
                          const std::vector<double>& efforts = {}) {
  Json joints = Json::object();
  joints.set("kind", "shape");
  joints.set("shape", "joints");
  Json points = Json::array();
  for (double position : positions) points.push(Json(position));
  joints.set("points", points);
  if (!names.empty()) {
    Json labels = Json::array();
    for (const std::string& name : names) labels.push(Json(name));
    joints.set("labels", labels);
  }

  Events events{joints};
  for (size_t index = 0; index < positions.size(); ++index) {
    const std::string label = index < names.size() ? names[index] : "joint" + std::to_string(index);
    if (index < velocities.size()) {
      events.push_back(event::sample(label + " velocity", velocities[index], "rad/s"));
    }
    if (index < efforts.size()) {
      events.push_back(event::sample(label + " effort", efforts[index], "Nm"));
    }
  }
  return events;
}

/// An occupancy grid in 0..100 per cell, row-major.
/// For **ROS2 Map Viewer**.
inline Events occupancy_grid(const std::vector<double>& cells, int cols, int rows) {
  if (static_cast<size_t>(cols) * static_cast<size_t>(rows) != cells.size()) {
    throw std::invalid_argument("an occupancy grid needs one cell per row and column");
  }
  Json grid = Json::object();
  grid.set("kind", "shape");
  grid.set("shape", "map");
  Json points = Json::array();
  for (double cell : cells) points.push(Json(cell));
  grid.set("points", points);
  grid.set("cols", cols);
  grid.set("rows", rows);
  return {grid};
}

/// A pose, which the face accumulates into a trail, plus any velocity.
/// For **ROS2 Odometry**.
inline Events odometry(double x, double y,
                       std::optional<double> yaw = std::nullopt,
                       std::optional<double> linear_x = std::nullopt,
                       std::optional<double> angular_z = std::nullopt) {
  Json pose = Json::object();
  pose.set("kind", "shape");
  pose.set("shape", "trail");
  Json points = Json::array();
  points.push(Json(x));
  points.push(Json(y));
  pose.set("points", points);

  Events events{pose};
  const Events velocity = samples({{"speed", linear_x}, {"turn rate", angular_z}, {"heading", yaw}});
  events.insert(events.end(), velocity.begin(), velocity.end());
  return events;
}

/// A camera frame, as raw bytes or an already-encoded `data:` URL.
///
/// Bytes are base64-encoded here so the caller does not have to. Only ever send
/// a frame a device actually produced: a modelled camera has no frame to give.
/// For **Camera Capture** and **ROS2 Image Stream**.
Events image_frame(const std::string& encoded_or_url, const std::string& mime = "image/png");
Events image_frame(const std::vector<unsigned char>& bytes, const std::string& mime = "image/png");

// ------------------------------------------------------------------ //
// General                                                             //
// ------------------------------------------------------------------ //

/// What a script printed, with its exit code. A non-zero code is an error.
/// For **Script Runner**, **Generic Control** and **CLI Terminal**.
inline Events script_output(const std::vector<std::string>& lines, int code = 0) {
  Events events;
  for (const std::string& line : lines) events.push_back(event::log(line, code == 0 ? "info" : "error"));
  events.push_back(event::log("exited " + std::to_string(code), code == 0 ? "info" : "error"));
  return events;
}

/// Whether a host answered, and how long it took.
///
/// Unreachable is a warning with a reason, not a zero-latency success: a health
/// check that reports healthy when nothing answered is worse than none.
/// For **Health Check**.
inline Events health_check(double latency_ms, bool reachable = true, const std::string& detail = "") {
  if (!reachable) {
    return {event::status("error"),
            event::log(detail.empty() ? "the host did not answer" : detail, "error")};
  }
  Events events{event::sample("ping", latency_ms, "ms")};
  events.push_back(event::status(latency_ms > 500 ? "warn" : "ok"));
  if (!detail.empty()) events.push_back(event::log(detail));
  return events;
}

/// A peer's presence and address. For **Tailscale Peer**.
inline Events tailscale_peer(const std::string& name, bool online = true,
                             const std::string& ip = "") {
  std::string described = name + (online ? " is online" : " is offline");
  if (!ip.empty()) described += " at " + ip;
  return {event::log(described, online ? "info" : "warn"),
          event::status(online ? "ok" : "warn")};
}

/// What a model replied. Text is a log line, not a sample.
/// For **AI API**.
inline Events ai_response(const std::string& text, std::optional<double> latency_ms = std::nullopt) {
  Events events{event::log(text)};
  if (latency_ms.has_value()) events.push_back(event::sample("ai latency", *latency_ms, "ms"));
  return events;
}

/// What a kill switch did.
///
/// `stopped` alone decides the status: a command that returned zero without
/// stopping anything is not a stop. For **Kill Switch**.
inline Events kill_switch_outcome(bool stopped, int code, const std::string& message = "") {
  if (!stopped) {
    return {event::status("error"),
            event::log(message.empty()
                           ? "the termination command did not stop the machine (exit " +
                                 std::to_string(code) + ")"
                           : message,
                       "error")};
  }
  return {event::status("fired"),
          event::log(message.empty() ? "stopped (exit " + std::to_string(code) + ")" : message, "warn")};
}

// ------------------------------------------------------------------ //
// ROS2                                                                //
// ------------------------------------------------------------------ //

/// An arbitrary topic message. Numeric fields become `topic/field` series so two
/// topics plotting the same name do not collide.
/// For **ROS2 Topic Subscriber**.
inline Events ros2_message(const std::string& topic,
                           std::initializer_list<std::pair<std::string, double>> fields) {
  Events events;
  for (const auto& [key, value] : fields) {
    events.push_back(event::sample(topic + "/" + key, value));
  }
  return events.empty() ? Events{event::log(topic + " published an empty message")} : events;
}

/// The nodes in the graph and whether each is alive. A dead node is an error.
/// For **ROS2 Node Monitor**.
inline Events ros2_nodes(const std::vector<std::pair<std::string, bool>>& nodes) {
  Events events;
  bool dead = false;
  for (const auto& [name, alive] : nodes) {
    dead = dead || !alive;
    events.push_back(event::log(name + (alive ? " alive" : " is gone"), alive ? "info" : "error"));
  }
  events.push_back(event::sample("nodes", static_cast<double>(nodes.size())));
  events.push_back(event::status(dead ? "error" : "ok"));
  return events;
}

/// The topics in the graph with their types and rates.
/// For **ROS2 Topic Monitor**.
inline Events ros2_topics(const std::vector<std::pair<std::string, std::string>>& topics,
                          const std::vector<double>& rates = {}) {
  Events events;
  for (size_t index = 0; index < topics.size(); ++index) {
    std::string described = topics[index].first + " · " + topics[index].second;
    if (index < rates.size()) {
      described += " · " + std::to_string(rates[index]) + " Hz";
      events.push_back(event::sample("scan", rates[index], "Hz"));
    }
    events.push_back(event::log(described));
  }
  return events.empty() ? Events{event::log("no topics in the graph", "warn")} : events;
}

/// A parameter's value, and whether it just changed.
/// For **ROS2 Parameter Server**.
inline Events ros2_parameter(const std::string& name, double value, bool changed = false) {
  return {event::sample("param " + name, value),
          event::log("parameter " + name + " = " + std::to_string(value) +
                        (changed ? " (changed)" : ""),
                    changed ? "warn" : "info")};
}

/// The transform tree. A stale transform is reported, not drawn as current.
/// For **ROS2 TF Monitor**.
inline Events ros2_transforms(
    const std::vector<std::tuple<std::string, std::string, bool>>& transforms) {
  Events events;
  for (const auto& [parent, child, stale] : transforms) {
    const std::string edge = parent + " → " + child;
    events.push_back(event::log(stale ? edge + " is stale" : edge, stale ? "warn" : "info"));
  }
  return events.empty() ? Events{event::log("no transforms published", "warn")} : events;
}

/// Per-component health. The worst level decides the module's status.
/// For **ROS2 Diagnostics**.
inline Events ros2_diagnostics(const std::vector<std::tuple<std::string, std::string, std::string>>& components) {
  Events events;
  int worst = 0;
  const std::vector<std::string> rank = {"ok", "warn", "error"};
  for (const auto& [component, level, message] : components) {
    const auto found = std::find(rank.begin(), rank.end(), level);
    const int index = found == rank.end() ? 0 : static_cast<int>(found - rank.begin());
    worst = std::max(worst, index);
    std::string described = component + ": " + level;
    if (!message.empty()) described += " · " + message;
    events.push_back(event::log(described, index == 0 ? "info" : rank[index]));
  }
  events.push_back(event::status(rank[worst]));
  return events;
}

/// A recording's progress. For **ROS2 Bag Recorder**.
inline Events ros2_bag_recording(const std::string& path, std::optional<double> duration_s = std::nullopt,
                                 std::optional<double> size_bytes = std::nullopt, bool active = true) {
  Events events{event::log("recording to " + path + (active ? "" : " stopped")),
                event::status(active ? "ok" : "idle")};
  const Events readings = samples({{"recording", duration_s}, {"bag size", size_bytes}});
  events.insert(events.end(), readings.begin(), readings.end());
  return events;
}

/// Where playback has reached. For **ROS2 Bag Player**.
inline Events ros2_bag_playback(const std::string& path, double position_s, bool playing = true) {
  return {event::sample("playback", position_s, "s"),
          event::status(playing ? "ok" : "idle"),
          event::log(path + " at " + std::to_string(position_s) + "s")};
}

/// A lifecycle node's state. For **ROS2 Lifecycle Manager**.
inline Events ros2_lifecycle(const std::string& node, const std::string& state) {
  static const std::vector<std::string> known = {"unconfigured", "inactive", "active", "finalized"};
  if (std::find(known.begin(), known.end(), state) == known.end()) {
    throw std::invalid_argument("unknown lifecycle state: " + state);
  }
  return {event::status(state == "active" ? "ok" : "idle"), event::log(node + " is " + state)};
}

/// How fast a topic is publishing. For **ROS2 Topic Monitor** and **Rate Meter**.
inline Events ros2_topic_rate(const std::string& topic, double hz) {
  return {event::sample("rate", hz, "msg/s"), event::log(topic + " at " + std::to_string(hz) + " Hz")};
}

/// A robot pose on its own, for the map overlay to follow.
inline Events ros2_pose(double x, double y) { return odometry(x, y); }

/// A velocity command, as the numbers that were sent.
/// For **ROS2 Velocity Controller**.
inline Events ros2_twist(double linear_x, double angular_z, double linear_y = 0.0) {
  return samples({{"cmd linear x", linear_x}, {"cmd linear y", linear_y}, {"cmd angular z", angular_z}});
}

/// A navigation goal and whether it was accepted. For **ROS2 Goal Sender**.
inline Events ros2_goal(double x, double y, bool accepted = true) {
  return {event::log("goal (" + std::to_string(x) + ", " + std::to_string(y) + "): " +
                         (accepted ? "accepted" : "rejected"),
                     accepted ? "info" : "warn"),
          event::status(accepted ? "ok" : "warn")};
}

/// What a service call answered.
/// For **ROS2 Service Client** and **ROS2 Action Client**.
inline Events ros2_service_response(const std::string& service, bool ok, const std::string& detail = "") {
  return {event::log(service + ": " + (detail.empty() ? (ok ? "ok" : "failed") : detail),
                     ok ? "info" : "error"),
          event::status(ok ? "ok" : "error")};
}

// ------------------------------------------------------------------ //
// IoT                                                                 //
// ------------------------------------------------------------------ //

/// A message on a topic, and a number pulled out of it if the caller says so.
/// For **MQTT Subscriber**.
inline Events mqtt_message(const std::string& topic, const std::string& payload,
                           std::optional<double> value = std::nullopt) {
  Events events{event::log(topic + ": " + payload)};
  if (value.has_value()) events.push_back(event::sample("mqtt", *value));
  return events;
}

/// What a poll returned. A non-2xx is an error, whatever the body says.
/// For **HTTP Poller** and **Webhook Receiver**.
inline Events http_response(std::optional<double> value = std::nullopt, const std::string& url = "",
                            int status_code = 200, std::optional<double> latency_ms = std::nullopt,
                            const std::string& body = "") {
  const std::string where = url.empty() ? "request" : url;
  if (status_code >= 400) {
    return {event::status("error"),
            event::log(where + " answered " + std::to_string(status_code) +
                           (body.empty() ? "" : ": " + body),
                       "error")};
  }
  Events events;
  if (value.has_value()) events.push_back(event::sample("poll", *value));
  const Events readings = samples({{"http latency", latency_ms}});
  events.insert(events.end(), readings.begin(), readings.end());
  events.push_back(event::log(where + " answered " + std::to_string(status_code) +
                              (body.empty() ? "" : ": " + body)));
  return events;
}

/// A line from a serial port, or bytes shown as hex when they are not text.
/// For **Serial Monitor**.
Events serial_data(const std::string& text);
Events serial_data(const std::vector<unsigned char>& bytes);

/// A pin's state. For **GPIO Controller**.
inline Events gpio_pin(int pin, double value, const std::string& mode = "out") {
  return {event::sample("gpio " + std::to_string(pin), value),
          event::log("pin " + std::to_string(pin) + " (" + mode + ") = " + std::to_string(value))};
}

/// A sensor register. The series names the address as well as the register,
/// because one bus carries many devices. For **I²C / SPI Sensor**.
inline Events i2c_register(int address, int reg, double value) {
  const std::string where = "0x" + std::to_string(address);
  return {event::sample("i2c " + where + ":" + std::to_string(reg), value),
          event::log(where + " register " + std::to_string(reg) + " = " + std::to_string(value))};
}

/// A Modbus register. For **Modbus RTU/TCP**.
inline Events modbus_register(int address, double value, int unit = 1,
                              const std::string& kind = "holding") {
  return {event::sample("modbus " + std::to_string(address), value),
          event::log(kind + " register " + std::to_string(address) + " (unit " +
                     std::to_string(unit) + ") = " + std::to_string(value))};
}

/// An OPC-UA node's value and quality. A quality that is not `good` is a
/// warning even when it carries a number: a stale value shown as current is the
/// failure this exists to avoid. For **OPC-UA Client**.
inline Events opcua_node(const std::string& node_id, double value, const std::string& quality = "good") {
  const bool good = quality == "good";
  Events events{event::sample("opcua " + node_id, value),
                event::log(node_id + " = " + std::to_string(value) + " [" + quality + "]",
                           good ? "info" : "warn")};
  if (!good) events.push_back(event::status("warn"));
  return events;
}

/// A CAN frame.
///
/// The identifier and the payload are both reported: the id as a series so a bus
/// can be watched for one frame, and the bytes as hex because that is how every
/// CAN tool prints them. For **CAN Bus Monitor**.
inline Events can_frame(uint32_t can_id, const std::vector<unsigned char>& payload,
                        bool extended = false) {
  char identifier[16];
  std::snprintf(identifier, sizeof(identifier), "%0*x", extended ? 8 : 4, can_id);
  std::string hex;
  for (unsigned char byte : payload) {
    char pair[4];
    std::snprintf(pair, sizeof(pair), "%02x ", byte);
    hex += pair;
  }
  if (!hex.empty()) hex.pop_back();

  return {event::sample("can id", static_cast<double>(can_id)),
          event::log(std::string("0x") + identifier + " [" + (extended ? "29" : "11") + "bit] " + hex)};
}

/// A CAN interface's own state, for a bus with no traffic on it.
/// For **CAN Bus Monitor**.
///
/// The parameter is `device` rather than `interface` on purpose: `interface` is a
/// macro on Windows (it expands to `struct`, via `combaseapi.h`), so naming an
/// argument that would fail to compile on the platform this most often runs on.
inline Events can_bus_state(const std::string& device, bool up = true, int errors = 0) {
  Events events{event::status(up ? "ok" : "error"),
                event::log(device + (up ? " is up" : " is down"), up ? "info" : "error")};
  if (errors) events.push_back(event::sample("can errors", static_cast<double>(errors)));
  return events;
}

/// A Zigbee or Z-Wave device's state. For **Zigbee/Z-Wave Device**.
inline Events zigbee_device(const std::string& device, const std::string& state,
                            std::optional<double> link_quality = std::nullopt,
                            std::optional<double> battery = std::nullopt) {
  Events events{event::log(device + ": " + state)};
  const Events readings = samples({{"link quality", link_quality}, {"battery", battery}});
  events.insert(events.end(), readings.begin(), readings.end());
  if (battery.has_value() && *battery < 2.5) events.push_back(event::status("warn"));
  return events;
}

/// A BLE advertisement. For **BLE Scanner**.
inline Events ble_advertisement(const std::string& device, std::optional<double> rssi = std::nullopt,
                                const std::string& payload = "") {
  Events events = samples({{"ble rssi", rssi}});
  std::string described = device;
  if (rssi.has_value()) described += " rssi " + std::to_string(*rssi);
  if (!payload.empty()) described += " · " + payload;
  events.push_back(event::log(described));
  return events;
}

/// A position fix. For **GPS / GNSS**.
///
/// No fix is not a position of zero — send `gps_no_fix` instead, so the map does
/// not place the robot in the Atlantic.
inline Events gps_fix(double latitude, double longitude,
                      std::optional<double> altitude = std::nullopt,
                      std::optional<double> speed = std::nullopt,
                      std::optional<double> course = std::nullopt,
                      std::optional<double> satellites = std::nullopt,
                      const std::string& fix_quality = "fix") {
  Events events = samples({{"latitude", latitude},
                           {"longitude", longitude},
                           {"altitude", altitude},
                           {"speed", speed},
                           {"course", course},
                           {"satellites", satellites}});
  events.push_back(event::log(std::to_string(latitude) + ", " + std::to_string(longitude) +
                              " · " + fix_quality));
  return events;
}

/// No position yet. Exists so "I have no fix" has somewhere to go that is not a
/// coordinate. For **GPS / GNSS**.
inline Events gps_no_fix(const std::string& reason = "") {
  return {event::status("warn"), event::log(reason.empty() ? "no satellite fix" : reason, "warn")};
}

/// Accelerometer, gyroscope and magnetometer, one series per axis.
/// For **IMU**.
inline Events imu(const std::vector<double>& accel = {}, const std::vector<double>& gyro = {},
                  const std::vector<double>& mag = {}, std::optional<double> temperature = std::nullopt) {
  Events events;
  const struct {
    const char* name;
    const std::vector<double>* axes;
    const char* unit;
  } groups[] = {{"accel", &accel, "m/s²"}, {"gyro", &gyro, "rad/s"}, {"mag", &mag, "µT"}};

  for (const auto& group : groups) {
    const char* axis_names = "xyz";
    for (size_t index = 0; index < group.axes->size() && index < 3; ++index) {
      events.push_back(event::sample(std::string(group.name) + " " + axis_names[index],
                                     (*group.axes)[index], group.unit));
    }
  }
  if (temperature.has_value()) events.push_back(event::sample("imu temperature", *temperature, "°C"));
  return events;
}

/// A temperature, and whatever else the sensor reads.
/// For **Temperature / Humidity**.
inline Events temperature(double celsius, std::optional<double> humidity = std::nullopt,
                          std::optional<double> pressure = std::nullopt) {
  return samples({{"temperature", celsius}, {"humidity", humidity}, {"pressure", pressure}});
}

/// Electrical readings. Watts are computed when voltage and current are given
/// and watts are not, because that is arithmetic the caller should not repeat.
/// For **Power Monitor**.
inline Events power(std::optional<double> voltage = std::nullopt,
                    std::optional<double> current = std::nullopt,
                    std::optional<double> watts = std::nullopt,
                    std::optional<double> energy_wh = std::nullopt) {
  if (!watts.has_value() && voltage.has_value() && current.has_value()) {
    watts = *voltage * *current;
  }
  return samples({{"battery", voltage}, {"current", current}, {"power", watts}, {"energy", energy_wh}});
}

/// A relay's state. The reading is 1 or 0 so it plots and thresholds like
/// anything else. For **Relay Controller**.
inline Events relay(int channel, bool closed, const std::string& label = "") {
  const std::string name = label.empty() ? "relay " + std::to_string(channel) : label;
  return {event::sample("relay " + std::to_string(channel), closed ? 1.0 : 0.0),
          event::log(name + (closed ? " is closed" : " is open"))};
}

/// A control loop's state. The error is computed here because every operator
/// wants it and computing it wrong once is enough. For **PID Controller**.
inline Events pid(double setpoint, double feedback, double output) {
  return samples({{"setpoint", setpoint},
                  {"feedback", feedback},
                  {"output", output},
                  {"error", setpoint - feedback}});
}

/// The containers on a host. One that is not running is a warning.
/// For **Docker Monitor**.
inline Events docker_containers(const std::vector<std::pair<std::string, std::string>>& containers) {
  Events events{event::sample("containers", static_cast<double>(containers.size()))};
  bool stopped = false;
  for (const auto& [name, status] : containers) {
    std::string lowered = status;
    std::transform(lowered.begin(), lowered.end(), lowered.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    const bool running = lowered.find("up") != std::string::npos;
    stopped = stopped || !running;
    events.push_back(event::log(name + " · " + status, running ? "info" : "warn"));
  }
  if (stopped) events.push_back(event::status("warn"));
  return events;
}

/// A systemd unit's state. `failed` is an error rather than a warning, because
/// that is the thing this module is watched for. For **Systemd Service**.
inline Events systemd_unit(const std::string& name, const std::string& state, bool enabled = true) {
  const bool failed = state.find("failed") != std::string::npos;
  return {event::log(name + " is " + state + (enabled ? " (enabled)" : " (disabled)"),
                     failed ? "error" : "info"),
          event::status(failed ? "error" : "ok")};
}

// ------------------------------------------------------------------ //
// Analytical                                                          //
// ------------------------------------------------------------------ //
//
// These modules compute server-side from whatever the streams carry, so a device
// normally feeds them through `sample`. These exist because a device that
// already computed one locally should be able to report it.

/// Summary statistics for a stream.
/// For **Rolling Statistics**, **Histogram** and **Data Table**.
inline Events statistics(const std::string& key,
                         std::initializer_list<std::pair<std::string, std::optional<double>>> values) {
  Events events;
  for (const auto& [name, value] : values) {
    if (value.has_value()) events.push_back(event::sample(key + " " + name, *value));
  }
  return events;
}

/// How two streams move together.
/// For **Correlation Matrix** and **Scatter Plot**.
inline Events correlation(const std::string& a, const std::string& b, double coefficient,
                          const std::string& method = "pearson") {
  if (coefficient < -1.0 || coefficient > 1.0) {
    throw std::invalid_argument("a correlation coefficient is between -1 and 1");
  }
  return {event::sample(a + "~" + b + " " + method, coefficient),
          event::log(method + " correlation of " + a + " and " + b + ": " +
                     std::to_string(coefficient))};
}

/// A frequency-domain reading. The peak is what an operator watches.
/// For **FFT Spectrum**.
inline Events fft_spectrum(const std::vector<double>& frequencies, const std::vector<double>& magnitudes) {
  if (frequencies.size() != magnitudes.size()) {
    throw std::invalid_argument("frequencies and magnitudes must be the same length");
  }
  Events events;
  if (!magnitudes.empty()) {
    const size_t peak = static_cast<size_t>(
        std::max_element(magnitudes.begin(), magnitudes.end()) - magnitudes.begin());
    events.push_back(event::sample("peak frequency", frequencies[peak], "Hz"));
    events.push_back(event::sample("peak magnitude", magnitudes[peak]));
  }
  events.push_back(event::log(std::to_string(frequencies.size()) + " bins"));
  return events;
}

/// A value flagged as out of the ordinary.
/// For **Anomaly Detector** and **Threshold Alarm**.
inline Events anomaly(const std::string& key, double value, std::optional<double> score = std::nullopt,
                      bool is_anomaly = false) {
  Events events{event::sample(key, value)};
  if (score.has_value()) events.push_back(event::sample(key + " z-score", *score));
  if (is_anomaly) {
    events.push_back(event::log(key + " is out of range", "warn"));
    events.push_back(event::status("warn"));
  }
  return events;
}

/// A fitted relationship between two streams. For **Regression**.
inline Events regression(const std::string& x, const std::string& y, double slope, double intercept,
                         double r_squared) {
  return {event::sample(y + "~" + x + " slope", slope),
          event::sample(y + "~" + x + " intercept", intercept),
          event::sample(y + "~" + x + " r²", r_squared),
          event::log(y + " = f(" + x + "), r² " + std::to_string(r_squared))};
}

/// How many times something has happened. For **Event Counter**.
inline Events event_count(const std::string& key, double count) {
  return {event::sample(key, count)};
}

/// How often something is happening. For **Rate Meter**.
inline Events rate(const std::string& key, double per_second) {
  return {event::sample(key, per_second, "msg/s")};
}

/// A round-trip time. For **Latency Monitor**.
inline Events latency(const std::string& key, double milliseconds) {
  return {event::sample(key, milliseconds, "ms")};
}

/// An alarm's state. A latched alarm stays until acknowledged, so the status
/// does not clear the moment the value wanders back. For **Threshold Alarm**.
inline Events alarm(const std::string& key, bool active, std::optional<double> value = std::nullopt,
                    const std::string& level = "lo", bool latched = false) {
  Events events;
  if (value.has_value()) events.push_back(event::sample(key, *value));
  if (active) {
    events.push_back(event::log(level + " alarm on " + key + (latched ? " (latched)" : ""), "warn"));
    events.push_back(event::status("warn"));
  } else {
    events.push_back(event::log(key + " back in range"));
    events.push_back(event::status("ok"));
  }
  return events;
}

// ------------------------------------------------------------------ //
// Coverage                                                            //
// ------------------------------------------------------------------ //

/// The function that feeds a preset, by name, or nullptr when none does.
///
/// Names rather than function pointers because the builders do not share a
/// signature; the name is what a coverage test needs, and it is what a reader
/// searches for.
const char* preset_builder_name(const std::string& preset);

/// Why a preset is never fed by a device, or nullptr when it is.
const char* non_capturing_reason(const std::string& preset);

/// Catalogue presets with no entry at all. Empty means every preset is either
/// fed by a function or explicitly recorded as not fed.
inline std::vector<std::string> uncovered() {
  std::vector<std::string> missing;
  for (const Preset& preset : presets()) {
    if (preset_builder_name(preset.name) == nullptr &&
        non_capturing_reason(preset.name) == nullptr) {
      missing.push_back(preset.name);
    }
  }
  return missing;
}

// ------------------------------------------------------------------ //
// Definitions that need more than the header                          //
// ------------------------------------------------------------------ //

inline const char* preset_builder_name(const std::string& preset) {
  static const std::map<std::string, const char*> builders = {
      // General
      {"Kill Switch", "kill_switch_outcome"},
      {"Script Runner", "script_output"},
      {"Health Check", "health_check"},
      {"CLI Terminal", "script_output"},
      {"Tailscale Peer", "tailscale_peer"},
      {"AI API", "ai_response"},
      {"Generic Control", "script_output"},
      // ROS2
      {"ROS2 Topic Subscriber", "ros2_message"},
      {"ROS2 Service Client", "ros2_service_response"},
      {"ROS2 Action Client", "ros2_service_response"},
      {"ROS2 Node Monitor", "ros2_nodes"},
      {"ROS2 Topic Monitor", "ros2_topics"},
      {"ROS2 Parameter Server", "ros2_parameter"},
      {"ROS2 TF Monitor", "ros2_transforms"},
      {"ROS2 Bag Recorder", "ros2_bag_recording"},
      {"ROS2 Bag Player", "ros2_bag_playback"},
      {"ROS2 Diagnostics", "ros2_diagnostics"},
      {"ROS2 Image Stream", "image_frame"},
      {"ROS2 Pointcloud Viewer", "point_cloud"},
      {"ROS2 Laser Scan", "laser_scan"},
      {"ROS2 Odometry", "odometry"},
      {"ROS2 Velocity Controller", "ros2_twist"},
      {"ROS2 Joint State", "joint_state"},
      {"ROS2 Map Viewer", "occupancy_grid"},
      {"ROS2 Goal Sender", "ros2_goal"},
      {"ROS2 Lifecycle Manager", "ros2_lifecycle"},
      // IoT
      {"MQTT Subscriber", "mqtt_message"},
      {"HTTP Poller", "http_response"},
      {"Webhook Receiver", "http_response"},
      {"Serial Monitor", "serial_data"},
      {"GPIO Controller", "gpio_pin"},
      {"I²C / SPI Sensor", "i2c_register"},
      {"Modbus RTU/TCP", "modbus_register"},
      {"OPC-UA Client", "opcua_node"},
      {"CAN Bus Monitor", "can_frame"},
      {"Zigbee/Z-Wave Device", "zigbee_device"},
      {"BLE Scanner", "ble_advertisement"},
      {"GPS / GNSS", "gps_fix"},
      {"IMU", "imu"},
      {"Temperature / Humidity", "temperature"},
      {"Power Monitor", "power"},
      {"Camera Capture", "image_frame"},
      {"Relay Controller", "relay"},
      {"PID Controller", "pid"},
      {"Docker Monitor", "docker_containers"},
      {"Systemd Service", "systemd_unit"},
      // Analytical
      {"Histogram", "statistics"},
      {"Scatter Plot", "correlation"},
      {"Correlation Matrix", "correlation"},
      {"FFT Spectrum", "fft_spectrum"},
      {"Anomaly Detector", "anomaly"},
      {"Rolling Statistics", "statistics"},
      {"Regression", "regression"},
      {"Event Counter", "event_count"},
      {"Rate Meter", "rate"},
      {"Latency Monitor", "latency"},
      {"Threshold Alarm", "alarm"},
      {"Data Table", "statistics"},
  };
  const auto found = builders.find(preset);
  return found == builders.end() ? nullptr : found->second;
}

inline const char* non_capturing_reason(const std::string& preset) {
  // Presets a device never feeds, and why. A control sends a command rather than
  // capturing data; an analytical module draws a stream it is given.
  static const std::map<std::string, const char*> reasons = {
      {"Data Logger", "records a stream it is pointed at"},
      {"ROS2 Topic Publisher", "sends a command"},
      {"MQTT Publisher", "sends a command"},
      {"Homing", "sends a command"},
      {"Live Plotter", "draws a stream"},
      {"Gauge", "draws a value"},
      {"Stat Tile", "draws a value"},
      {"Heatmap", "buckets a stream server-side"},
      {"Comparison View", "compares two sessions"},
      {"Report Generator", "compiles the session log"},
  };
  const auto found = reasons.find(preset);
  return found == reasons.end() ? nullptr : found->second;
}

namespace detail {

inline std::string base64(const std::vector<unsigned char>& bytes) {
  static const char* alphabet =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::string out;
  out.reserve(((bytes.size() + 2) / 3) * 4);
  size_t index = 0;
  while (index + 2 < bytes.size()) {
    const uint32_t chunk = (static_cast<uint32_t>(bytes[index]) << 16) |
                           (static_cast<uint32_t>(bytes[index + 1]) << 8) |
                           static_cast<uint32_t>(bytes[index + 2]);
    out += alphabet[(chunk >> 18) & 0x3F];
    out += alphabet[(chunk >> 12) & 0x3F];
    out += alphabet[(chunk >> 6) & 0x3F];
    out += alphabet[chunk & 0x3F];
    index += 3;
  }
  const size_t remaining = bytes.size() - index;
  if (remaining == 1) {
    const uint32_t chunk = static_cast<uint32_t>(bytes[index]) << 16;
    out += alphabet[(chunk >> 18) & 0x3F];
    out += alphabet[(chunk >> 12) & 0x3F];
    out += "==";
  } else if (remaining == 2) {
    const uint32_t chunk = (static_cast<uint32_t>(bytes[index]) << 16) |
                           (static_cast<uint32_t>(bytes[index + 1]) << 8);
    out += alphabet[(chunk >> 18) & 0x3F];
    out += alphabet[(chunk >> 12) & 0x3F];
    out += alphabet[(chunk >> 6) & 0x3F];
    out += '=';
  }
  return out;
}

inline bool printable(const std::string& text) {
  for (unsigned char character : text) {
    if (character < 0x20 && character != '\t') return false;
  }
  return true;
}

}  // namespace detail

inline Events image_frame(const std::string& encoded_or_url, const std::string& mime) {
  const std::string url = encoded_or_url.rfind("data:", 0) == 0
                              ? encoded_or_url
                              : "data:" + mime + ";base64," + encoded_or_url;
  Json frame = Json::object();
  frame.set("kind", "shape");
  frame.set("shape", "image");
  frame.set("image", url);
  return {frame};
}

inline Events image_frame(const std::vector<unsigned char>& bytes, const std::string& mime) {
  Json frame = Json::object();
  frame.set("kind", "shape");
  frame.set("shape", "image");
  frame.set("image", "data:" + mime + ";base64," + detail::base64(bytes));
  return {frame};
}

inline Events serial_data(const std::string& text) { return {event::log(text)}; }

inline Events serial_data(const std::vector<unsigned char>& bytes) {
  const std::string text(bytes.begin(), bytes.end());
  if (detail::printable(text)) return {event::log(text)};

  std::string hex;
  for (unsigned char byte : bytes) {
    char pair[4];
    std::snprintf(pair, sizeof(pair), "%02x ", byte);
    hex += pair;
  }
  if (!hex.empty()) hex.pop_back();
  return {event::log(hex)};
}

}  // namespace data
}  // namespace buildbox
