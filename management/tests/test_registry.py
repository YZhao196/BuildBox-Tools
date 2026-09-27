"""Identity: who may speak, and what each one may touch."""

from __future__ import annotations

import time

import pytest

from buildbox_management.registry import Registry, digest


@pytest.fixture
def registry() -> Registry:
    reg = Registry()
    reg.add_module("proj-1", "mod-temp", name="Temperature")
    reg.add_module("proj-1", "mod-lidar", name="Lidar", shapes=["scan"])
    return reg


def test_a_token_resolves_to_its_device(registry):
    device, token = registry.mint("proj-1", "rover", ["mod-temp"])
    assert registry.resolve(token).id == device.id


def test_the_token_is_not_stored_anywhere_readable(registry):
    device, token = registry.mint("proj-1", "rover", ["mod-temp"])
    stored = registry._by_id[device.id].token_digest
    assert token not in stored
    assert stored == digest(token)


def test_an_unknown_token_resolves_to_nothing(registry):
    assert registry.resolve("not-a-token") is None
    assert registry.resolve(None) is None
    assert registry.resolve("") is None


def test_a_device_must_be_scoped_to_at_least_one_module(registry):
    # A device scoped to "everything" by accident is the mistake this prevents.
    with pytest.raises(ValueError):
        registry.mint("proj-1", "rover", [])


def test_a_device_cannot_be_scoped_to_a_module_that_does_not_exist(registry):
    with pytest.raises(ValueError) as error:
        registry.mint("proj-1", "rover", ["mod-temp", "mod-typo"])
    assert "mod-typo" in str(error.value)


def test_a_module_on_another_project_does_not_count(registry):
    registry.add_module("proj-2", "mod-other")
    with pytest.raises(ValueError):
        registry.mint("proj-1", "rover", ["mod-other"])


def test_scope_is_what_the_device_may_report_on(registry):
    device, _ = registry.mint("proj-1", "rover", ["mod-temp"])
    assert device.may_report_on("mod-temp")
    assert not device.may_report_on("mod-lidar")


def test_revoking_takes_effect_on_the_token_immediately(registry):
    device, token = registry.mint("proj-1", "rover", ["mod-temp"])
    assert registry.revoke("proj-1", device.id) is True
    # Not merely marked: the credential stops being a key that resolves at all.
    assert registry.resolve(token) is None


def test_revoking_a_device_that_is_not_there_is_false(registry):
    assert registry.revoke("proj-1", "d-nope") is False


def test_a_device_is_offline_until_it_speaks(registry):
    device, _ = registry.mint("proj-1", "rover", ["mod-temp"])
    assert registry.online(device) is False


def test_a_device_is_online_after_it_speaks(registry):
    device, _ = registry.mint("proj-1", "rover", ["mod-temp"])
    registry.touch(device.id)
    assert registry.online(device) is True


def test_a_device_goes_offline_when_the_window_passes(registry):
    device, _ = registry.mint("proj-1", "rover", ["mod-temp"])
    registry.touch(device.id, now=time.time() - registry.online_window - 1)
    # A device that has stopped answering must not hold itself open by claiming
    # to be alive; the window is the only thing that decides.
    assert registry.online(device) is False


def test_a_revoked_device_is_never_online(registry):
    device, _ = registry.mint("proj-1", "rover", ["mod-temp"])
    registry.touch(device.id)
    registry.revoke("proj-1", device.id)
    assert registry.online(device) is False


def test_a_connected_device_is_only_returned_for_its_own_modules(registry):
    device, _ = registry.mint("proj-1", "rover", ["mod-temp"])
    registry.touch(device.id)
    assert registry.connected_for_module("mod-temp").id == device.id
    assert registry.connected_for_module("mod-lidar") is None


def test_a_registered_but_silent_device_is_not_connected(registry):
    # "A device exists" and "a device is here" are different questions, and only
    # the second may be reported as available.
    registry.mint("proj-1", "rover", ["mod-temp"])
    assert registry.connected_for_module("mod-temp") is None
