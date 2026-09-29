# buildbox-management — Python

The other end of the bridge: receive readings from devices, and command them.

The `buildbox` package is what runs *on* a robot. This is what runs *where the
data goes* — a script, a workshop machine, a research rig, your own service. It
speaks the same protocol, so a device written against either one talks to the
other, and neither needs BuildBox to be running.

Standard library only, and no dependency on the device package: a program that
only receives should not have to install what a robot runs.

```bash
# From the public repository — no access to the product needed:
pip install "buildbox-management @ git+https://github.com/YZhao196/BuildBox-Tools#subdirectory=management"

# Or from a checkout:
pip install ./management
```

Not published to PyPI. Install it from the repository, or from a wheel built out
of it:

```bash
python -m pip wheel ./management -w dist --no-deps
```

## Receiving

```python
from buildbox_management import Management

mgmt = Management()

# What devices may report on. `shapes` is the structural readings the module
# displays — a lidar sees scans, a depth camera sees clouds.
mgmt.add_module("proj-1", "mod-temp", name="Temperature")
mgmt.add_module("proj-1", "mod-lidar", name="Lidar", shapes=["scan"])

# Mint a device and hand it the token. It is shown once and stored only as a
# digest, so a lost token is replaced rather than recovered.
device, token = mgmt.mint("proj-1", "rover", ["mod-temp", "mod-lidar"])

@mgmt.on_events
def received(reading):
    for event in reading.events:
        store(reading.module_id, event)

mgmt.serve("127.0.0.1", 8787)
```

`on_events` runs on the thread handling the request, so a slow callback holds
that device's request open. Batch into a queue if that matters.

## Commanding

```python
result = mgmt.command(device.id, "mod-temp", "read", cmd="read_temp", timeout=5.0)

if result.ok:
    print(result.output)
else:
    print(result.reason)     # never empty when ok is False
```

`command` blocks until the device reports what it did, and **a command that was
never answered is a failure** — `ok=False` with a reason, never silence and never
success.

Two things follow from that, and both are enforced rather than promised. If the
wait runs out before the device collects the command, the command is taken back
out of the queue — so a caller told "nothing was confirmed" cannot have the relay
fire a moment later, and a retry cannot queue a second command the device's
de-duplication would not catch. If the command *had* already reached the device,
it may still be applied: `ok=False` means unconfirmed, which is exactly what it
says, and no library can claim better over a network that can drop an answer.

It also refuses before sending when the device is not scoped to the module, when
the module does not exist, or when nothing has polled recently. The last of those
is a courtesy the protocol does not require: the alternative is holding the
caller for the whole timeout to be told what was already true when they asked.

## What this checks, and why

Two rules run through the whole thing, and they come from the protocol rather
than from taste:

1. **A batch is labelled by its sender.** Every `events` message carries
   `source: "device"` on the batch, and this end refuses one that claims
   otherwise — so the readings a sensor reported are never taken for anything
   but the sensor's.
2. **A write is never invented.** A command a device cannot perform is refused,
   a device that fails to answer is reported as unconfirmed, and a command that
   timed out is never delivered afterwards.

Alongside them:

- **A batch is all or nothing.** Half of one would leave your program holding
  readings a device never sent as a set, so one bad reading refuses the lot.
- **A device may only report on modules it was given.** The scope is checked on
  every request, so narrowing or revoking takes effect on the device's next call
  rather than its next reconnect.
- **A structural reading must be one the module displays.** A laser sweep sent to
  a temperature module is refused, because the alternative is drawing something
  no sensor measured.

## Mounting it in your own server

`serve()` is a convenience, not the interface. The protocol is one function:

```python
def handle(method, path, *, query="", headers=None, body=None) -> Response
```

It returns a status, a body and headers, so it can be mounted in whatever the
embedding program already runs — Flask, FastAPI, an existing loop — without this
package knowing what a router is. `mgmt.handle(...)` is what `serve()` calls.

## From the command line

```bash
buildbox-management serve --project demo --module mod-temp --module mod-lidar:scan --label rover
```

It mints a device, prints the token once on stderr, and prints every accepted
batch as one JSON line on stdout — so a shell can pipe readings somewhere without
writing any Python at all.

## What it will not do

- **It will not recover a token.** Only a digest is stored. A lost token is
  rotated.
- **It will not let a device widen its own scope** by asking. `whoami` answers
  with what the device could already act on, and nothing more.
- **It will not claim a delivery it did not make.** A callback that raises is
  reported to the device as a failure, so the reading is not dropped on the
  floor.
- **It is not BuildBox.** There are no accounts, no projects on a canvas and no
  interface here. Those belong to the program embedding it, which already has its
  own idea of who its operators are.

## Tests

```bash
python -m pytest
```

Two kinds. Most of the suite is the receiver on its own. `tests/test_server.py`
starts a real HTTP server and drives it with the real `buildbox` device client,
over a real socket — the two libraries talking to each other with no BuildBox in
the process. That file is the test that would break first if either library ever
grew a dependency on the product; the device package is not a dependency of this
one, so when it is absent that file reports itself **skipped** rather than
passing quietly.

## Licence

MIT — see [LICENSE](LICENSE), and [the repository root](../LICENSE).
