"""The port, checked against the specification it is a port of.

`docs/bridge-protocol.md` is normative, and the examples in it are the contract
a device in any language is written against. This pulls those examples out of
the document and pushes them through the real client, so the two cannot drift
apart quietly.

`buildbox/protocol.py` cites this file by name. Before it existed, that was a
claim about a test nobody had written.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict

import pytest

from buildbox import Client, Command, Result, protocol

SPECIFICATION = Path(__file__).resolve().parents[2] / "docs" / "bridge-protocol.md"

if not SPECIFICATION.exists():
    # The specification lives beside the packages, not inside one, so it is here
    # in any checkout and absent from an installed copy. Reported as skipped
    # rather than passed, for the same reason the management suite does it.
    pytest.skip(
        f"No specification at {SPECIFICATION}.", allow_module_level=True
    )


def specification_examples() -> Dict[str, Dict[str, Any]]:
    """Every JSON example in the specification, keyed by its `type`."""
    text = SPECIFICATION.read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)```", text, re.S)
    assert blocks, f"No JSON examples found in {SPECIFICATION}."

    examples: Dict[str, Dict[str, Any]] = {}
    for block in blocks:
        message = json.loads(block)
        examples[message["type"]] = message
    return examples


EXAMPLES = specification_examples()


def a_client(monkeypatch, responses: Dict[str, Any]) -> Client:
    """A client whose transport answers from `responses`, keyed by path prefix."""
    monkeypatch.setattr(
        Client,
        "_request",
        lambda self, method, path, payload=None, timeout=None: next(
            response for prefix, response in responses.items() if path.startswith(prefix)
        ),
    )
    return Client("http://example.test", "token-abc")


# ------------------------------------------------------------------ #
# The examples are still examples                                    #
# ------------------------------------------------------------------ #


def test_the_specification_still_documents_all_four_messages():
    # If a message is added to the document and not here, this fails — which is
    # the reminder to port it.
    assert set(EXAMPLES) == {"hello", "events", "command", "result"}


@pytest.mark.parametrize("kind", ["hello", "events", "command", "result"])
def test_every_example_is_written_in_the_version_this_port_speaks(kind):
    assert EXAMPLES[kind]["v"] == protocol.VERSION


# ------------------------------------------------------------------ #
# Uplink                                                             #
# ------------------------------------------------------------------ #


def test_hello_carries_the_fields_the_specification_shows():
    assert set(protocol.hello(["temperature"])) == set(EXAMPLES["hello"])


def test_the_envelope_carries_the_fields_the_specification_shows():
    built = protocol.envelope("mod-temp", [protocol.sample("temperature", 21.5)])
    assert set(built) == set(EXAMPLES["events"])


def test_a_reading_is_labelled_a_devices_and_the_specification_says_so():
    # Rule one of the document, in both directions: the example carries it, and
    # the port cannot build an envelope without it.
    assert EXAMPLES["events"]["source"] == "device"
    assert protocol.envelope("mod-temp", [])["source"] == "device"


def test_every_reading_in_the_specification_is_one_this_port_can_build():
    kinds = {event["kind"] for event in EXAMPLES["events"]["events"]}
    assert kinds <= protocol.KINDS


# ------------------------------------------------------------------ #
# Downlink                                                           #
# ------------------------------------------------------------------ #


def test_the_specifications_command_reaches_a_device_intact(monkeypatch):
    example = EXAMPLES["command"]
    client = a_client(monkeypatch, {"/api/device/commands": (200, example)})

    command = client.poll(wait=0)

    assert command.cmd_id == example["cmdId"]
    assert command.module_id == example["moduleId"]
    assert command.action == example["action"]
    assert command.cmd == example["cmd"]
    assert command.target == example["target"]
    assert command.timeout_ms == example["timeoutMs"]
    # The example is `run kill_switch 1`, which changes something.
    assert command.writes is True


def test_the_specifications_result_is_the_shape_this_port_sends_back(monkeypatch):
    example = EXAMPLES["result"]
    sent: Dict[str, Any] = {}

    def record(self, method, path, payload=None, timeout=None):
        sent.update(payload or {})
        return 200, {"accepted": True}

    monkeypatch.setattr(Client, "_request", record)
    client = Client("http://example.test", "token-abc")

    @client.on_command
    def handle(command):
        return Result(ok=True, output=list(example["output"]), code=example["code"])

    client.apply(
        Command(
            cmd_id=example["cmdId"],
            module_id=example["moduleId"],
            action="read",
            cmd="",
        )
    )

    # Exactly the fields, not merely a superset: a device that sends a field the
    # specification does not name is inventing wire format.
    assert set(sent) == set(example)
    assert sent["ok"] is True
    assert sent["output"] == list(example["output"])
