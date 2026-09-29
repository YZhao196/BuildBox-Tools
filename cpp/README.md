# BuildBox — C++

Report readings from a device, and answer the commands it is sent.

Header-only and dependency-free: a socket, a small JSON value, and nothing else
to link but Winsock. A robot's build is the last place to introduce a library.

```cpp
#include <buildbox/client.hpp>

buildbox::Client bb("http://127.0.0.1:8787", token);

bb.on_command_match("kill_switch", [](const buildbox::Command& command) {
  return buildbox::Result::exit_code(0);   // 0 stopped, anything else did not
});

while (true) {
  bb.send_data("temperature", read_sensor(), "C");
  bb.check();                              // answer anything waiting, without blocking
}
```

## Building

With CMake:

```bash
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build
ctest --test-dir build --output-on-failure
```

Neither CMake nor a generator needs to be installed system-wide — both ship as
Python wheels, so a machine with nothing but Python and a C++ compiler can build
and test this. That is the route this repository was verified on:

```bash
pip install cmake ninja
cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build
ctest --test-dir build --output-on-failure
```

Into your own project, `find_package(BuildBox)` and link `buildbox::buildbox` —
which is also verified, by a separate consumer project built against an
installed prefix. There is a `vcpkg.json` for a vcpkg-based build. The project is
header-only, so vendoring `cpp/include/` also works.

By hand — note that WinSock has to be linked explicitly on MinGW:

```bash
g++ -std=c++17 -Iinclude your_device.cpp -o your_device          # POSIX
g++ -std=c++17 -Iinclude your_device.cpp -o your_device -lws2_32 # Windows
```

## Reporting

```cpp
bb.send_data("temperature", 21.5, "C");
bb.send({buildbox::event::log("sensor warmed up")});
```

## Every kind of reading a module can carry

`buildbox/data.hpp` has a function for each one, so sending a CAN frame or a GPS
fix does not mean remembering which field the server wants where.

```cpp
#include <buildbox/data.hpp>

using namespace buildbox;

bb.send(data::temperature(21.5, 44.0));            // Temperature / Humidity
bb.send(data::laser_scan(ranges, -1.57, 1.57));    // ROS2 Laser Scan
bb.send(data::joint_state({0.1, 0.2}, {"l", "r"}));  // ROS2 Joint State
bb.send(data::gps_fix(-37.8, 144.9));              // GPS / GNSS
bb.send(data::can_frame(0x123, {0x01, 0x02}));     // CAN Bus Monitor
bb.send(data::power(12.4, 1.2));                   // Power Monitor
bb.send(data::imu({0, 0, 9.81}, {0, 0, 0}));       // IMU
bb.send(data::image_frame(jpeg_bytes));            // Camera Capture
```

Every function returns a `std::vector<Json>`, because most real readings are more
than one number. `preset_builder_name` maps each of the 68 catalogue presets to
the function that feeds it, `non_capturing_reason` says why the other ten have
none, and `uncovered()` reports any preset with neither — which `test_data.cpp`
asserts is empty. Add a preset without a function and the build's tests fail.

### Two rules the builders follow

- **A sample is a number.** The protocol has no string sample, so prose becomes a
  log line rather than being coerced into a number it is not.
- **A missing field is omitted, not zeroed.** A sensor that could not read its
  humidity passes `std::nullopt`, and no humidity series is reported.

They refuse what the server would refuse: a map that does not fill its grid, a
correlation outside −1..1, an unknown lifecycle state. `gps_no_fix()` exists so
that "I have no fix" has somewhere to go that is not a coordinate.

## Answering

```cpp
bb.on_command_match("kill_switch", handler);   // commands naming this text
bb.on_command_action("run", handler);          // commands with this action
bb.on_command(handler);                        // everything else
```

Handlers return a `Result`:

```cpp
return buildbox::Result::exit_code(0);         // a shell-style verdict
return buildbox::Result::text("done");         // worked, with output
return buildbox::Result::failure("no bus");    // did not work, and why
return buildbox::Result::success();
```

A handler that throws is reported as a failure. **A command no handler claims is
reported as a failure too** — silently claiming success for work that did not
happen is the one thing this must never do.

Handlers are tried most-specific-first, so a catch-all registered for logging
does not swallow the handlers declared after it.

## What it will not do

- **Plain HTTP only.** This speaks `http://`. A deployment that needs TLS should
  terminate it at a proxy; a URL starting `https://` is refused at construction
  with that explanation rather than failing obscurely later.
- **It will not invent a success**, and it will not report on a module it was not
  scoped to.

## Tests

Two suites, both wired into `ctest` and neither needing a server:

- `tests/test_client.cpp` — the JSON value, event builders, command matching and
  result semantics.
- `tests/test_data.cpp` — the reading builders, plus the check that every preset
  in the catalogue has a function that feeds it.

```bash
ctest --test-dir build --output-on-failure
```

By hand, without CMake:

```bash
g++ -std=c++17 -Iinclude tests/test_client.cpp -o /tmp/bb_test -lws2_32 && /tmp/bb_test
g++ -std=c++17 -Iinclude tests/test_data.cpp   -o /tmp/bb_data -lws2_32 && /tmp/bb_data
```

The wire contract itself is exercised in this repository by the management
library's suite (`management/tests/test_server.py`), which drives the Python
binding against a real receiver over a real socket — token scope, ingest
validation, shape refusal and the command round trip. The same semantics are
covered against the real BuildBox server by that repository's
`apps/server/src/routes/devices.test.ts`, which cannot be run from here.

## Licence

MIT — see [LICENSE](../LICENSE) at the repository root.
