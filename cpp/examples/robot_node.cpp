// A device on a robot: report a reading, and stop when it is told to.
//
//     g++ -std=c++17 -I../include robot_node.cpp -o robot_node
//     BUILDBOX_URL=http://127.0.0.1:8787 BUILDBOX_TOKEN=<token> ./robot_node
//
// Mint the token from the interface. It is shown once, and scoping it to a
// module is what lets it report against that module and nothing else.

#include <buildbox/client.hpp>

#include <chrono>
#include <cstdlib>
#include <iostream>
#include <random>
#include <string>
#include <thread>

namespace {

std::string from_environment(const char* name, const std::string& fallback = "") {
  const char* value = std::getenv(name);
  return value ? std::string(value) : fallback;
}

double read_temperature(std::mt19937& generator) {
  std::uniform_real_distribution<double> spread(-0.6, 0.6);
  return 21.0 + spread(generator);
}

}  // namespace

int main() {
  const std::string url = from_environment("BUILDBOX_URL", "http://127.0.0.1:8787");
  const std::string token = from_environment("BUILDBOX_TOKEN");
  if (token.empty()) {
    std::cerr << "No device token. Set BUILDBOX_TOKEN.\n";
    return 2;
  }

  buildbox::Client bb(url, token);
  bool stopped = false;

  // Specific handlers win over the catch-all below, whichever order they are
  // registered in.
  bb.on_command_match("kill_switch", [&stopped](const buildbox::Command& command) {
    // Returning an exit code is the whole answer: 0 means it stopped, anything
    // else means it did not, and the server reports that to whoever asked. This
    // is the one answer in the system that must never be optimistic.
    if (command.matches("1")) {
      stopped = true;
      return buildbox::Result::exit_code(0);
    }
    buildbox::Result result = buildbox::Result::exit_code(1);
    result.reason = "Not stopped: expected \"kill_switch 1\".";
    return result;
  });

  // Replace `home_axes` with your motion controller's homing routine. This
  // example has no axes, so it cannot home anything, and says so.
  const auto home_axes = []() { return false; };
  bb.on_command_match("home", [&home_axes](const buildbox::Command&) {
    if (home_axes()) return buildbox::Result::exit_code(0);
    buildbox::Result result =
        buildbox::Result::failure("Not homed: home_axes() is not wired to a motion controller.");
    result.code = 1;
    return result;
  });

  // Everything else is refused, never acknowledged as done: answering success
  // would tell whoever pressed the button that something happened when nothing did.
  bb.on_command([](const buildbox::Command& command) {
    buildbox::Result result = buildbox::Result::failure(
        "This device has no handler for \"" + command.action + ": " + command.cmd +
        "\". Nothing was done.");
    result.code = 1;
    return result;
  });

  try {
    const buildbox::Json who = bb.whoami();
    std::cout << "connected as " << who["device"]["label"].string_or("?") << "\n";
    for (const buildbox::Json& module : who["modules"].items()) {
      std::cout << "  reporting on " << module["name"].string_or("?") << "\n";
    }
  } catch (const std::exception& error) {
    std::cerr << "could not connect: " << error.what() << "\n";
    return 1;
  }

  std::mt19937 generator(std::random_device{}());
  std::cout << "sending a reading every second; Ctrl-C to stop\n";

  while (!stopped) {
    try {
      bb.send_data("temperature", read_temperature(generator), "C");
      // `check` answers anything waiting without blocking, so a device with its
      // own loop keeps its own rhythm.
      bb.check(0.0);
    } catch (const std::exception& error) {
      std::cerr << "send failed: " << error.what() << "\n";
    }
    std::this_thread::sleep_for(std::chrono::seconds(1));
  }

  std::cout << "stopped\n";
  return 0;
}
