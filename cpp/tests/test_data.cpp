// Tests for the reading builders: every preset is fed, and every reading is one
// the server will accept.
//
//     g++ -std=c++17 -I../include test_data.cpp -o test_data -lws2_32 && ./test_data

#include <buildbox/client.hpp>
#include <buildbox/data.hpp>
#include <buildbox/presets.hpp>

#include <cmath>
#include <functional>
#include <iostream>
#include <string>
#include <vector>

namespace {

int failures = 0;

void check(const std::string& label, bool condition, const std::string& detail = "") {
  std::cout << (condition ? "  [ok  ] " : "  [FAIL] ") << label;
  if (!detail.empty()) std::cout << " — " << detail;
  std::cout << "\n";
  if (!condition) ++failures;
}

const std::vector<std::string> KINDS = {"sample", "log", "status", "shape"};
const std::vector<std::string> SEVERITIES = {"info", "warn", "error"};
const std::vector<std::string> STATUSES = {"ok", "warn", "error", "idle", "armed", "fired"};
const std::vector<std::string> SHAPES = {"scan", "cloud", "joints", "map", "trail", "image"};

bool contains(const std::vector<std::string>& haystack, const std::string& needle) {
  return std::find(haystack.begin(), haystack.end(), needle) != haystack.end();
}

/// Checks one batch the way the server would, so a builder that emits something
/// undrawable is caught here rather than at the socket.
std::vector<std::string> problems_with(const buildbox::data::Events& events, const std::string& where) {
  std::vector<std::string> problems;
  if (events.empty()) {
    problems.push_back(where + " returned nothing at all");
    return problems;
  }
  for (const buildbox::Json& event : events) {
    const std::string kind = event["kind"].string_or("");
    if (!contains(KINDS, kind)) {
      problems.push_back(where + " produced kind " + kind);
      continue;
    }
    if (kind == "sample") {
      if (event["key"].string_or("").empty()) problems.push_back(where + ": sample needs a key");
      if (!event["value"].is_number()) problems.push_back(where + ": sample value must be a number");
      else if (!std::isfinite(event["value"].as_number())) {
        problems.push_back(where + ": sample value must be finite");
      }
    } else if (kind == "log") {
      if (event["message"].string_or("").empty()) problems.push_back(where + ": log needs a message");
      if (!contains(SEVERITIES, event["severity"].string_or(""))) {
        problems.push_back(where + ": bad severity");
      }
    } else if (kind == "status") {
      if (!contains(STATUSES, event["status"].string_or(""))) {
        problems.push_back(where + ": bad status");
      }
    } else if (kind == "shape") {
      const std::string shape = event["shape"].string_or("");
      if (!contains(SHAPES, shape)) {
        problems.push_back(where + ": bad shape " + shape);
      } else if (shape == "image") {
        if (event["image"].string_or("").empty()) problems.push_back(where + ": image needs data");
      } else if (event["points"].items().empty()) {
        problems.push_back(where + ": a " + shape + " shape needs points");
      } else if (shape == "map") {
        const size_t cells = event["points"].items().size();
        if (cells != static_cast<size_t>(event["cols"].as_int()) *
                         static_cast<size_t>(event["rows"].as_int())) {
          problems.push_back(where + ": map points must fill the grid");
        }
      }
    }
  }
  return problems;
}

using Builder = std::pair<std::string, std::function<buildbox::data::Events()>>;

const std::vector<Builder>& examples() {
  using namespace buildbox::data;
  static const std::vector<Builder> all = {
      // General
      {"Kill Switch", [] { return kill_switch_outcome(true, 0); }},
      {"Script Runner", [] { return script_output({"one", "two"}, 0); }},
      {"Health Check", [] { return health_check(12.5); }},
      {"CLI Terminal", [] { return script_output({"$ uptime"}); }},
      {"Tailscale Peer", [] { return tailscale_peer("robot-01", true, "100.64.0.5"); }},
      {"AI API", [] { return ai_response("nominal", 120.0); }},
      {"Generic Control", [] { return script_output({"done"}); }},
      // ROS2
      {"ROS2 Topic Subscriber", [] { return ros2_message("/battery", {{"voltage", 12.4}}); }},
      {"ROS2 Service Client", [] { return ros2_service_response("/calibrate", true); }},
      {"ROS2 Action Client", [] { return ros2_service_response("/navigate", false, "rejected"); }},
      {"ROS2 Node Monitor", [] { return ros2_nodes({{"/amcl", true}, {"/scan", false}}); }},
      {"ROS2 Topic Monitor", [] { return ros2_topics({{"/scan", "LaserScan"}}, {12.0}); }},
      {"ROS2 Parameter Server", [] { return ros2_parameter("max_speed", 1.5, true); }},
      {"ROS2 TF Monitor", [] { return ros2_transforms({{"map", "odom", false}}); }},
      {"ROS2 Bag Recorder", [] { return ros2_bag_recording("/bags/run1", 12.0, 4096.0); }},
      {"ROS2 Bag Player", [] { return ros2_bag_playback("/bags/run1", 3.5); }},
      {"ROS2 Diagnostics", [] { return ros2_diagnostics({{"imu", "warn", "drift"}}); }},
      {"ROS2 Image Stream", [] { return image_frame(std::vector<unsigned char>{0x89, 0x50}); }},
      {"ROS2 Pointcloud Viewer", [] { return point_cloud({0.0, 0.0, 1.0, 1.0, 0.0, 1.0}); }},
      {"ROS2 Laser Scan", [] { return laser_scan({1.0, 1.2}, -1.57, 1.57); }},
      {"ROS2 Odometry", [] { return odometry(1.0, 2.0, 0.3, 0.5); }},
      {"ROS2 Velocity Controller", [] { return ros2_twist(0.5, 0.1); }},
      {"ROS2 Joint State", [] { return joint_state({0.1, 0.2}, {"left", "right"}, {0.0, 0.1}); }},
      {"ROS2 Map Viewer", [] { return occupancy_grid({0, 50, 100, 0}, 2, 2); }},
      {"ROS2 Goal Sender", [] { return ros2_goal(3.0, 4.0); }},
      {"ROS2 Lifecycle Manager", [] { return ros2_lifecycle("/camera", "active"); }},
      // IoT
      {"MQTT Subscriber", [] { return mqtt_message("home/temp", "21.5", 21.5); }},
      {"HTTP Poller", [] { return http_response(21.5, "http://host/state", 200, 8.0); }},
      {"Webhook Receiver", [] { return http_response(std::nullopt, "http://host/hook", 200, std::nullopt, "{}"); }},
      {"Serial Monitor", [] { return serial_data(std::vector<unsigned char>{0x01, 0x02}); }},
      {"GPIO Controller", [] { return gpio_pin(17, 1); }},
      {"I²C / SPI Sensor", [] { return i2c_register(0x44, 0, 21.5); }},
      {"Modbus RTU/TCP", [] { return modbus_register(40001, 23.5); }},
      {"OPC-UA Client", [] { return opcua_node("ns=2;s=Temp", 21.5); }},
      {"CAN Bus Monitor", [] { return can_frame(0x123, {0x01, 0x02}); }},
      {"Zigbee/Z-Wave Device", [] { return zigbee_device("lamp", "on", 180.0, 3.0); }},
      {"BLE Scanner", [] { return ble_advertisement("Beacon", -62.0, "aa:bb"); }},
      {"GPS / GNSS", [] { return gps_fix(-37.8, 144.9, 30.0, 1.2, 90.0, 9.0); }},
      {"IMU", [] { return imu({0.0, 0.0, 9.81}, {0.0, 0.0, 0.0}, {1.0, 2.0, 3.0}); }},
      {"Temperature / Humidity", [] { return temperature(21.5, 44.0); }},
      {"Power Monitor", [] { return power(12.4, 1.2); }},
      {"Camera Capture", [] { return image_frame(std::vector<unsigned char>{0x00, 0x01}, "image/jpeg"); }},
      {"Relay Controller", [] { return relay(1, true, "pump"); }},
      {"PID Controller", [] { return pid(1.0, 0.9, 0.2); }},
      {"Docker Monitor", [] { return docker_containers({{"web", "Up 3 hours"}}); }},
      {"Systemd Service", [] { return systemd_unit("robot.service", "active"); }},
      // Analytical
      {"Histogram", [] { return statistics("temp", {{"min", 20.0}, {"max", 23.0}}); }},
      {"Scatter Plot", [] { return correlation("speed", "current", 0.82); }},
      {"Correlation Matrix", [] { return correlation("a", "b", -0.4, "spearman"); }},
      {"FFT Spectrum", [] { return fft_spectrum({1.0, 2.0, 3.0}, {0.1, 0.9, 0.2}); }},
      {"Anomaly Detector", [] { return anomaly("temp", 99.0, 4.2, true); }},
      {"Rolling Statistics", [] { return statistics("temp", {{"mean", 21.5}, {"std", 0.4}}); }},
      {"Regression", [] { return regression("x", "y", 2.0, 1.0, 0.98); }},
      {"Event Counter", [] { return event_count("faults", 3.0); }},
      {"Rate Meter", [] { return rate("messages", 42.0); }},
      {"Latency Monitor", [] { return latency("round trip", 12.5); }},
      {"Threshold Alarm", [] { return alarm("temp", true, 91.0, "hi", true); }},
      {"Data Table", [] { return statistics("temp", {{"count", 10.0}}); }},
  };
  return all;
}

void test_coverage() {
  std::cout << "\ncoverage\n";
  const std::vector<std::string> missing = buildbox::data::uncovered();
  std::string joined;
  for (const std::string& name : missing) joined += (joined.empty() ? "" : ", ") + name;

  check("every catalogue preset is accounted for", missing.empty(), joined);
  check("the catalogue has presets to check", !buildbox::presets().empty(),
        std::to_string(buildbox::presets().size()) + " presets");

  // Both directions: nothing missing, and nothing named that does not exist.
  std::vector<std::string> invented;
  for (const buildbox::Preset& preset : buildbox::presets()) {
    const bool known = buildbox::data::preset_builder_name(preset.name) != nullptr ||
                       buildbox::data::non_capturing_reason(preset.name) != nullptr;
    if (!known) invented.push_back(preset.name);
  }
  check("no preset lacks both a builder and a reason", invented.empty());

  size_t capturing = 0;
  for (const buildbox::Preset& preset : buildbox::presets()) {
    if (buildbox::data::preset_builder_name(preset.name) != nullptr) ++capturing;
  }
  std::cout << "        " << capturing << " of " << buildbox::presets().size()
            << " presets are fed by a builder\n";
}

void test_every_builder_has_an_example() {
  std::cout << "\nexamples\n";
  std::vector<std::string> unexercised;
  for (const buildbox::Preset& preset : buildbox::presets()) {
    if (buildbox::data::preset_builder_name(preset.name) == nullptr) continue;
    bool found = false;
    for (const Builder& example : examples()) {
      if (example.first == preset.name) {
        found = true;
        break;
      }
    }
    if (!found) unexercised.push_back(preset.name);
  }
  std::string joined;
  for (const std::string& name : unexercised) joined += (joined.empty() ? "" : ", ") + name;
  check("every builder is exercised by an example", unexercised.empty(), joined);
}

void test_each_builder_produces_legal_events() {
  std::cout << "\nreadings\n";
  std::vector<std::string> all_problems;
  std::vector<std::string> shapes_seen;

  for (const Builder& example : examples()) {
    const buildbox::data::Events events = example.second();
    for (const std::string& problem : problems_with(events, example.first)) {
      all_problems.push_back(problem);
    }
    for (const buildbox::Json& event : events) {
      if (event["kind"].string_or("") == "shape") {
        const std::string shape = event["shape"].string_or("");
        if (!contains(shapes_seen, shape)) shapes_seen.push_back(shape);
      }
    }
  }

  std::string joined;
  for (const std::string& problem : all_problems) joined += "\n          " + problem;
  check("every builder produces legal events", all_problems.empty(), joined);
  check("all six structural readings are produced by something", shapes_seen.size() == 6,
        std::to_string(shapes_seen.size()) + " of 6");
}

void test_the_rules() {
  std::cout << "\nthe rules\n";

  // Prose must not become a number.
  const buildbox::data::Events prose = buildbox::data::ai_response("the answer is 42");
  bool all_logs = true;
  for (const buildbox::Json& event : prose) all_logs = all_logs && event["kind"].string_or("") == "log";
  check("text is a log line and never a sample", all_logs);

  // A field the device could not read is omitted, not zeroed.
  const buildbox::data::Events temp = buildbox::data::temperature(21.5);
  check("a missing field is omitted rather than guessed", temp.size() == 1,
        std::to_string(temp.size()) + " events");

  // No fix is not a position of zero.
  const buildbox::data::Events no_fix = buildbox::data::gps_no_fix();
  bool has_position = false;
  for (const buildbox::Json& event : no_fix) {
    const std::string key = event["key"].string_or("");
    if (key == "latitude" || key == "longitude") has_position = true;
  }
  check("no fix is not a position", !has_position);

  // An unreachable host is not a zero-latency success.
  const buildbox::data::Events dead = buildbox::data::health_check(0.0, false, "no route");
  bool sampled = false;
  bool errored = false;
  for (const buildbox::Json& event : dead) {
    if (event["kind"].string_or("") == "sample") sampled = true;
    if (event["status"].string_or("") == "error") errored = true;
  }
  check("an unreachable health check is not a success", !sampled && errored);

  // Arithmetic the caller should not have to repeat.
  const buildbox::data::Events watts = buildbox::data::power(12.0, 2.0);
  bool computed = false;
  for (const buildbox::Json& event : watts) {
    if (event["key"].string_or("") == "power" && event["value"].as_number() == 24.0) computed = true;
  }
  check("power computes watts when it is not given", computed);

  const buildbox::data::Events loop = buildbox::data::pid(1.0, 0.75, 0.1);
  bool error_computed = false;
  for (const buildbox::Json& event : loop) {
    if (event["key"].string_or("") == "error" &&
        std::abs(event["value"].as_number() - 0.25) < 1e-9) {
      error_computed = true;
    }
  }
  check("pid computes the error", error_computed);

  // A frame is a drawable data URL.
  const buildbox::data::Events frame =
      buildbox::data::image_frame(std::vector<unsigned char>{0x89, 0x50, 0x4E});
  bool data_url = false;
  for (const buildbox::Json& event : frame) {
    if (event["kind"].string_or("") == "shape") {
      data_url = event["image"].string_or("").rfind("data:image/png;base64,", 0) == 0;
    }
  }
  check("a frame is encoded as a data URL", data_url);

  // Bytes that are not text are shown as hex.
  const buildbox::data::Events raw =
      buildbox::data::serial_data(std::vector<unsigned char>{0x00, 0x01, 0x02});
  check("bytes that are not text are shown as hex",
        raw[0]["message"].string_or("") == "00 01 02",
        raw[0]["message"].string_or("?"));

  // A malformed argument is refused rather than sent.
  bool threw = false;
  try {
    buildbox::data::occupancy_grid({1, 2, 3}, 2, 2);
  } catch (const std::invalid_argument&) {
    threw = true;
  }
  check("a map that does not fill its grid is refused", threw);

  threw = false;
  try {
    buildbox::data::correlation("a", "b", 1.5);
  } catch (const std::invalid_argument&) {
    threw = true;
  }
  check("a correlation outside -1..1 is refused", threw);
}

}  // namespace

int main() {
  test_coverage();
  test_every_builder_has_an_example();
  test_each_builder_produces_legal_events();
  test_the_rules();

  std::cout << "\n" << (failures == 0 ? "ALL TESTS PASSED" : std::to_string(failures) + " FAILED")
            << "\n";
  return failures == 0 ? 0 : 1;
}
