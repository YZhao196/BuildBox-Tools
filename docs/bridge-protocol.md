# The Bridge Protocol

Version `bbp/1`. This document is normative. The implementations in this
repository are ports of it, one per language per end: `python/buildbox/` and
`cpp/include/buildbox/` are the device end — the program that runs on a robot —
and `management/buildbox_management/` is the receiving end, the program the
readings arrive at.

It exists so that a device can be written in any language against one stable
contract, rather than each integration being a new piece of software. The device
end ships in Python and C++ today, and the receiving end in Python; a further
binding is a port, not a project.

---

## Two rules a device must not break

These are not conventions. The server enforces both, and a device that assumes
otherwise will be refused rather than accommodated.

1. **A batch is labelled by its sender.** Every `events` message carries
   `source: "device"` on the batch, and the server refuses one that claims
   otherwise. A reading is sent by the thing that measured it, and a device
   cannot present its readings as anyone else's.
2. **A write is never invented.** If a device cannot do what it was asked, it
   answers `ok: false` with a reason. It must never answer `ok: true` for work it
   did not finish. A kill switch that reports having stopped something is the most
   dangerous lie this system can tell.

There is no third state. A device that does not know the answer reports failure.

---

## Identity

A device is not a user. It has no session cookie, no collaborator level, and no
way to reach a project it was not scoped to. Its whole authority is:

- a **token**, shown once when minted and stored only as a digest;
- a **project**;
- an **explicit list of module ids**.

The scope is read from the server's database on every request, so revoking or
narrowing a device takes effect on its next call rather than at its next
reconnect.

Every device-facing request carries the token as a bearer credential:

```
authorization: Bearer <token>
```

---

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/device/ingest` | readings |
| `GET` | `/api/device/commands?wait=<ms>` | the long poll: work to do |
| `POST` | `/api/device/results` | what a command did |
| `GET` | `/api/device/whoami` | the device's label and its scope |

These paths are reachable without a session cookie, because a robot cannot sign
in. That is not the same as being unauthenticated: every one of them resolves the
bearer token first and refuses without one.

---

## Uplink

### `hello`

Optional. Sent to `POST /api/device/ingest`, so the server can record what a
device is; it counts as a heartbeat and publishes nothing.

```json
{ "v": "bbp/1", "type": "hello", "agent": "python/0.1.0", "capabilities": ["temperature"] }
```

### `events`

Readings, batched. A device sampling at 50 Hz should send one request per batch,
not fifty per second.

```json
{
  "v": "bbp/1",
  "type": "events",
  "moduleId": "mod-temp",
  "source": "device",
  "events": [
    { "kind": "sample", "key": "temperature", "value": 21.5, "unit": "C" },
    { "kind": "log", "severity": "info", "message": "sensor warmed up" },
    { "kind": "status", "status": "ok" },
    { "kind": "shape", "shape": "scan", "points": [1.0, 1.2], "angleMin": -1.57, "angleMax": 1.57 }
  ]
}
```

`kind` is one of `sample`, `log`, `status`, `shape`. A `shape` carries structure
a single number cannot:

| `shape` | `points` holds | also |
| --- | --- | --- |
| `scan` | ranges in metres, ordered | `angleMin`, `angleMax` in radians |
| `cloud` | x, y, z triples in metres, sensor frame | |
| `joints` | one position per joint | `labels` |
| `map` | occupancy 0..100 per cell, row-major | `cols`, `rows` |
| `trail` | one x, y pose; the client accumulates the path | |
| `image` | — | `image`, a base64 PNG, JPEG, GIF or WebP `data:` URL of at most 524288 characters |

A batch is **all or nothing**. A partially applied batch would leave the
interface showing values the device never measured.

### Four validations, each a refusal

1. **Token** — exists, not revoked.
2. **Module** — on the device's project.
3. **Scope** — in the device's module list. A device may not report on a module
   it was not given, and may not widen its own scope by asking.
4. **Shape** — a reading the module's preset could have produced. A `scan` on a
   temperature module is refused (`422`), because the alternative is the canvas
   drawing something no sensor measured.

---

## Downlink

### `command`

Delivered by the long poll. The request is held open until there is something to
do or `wait` elapses; an empty poll answers `204`. The poll doubles as the
heartbeat — a device with nothing to do still returns and is recorded as alive,
which is how the server decides a device is gone **without taking the device's
word for it**.

```json
{
  "v": "bbp/1",
  "type": "command",
  "cmdId": "3f2c…",
  "moduleId": "mod-relay",
  "action": "run",
  "cmd": "kill_switch 1",
  "target": "10.0.0.5",
  "timeoutMs": 30000
}
```

`action` is one of the driver actions: `read`, `check`, `poll`, `listen`,
`scan`, `subscribe`, `open`, `capture` (these only observe) or `run`, `write`,
`publish`, `call`, `toggle`, `on`, `off`, `reset` (these change something).

**A destructive action has already been confirmed before a command is sent.** The
server holds that decision, not the device; a device is never the thing that
decides whether an action was authorised.

### `result`

```json
{
  "v": "bbp/1",
  "type": "result",
  "cmdId": "3f2c…",
  "moduleId": "mod-relay",
  "ok": true,
  "output": ["relay opened"],
  "code": 0
}
```

`reason` must be set when `ok` is false, and should say what went wrong in terms
an operator can act on.

**Delivery is at-least-once; exactly-once is not achievable and is not claimed.**
A device keeps the `cmdId`s it has applied, with the result it reported for each,
and treats a repeat as a no-op that replays the original result — `ok`, `output`,
`code` and `reason` as first sent — without repeating the work. That is what stops
a redelivered command from firing a relay twice, and a redelivered failure from
being answered as a success.

---

## Errors

| Status | Meaning |
| --- | --- |
| `400` | malformed, or `source` was not `"device"` |
| `401` | token missing, unknown or revoked |
| `403` | module outside the device's scope |
| `404` | module not on the device's project |
| `409` | the message's `v` is not `bbp/1` (or is missing); the body names both versions |
| `422` | a reading the module's preset could not have produced |

Every refusal body carries `{ "error": "<code>", "message": "<sentence>" }` (a `400`
adds the schema `issues`). The version
check (`error: "bridge-version"`) applies to `POST /api/device/ingest` and
`POST /api/device/results`, and runs after the token check, so a bad token is still
`401` whatever the version.

A `result` whose `cmdId` the server is no longer waiting for (it already timed the
command out) is not an error: it answers `200` with
`{ "accepted": false, "reason": "…" }`.

---

## Availability, and why it is not the device's to claim

A preset reports `available` when a device is connected **and** scoped to the
module in question. The connection is a server-side fact derived from the long
poll, so a device that has stopped answering cannot hold the state open, and a
device asserting that it is alive is not evidence that it is.

With no device connected, the preset reports `unavailable` with a reason naming
what is missing — unless the server's own local path can genuinely reach the
hardware (a serial port that exists on the server, say), in which case it reports
that `available` for a read. A write with no device is always `unavailable`.
There is no `simulated` state: no value is ever modelled in place of one a device
did not send.

`available` never implies a write will succeed. Every action that changes
anything requires a single-use confirmation token minted by the server, and that
is true whether a device is attached or not.
