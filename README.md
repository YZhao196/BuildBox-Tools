# BuildBox Tools

Client libraries for connecting a program on a robot or a device to a BuildBox
server: report what it measures, and answer the commands it is sent.

These live in their own repository so they can be used without access to
BuildBox itself. The libraries are MIT-licensed and depend on nothing beyond the
standard library, plus a serial driver if you want one.

- **[`python/`](python/README.md)** — `pip install`-able, standard library only.
- **[`cpp/`](cpp/README.md)** — header-only, no dependencies.
- **[`docs/bridge-protocol.md`](docs/bridge-protocol.md)** — the wire format both
  speak, written down so a binding in any other language is a port rather than a
  project.

## Python

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

## C++

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
cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build && ctest --test-dir build --output-on-failure
```

## The two rules these are built around

They come from the server, which enforces both — but a client that assumed
otherwise would be refused rather than accommodated.

1. **A reading is always labelled.** Everything sent from here is marked as
   coming from a device, and the server sets that itself. A value a sensor
   reported can never be confused with one the server modelled when no sensor
   was there.
2. **A write is never invented.** A command a device cannot perform is answered
   `ok=False` with a reason. It never answers success for work it did not
   finish, because a kill switch that reports having stopped something is the
   most dangerous lie this could tell.

A reader that cannot read raises, and the failure is logged rather than plotted.
A reader with nothing to report yet returns `None`. A sensor waiting for a fix
and a sensor that is broken are different things.

## Layout

```
python/     the Python package, its tests and an example
cpp/        the header-only C++ library, its tests and an example
docs/       the bridge protocol, normative
scripts/    sync_presets.py — keeps the preset lists here in step with the catalogue
```

`scripts/sync_presets.py` reads the preset catalogue out of the product
repository, so regenerating the lists needs that checkout too. Point
`BUILDBOX_CATALOGUE` at its `packages/shared/src/catalog.ts`. The generated
lists are committed, so you only need this if the catalogue has changed.

## Tests

```bash
python -m pytest python/tests            # no network, no server
ctest --test-dir build                   # after configuring, above
```

## Licence

MIT — see [LICENSE](LICENSE).
