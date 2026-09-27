"""What a reading is, and what it is not."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict

import pytest

from buildbox_management import Management, protocol
from buildbox_management.channel import new_command

SPECIFICATION = Path(__file__).resolve().parents[2] / "docs" / "bridge-protocol.md"

if not SPECIFICATION.exists():
    pytest.skip(f"No specification at {SPECIFICATION}.", allow_module_level=True)


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


def refusal(event):
    return protocol.validate_event(event)


# ------------------------------------------------------------------ #
# Kinds                                                              #
# ------------------------------------------------------------------ #


def test_a_reading_must_be_an_object():
    assert "object" in refusal(["not", "a", "reading"])


def test_a_reading_must_be_a_known_kind():
    assert "kind of reading" in refusal({"kind": "telepathy"})


def test_a_word_field_that_is_not_a_word_is_refused_rather_than_crashing():
    # These are `in frozenset` tests, which raise `TypeError` on an unhashable
    # value — so the guard is what turns a nonsense body into a refusal instead
    # of a 500 and a traceback.
    assert refusal({"kind": ["sample"]}) is not None
    assert refusal({"kind": {"sample": True}}) is not None
    assert refusal({"kind": "log", "message": "x", "severity": ["info"]}) is not None
    assert refusal({"kind": "status", "status": {"ok": True}}) is not None
    assert refusal({"kind": "shape", "shape": ["scan"], "points": [1.0]}) is not None


def test_a_map_dimension_that_is_a_boolean_is_refused():
    # bool is a subclass of int, so `True` would otherwise read as a 1x1 grid.
    assert refusal(
        {"kind": "shape", "shape": "map", "points": [1], "cols": True, "rows": True}
    ) is not None


def test_a_sample_needs_a_series_and_a_finite_number():
    assert refusal({"kind": "sample", "key": "temperature", "value": 21.5}) is None
    assert "series" in refusal({"kind": "sample", "value": 21.5})
    assert "finite" in refusal({"kind": "sample", "key": "t", "value": "warm"})


def test_a_sample_that_is_not_a_number_is_refused():
    # NaN is a float, so `isinstance(x, float)` is not the check that matters: a
    # NaN would serialize to a token the reader is entitled to reject, or plot
    # as a gap nobody explained.
    assert refusal({"kind": "sample", "key": "t", "value": float("nan")}) is not None
    assert refusal({"kind": "sample", "key": "t", "value": float("inf")}) is not None


def test_a_log_line_needs_a_message_and_a_known_severity():
    assert refusal({"kind": "log", "message": "warmed up", "severity": "info"}) is None
    assert "message" in refusal({"kind": "log", "severity": "info"})
    assert "severity" in refusal({"kind": "log", "message": "x", "severity": "shout"})


def test_a_status_must_be_one_the_interface_knows():
    assert refusal({"kind": "status", "status": "armed"}) is None
    assert "status" in refusal({"kind": "status", "status": "vibing"})


# ------------------------------------------------------------------ #
# Structural readings                                                #
# ------------------------------------------------------------------ #


def test_a_structural_reading_must_say_which_shape_it_is():
    assert "which shape" in refusal({"kind": "shape"})


def test_a_scan_carries_its_ranges():
    assert refusal({"kind": "shape", "shape": "scan", "points": [1.0, 1.2]}) is None
    assert "points" in refusal({"kind": "shape", "shape": "scan", "points": "far"})


def test_a_map_must_fill_its_grid():
    # A map that does not fill its grid would draw cells nothing measured, so it
    # is refused rather than padded.
    assert refusal(
        {"kind": "shape", "shape": "map", "points": [1, 2, 3, 4], "cols": 2, "rows": 2}
    ) is None
    assert "row-major" in refusal(
        {"kind": "shape", "shape": "map", "points": [1, 2, 3], "cols": 2, "rows": 2}
    )


def test_an_image_carries_a_frame_rather_than_points():
    assert refusal({"kind": "shape", "shape": "image", "image": "data:image/png;base64,x"}) is None
    assert "data URL" in refusal({"kind": "shape", "shape": "image"})


# ------------------------------------------------------------------ #
# Whether a module could have produced it                            #
# ------------------------------------------------------------------ #


def test_a_module_with_no_shapes_carries_no_structural_reading():
    event = {"kind": "shape", "shape": "scan", "points": [1.0]}
    assert "does not display" in protocol.refusal_for_shape(set(), event)


def test_a_scan_is_refused_on_a_module_that_displays_clouds():
    event = {"kind": "shape", "shape": "scan", "points": [1.0]}
    refusal = protocol.refusal_for_shape({"cloud"}, event)
    assert "cloud" in refusal and "scan" in refusal


def test_a_scan_is_allowed_on_the_module_that_displays_it():
    event = {"kind": "shape", "shape": "scan", "points": [1.0]}
    assert protocol.refusal_for_shape({"scan"}, event) is None


# ------------------------------------------------------------------ #
# Vocabulary                                                         #
# ------------------------------------------------------------------ #


@pytest.mark.parametrize("action", ["read", "check", "poll", "scan", "capture"])
def test_observing_actions_are_reads(action):
    assert protocol.is_read(action)


@pytest.mark.parametrize("action", ["run", "write", "publish", "call", "toggle", "reset"])
def test_changing_actions_are_writes(action):
    # The distinction is what the caller uses to know whether it is about to ask
    # a robot to do something.
    assert not protocol.is_read(action)


def test_the_version_is_the_one_the_protocol_document_names():
    assert protocol.VERSION == "bbp/1"


# ------------------------------------------------------------------ #
# Against the specification itself                                   #
# ------------------------------------------------------------------ #
#
# `docs/bridge-protocol.md` is normative and this is a port of it. These push
# the document's own examples through the receiver, so a change to one that is
# not made to the other fails here rather than quietly at a customer's site.


def _post(management, path, token, message):
    return management.handle(
        "POST",
        path,
        headers={"authorization": f"Bearer {token}"},
        body=json.dumps(message).encode("utf-8"),
    )


def test_the_receiver_speaks_the_version_the_examples_are_written_in():
    for name in ("hello", "events", "command", "result"):
        assert EXAMPLES[name]["v"] == protocol.VERSION, name


def test_the_specifications_command_is_the_shape_this_receiver_sends():
    example = EXAMPLES["command"]
    built = new_command(
        example["moduleId"],
        example["action"],
        cmd=example["cmd"],
        target=example["target"],
    ).as_dict()

    # Exactly the fields — a receiver that invents wire format would send one
    # the specification does not name, and fail here.
    assert set(built) == set(example)
    assert built["v"] == example["v"]


def test_the_specifications_readings_are_accepted():
    example = EXAMPLES["events"]
    management = Management()
    # The example carries a scan, so its module displays one.
    management.add_module("proj-1", example["moduleId"], shapes=["scan"])
    _, token = management.mint("proj-1", "rover", [example["moduleId"]])

    response = _post(management, "/api/device/ingest", token, example)

    assert response.status == 200
    assert response.body == {"accepted": len(example["events"])}


def test_the_specifications_hello_is_accepted():
    example = EXAMPLES["hello"]
    management = Management()
    management.add_module("proj-1", "mod-temp")
    _, token = management.mint("proj-1", "rover", ["mod-temp"])

    response = _post(management, "/api/device/ingest", token, example)

    assert response.status == 200


def test_the_specifications_result_is_understood():
    example = EXAMPLES["result"]
    management = Management()
    management.add_module("proj-1", "mod-temp")
    _, token = management.mint("proj-1", "rover", ["mod-temp"])

    response = _post(management, "/api/device/results", token, example)

    # Nothing was waiting for that id, which is reported rather than refused —
    # but the message itself was understood, which is what this checks.
    assert response.status == 200
    assert response.body["accepted"] is False
