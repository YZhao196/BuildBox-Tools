# BuildBox Tools

The two ends of the bridge, and nothing else:

- the **device library** runs *on* a robot or a microcomputer. It reports what the
  machine measures, and answers the commands it is sent.
- the **management library** runs *where the data goes* — a script, a workshop
  machine, your own service. It receives those readings and sends those commands.

They speak one protocol, so a device written against either end talks to the
other. Neither needs BuildBox to be running: this repository exists so the bridge
can be used by programs that have never heard of it.

Everything here is MIT-licensed. The device libraries depend on nothing beyond the
standard library (plus a serial driver if you want one), and so does the
management library.

- **[`python/`](python/README.md)** — the device library, `pip install`-able.
- **[`cpp/`](cpp/README.md)** — the device library, header-only, no dependencies.
- **[`management/`](management/README.md)** — the management library, `pip
  install`-able.
- **[`docs/bridge-protocol.md`](docs/bridge-protocol.md)** — the wire format all
  three speak, written down so a binding in any other language is a port rather
  than a project.

## Device — Python

```bash
pip install "buildbox @ git+https://github.com/YZhao196/BuildBox-Tools#subdirectory=python"
```

```python
from buildbox import Client, sensors

bb = Client("http://127.0.0.1:8787", token=TOKEN)

# Reading a sensor is a declaration, not a loop. On a Pi or a Jetson this is how
# temperature is read: the kernel exposes it as a file, in millidegrees.
bb.sensor("temperature", sensors.file_number("/sys/class/thermal/thermal_zone0/temp",
                                             scale=1e-3),
          unit="C", every=2.0)

@bb.on_command(match="kill_switch")
def kill_switch(command):
    if not stopped():
        return 1        # non-zero: it did not stop, and the server says so
    return 0

bb.run()                # reads the sensors and answers commands
```

## Device — C++

```cpp
#include <buildbox/client.hpp>
#include <buildbox/data.hpp>

buildbox::Client bb("http://127.0.0.1:8787", token);

bb.on_command_match("kill_switch", [](const buildbox::Command& command) {
  return buildbox::Result::exit_code(0);   // 0 stopped, anything else did not
});

while (true) {
  bb.send(buildbox::data::temperature(read_sensor(), 44.0));
  bb.check();                              // answer anything waiting
}
```

```bash
pip install cmake ninja                     # both ship as wheels
cd cpp
cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build && ctest --test-dir build --output-on-failure
```

## Management — Python

```bash
pip install "buildbox-management @ git+https://github.com/YZhao196/BuildBox-Tools#subdirectory=management"
```

```python
from buildbox_management import Management

mgmt = Management()
mgmt.add_module("proj-1", "mod-temp", name="Temperature")
mgmt.add_module("proj-1", "mod-lidar", name="Lidar", shapes=["scan"])

device, token = mgmt.mint("proj-1", "rover", ["mod-temp", "mod-lidar"])

@mgmt.on_events
def received(reading):
    store(reading.module_id, reading.events)

mgmt.serve("127.0.0.1", 8787)     # hand `token` to the device
```

```bash
# Or run one from the command line and watch readings go past as JSON lines:
buildbox-management serve --project demo --module mod-temp --module mod-lidar:scan
```

## The two rules these are built around

They come from the protocol, and every library here enforces its own half — so a
client that assumed otherwise would be refused rather than accommodated.

1. **A reading is always labelled.** Everything a device sends is marked as
   coming from a device, and the receiver sets that itself rather than trusting
   the wire. A value a sensor reported can never be confused with one a model
   produced.
2. **A write is never invented.** A command a device cannot perform is answered
   `ok=False` with a reason. It never answers success for work it did not
   finish, because a kill switch that reports having stopped something is the
   most dangerous lie this could tell. On the sending side, a command nobody
   answered is a failure too — not silence, and not success.

## Layout

```
python/       the device library, its tests and an example
cpp/          the device library, header-only, with its tests and an example
management/   the management library and its tests
docs/         the bridge protocol, normative
scripts/      sync_presets.py — keeps the device preset lists in step with the catalogue
```

`scripts/sync_presets.py` reads the preset catalogue out of the product
repository, so regenerating those lists needs that checkout too. Point
`BUILDBOX_CATALOGUE` at its `packages/shared/src/catalog.ts`. The generated
lists are committed, so you only need this if the catalogue has changed — and
nothing else here reads from BuildBox at all.

## Tests

```bash
python -m pytest                         # both Python libraries, from here
ctest --test-dir cpp/build               # after configuring, above
```

`management/tests/test_server.py` starts a real receiver and drives it with the
real device client over a real socket — the two libraries talking to each other
with no BuildBox in the process. It is the test that would break first if either
one ever grew a dependency on the product.

## Licence

MIT — see [LICENSE](LICENSE).
