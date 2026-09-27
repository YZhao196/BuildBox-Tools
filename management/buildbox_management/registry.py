"""Who is allowed to speak, and what each one may touch.

A device is deliberately not a user. It has no session and no collaborator
level; its whole authority is a token, a project, and an explicit list of module
ids. That is the reason this is its own module rather than a flag on anything
else: a credential handed to a machine must not carry the powers of the person
who minted it.

The token is returned once, when it is minted, and only its digest is stored —
the same bargain a session token makes. Revocation is checked when the token is
resolved, so cutting a device off takes effect on its very next request rather
than whenever it next reconnects.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

#: How long a device is considered connected after it last spoke.
#:
#: The long poll is the heartbeat — a device with nothing to do still returns
#: and touches itself — so a window comfortably wider than the poll is what
#: separates "idle" from "gone". A device's own claim that it is alive is not
#: evidence, which is why this is derived from when it was last heard from.
DEFAULT_ONLINE_WINDOW = 30.0


@dataclass
class Module:
    """Something on a project a device can report on.

    `shapes` is the structural readings this module displays. Declaring it here
    is what lets the receiving end refuse a laser sweep on a temperature module
    instead of passing on a reading that no sensor measured. Empty is the common
    case and means the module carries numbers only.
    """

    id: str
    name: str
    shapes: frozenset = frozenset()


@dataclass
class Device:
    """A machine's identity, as stored. Never carries the token or its digest."""

    id: str
    project_id: str
    label: str
    module_ids: List[str]
    created_at: float
    last_seen_at: Optional[float] = None
    revoked_at: Optional[float] = None

    def may_report_on(self, module_id: str) -> bool:
        return module_id in self.module_ids


@dataclass
class DeviceSummary:
    """The face of a device. Safe to show a person, and carries no credential."""

    id: str
    project_id: str
    label: str
    module_ids: List[str]
    created_at: float
    last_seen_at: Optional[float]
    online: bool

    def as_dict(self) -> Dict[str, object]:
        return {
            "id": self.id,
            "projectId": self.project_id,
            "label": self.label,
            "moduleIds": list(self.module_ids),
            "createdAt": int(self.created_at * 1000),
            "lastSeenAt": None if self.last_seen_at is None else int(self.last_seen_at * 1000),
            "online": self.online,
        }


def digest(token: str) -> str:
    """The stored form of a token.

    A plain SHA-256 rather than a slow password hash, and deliberately: this
    digest is looked up by value on every request, and the token is 256 bits of
    `secrets` rather than something a person chose, so there is no dictionary to
    run against it. A slow hash here would buy nothing and cost a lookup.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class _Stored:
    device: Device
    token_digest: str


class Registry:
    """Devices, the modules they may report on, and nothing else."""

    def __init__(self, *, online_window: float = DEFAULT_ONLINE_WINDOW) -> None:
        self.online_window = float(online_window)
        self._modules: Dict[str, Dict[str, Module]] = {}
        self._by_id: Dict[str, _Stored] = {}
        self._by_digest: Dict[str, str] = {}
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- #
    # Modules                                                          #
    # ---------------------------------------------------------------- #

    def add_module(
        self,
        project_id: str,
        module_id: str,
        *,
        name: Optional[str] = None,
        shapes: Optional[List[str]] = None,
    ) -> Module:
        """Declare a module a device can be scoped to.

        A device scoped to a module that does not exist is a mistake worth
        naming when the device is minted rather than a silence discovered later,
        so this is what `mint` checks against.
        """
        module = Module(
            id=module_id, name=name or module_id, shapes=frozenset(shapes or ())
        )
        with self._lock:
            self._modules.setdefault(project_id, {})[module_id] = module
        return module

    def module(self, project_id: str, module_id: str) -> Optional[Module]:
        return self._modules.get(project_id, {}).get(module_id)

    def modules(self, project_id: str) -> List[Module]:
        return list(self._modules.get(project_id, {}).values())

    # ---------------------------------------------------------------- #
    # Devices                                                          #
    # ---------------------------------------------------------------- #

    def mint(
        self, project_id: str, label: str, module_ids: List[str]
    ) -> Tuple[Device, str]:
        """Register a device and return it with its token, once.

        `module_ids` is required and may not be empty — a device scoped to
        "everything" by accident is the mistake this parameter exists to
        prevent, and an empty list is a device that can report on nothing, which
        is refused loudly rather than accepted quietly.
        """
        if not label:
            raise ValueError("A device needs a label.")
        if not module_ids:
            raise ValueError(
                "A device must be scoped to at least one module. There is no "
                "way to mint one that may report anywhere."
            )
        unknown = [mid for mid in module_ids if self.module(project_id, mid) is None]
        if unknown:
            raise ValueError(
                f"No module with that id is on {project_id}: {', '.join(unknown)}."
            )

        token = secrets.token_urlsafe(32)
        device = Device(
            id="d-" + secrets.token_hex(8),
            project_id=project_id,
            label=label,
            module_ids=list(module_ids),
            created_at=time.time(),
        )
        with self._lock:
            self._by_id[device.id] = _Stored(device, digest(token))
            self._by_digest[digest(token)] = device.id
        return device, token

    def resolve(self, token: Optional[str]) -> Optional[Device]:
        """The device a token belongs to, or None.

        Revocation is checked here rather than at whatever transport carried the
        request, so it takes effect on the next call rather than the next
        reconnect.
        """
        if not token:
            return None
        stored_id = self._by_digest.get(digest(token))
        if stored_id is None:
            return None
        stored = self._by_id.get(stored_id)
        if stored is None or stored.device.revoked_at is not None:
            return None
        return stored.device

    def get(self, device_id: str) -> Optional[Device]:
        stored = self._by_id.get(device_id)
        return stored.device if stored else None

    def devices(self, project_id: str) -> List[Device]:
        # Under the lock, because this iterates while `mint` may be inserting —
        # and a dictionary that changes size mid-iteration raises.
        with self._lock:
            return [
                stored.device
                for stored in list(self._by_id.values())
                if stored.device.project_id == project_id
            ]

    def revoke(self, project_id: str, device_id: str) -> bool:
        with self._lock:
            stored = self._by_id.get(device_id)
            if stored is None or stored.device.project_id != project_id:
                return False
            if stored.device.revoked_at is not None:
                return False
            stored.device.revoked_at = time.time()
            # The token is unindexed as well as marked, so a revoked credential
            # is not merely refused — it stops being a key that resolves at all.
            self._by_digest.pop(stored.token_digest, None)
            return True

    def touch(self, device_id: str, now: Optional[float] = None) -> None:
        """Record that a device just spoke. This is what `online` reads."""
        stored = self._by_id.get(device_id)
        if stored is not None:
            stored.device.last_seen_at = time.time() if now is None else now

    def online(self, device: Device, now: Optional[float] = None) -> bool:
        moment = time.time() if now is None else now
        return (
            device.revoked_at is None
            and device.last_seen_at is not None
            and moment - device.last_seen_at < self.online_window
        )

    def summary(self, device: Device, now: Optional[float] = None) -> DeviceSummary:
        return DeviceSummary(
            id=device.id,
            project_id=device.project_id,
            label=device.label,
            module_ids=list(device.module_ids),
            created_at=device.created_at,
            last_seen_at=device.last_seen_at,
            online=self.online(device, now),
        )

    def connected_for_module(
        self, module_id: str, now: Optional[float] = None
    ) -> Optional[Device]:
        """A device registered for this module that is answering right now.

        A device that is registered but silent is deliberately not returned:
        "a device exists" and "a device is here" are different, and only the
        second may be reported as available.
        """
        moment = time.time() if now is None else now
        with self._lock:
            for stored in list(self._by_id.values()):
                device = stored.device
                if device.may_report_on(module_id) and self.online(device, moment):
                    return device
        return None
