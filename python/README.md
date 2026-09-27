# buildbox — Python

Report readings from a device, and answer the commands it is sent.

Standard library only, so it installs on a robot with no wheel for its
architecture and nothing to compile.

```bash
# From the public repository — no access to the product needed:
pip install "buildbox @ git+https://github.com/YZhao196/BuildBox-Tools#subdirectory=python"

# Or from a checkout, either repository:
pip install ./python
```

Not published to PyPI — `pip install buildbox` would fetch something else, or
nothing. Install it from the repository, or from a wheel built out of it:

```bash
python -m pip wheel ./python -w dist --no-deps
```

Requires Python 3.9 or later, and nothing else. `buildbox doctor` reports which
optional hardware libraries are importable on the machine it runs on; the
`drivers` extra pulls the common ones — quoted, because the brackets are a glob
to a shell otherwise:

```bash
pip install "./python[drivers]"
```

## Reporting

```python
from buildbox import Client

bb = Client("http://127.0.0.1:8787", token=TOKEN)

bb.send_data("temperature", 21.5, unit="C")
bb.send_many({"temperature": 21.5, "humidity": 44})
bb.send_log("sensor warmed up", "info")
```

`BUILDBOX_URL` and `BUILDBOX_TOKEN` are read from the environment, which is how a
service on a robot usually gets its configuration.

## Every kind of reading a module can carry

`buildbox.data` has a function for each one, so sending a CAN frame or a GPS fix
does not mean remembering which field the server wants where.

```python
from buildbox import Client, data

bb.send(data.temperature(21.5, humidity=44))          # Temperature / Humidity
bb.send(data.laser_scan(ranges, -1.57, 1.57))         # ROS2 Laser Scan
bb.send(data.joint_state([0.1, 0.2], names=["l", "r"]))   # ROS2 Joint State
bb.send(data.gps_fix(latitude=-37.8, longitude=144.9))   # GPS / GNSS
bb.send(data.can_frame(0x123, b"\x01\x02"))           # CAN Bus Monitor
bb.send(data.power(voltage=12.4, current=1.2))        # Power Monitor
bb.send(data.imu(accel=(0, 0, 9.81), gyro=(0, 0, 0)))  # IMU
bb.send(data.image_frame(jpeg_bytes))                 # Camera Capture
```

Every function returns a **list** of events, because most real readings are more
than one number — an IMU is nine, a GPS fix is six. `send` takes either a list or
a single event. The whole set is listed in `buildbox/data.py`, and
`data.PRESET_BUILDERS` maps each of the 67 presets in the catalogue to the
function that feeds it. Nine are deliberately absent — a control sends a command
rather than capturing data — and `data.NON_CAPTURING` says which and why.

A test proves it stays complete: add a preset to the catalogue without a function
and `pytest` fails.

### Two rules the builders follow

- **A sample is a number.** The protocol has no string sample, so prose — an AI
  reply, a shell line — becomes a log line rather than being coerced into a
  number it is not.
- **A missing field is omitted, not zeroed.** A sensor that could not read its
  humidity passes `None`, and no humidity series is reported. A placeholder would
  be a number nobody measured.

They also refuse what the server would refuse: a map that does not fill its grid,
a correlation outside −1..1, an unknown lifecycle state. And `gps_no_fix()` exists
so that "I have no fix" has somewhere to go that is not a coordinate.

## Reading a sensor

Declare what the device reads rather than writing the loop. `run()` calls each
reader on its own interval and sends what comes back, while answering commands:

```python
from buildbox import Client, sensors

bb = Client()

# On a Raspberry Pi or a Jetson this is how a temperature sensor is read: the
# kernel exposes it as a file, in millidegrees.
bb.sensor("temperature", sensors.file_number("/sys/class/thermal/thermal_zone0/temp",
                                             scale=1e-3),
          unit="C", every=2.0)

# Anything callable works — a library, an attribute, a computation.
bb.sensor("cpu", lambda: psutil.cpu_percent(), unit="%", every=5.0)

bb.run()
```

A reader may return a number, a **mapping** (a device reading temperature *and*
humidity is one function returning both), or `None` for "nothing to report
yet".

### Two rules, and they matter more than the convenience

- **A reader that cannot read raises.** It never returns a plausible number. The
  sampler logs the failure and plots nothing, so an unplugged probe shows as
  unplugged rather than as a steady zero. `sensors.SensorError` carries a reason
  worth reading, and the reader's `last_error` is set.
- **A reader with nothing to report returns `None`.** Waiting for a satellite
  fix and being broken are different things and must not look alike — `None`
  sends nothing, and is not a failure.

A send that fails does not stop the sampler; the network comes back and the next
tick reports again. A device with its own loop can use `sample_once()` and
`check()` instead of `run()`.

### Ship readers

`sensors.file_number(path, scale=, offset=, pattern=)` reads a number out of a
file — which is how thermal zones, voltages and fan speeds are read on a Pi.
`sensors.serial_line(port, baudrate=, pattern=)` reads one line at a time from a
serial sensor through pyserial, keeping the port open and reconnecting if it
disappears.

## Answering

```python
@bb.on_command(match="kill_switch")
def kill_switch(command):
    if not actually_stopped():
        return 1          # non-zero: it did not stop, and the server says so
    return 0              # zero: it stopped

bb.run_forever()
```

A handler may return:

| Return | Meaning |
| --- | --- |
| `None` | it worked, nothing to add |
| `0` / `1` | a shell-style verdict — anything non-zero means it did not work |
| `True` / `False` | the same, said differently |
| `"text"` or `["a", "b"]` | it worked, and this is the output |
| `Result(ok=…, reason=…)` | spelled out, for anything else |

A handler that raises is reported as a failure with the exception in the reason.
**A command with no handler is reported as a failure too** — a device that
silently claimed success for work it did not do would be lying to whoever pressed
the button.

Handlers are matched most-specific-first, so registering a catch-all for logging
does not swallow the handlers declared after it.

If a device has nothing to do but be commanded, `bb.run_forever()` answers until
it is stopped, reconnecting with a widening backoff when the network drops.
If it has its own loop, call `bb.check()` each time round — it answers anything
waiting without blocking.

## Checking a machine

```bash
buildbox doctor
```

Probes the server, the token, and the optional hardware libraries, and reports
each in the same words the interface uses: `available`, `unavailable`,
`unsupported`, `misconfigured`. A library installed on a robot should be able to
say what that robot can actually do.

## What it will not do

- **It will not invent a success.** A command it cannot perform is reported as a
  failure with a reason.
- **It will not report on a module it was not scoped to.** The server refuses
  that, and there is no parameter here that would let you ask.
- **It will not label a reading as anything but a device reading.** The server
  sets that, not this package.

## Tests

```bash
python -m pytest
```

No network and no server needed: the transport is stubbed, so the suite checks
what the device decides and what it sends. The wire contract itself is exercised
against a real server by the server's own suite
(`apps/server/src/routes/devices.test.ts`), which covers token scope, ingest
validation and the command round trip.

One of these tests is a guarantee rather than a check: `tests/test_data.py` fails
if a preset is added to the catalogue without a function in `buildbox.data` that
feeds it. `scripts/sync_presets.py` regenerates the preset list both language
bindings mirror, so the two cannot drift apart quietly.

## Licence

MIT — see [LICENSE](LICENSE), and [the repository root](../LICENSE).
