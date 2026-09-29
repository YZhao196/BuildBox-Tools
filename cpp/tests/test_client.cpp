// Unit tests for the parts that do not need a server.
//
//     g++ -std=c++17 -I../include test_client.cpp -o test_client && ./test_client
//
// The wire contract itself is exercised over a real socket by the management
// library's suite (`management/tests/test_server.py`), which drives the Python
// binding against a real receiver, and against the real BuildBox server by that
// repository's `apps/server/src/routes/devices.test.ts` — a path in a checkout
// this file cannot see, so it is named rather than depended on. What is checked
// here is that this binding encodes and decodes the same contract.

#include <buildbox/client.hpp>
#include <buildbox/json.hpp>

#include <iostream>
#include <string>

namespace {

int failures = 0;

void check(const std::string& label, bool condition, const std::string& detail = "") {
  std::cout << (condition ? "  [ok  ] " : "  [FAIL] ") << label;
  if (!detail.empty()) std::cout << " — " << detail;
  std::cout << "\n";
  if (!condition) ++failures;
}

void test_json_round_trip() {
  std::cout << "\njson\n";
  const std::string source = R"({"a":1,"b":"two","c":[true,null,2.5],"d":{"e":"f"}})";
  const buildbox::Json parsed = buildbox::Json::parse(source);
  const buildbox::Json reparsed = buildbox::Json::parse(parsed.dump());

  check("object member reads back", parsed["a"].as_int() == 1);
  check("string member reads back", parsed["b"].as_string() == "two");
  check("array length survives", parsed["c"].items().size() == 3);
  check("nested member reads back", parsed["d"]["e"].as_string() == "f");
  check("dump then parse is stable", reparsed["b"].as_string() == "two");
  check("a missing member is null, not a crash", parsed["zzz"].is_null());
}

void test_json_escapes() {
  std::cout << "\njson escaping\n";
  buildbox::Json value = buildbox::Json::object();
  value.set("m", std::string("line\nbreak \"quoted\" \\slash"));
  const buildbox::Json back = buildbox::Json::parse(value.dump());
  check("escaped characters survive a round trip",
        back["m"].as_string() == "line\nbreak \"quoted\" \\slash");

  // A unit string like a degree sign is UTF-8 on the wire and must not be
  // mangled by the writer.
  buildbox::Json unit = buildbox::Json::object();
  unit.set("u", std::string("\xc2\xb0""C"));
  const buildbox::Json unit_back = buildbox::Json::parse(unit.dump());
  check("utf-8 passes through unchanged", unit_back["u"].as_string() == "\xc2\xb0""C");

  // A producer may write a non-ASCII code point as an escape instead.
  const buildbox::Json escaped = buildbox::Json::parse(R"({"u":"°C"})");
  check("\\u escapes decode to utf-8", escaped["u"].as_string() == "\xc2\xb0""C");
}

void test_events() {
  std::cout << "\nevents\n";
  const buildbox::Json sample = buildbox::event::sample("temperature", 21.5, "C");
  check("a sample names its kind", sample["kind"].as_string() == "sample");
  check("a sample carries its value", sample["value"].as_number() == 21.5);
  check("a sample carries its unit", sample["unit"].as_string() == "C");

  const buildbox::Json bare = buildbox::event::sample("temperature", 21.5);
  check("a sample without a unit omits the field", bare["unit"].is_null());

  const buildbox::Json shape = buildbox::event::shape("scan", {1.0, 1.2, 1.1});
  check("a shape lists its points", shape["points"].items().size() == 3);
  check("a shape names itself", shape["shape"].as_string() == "scan");
}

void test_commands() {
  std::cout << "\ncommands\n";
  buildbox::Command command;
  command.action = "run";
  command.cmd = "sudo systemctl stop robot";

  check("matching finds a whole word", command.matches("stop"));
  check("matching ignores case", command.matches("SYSTEMCTL"));
  check("matching finds words in order", command.matches("stop robot"));
  check("matching rejects what is absent", !command.matches("start"));
  check("a fragment of a word does not match", !command.matches("sto"));
  buildbox::Command homebridge;
  homebridge.cmd = "stop homebridge";
  check("'home' does not match inside 'homebridge'", !homebridge.matches("home"));

  command.action = "read";
  check("read is a read", command.reads());
  command.action = "run";
  check("run is a write", !command.reads());
}

void test_results() {
  std::cout << "\nresults\n";
  check("exit code zero is success", buildbox::Result::exit_code(0).ok);
  check("exit code zero is carried", buildbox::Result::exit_code(0).code == 0);
  check("a non-zero exit code failed", !buildbox::Result::exit_code(1).ok);
  check("a failure carries its reason",
        buildbox::Result::failure("no").reason == "no");
  check("text becomes output", buildbox::Result::text("hi").output.size() == 1);
  check("success with nothing to say is ok", buildbox::Result::success().ok);
}

void test_client_construction() {
  std::cout << "\nclient\n";
  bool threw = false;
  try {
    buildbox::Client client("http://example.test", "");
  } catch (const buildbox::BuildBoxError&) {
    threw = true;
  }
  check("a client without a token is refused", threw);

  threw = false;
  try {
    buildbox::Client client("https://example.test", "token");
  } catch (const buildbox::BuildBoxError& error) {
    // Plain HTTP only; saying so plainly beats failing obscurely at connect time.
    threw = std::string(error.what()).find("TLS") != std::string::npos;
  }
  check("TLS is refused with an explanation", threw);
}

buildbox::Command make_command(const std::string& id, const std::string& text) {
  buildbox::Command command;
  command.cmd_id = id;
  command.module_id = "m";
  command.action = "run";
  command.cmd = text;
  return command;
}

std::string first_line(const buildbox::Result& result) {
  return result.output.empty() ? "" : result.output[0];
}

void test_routing() {
  std::cout << "\nrouting\n";
  buildbox::Client client("http://example.test", "token", "m");
  // Registered first, so substring matching with registration-order ties
  // would hand it a stop.
  client.on_command_match("home", [](const buildbox::Command&) { return buildbox::Result::text("home"); });
  client.on_command_match("stop", [](const buildbox::Command&) { return buildbox::Result::text("stop"); });
  client.on_command_match("stop home", [](const buildbox::Command&) {
    return buildbox::Result::text("stop home");
  });

  const buildbox::Result stop = client.dispatch(make_command("c", "sudo systemctl stop homebridge"));
  check("a stop mentioning homebridge reaches the stop handler", first_line(stop) == "stop",
        "went to '" + first_line(stop) + "'");
  check("the longest match wins",
        first_line(client.dispatch(make_command("c", "stop home now"))) == "stop home");

  const buildbox::Result tie = client.dispatch(make_command("c", "home then stop"));
  check("an equal tie is refused, not settled by registration order",
        !tie.ok && tie.reason.find("ambiguous") != std::string::npos, tie.reason);

  buildbox::Client coded("http://example.test", "token", "m");
  coded.on_command([](const buildbox::Command&) { return buildbox::Result::exit_code(3); });
  const buildbox::Result failed = coded.dispatch(make_command("c", "anything"));
  check("a failure without a reason is given one naming the exit code",
        !failed.ok && failed.reason.find('3') != std::string::npos, failed.reason);

  bool threw = false;
  try {
    client.on_command_match("STOP", [](const buildbox::Command&) { return buildbox::Result::success(); });
  } catch (const buildbox::BuildBoxError&) {
    threw = true;
  }
  check("registering the same match twice is an error", threw);
}

void test_redelivery() {
  std::cout << "\nredelivery\n";
  buildbox::Client client("http://example.test", "token", "m");
  std::vector<std::string> posted;
  client.set_transport([&posted](const std::string&, const std::string&, const std::string&,
                                 const std::string& body, int) {
    posted.push_back(body);
    buildbox::HttpResponse response;
    response.status = 200;
    return response;
  });
  int runs = 0;
  client.on_command([&runs](const buildbox::Command&) {
    ++runs;
    buildbox::Result result = buildbox::Result::failure("the relay did not move");
    result.code = 2;
    return result;
  });

  const buildbox::Command command = make_command("c2", "stop");
  client.apply(command);
  const buildbox::Result again = client.apply(command);

  check("a redelivered command is not run twice", runs == 1);
  check("a redelivered failure is not answered as a success", !again.ok);
  check("the original reason is replayed", again.reason == "the relay did not move", again.reason);
  check("the same report is sent both times", posted.size() == 2 && posted[0] == posted[1]);
}

}  // namespace

int main() {
  test_json_round_trip();
  test_json_escapes();
  test_events();
  test_commands();
  test_results();
  test_client_construction();
  test_routing();
  test_redelivery();

  std::cout << "\n" << (failures == 0 ? "ALL TESTS PASSED" : std::to_string(failures) + " FAILED")
            << "\n";
  return failures == 0 ? 0 : 1;
}
