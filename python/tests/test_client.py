"""The client's behaviour, tested against a stubbed transport.

The transport is stubbed rather than a server being started, because what is
being checked here is what the device decides and what it sends. The wire
contract itself is exercised over a real socket by
``management/tests/test_server.py``, which drives this client against a real
receiver, and against the real BuildBox server by that repository's
``apps/server/src/routes/devices.test.ts`` — a path in a checkout this suite
cannot see, so it is named here rather than depended on.
"""

from __future__ import annotations

import json

import pytest

from buildbox import Client, Command, Result, data, interpret
from buildbox import protocol


def make_client(**kwargs) -> Client:
    return Client("http://example.test", "token-abc", **kwargs)


class Recorder:
    """Stands in for the HTTP call and remembers what was sent."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, method, path, payload=None, timeout=None):
        self.calls.append((method, path, payload))
        for prefix, response in self.responses.items():
            if path.startswith(prefix):
                return response
        return 200, {"accepted": 1}


@pytest.fixture
def recorder(monkeypatch):
    recorder = Recorder({"/api/device/whoami": (200, {"modules": [{"id": "mod-1", "name": "temp"}]})})
    monkeypatch.setattr(Client, "_request", lambda self, m, p, payload=None, timeout=None: recorder(m, p, payload, timeout))
    return recorder


# ------------------------------------------------------------------ #
# Return values                                                       #
# ------------------------------------------------------------------ #


def test_a_handler_may_return_nothing_for_success():
    assert interpret(None) == Result(ok=True)


def test_an_exit_code_is_a_verdict():
    assert interpret(0).ok is True
    assert interpret(0).code == 0
    # Anything that is not zero did not work, which is what a shell exit code
    # means and what an operator reading the output will assume.
    assert interpret(1).ok is False
    assert interpret(1).code == 1


def test_true_is_success_rather_than_exit_code_one():
    # bool is a subclass of int, so this ordering is load-bearing.
    assert interpret(True).ok is True
    assert interpret(True).code == 0
    assert interpret(False).ok is False


def test_text_becomes_output():
    assert interpret("stopped").output == ["stopped"]
    assert interpret(["a", "b"]).output == ["a", "b"]


def test_a_result_is_taken_as_given():
    given = Result(ok=False, reason="no")
    assert interpret(given) is given


def test_an_unusable_return_value_is_named():
    with pytest.raises(TypeError):
        interpret(object())


# ------------------------------------------------------------------ #
# Protocol builders                                                   #
# ------------------------------------------------------------------ #


def test_sample_carries_its_unit():
    assert protocol.sample("temp", 21.5, "C") == {
        "kind": "sample",
        "key": "temp",
        "value": 21.5,
        "unit": "C",
    }


def test_shape_refuses_a_kind_it_does_not_know():
    with pytest.raises(ValueError):
        protocol.shape("spiral", [1, 2])


def test_a_shape_needs_points_or_an_image():
    with pytest.raises(ValueError):
        protocol.shape("image", [])
    assert protocol.shape("image", [], image="data:image/png;base64,AA")["image"].startswith("data:")


def test_the_envelope_always_claims_to_be_a_device():
    # There is no parameter for this, so a caller cannot say otherwise.
    assert protocol.envelope("m", [])["source"] == "device"


# ------------------------------------------------------------------ #
# Commands                                                            #
# ------------------------------------------------------------------ #


def test_matching_is_by_whole_word_and_ignores_case():
    command = Command("c1", "m", "run", "sudo systemctl stop robot")
    assert command.matches("stop")
    assert command.matches("SYSTEMCTL")
    assert command.matches("stop robot")
    assert not command.matches("start")
    # A word inside another word is not that word.
    assert not command.matches("sto"), "a fragment of a word must not match"
    assert not Command("c", "m", "run", "stop homebridge").matches("home"), (
        "'home' must not match inside 'homebridge'"
    )


def test_a_stop_command_mentioning_home_does_not_reach_the_homing_handler():
    client = make_client()

    # Registered first, so substring matching with registration-order ties
    # would hand it a stop.
    @client.on_command(match="home")
    def homing(command):
        return "homing"

    @client.on_command(match="stop")
    def stop(command):
        return "stopped"

    result = client.dispatch(Command("c", "m", "run", "sudo systemctl stop homebridge"))
    assert result.output == ["stopped"], f"stop went to {result.output!r}"


def test_the_longest_match_wins_and_an_equal_tie_is_refused():
    client = make_client()

    @client.on_command(match="stop")
    def stop(command):
        return "stop"

    @client.on_command(match="stop home")
    def stop_home(command):
        return "stop home"

    @client.on_command(match="home")
    def home(command):
        return "home"

    assert client.dispatch(Command("c", "m", "run", "stop home now")).output == ["stop home"]
    # Two handlers, equally specific, both named: guessing is worse than saying so.
    tie = client.dispatch(Command("c", "m", "run", "home then stop"))
    assert tie.ok is False, "an equal tie must be refused, not settled by registration order"
    assert "ambiguous" in (tie.reason or "")


def test_a_failure_without_a_reason_is_given_one_naming_the_exit_code():
    client = make_client()

    @client.on_command(match="stop")
    def stop(command):
        return 3

    @client.on_command(match="home")
    def home(command):
        return Result(ok=False)

    coded = client.dispatch(Command("c", "m", "run", "stop"))
    assert coded.ok is False
    assert coded.reason and "3" in coded.reason, f"no reason naming the exit code: {coded.reason!r}"
    bare = client.dispatch(Command("c", "m", "run", "home"))
    assert bare.reason, "a failure must carry a reason (bridge-protocol.md)"


def test_registering_the_same_match_twice_is_an_error():
    client = make_client()
    client.on_command(match="stop")(lambda command: 0)
    with pytest.raises(ValueError):
        client.on_command(match="STOP")(lambda command: 0)


def test_an_action_knows_whether_it_writes():
    assert Command("c", "m", "read", "").reads is True
    assert Command("c", "m", "run", "").writes is True


def test_a_command_with_no_handler_is_refused_not_ignored():
    client = make_client()
    result = client.dispatch(Command("c", "m", "run", "stop"))

    # Silently reporting success would tell the interface something happened
    # that did not.
    assert result.ok is False
    assert "no handler" in (result.reason or "")


def test_a_handler_that_raises_did_not_do_the_thing():
    client = make_client()

    @client.on_command
    def explode(command):
        raise RuntimeError("the motor bus is not answering")

    result = client.dispatch(Command("c", "m", "run", "stop"))
    assert result.ok is False
    assert "the motor bus is not answering" in (result.reason or "")


def test_a_specific_handler_wins_over_a_catch_all():
    client = make_client()

    @client.on_command
    def everything(command):
        return "catch-all"

    @client.on_command(action="run")
    def writes(command):
        return "write"

    assert client.dispatch(Command("c", "m", "read", "")).output == ["catch-all"]
    assert client.dispatch(Command("c", "m", "run", "")).output == ["write"]


def test_a_handler_matching_the_command_text_is_selected():
    client = make_client()

    @client.on_command(match="kill_switch")
    def kill(command):
        return 0

    assert client.dispatch(Command("c", "m", "run", "kill_switch 1")).ok is True
    assert client.dispatch(Command("c", "m", "run", "something else")).ok is False


# ------------------------------------------------------------------ #
# Transport                                                           #
# ------------------------------------------------------------------ #


def test_send_data_reports_against_the_only_module_when_unnamed(recorder):
    client = make_client()
    assert client.send_data("temperature", 21.5, "C") == 1

    method, path, payload = recorder.calls[-1]
    assert (method, path) == ("POST", "/api/device/ingest")
    assert payload["moduleId"] == "mod-1"
    assert payload["source"] == "device"
    assert payload["events"][0]["value"] == 21.5


def test_send_many_batches_into_one_request(recorder):
    client = make_client()
    client.send_many({"temperature": 21.5, "humidity": 44})
    ingest_calls = [call for call in recorder.calls if call[1].endswith("/ingest")]
    assert len(ingest_calls) == 1
    assert len(ingest_calls[0][2]["events"]) == 2


def test_a_device_scoped_to_several_modules_must_say_which(monkeypatch):
    recorder = Recorder(
        {
            "/api/device/whoami": (
                200,
                {"modules": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]},
            )
        }
    )
    monkeypatch.setattr(
        Client,
        "_request",
        lambda self, m, p, payload=None, timeout=None: recorder(m, p, payload, timeout),
    )
    client = make_client()

    with pytest.raises(Exception) as caught:
        client.send_data("temperature", 21.5)
    assert "pass module=" in str(caught.value)

    # Naming one is enough.
    client.send_data("temperature", 21.5, module="b")
    assert recorder.calls[-1][2]["moduleId"] == "b"


def test_an_applied_command_is_not_applied_twice(recorder):
    client = make_client()
    seen = []

    @client.on_command
    def handle(command):
        seen.append(command.cmd_id)
        return 0

    command = Command("c1", "m", "run", "stop")
    client.apply(command)
    client.apply(command)

    # The transport is at-least-once, so a redelivery must not fire the relay
    # a second time.
    assert seen == ["c1"]

    first, second = [call[2] for call in recorder.calls if call[1].endswith("/results")]
    assert second == first


def test_a_redelivered_failure_replays_the_failure(recorder):
    client = make_client()
    seen = []

    @client.on_command
    def handle(command):
        seen.append(command.cmd_id)
        return Result(ok=False, reason="the relay did not move", code=2)

    command = Command("c2", "m", "run", "stop")
    client.apply(command)
    client.apply(command)

    assert seen == ["c2"]
    second = [call[2] for call in recorder.calls if call[1].endswith("/results")][-1]
    assert second["ok"] is False, "a redelivered failure must not be answered as a success"
    assert second["reason"] == "the relay did not move"
    assert second["code"] == 2


def test_apply_reports_a_failure_with_its_reason(recorder):
    client = make_client()

    @client.on_command
    def handle(command):
        return Result(ok=False, reason="the relay did not move", code=2)

    client.apply(Command("c9", "m", "run", "stop"))
    payload = [call for call in recorder.calls if call[1].endswith("/results")][-1][2]

    assert payload["ok"] is False
    assert payload["reason"] == "the relay did not move"
    assert payload["code"] == 2
    assert payload["cmdId"] == "c9"


def test_a_client_needs_a_token():
    with pytest.raises(ValueError):
        Client("http://example.test", "")


def test_polling_returns_none_when_the_server_has_nothing(monkeypatch):
    monkeypatch.setattr(
        Client, "_request", lambda self, m, p, payload=None, timeout=None: (204, None)
    )
    assert make_client().poll(wait=0) is None


def test_polling_reads_a_command(monkeypatch):
    body = {
        "cmdId": "c1",
        "moduleId": "m",
        "action": "run",
        "cmd": "stop",
        "target": "10.0.0.5",
        "timeoutMs": 5000,
    }
    monkeypatch.setattr(
        Client, "_request", lambda self, m, p, payload=None, timeout=None: (200, body)
    )
    command = make_client().poll(wait=0)
    assert command is not None
    assert (command.cmd_id, command.action, command.target) == ("c1", "run", "10.0.0.5")


def test_the_json_we_send_is_serialisable(recorder):
    client = make_client()
    client.send_data("temperature", 21.5)
    # A reading that cannot be serialised would fail at the socket, not here.
    json.dumps(recorder.calls[-1][2])


# ------------------------------------------------------------------ #
# What a reader may return                                            #
# ------------------------------------------------------------------ #


def test_a_reader_may_hand_back_a_structural_reading(recorder):
    # A laser sweep is not a series of samples, so a reader returns the events
    # `buildbox.data` builds and they go out as the shape they are.
    client = make_client()
    client.sensor("scan", lambda: data.laser_scan([1.0, 2.0], -1.57, 1.57), module="mod-1")

    client.sample_once()

    sent = recorder.calls[-1][2]["events"]
    assert sent[0]["kind"] == "shape"
    assert sent[0]["shape"] == "scan"
    assert sent[0]["points"] == [1.0, 2.0]


def test_a_reader_passing_off_a_plain_list_is_refused(recorder):
    # Several numbers are said with a mapping; a bare list is a mistake, and
    # sending it would put something on the wire nobody meant.
    client = make_client()
    client.sensor("junk", lambda: [1.0, 2.0], module="mod-1")

    with pytest.raises(TypeError):
        client.sample_once()


def test_a_reader_may_return_nothing_to_report(recorder):
    # A topic with no publisher yet is not a broken sensor, and must not send a
    # number nobody measured — so nothing goes out at all.
    client = make_client()
    client.sensor("battery", lambda: None, module="mod-1")

    assert client.sample_once() == 0
    assert not [call for call in recorder.calls if call[1].endswith("/ingest")]
