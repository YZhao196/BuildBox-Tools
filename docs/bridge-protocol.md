# The Bridge Protocol

Version `bbp/1`. This document is normative; the two implementations
(`python/buildbox/protocol.py` and `cpp/include/buildbox/client.hpp`) are ports
of it, and `packages/shared/src/bridge.ts` is its executable form.

It exists so that a device can be written in any language against one stable
contract, rather than each integration being a new piece of software. Python and
C++ ship today; a third binding is a port, not a project.

---

## Two rules a device must not break

These are not conventions. The server enforces both, and a device that assumes
otherwise will be refused rather than accommodated.

1. **A reading is always labelled.** Every event sent to `/api/device/ingest`
   carries `source: "device"`, which the server sets itself rather than reading
   from the wire. A value a device reports can therefore never be confused with
   one the server modelled when no device was present, and a device cannot claim
   to be the model.
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

Optional. Sent so the server can record what a device is.

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
| `image` | — | `image`, a `data:` URL |

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
A device keeps the `cmdId`s it has applied and treats a repeat as a no-op,
answering success without repeating the work. That is what stops a redelivered
command from firing a relay twice.

---

## Errors

| Status | Meaning |
| --- | --- |
| `400` | malformed, or `source` was not `"device"` |
| `401` | token missing, unknown or revoked |
| `403` | module outside the device's scope |
| `404` | module not on the device's project |
| `422` | a reading the module's preset could not have produced |

---

## Availability, and why it is not the device's to claim

A preset reports `available` when a device is connected **and** scoped to the
module in question. The connection is a server-side fact derived from the long
poll, so a device that has stopped answering cannot hold the state open, and a
device asserting that it is alive is not evidence that it is.

With no device connected, the preset reports what it always did — `simulated`
with a reason naming what is missing, or `unavailable`.

`available` never implies a write will succeed. Every action that changes
anything requires a single-use confirmation token minted by the server, and that
is true whether a device is attached or not.
