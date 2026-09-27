// A device's connection to a server, in C++.
//
//     #include <buildbox/client.hpp>
//
//     buildbox::Client bb("http://127.0.0.1:8787", token);
//
//     bb.on_command_match("kill_switch", [](const buildbox::Command& command) {
//       return buildbox::Result::exit_code(0);   // stopped
//     });
//
//     while (true) {
//       bb.send_data("temperature", read_sensor(), "C");
//       bb.check();                                // answer anything waiting
//     }
//
// Header-only and dependency-free, on purpose. A robot's build is the last
// place to introduce a library, and the protocol needs so little that a socket
// and a small JSON value are the whole of it. That does mean plain HTTP only:
// this speaks `http://`, and a deployment that needs TLS should terminate it at
// a proxy rather than expecting this to grow a TLS stack.
//
// The two rules the Python binding is built around hold here too, because the
// server enforces them either way: a write is never invented, and readings are
// always labelled as having come from a device.

#pragma once

#include <algorithm>
#include <chrono>
#include <cstring>
#include <functional>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "buildbox/json.hpp"

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
// Winsock is a library, not just headers. MSVC can be told here; MinGW has no
// equivalent pragma, so it needs `-lws2_32` on the command line (the CMake
// project adds it, and the README says so for hand-rolled builds).
#if defined(_MSC_VER)
#pragma comment(lib, "ws2_32.lib")
#endif
using socket_handle = SOCKET;
#define BUILDBOX_CLOSE_SOCKET closesocket
#define BUILDBOX_INVALID_SOCKET INVALID_SOCKET
#else
#include <netdb.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>
using socket_handle = int;
#define BUILDBOX_CLOSE_SOCKET ::close
#define BUILDBOX_INVALID_SOCKET (-1)
#endif

namespace buildbox {

/// The protocol version this header speaks.
inline constexpr const char* kVersion = "bbp/1";

/// What a device calls itself, in the server's log line.
inline constexpr const char* kAgent = "cpp/0.1.0";

/* ------------------------------------------------------------------ */
/* Errors                                                              */
/* ------------------------------------------------------------------ */

class BuildBoxError : public std::runtime_error {
 public:
  explicit BuildBoxError(const std::string& what) : std::runtime_error(what) {}
};

/// The token is missing, unknown, or revoked.
class AuthError : public BuildBoxError {
 public:
  using BuildBoxError::BuildBoxError;
};

/// The device is not scoped to that module.
class ScopeError : public BuildBoxError {
 public:
  using BuildBoxError::BuildBoxError;
};

/// The reading is not one that module's preset could have produced.
class ShapeError : public BuildBoxError {
 public:
  using BuildBoxError::BuildBoxError;
};

/* ------------------------------------------------------------------ */
/* Values                                                              */
/* ------------------------------------------------------------------ */

/// What a handler decided, in the form the server takes.
struct Result {
  bool ok = true;
  std::vector<std::string> output;
  /// Exit code, or -1 for "not applicable". A code of 0 means it worked.
  int code = -1;
  std::string reason;

  static Result success(std::vector<std::string> output = {}) {
    Result result;
    result.output = std::move(output);
    return result;
  }

  /// A shell-style verdict: 0 worked, anything else did not.
  static Result exit_code(int code) {
    Result result;
    result.ok = (code == 0);
    result.code = code;
    return result;
  }

  static Result failure(std::string reason) {
    Result result;
    result.ok = false;
    result.reason = std::move(reason);
    return result;
  }

  static Result text(std::string line) {
    Result result;
    result.output.push_back(std::move(line));
    return result;
  }
};

/// Something the server has asked this device to do.
struct Command {
  std::string cmd_id;
  std::string module_id;
  std::string action;
  std::string cmd;
  std::string target;
  int timeout_ms = 30000;

  /// Whether the command text contains `text`, case-insensitively.
  ///
  /// Substring rather than equality because the command field is free text a
  /// person typed — `sudo systemctl stop robot` should reach a handler for
  /// `stop` without the operator knowing this library's rules.
  bool matches(const std::string& text) const {
    std::string haystack;
    haystack.reserve(cmd.size());
    for (char character : cmd) {
      haystack += static_cast<char>(std::tolower(static_cast<unsigned char>(character)));
    }
    std::string needle;
    needle.reserve(text.size());
    for (char character : text) {
      needle += static_cast<char>(std::tolower(static_cast<unsigned char>(character)));
    }
    return haystack.find(needle) != std::string::npos;
  }

  /// Whether this only observes. Writes have already been confirmed server-side.
  bool reads() const {
    static const std::vector<std::string> reads = {"read",  "check", "poll",    "listen",
                                                   "scan",  "subscribe", "open", "capture"};
    return std::find(reads.begin(), reads.end(), action) != reads.end();
  }
};

/* ------------------------------------------------------------------ */
/* Events                                                              */
/* ------------------------------------------------------------------ */

/// Builders for the four things a device can report.
namespace event {

inline Json sample(const std::string& key, double value, const std::string& unit = "") {
  Json json = Json::object();
  json.set("kind", "sample");
  json.set("key", key);
  json.set("value", value);
  if (!unit.empty()) json.set("unit", unit);
  return json;
}

inline Json log(const std::string& message, const std::string& severity = "info") {
  Json json = Json::object();
  json.set("kind", "log");
  json.set("message", message);
  json.set("severity", severity);
  return json;
}

inline Json status(const std::string& value) {
  Json json = Json::object();
  json.set("kind", "status");
  json.set("status", value);
  return json;
}

/// A reading with structure: scan, cloud, joints, map, trail or image.
///
/// The server checks this against the module's preset and refuses a reading that
/// module could not have produced, so a laser sweep arriving on a temperature
/// module is rejected rather than drawn.
inline Json shape(const std::string& kind, const std::vector<double>& points) {
  Json json = Json::object();
  json.set("kind", "shape");
  json.set("shape", kind);
  Json list = Json::array();
  for (double point : points) list.push(Json(point));
  json.set("points", list);
  return json;
}

}  // namespace event

/* ------------------------------------------------------------------ */
/* Transport                                                           */
/* ------------------------------------------------------------------ */

/// One HTTP exchange. Not intended to be used directly.
struct HttpResponse {
  int status = 0;
  std::string body;
};

namespace detail {

inline void ensure_winsock() {
#ifdef _WIN32
  // Once per process, and deliberately never cleaned up: a device runs for the
  // life of the machine, and tearing Winsock down under a live socket is a way
  // to turn a shutdown into a crash.
  static bool started = [] {
    WSADATA data;
    WSAStartup(MAKEWORD(2, 2), &data);
    return true;
  }();
  (void)started;
#endif
}

struct Url {
  std::string host;
  std::string port = "80";
  std::string path = "/";
};

inline void require_plain_http(const std::string& url) {
  if (url.rfind("https://", 0) == 0) {
    throw BuildBoxError(
        "This client speaks plain HTTP only. Terminate TLS at a proxy, or use the "
        "Python binding with a TLS-capable client.");
  }
}

inline Url parse_url(const std::string& url) {
  Url parsed;
  std::string rest = url;
  const std::string scheme = "http://";
  if (rest.rfind(scheme, 0) == 0) {
    rest = rest.substr(scheme.size());
  } else {
    require_plain_http(rest);
  }

  const size_t slash = rest.find('/');
  std::string authority = (slash == std::string::npos) ? rest : rest.substr(0, slash);
  parsed.path = (slash == std::string::npos) ? "/" : rest.substr(slash);

  const size_t colon = authority.rfind(':');
  if (colon != std::string::npos) {
    parsed.host = authority.substr(0, colon);
    parsed.port = authority.substr(colon + 1);
  } else {
    parsed.host = authority;
  }
  if (parsed.host.empty()) throw BuildBoxError("The server URL has no host.");
  return parsed;
}

inline HttpResponse exchange(const std::string& method, const std::string& url,
                             const std::string& token, const std::string& body,
                             int timeout_seconds) {
  ensure_winsock();
  const Url parsed = parse_url(url);

  addrinfo hints{};
  hints.ai_family = AF_UNSPEC;
  hints.ai_socktype = SOCK_STREAM;
  addrinfo* addresses = nullptr;
  if (getaddrinfo(parsed.host.c_str(), parsed.port.c_str(), &hints, &addresses) != 0) {
    throw BuildBoxError("Could not resolve " + parsed.host + ".");
  }

  socket_handle handle = BUILDBOX_INVALID_SOCKET;
  for (addrinfo* candidate = addresses; candidate != nullptr; candidate = candidate->ai_next) {
    handle = ::socket(candidate->ai_family, candidate->ai_socktype, candidate->ai_protocol);
    if (handle == BUILDBOX_INVALID_SOCKET) continue;
    if (::connect(handle, candidate->ai_addr, static_cast<int>(candidate->ai_addrlen)) == 0) break;
    BUILDBOX_CLOSE_SOCKET(handle);
    handle = BUILDBOX_INVALID_SOCKET;
  }
  freeaddrinfo(addresses);

  if (handle == BUILDBOX_INVALID_SOCKET) {
    throw BuildBoxError("Could not reach " + parsed.host + ":" + parsed.port + ".");
  }

  // The long poll can legitimately hold a request open, so the socket has to
  // outlast the wait the caller asked for rather than a fixed number.
#ifdef _WIN32
  const DWORD limit = static_cast<DWORD>(timeout_seconds * 1000);
  setsockopt(handle, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&limit), sizeof(limit));
  setsockopt(handle, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&limit), sizeof(limit));
#else
  timeval limit{};
  limit.tv_sec = timeout_seconds;
  setsockopt(handle, SOL_SOCKET, SO_RCVTIMEO, &limit, sizeof(limit));
  setsockopt(handle, SOL_SOCKET, SO_SNDTIMEO, &limit, sizeof(limit));
#endif

  std::string request;
  request += method + " " + parsed.path + " HTTP/1.1\r\n";
  request += "Host: " + parsed.host + ":" + parsed.port + "\r\n";
  request += "authorization: Bearer " + token + "\r\n";
  request += "accept: application/json\r\n";
  request += "connection: close\r\n";
  if (!body.empty()) {
    request += "content-type: application/json\r\n";
    request += "content-length: " + std::to_string(body.size()) + "\r\n";
  }
  request += "\r\n";
  request += body;

  size_t sent = 0;
  while (sent < request.size()) {
    const int written = ::send(handle, request.data() + sent,
                               static_cast<int>(request.size() - sent), 0);
    if (written <= 0) {
      BUILDBOX_CLOSE_SOCKET(handle);
      throw BuildBoxError("The request could not be sent.");
    }
    sent += static_cast<size_t>(written);
  }

  std::string raw;
  char buffer[8192];
  while (true) {
    const int read = ::recv(handle, buffer, sizeof(buffer), 0);
    if (read > 0) {
      raw.append(buffer, static_cast<size_t>(read));
      continue;
    }
    if (read == 0) break;  // The server closed; `connection: close` makes that the end.
#ifdef _WIN32
    const int error = WSAGetLastError();
    if (error == WSAETIMEDOUT) break;
#else
    if (errno == EAGAIN || errno == EWOULDBLOCK) break;
#endif
    BUILDBOX_CLOSE_SOCKET(handle);
    throw BuildBoxError("The connection dropped while reading the response.");
  }
  BUILDBOX_CLOSE_SOCKET(handle);

  const size_t header_end = raw.find("\r\n\r\n");
  if (header_end == std::string::npos) {
    throw BuildBoxError("The server sent a truncated response.");
  }

  HttpResponse response;
  const size_t status_start = raw.find(' ');
  if (status_start != std::string::npos) {
    response.status = std::atoi(raw.c_str() + status_start + 1);
  }

  std::string headers = raw.substr(0, header_end);
  std::string lower;
  for (char character : headers) {
    lower += static_cast<char>(std::tolower(static_cast<unsigned char>(character)));
  }
  response.body = raw.substr(header_end + 4);

  if (lower.find("transfer-encoding: chunked") != std::string::npos) {
    // Fastify streams some responses. Decoded here because the alternative is
    // handing the caller a chunk-length line where its JSON should be.
    std::string decoded;
    size_t at = 0;
    while (at < response.body.size()) {
      const size_t line_end = response.body.find("\r\n", at);
      if (line_end == std::string::npos) break;
      const size_t length = std::strtoul(response.body.substr(at, line_end - at).c_str(), nullptr, 16);
      if (length == 0) break;
      const size_t start = line_end + 2;
      if (start + length > response.body.size()) break;
      decoded.append(response.body, start, length);
      at = start + length + 2;
    }
    response.body = decoded;
  }
  return response;
}

}  // namespace detail

/* ------------------------------------------------------------------ */
/* The client                                                          */
/* ------------------------------------------------------------------ */

class Client {
 public:
  using Handler = std::function<Result(const Command&)>;

  /// A device's connection. The token identifies it; the server reads what it
  /// may touch from its own database, so nothing sent here can widen that.
  explicit Client(std::string url, std::string token, std::string module = "")
      : url_(std::move(url)), token_(std::move(token)), module_(std::move(module)) {
    if (url_.empty()) throw BuildBoxError("No server URL.");
    if (token_.empty()) throw BuildBoxError("No device token.");
    // Checked here rather than at the first request, so a misconfigured URL is
    // reported where it was written rather than when the device first tries to
    // report something and cannot.
    detail::require_plain_http(url_);
    while (!url_.empty() && url_.back() == '/') url_.pop_back();
  }

  /* ---------------------------------------------------------------- */
  /* Reporting                                                         */
  /* ---------------------------------------------------------------- */

  /// Send a batch. Returns how many the server accepted.
  int send(const std::vector<Json>& events, const std::string& module = "") {
    if (events.empty()) return 0;
    Json envelope = Json::object();
    envelope.set("v", kVersion);
    envelope.set("type", "events");
    envelope.set("moduleId", module.empty() ? module_id() : module);
    envelope.set("source", "device");
    Json list = Json::array();
    for (const Json& event_value : events) list.push(event_value);
    envelope.set("events", list);

    const Json answer = post("/api/device/ingest", envelope);
    return answer["accepted"].is_number() ? answer["accepted"].as_int() : 0;
  }

  /// Report one numeric reading.
  ///
  ///     bb.send_data("temperature", 21.5, "C");
  int send_data(const std::string& key, double value, const std::string& unit = "",
                const std::string& module = "") {
    return send({event::sample(key, value, unit)}, module);
  }

  int send_log(const std::string& message, const std::string& severity = "info") {
    return send({event::log(message, severity)});
  }

  int send_status(const std::string& value) { return send({event::status(value)}); }

  int send_shape(const std::string& kind, const std::vector<double>& points) {
    return send({event::shape(kind, points)});
  }

  /* ---------------------------------------------------------------- */
  /* Identity                                                          */
  /* ---------------------------------------------------------------- */

  /// The device's label and the modules it may report on.
  Json whoami() { return get("/api/device/whoami", 15); }

  /// The module to report on when the caller did not name one.
  ///
  /// A device scoped to exactly one module is the common case, and naming it on
  /// every call is noise. A device scoped to several is asked to be explicit
  /// rather than guessed at — reporting against the wrong module is worse than
  /// an error.
  const std::string& module_id() {
    if (!module_.empty()) return module_;
    const Json identity = whoami();
    const Json& modules = identity["modules"];
    if (modules.is_array() && modules.items().size() == 1) {
      module_ = modules.items()[0]["id"].string_or("");
      return module_;
    }
    if (!modules.is_array() || modules.items().empty()) {
      throw ScopeError("This device is not scoped to any module.");
    }
    throw ScopeError("This device is scoped to several modules; pass module= to say which.");
  }

  /* ---------------------------------------------------------------- */
  /* Commands                                                          */
  /* ---------------------------------------------------------------- */

  /// Handle every command.
  void on_command(Handler handler) { handlers_.push_back({0, nullptr, std::move(handler)}); }

  /// Handle commands with this action (`read`, `run`, `publish`…).
  void on_command_action(const std::string& action, Handler handler) {
    handlers_.push_back({1, [action](const Command& command) { return command.action == action; },
                         std::move(handler)});
  }

  /// Handle commands whose text contains `text`.
  void on_command_match(const std::string& text, Handler handler) {
    handlers_.push_back({1, [text](const Command& command) { return command.matches(text); },
                         std::move(handler)});
  }

  /// Wait for the next command, or return nothing when the wait elapses.
  std::optional<Command> poll(double wait_seconds = 20.0) {
    const int milliseconds = static_cast<int>(wait_seconds * 1000);
    const Json answer = get("/api/device/commands?wait=" + std::to_string(milliseconds),
                            static_cast<int>(wait_seconds) + 10, /*allow_empty=*/true);
    if (answer.is_null()) return std::nullopt;

    Command command;
    command.cmd_id = answer["cmdId"].string_or("");
    command.module_id = answer["moduleId"].string_or("");
    command.action = answer["action"].string_or("");
    command.cmd = answer["cmd"].string_or("");
    command.target = answer["target"].string_or("");
    command.timeout_ms = answer["timeoutMs"].is_number() ? answer["timeoutMs"].as_int() : 30000;
    if (command.cmd_id.empty()) return std::nullopt;
    return command;
  }

  /// Run a command and report what happened, exactly once.
  Result apply(const Command& command) {
    Result result;
    const bool already = std::find(applied_.begin(), applied_.end(), command.cmd_id) != applied_.end();
    if (already) {
      // The transport is at-least-once, so a redelivery must not fire a relay a
      // second time. Answering "already done" is not a lie: it was done.
      result = Result::success({"Already applied; not repeated."});
    } else {
      result = dispatch(command);
      applied_.push_back(command.cmd_id);
      if (applied_.size() > 512) applied_.erase(applied_.begin(), applied_.begin() + 256);
    }

    Json report = Json::object();
    report.set("v", kVersion);
    report.set("type", "result");
    report.set("cmdId", command.cmd_id);
    report.set("moduleId", command.module_id);
    report.set("ok", result.ok);
    Json output = Json::array();
    for (const std::string& line : result.output) output.push(Json(line));
    report.set("output", output);
    if (result.code >= 0) report.set("code", result.code);
    if (!result.reason.empty()) report.set("reason", result.reason);
    post("/api/device/results", report);
    return result;
  }

  /// Answer one pending command if there is one. False when there was none.
  bool check(double wait_seconds = 0.0) {
    const std::optional<Command> command = poll(wait_seconds);
    if (!command) return false;
    apply(*command);
    return true;
  }

  /// Answer commands until the process is stopped.
  ///
  /// Connection failures are retried with a widening backoff rather than thrown,
  /// because a device on a robot is expected to outlive the network. Nothing is
  /// retried that could double-apply an action — that is `apply`'s job, and it
  /// holds the list of what has already run.
  void run_forever(double poll_wait_seconds = 20.0) {
    double delay = 1.0;
    while (true) {
      try {
        const std::optional<Command> command = poll(poll_wait_seconds);
        delay = 1.0;
        if (command) apply(*command);
      } catch (const std::exception& error) {
        if (on_error_) on_error_(error.what());
        std::this_thread::sleep_for(std::chrono::milliseconds(static_cast<int>(delay * 1000)));
        delay = std::min(delay * 2, 30.0);
      }
    }
  }

  /// Called when the connection drops and is about to be retried.
  void set_error_handler(std::function<void(const std::string&)> handler) {
    on_error_ = std::move(handler);
  }

 private:
  struct Registered {
    int specificity;
    std::function<bool(const Command&)> predicate;  // null means "anything"
    Handler handler;
  };

  Result dispatch(const Command& command) {
    // Most specific first, so registering a catch-all for logging does not
    // quietly swallow every handler declared after it.
    std::vector<const Registered*> ordered;
    for (const Registered& entry : handlers_) ordered.push_back(&entry);
    std::stable_sort(ordered.begin(), ordered.end(),
                     [](const Registered* a, const Registered* b) {
                       return a->specificity > b->specificity;
                     });

    for (const Registered* entry : ordered) {
      if (entry->predicate && !entry->predicate(command)) continue;
      try {
        return entry->handler(command);
      } catch (const std::exception& error) {
        // A handler that threw did not do the thing. Saying so is the point.
        return Result::failure(std::string("handler threw: ") + error.what());
      }
    }

    // No handler is a refusal, not a success.
    return Result::failure("This device has no handler for \"" + command.action +
                           "\". Nothing was done.");
  }

  Json get(const std::string& path, int timeout_seconds, bool allow_empty = false) {
    const HttpResponse response =
        detail::exchange("GET", url_ + path, token_, "", timeout_seconds);
    if (allow_empty && response.status == 204) return Json(nullptr);
    check_status(response, path);
    if (response.body.empty()) return Json(nullptr);
    return Json::parse(response.body);
  }

  Json post(const std::string& path, const Json& payload) {
    const HttpResponse response =
        detail::exchange("POST", url_ + path, token_, payload.dump(), 30);
    check_status(response, path);
    if (response.body.empty()) return Json(nullptr);
    return Json::parse(response.body);
  }

  static void check_status(const HttpResponse& response, const std::string& path) {
    if (response.status >= 200 && response.status < 300) return;

    std::string message = "The server answered " + std::to_string(response.status) + ".";
    try {
      const Json body = Json::parse(response.body);
      const std::string explained = body["message"].string_or("");
      if (!explained.empty()) message = explained;
    } catch (const std::exception&) {
      // A refusal without a parseable body still has its status to report.
    }
    if (path.empty()) message += " (request failed)";

    if (response.status == 401) throw AuthError(message);
    if (response.status == 403) throw ScopeError(message);
    if (response.status == 422) throw ShapeError(message);
    throw BuildBoxError(message);
  }

  std::string url_;
  std::string token_;
  std::string module_;
  std::vector<Registered> handlers_;
  std::vector<std::string> applied_;
  std::function<void(const std::string&)> on_error_;
};

}  // namespace buildbox
