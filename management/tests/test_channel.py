"""The live channel: long polls, commands, and what a timeout means."""

from __future__ import annotations

import threading
import time

import pytest

from buildbox_management.channel import Channel, Result, new_command


@pytest.fixture
def channel() -> Channel:
    return Channel(poll_wait=5.0, command_timeout=5.0)


def _in_a_thread(target):
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def test_a_command_queued_before_the_poll_is_handed_over(channel):
    command = new_command("mod-1", "read")
    channel._queued.setdefault("d-1", []).append(command)
    assert channel.next_command("d-1", wait=1.0).cmd_id == command.cmd_id


def test_an_empty_poll_answers_nothing_when_the_wait_elapses(channel):
    # The long poll is the heartbeat, so returning empty is a normal answer and
    # not an error.
    assert channel.next_command("d-1", wait=0.05) is None


def test_a_poll_waiting_is_woken_by_a_command(channel):
    seen = {}

    def poll():
        seen["command"] = channel.next_command("d-1", wait=5.0)

    thread = _in_a_thread(poll)
    time.sleep(0.05)  # let the poll register before the command arrives
    command = new_command("mod-1", "read")

    def call():
        seen["result"] = channel.dispatch("d-1", command)

    caller = _in_a_thread(call)
    thread.join(timeout=2)
    assert seen["command"].cmd_id == command.cmd_id

    channel.complete(Result(cmd_id=command.cmd_id, module_id="mod-1", ok=True))
    caller.join(timeout=2)
    assert seen["result"].ok is True


def test_a_command_nobody_answers_is_a_failure_not_a_silence():
    # The property the whole design rests on: a relay that did not confirm is a
    # relay nobody knows the state of, and it must not read as success.
    channel = Channel()
    result = channel.dispatch("d-1", new_command("mod-1", "run"), timeout=0.05)
    assert result.ok is False
    assert "Nothing was confirmed" in result.reason


def test_a_result_for_a_command_nobody_is_waiting_for_is_reported_not_raised(channel):
    # A device may answer after the caller gave up. That is not the device's
    # fault, so it is reported rather than treated as an error.
    assert channel.complete(Result(cmd_id="nope", module_id="mod-1", ok=True)) is False


def test_a_command_that_timed_out_is_not_left_for_the_device_to_collect():
    # The whole safety story in one test. If the command were still queued, the
    # device would collect it on its next poll and fire the relay — after the
    # caller was told nothing was confirmed. A retry would then queue a second
    # command under a new id, which the device's de-duplication cannot catch.
    channel = Channel()
    result = channel.dispatch("d-1", new_command("mod-1", "run", cmd="kill_switch 1"), timeout=0.05)
    assert result.ok is False

    assert channel.next_command("d-1", wait=0.05) is None
    assert channel.pending("d-1") == 0


def test_a_command_that_timed_out_does_not_leave_an_empty_queue_behind(channel):
    channel.dispatch("d-1", new_command("mod-1", "read"), timeout=0.05)
    assert "d-1" not in channel._queued


def test_another_device_cannot_answer_a_command_it_was_not_sent():
    # Ids are unguessable, but a command belongs to the device it was sent to.
    channel = Channel()
    command = new_command("mod-1", "run")
    _in_a_thread(lambda: channel.dispatch("d-1", command, timeout=2.0))
    time.sleep(0.05)

    assert channel.complete(Result(cmd_id=command.cmd_id, module_id="mod-1", ok=True), device_id="d-2") is False
    assert channel.complete(Result(cmd_id=command.cmd_id, module_id="mod-1", ok=True), device_id="d-1") is True


def test_forgetting_a_device_releases_the_thread_holding_its_poll(channel):
    seen = {}

    def poll():
        seen["command"] = channel.next_command("d-1", wait=5.0)

    thread = _in_a_thread(poll)
    time.sleep(0.05)
    channel.forget("d-1")
    thread.join(timeout=2)
    assert seen["command"] is None


def test_a_second_poll_replaces_the_first(channel):
    # A device that reconnects while its old poll is open would otherwise leave
    # that one to time out on its own, holding a thread for no reason.
    seen = {}

    def first():
        seen["first"] = channel.next_command("d-1", wait=5.0)

    def second():
        seen["second"] = channel.next_command("d-1", wait=5.0)

    a = _in_a_thread(first)
    time.sleep(0.05)
    b = _in_a_thread(second)
    a.join(timeout=2)
    assert seen["first"] is None

    channel.forget("d-1")
    b.join(timeout=2)


def test_the_command_wire_form_matches_the_protocol():
    command = new_command("mod-1", "run", cmd="kill_switch 1", target="10.0.0.5")
    message = command.as_dict()
    assert message["v"] == "bbp/1"
    assert message["type"] == "command"
    assert message["moduleId"] == "mod-1"
    assert message["action"] == "run"
    assert message["cmd"] == "kill_switch 1"
    assert message["target"] == "10.0.0.5"


def test_an_observing_action_is_a_read_and_a_changing_one_is_not():
    assert new_command("mod-1", "scan").reads is True
    assert new_command("mod-1", "run").writes is True


def test_a_result_round_trips_through_the_wire():
    original = Result(cmd_id="c-1", module_id="mod-1", ok=False, reason="relay stuck")
    assert Result.from_wire(original.as_dict()) == original
