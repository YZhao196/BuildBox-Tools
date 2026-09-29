"""The two libraries talking to each other, over a real socket.

This is the test that matters for independence. It starts a real HTTP server
from `buildbox_management`, points the real `buildbox` client at it, and drives
the protocol end to end — with no BuildBox anywhere in the process. If either
library ever grew a dependency on the product, this file would stop working
before anything else did.

The device package is not a dependency of this one, so it is skipped rather than
required when someone installs this package on its own.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

import pytest

pytest.importorskip("buildbox", reason="the device package is not installed here")

from buildbox import AuthError, BuildBoxError, Client, ScopeError, ShapeError

from buildbox_management import Management


@pytest.fixture
def box():
    """A receiver on a real port, with two modules on one project."""
    management = Management(poll_wait=2.0, command_timeout=5.0)
    management.add_module("proj-1", "mod-temp", name="Temperature")
    management.add_module("proj-1", "mod-lidar", name="Lidar", shapes=["scan"])

    received = []
    management.on_events(received.append)

    server = management.serve("127.0.0.1", 0, block=False)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield management, url, received
    finally:
        server.shutdown()
        server.server_close()


def a_device(management, url, modules=("mod-temp",), label="rover"):
    """Mint a device and a client for it, as a program would hand one to a robot."""
    device, token = management.mint("proj-1", label, list(modules))
    return device, Client(url, token, poll_wait=0.2, timeout=5.0)


def post(url, path, token, payload):
    """A raw POST, for the messages the shipped client has no method for.

    Returns a refusal as its status rather than raising, because a refusal is
    the answer several of these tests are checking for.
    """
    request = urllib.request.Request(
        url + path, data=json.dumps(payload).encode("utf-8"), method="POST"
    )
    request.add_header("authorization", f"Bearer {token}")
    request.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, (json.loads(raw) if raw else None)


# ------------------------------------------------------------------ #
# Identity                                                           #
# ------------------------------------------------------------------ #


def test_a_device_can_ask_what_it_is_and_what_it_may_touch(box):
    management, url, _ = box
    _, client = a_device(management, url, ["mod-temp", "mod-lidar"])

    info = client.whoami()

    assert info["device"]["label"] == "rover"
    assert info["device"]["online"] is True
    assert [module["id"] for module in info["modules"]] == ["mod-temp", "mod-lidar"]
    # It is told the names it may show, and nothing about the rest of the project.
    assert info["modules"][0]["name"] == "Temperature"


def test_a_token_that_is_not_a_token_is_refused(box):
    _, url, _ = box
    client = Client(url, "not-a-token", poll_wait=0.2)
    with pytest.raises(AuthError):
        client.whoami()


def test_revoking_a_device_stops_it_on_its_very_next_call(box):
    management, url, _ = box
    device, client = a_device(management, url)
    client.whoami()

    assert management.revoke("proj-1", device.id) is True

    with pytest.raises(AuthError):
        client.whoami()


def test_a_revoked_device_cannot_report(box):
    management, url, received = box
    device, client = a_device(management, url)
    management.revoke("proj-1", device.id)

    with pytest.raises(AuthError):
        client.send_data("temperature", 21.5, module="mod-temp")
    assert received == []


# ------------------------------------------------------------------ #
# Gathering                                                          #
# ------------------------------------------------------------------ #


def test_a_reading_arrives_unaltered(box):
    management, url, received = box
    _, client = a_device(management, url)

    client.send_data("temperature", 21.5, unit="C", module="mod-temp")

    assert len(received) == 1
    reading = received[0]
    assert reading.module_id == "mod-temp"
    assert reading.device_label == "rover"
    assert reading.events[0]["key"] == "temperature"
    assert reading.events[0]["value"] == 21.5
    # The receiver adds nothing to a reading. Provenance was the one field it
    # used to stamp on each event; with modelling gone there is no second value
    # to distinguish, so the field is deleted at both ends rather than restamped.
    assert "source" not in reading.events[0]


def test_a_device_scoped_to_one_module_need_not_name_it_every_time(box):
    management, url, received = box
    _, client = a_device(management, url, ["mod-temp"])
    client.send_data("temperature", 21.5)
    assert received[0].module_id == "mod-temp"


def test_a_device_scoped_to_several_is_asked_which_rather_than_guessed_at(box):
    management, url, received = box
    _, client = a_device(management, url, ["mod-temp", "mod-lidar"])

    # Reporting a temperature against the wrong module is worse than an error.
    with pytest.raises(ScopeError):
        client.send_data("temperature", 21.5)
    assert received == []


def test_a_device_cannot_report_on_a_module_it_was_not_given(box):
    management, url, received = box
    _, client = a_device(management, url, ["mod-temp"])

    with pytest.raises(ScopeError):
        client.send_data("range", 1.0, module="mod-lidar")
    assert received == []


def test_a_batch_arrives_whole(box):
    management, url, received = box
    _, client = a_device(management, url)

    client.send_many({"temperature": 21.5, "humidity": 44}, module="mod-temp")

    assert [event["key"] for event in received[0].events] == ["temperature", "humidity"]


# ------------------------------------------------------------------ #
# Refusals                                                           #
# ------------------------------------------------------------------ #


def test_a_reading_the_module_could_not_have_produced_is_refused(box):
    management, url, received = box
    _, client = a_device(management, url, ["mod-temp"])

    # A temperature module carries numbers; a laser sweep on it would draw
    # something no sensor measured.
    with pytest.raises(ShapeError):
        client.send_shape("scan", [1.0, 1.2], module="mod-temp")
    assert received == []


def test_a_module_that_displays_a_scan_refuses_a_cloud(box):
    management, url, received = box
    _, client = a_device(management, url, ["mod-lidar"])

    with pytest.raises(ShapeError):
        client.send_shape("cloud", [1.0, 2.0, 3.0], module="mod-lidar")
    assert received == []


def test_the_shape_the_module_displays_arrives(box):
    management, url, received = box
    _, client = a_device(management, url, ["mod-lidar"])

    client.send_shape("scan", [1.0, 1.2], angleMin=-1.57, angleMax=1.57, module="mod-lidar")

    assert received[0].events[0]["shape"] == "scan"
    assert received[0].events[0]["points"] == [1.0, 1.2]


def test_a_malformed_reading_is_refused(box):
    management, url, received = box
    _, client = a_device(management, url)

    with pytest.raises(BuildBoxError):
        client.send({"kind": "sample", "value": 1.0}, module="mod-temp")
    assert received == []


def test_a_batch_with_one_bad_reading_takes_none_of_it(box):
    management, url, received = box
    _, client = a_device(management, url)

    # All or nothing: half a batch would leave the program holding readings the
    # device never sent as a set.
    with pytest.raises(BuildBoxError):
        client.send(
            [
                {"kind": "sample", "key": "temperature", "value": 21.5},
                {"kind": "sample", "key": "humidity", "value": "damp"},
            ],
            module="mod-temp",
        )
    assert received == []


def test_an_oversized_body_is_refused_before_it_is_read(box):
    management, url, _ = box
    _, token = management.mint("proj-1", "big", ["mod-temp"])
    parts = urlsplit(url)

    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    try:
        connection.putrequest("POST", "/api/device/ingest")
        connection.putheader("authorization", f"Bearer {token}")
        connection.putheader("content-type", "application/json")
        connection.putheader("connection", "close")
        # A Content-Length is not a promise the bytes are coming. The point is
        # that this is refused on the header, without reading what was offered.
        connection.putheader("content-length", str(200 * 1024 * 1024))
        connection.endheaders()

        response = connection.getresponse()
        assert response.status == 413
    finally:
        connection.close()


def test_a_program_that_fails_to_take_delivery_is_not_told_it_succeeded(box):
    management, url, _ = box

    def explode(reading):
        raise RuntimeError("the disk is full")

    management.on_events(explode)
    _, client = a_device(management, url)

    # Answering success would let the device drop the reading on the floor.
    with pytest.raises(BuildBoxError):
        client.send_data("temperature", 21.5, module="mod-temp")


# ------------------------------------------------------------------ #
# Commanding                                                         #
# ------------------------------------------------------------------ #


def _listening(client):
    """Drive the device's command channel on its own thread until stopped."""
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            client.check(wait=0.2)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return stop, thread


def _finish(stop, thread):
    stop.set()
    thread.join(timeout=3)


def test_a_command_reaches_the_device_and_its_answer_comes_back(box):
    management, url, _ = box
    device, client = a_device(management, url)

    handled = []

    @client.on_command(match="read_temp")
    def handle(command):
        handled.append(command.cmd)
        return 0

    client.whoami()  # the device has spoken, so it is online
    stop, thread = _listening(client)
    try:
        result = management.command(
            device.id, "mod-temp", "read", cmd="read_temp", timeout=5.0
        )
    finally:
        _finish(stop, thread)

    assert handled == ["read_temp"]
    assert result.ok is True
    assert result.code == 0


def test_a_command_the_device_has_no_handler_for_is_reported_as_a_failure(box):
    management, url, _ = box
    device, client = a_device(management, url)
    client.whoami()
    stop, thread = _listening(client)
    try:
        result = management.command(
            device.id, "mod-temp", "run", cmd="launch_rocket", timeout=5.0
        )
    finally:
        _finish(stop, thread)

    # A device that silently claimed success for work it did not do would be
    # lying to whoever pressed the button.
    assert result.ok is False
    assert "no handler" in (result.reason or "").lower()


def test_a_handler_that_raises_is_a_reported_failure(box):
    management, url, _ = box
    device, client = a_device(management, url)

    @client.on_command(match="explode")
    def handle(command):
        raise RuntimeError("the motor is jammed")

    client.whoami()
    stop, thread = _listening(client)
    try:
        result = management.command(
            device.id, "mod-temp", "run", cmd="explode", timeout=5.0
        )
    finally:
        _finish(stop, thread)

    assert result.ok is False
    assert "the motor is jammed" in (result.reason or "")


def test_an_unanswered_command_fails_rather_than_hanging(box):
    management, url, _ = box
    device, client = a_device(management, url)
    client.whoami()  # online, but nothing is polling

    result = management.command(device.id, "mod-temp", "read", timeout=0.2)

    assert result.ok is False
    assert "Nothing was confirmed" in (result.reason or "")


def test_a_command_that_timed_out_is_never_delivered_afterwards(box):
    """The safety rule, end to end: told nothing happened, so nothing happens."""
    management, url, _ = box
    device, client = a_device(management, url)
    client.whoami()  # online, but nothing is polling right now

    result = management.command(
        device.id, "mod-temp", "run", cmd="kill_switch 1", timeout=0.2
    )
    assert result.ok is False

    # The device polls a moment later. A command left in the queue would be
    # handed over here — firing the relay after the caller was told it was not
    # confirmed, which is the exact lie this library exists to prevent.
    assert client.poll(wait=0.2) is None


def test_a_device_may_announce_itself_without_reporting_anything(box):
    management, url, received = box
    device, token = management.mint("proj-1", "rover", ["mod-temp"])

    # `hello` is optional in the protocol and measures nothing, so nothing is
    # published — but the device is talking, so it counts as alive.
    status, body = post(
        url,
        "/api/device/ingest",
        token,
        {"v": "bbp/1", "type": "hello", "agent": "python/0.1.0", "capabilities": []},
    )

    assert status == 200
    assert body == {"accepted": 0}
    assert received == []
    assert management.registry.online(management.device(device.id)) is True


def test_a_reading_that_is_nonsense_is_refused_rather_than_crashing_the_receiver(box):
    management, url, received = box
    _, token = management.mint("proj-1", "rover", ["mod-temp"])

    # A kind that is a list cannot be looked up in a set, so this is the input
    # that used to raise and surface as a 500 instead of a refusal.
    status, body = post(
        url,
        "/api/device/ingest",
        token,
        {
            "v": "bbp/1",
            "type": "events",
            "moduleId": "mod-temp",
            "source": "device",
            "events": [{"kind": ["sample"]}],
        },
    )

    assert status == 422
    assert body["error"] == "invalid-reading"
    assert received == []


def test_a_command_is_not_sent_to_a_device_that_is_not_polling(box):
    management, url, _ = box
    device, _ = a_device(management, url)  # never speaks

    result = management.command(device.id, "mod-temp", "read")

    # Refused before sending, rather than holding the caller for the whole
    # timeout to be told what was already true when they asked.
    assert result.ok is False
    assert "nothing was sent" in (result.reason or "").lower()


def test_the_receiver_refuses_to_command_outside_a_devices_scope(box):
    management, url, _ = box
    device, client = a_device(management, url, ["mod-temp"])
    client.whoami()

    result = management.command(device.id, "mod-lidar", "read")

    assert result.ok is False
    assert "not scoped" in (result.reason or "")


def test_the_long_poll_is_the_heartbeat(box):
    management, url, _ = box
    device, client = a_device(management, url)

    # A device that has only polled and found nothing to do is still alive, and
    # the server decides that from the poll rather than the device's word.
    client.poll(wait=0)

    assert management.registry.online(management.device(device.id)) is True

    time.sleep(0.01)
    assert management.registry.connected_for_module("mod-temp").id == device.id
