"""Equipment registry: machine dossiers, parameter versions and slot history.

The site ledger names *slots* (the fixed tower positions such as ``crane-1``);
the physical tower cranes that occupy those slots come and go and differ in
model and parameters. This module keeps the two identities apart:

* an :class:`EquipmentRecord` is one physical machine, identified by a unique
  serial number, carrying an append-only chain of :class:`ArchiveVersion`
  parameter sets;
* an :class:`Assignment` records that one machine, at one parameter version,
  ran on one slot during a half-open ``[start_at, end_at)`` time interval.

Swapping a machine closes the open interval and opens a new one; changing
parameters appends an archive version and rotates the open interval onto it.
Nothing is ever overwritten, so the exact dossier in force at any past moment
can be reconstructed with :func:`effective_assignment`.

All mutating helpers append one :class:`RegistryEvent` to the journal; the
journal together with the slots/records is persisted as a single snapshot.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .errors import (
    EquipmentExists,
    EquipmentMountedElsewhere,
    EquipmentNotFound,
    RegistryValidationError,
    SlotEmpty,
    SlotOccupied,
    StoreNotFound,
)
from .store import Store

REGISTRY_KEY = "registry/state"
SNAPSHOT_VERSION = 1

ACTION_REGISTER = "register"
ACTION_MOUNT = "mount"
ACTION_SWAP = "swap"
ACTION_REVISE = "revise"
ACTION_UNMOUNT = "unmount"
MUTATING_ACTIONS = (ACTION_REGISTER, ACTION_MOUNT, ACTION_SWAP, ACTION_REVISE, ACTION_UNMOUNT)

PARAM_FIELDS = (
    "model",
    "manufacturer",
    "rated_capacity_t",
    "jib_length_m",
    "tip_capacity_t",
    "mast_height_m",
    "max_slew_speed_rpm",
    "rated_wind_speed_ms",
)


def now_iso() -> str:
    """Return the current time as a UTC ISO-8601 string."""

    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, rejecting naive and malformed values."""

    if not isinstance(value, str) or not value:
        raise RegistryValidationError("timestamp required")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RegistryValidationError(f"invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise RegistryValidationError(f"timestamp must carry a timezone: {value}")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class CraneParams:
    """The technical parameter set captured in one archive version."""

    model: str
    manufacturer: str = ""
    rated_capacity_t: float = 0.0
    jib_length_m: float = 0.0
    tip_capacity_t: float = 0.0
    mast_height_m: float = 0.0
    max_slew_speed_rpm: float = 0.0
    rated_wind_speed_ms: float = 0.0
    extras: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {name: getattr(self, name) for name in PARAM_FIELDS}
        payload["extras"] = dict(self.extras)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "CraneParams":
        data = dict(payload)
        model = str(data.get("model") or "").strip()
        if not model:
            raise RegistryValidationError("model is required")
        manufacturer = str(data.get("manufacturer") or "").strip()

        numbers: dict[str, float] = {}
        for name in PARAM_FIELDS[2:]:
            try:
                numbers[name] = float(data.get(name, 0.0))
            except (TypeError, ValueError) as exc:
                raise RegistryValidationError(f"{name} must be a number") from exc
        non_negative = ("max_slew_speed_rpm",)
        for name, value in numbers.items():
            if value < 0 or (name not in non_negative and value <= 0):
                raise RegistryValidationError(f"{name} must be positive")
        if numbers["tip_capacity_t"] > numbers["rated_capacity_t"]:
            raise RegistryValidationError("tip_capacity_t cannot exceed rated_capacity_t")

        extras_raw = data.get("extras") or {}
        if not isinstance(extras_raw, Mapping):
            raise RegistryValidationError("extras must be an object")
        reserved = set(PARAM_FIELDS)
        extras = {
            str(key): str(value)
            for key, value in extras_raw.items()
            if str(key) not in reserved and value is not None
        }
        return cls(model=model, manufacturer=manufacturer, extras=extras, **numbers)


@dataclass(frozen=True)
class ArchiveVersion:
    """One immutable parameter revision of an equipment dossier."""

    version_no: int
    params: CraneParams
    reason: str
    changed_fields: tuple[str, ...]
    effective_at: str
    created_at: str
    event_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "version_no": self.version_no,
            "params": self.params.to_dict(),
            "reason": self.reason,
            "changed_fields": list(self.changed_fields),
            "effective_at": self.effective_at,
            "created_at": self.created_at,
            "event_id": self.event_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ArchiveVersion":
        return cls(
            version_no=int(payload["version_no"]),
            params=CraneParams.from_dict(payload["params"]),
            reason=str(payload.get("reason", "")),
            changed_fields=tuple(payload.get("changed_fields", ())),
            effective_at=str(payload["effective_at"]),
            created_at=str(payload["created_at"]),
            event_id=str(payload["event_id"]),
        )


@dataclass(frozen=True)
class EquipmentRecord:
    """A physical tower crane, identified by its unique serial number."""

    equipment_id: str
    serial: str
    versions: list[ArchiveVersion]
    registered_at: str

    def current(self) -> ArchiveVersion:
        return self.versions[-1]

    def version(self, version_no: int) -> ArchiveVersion | None:
        for candidate in self.versions:
            if candidate.version_no == version_no:
                return candidate
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "equipment_id": self.equipment_id,
            "serial": self.serial,
            "registered_at": self.registered_at,
            "versions": [item.to_dict() for item in self.versions],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EquipmentRecord":
        return cls(
            equipment_id=str(payload["equipment_id"]),
            serial=str(payload["serial"]),
            registered_at=str(payload["registered_at"]),
            versions=[ArchiveVersion.from_dict(item) for item in payload["versions"]],
        )


@dataclass(frozen=True)
class Assignment:
    """A half-open ``[start_at, end_at)`` mount of one dossier version on a slot."""

    assignment_id: str
    slot_id: str
    equipment_id: str
    version_no: int
    start_at: str
    end_at: str | None
    start_event_id: str
    end_event_id: str | None

    def covers(self, moment: datetime) -> bool:
        start = parse_iso(self.start_at)
        if moment < start:
            return False
        return self.end_at is None or moment < parse_iso(self.end_at)

    def to_dict(self) -> dict[str, object]:
        return {
            "assignment_id": self.assignment_id,
            "slot_id": self.slot_id,
            "equipment_id": self.equipment_id,
            "version_no": self.version_no,
            "start_at": self.start_at,
            "end_at": self.end_at,
            "start_event_id": self.start_event_id,
            "end_event_id": self.end_event_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Assignment":
        end_at = payload.get("end_at")
        end_event = payload.get("end_event_id")
        return cls(
            assignment_id=str(payload["assignment_id"]),
            slot_id=str(payload["slot_id"]),
            equipment_id=str(payload["equipment_id"]),
            version_no=int(payload["version_no"]),
            start_at=str(payload["start_at"]),
            end_at=str(end_at) if end_at else None,
            start_event_id=str(payload["start_event_id"]),
            end_event_id=str(end_event) if end_event else None,
        )


@dataclass(frozen=True)
class RegistryEvent:
    """One journal entry proving who changed what dossier and when."""

    event_id: str
    seq: int
    action: str
    at: str
    recorded_at: str
    slot_id: str
    equipment_id: str
    version_no: int
    operator: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "seq": self.seq,
            "action": self.action,
            "at": self.at,
            "recorded_at": self.recorded_at,
            "slot_id": self.slot_id,
            "equipment_id": self.equipment_id,
            "version_no": self.version_no,
            "operator": self.operator,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RegistryEvent":
        return cls(
            event_id=str(payload["event_id"]),
            seq=int(payload["seq"]),
            action=str(payload["action"]),
            at=str(payload["at"]),
            recorded_at=str(payload["recorded_at"]),
            slot_id=str(payload.get("slot_id", "")),
            equipment_id=str(payload.get("equipment_id", "")),
            version_no=int(payload.get("version_no", 0)),
            operator=str(payload.get("operator", "")),
            reason=str(payload.get("reason", "")),
        )


class EquipmentRegistry:
    """Append-only dossiers, versions and mount intervals for the whole site."""

    def __init__(self) -> None:
        # RLock: module-level mutators hold the lock while calling the public
        # read helpers, which acquire it again on the same thread.
        self._lock = threading.RLock()
        self.slots: dict[str, str] = {}
        self.records: dict[str, EquipmentRecord] = {}
        self.assignments: dict[str, list[Assignment]] = {}
        self.events: list[RegistryEvent] = []

    # ------------------------------------------------------------- look-ups

    def slot_name(self, slot_id: str) -> str:
        with self._lock:
            return self.slots.get(slot_id, slot_id)

    def record(self, equipment_id: str) -> EquipmentRecord | None:
        with self._lock:
            return self.records.get(equipment_id)

    def slot_ids(self) -> list[str]:
        with self._lock:
            return sorted(self.slots)

    def equipment_ids(self) -> list[str]:
        with self._lock:
            return sorted(self.records)

    def timeline(self, slot_id: str) -> list[Assignment]:
        """Return every mount interval for a slot, oldest first."""

        with self._lock:
            return list(self.assignments.get(slot_id, ()))

    def open_assignment(self, slot_id: str) -> Assignment | None:
        with self._lock:
            for item in reversed(self.assignments.get(slot_id, ())):  # noqa: SLF001
                if item.end_at is None:
                    return item
        return None

    def slot_of(self, equipment_id: str) -> str | None:
        """Return the slot the machine currently occupies, if it is mounted."""

        with self._lock:
            for slot_id, items in self.assignments.items():
                for item in reversed(items):
                    if item.end_at is not None:
                        break
                    if item.equipment_id == equipment_id:
                        return slot_id
        return None

    def events_snapshot(self) -> list[RegistryEvent]:
        with self._lock:
            return list(self.events)


def _ensure_slot(registry: EquipmentRegistry, slot_id: str, slot_name: str) -> None:
    registry.slots.setdefault(slot_id, slot_name or slot_id)
    registry.assignments.setdefault(slot_id, [])


def _last_slot_event(registry: EquipmentRegistry, slot_id: str) -> RegistryEvent | None:
    for event in reversed(registry.events):
        if event.slot_id == slot_id and event.action in MUTATING_ACTIONS:
            return event
    return None


def _assert_after_last_slot_event(
    registry: EquipmentRegistry, slot_id: str, at: datetime
) -> None:
    last = _last_slot_event(registry, slot_id)
    if last is not None and at <= parse_iso(last.at):
        raise RegistryValidationError(
            f"effective time must be later than the last change for {slot_id} at {last.at}"
        )


def _append_event(
    registry: EquipmentRegistry,
    action: str,
    at: str,
    *,
    slot_id: str = "",
    equipment_id: str = "",
    version_no: int = 0,
    operator: str = "",
    reason: str = "",
) -> RegistryEvent:
    event = RegistryEvent(
        event_id=uuid.uuid4().hex,
        seq=len(registry.events) + 1,
        action=action,
        at=at,
        recorded_at=now_iso(),
        slot_id=slot_id,
        equipment_id=equipment_id,
        version_no=version_no,
        operator=operator,
        reason=reason,
    )
    registry.events.append(event)
    return event


def register_equipment(
    registry: EquipmentRegistry,
    params: Mapping[str, Any],
    *,
    equipment_id: str = "",
    serial: str = "",
    slot_id: str = "",
    slot_name: str = "",
    at: str = "",
    operator: str = "",
    reason: str = "",
) -> tuple[EquipmentRecord, RegistryEvent]:
    """Register a new machine dossier, optionally mounting it straight away.

    With ``slot_id`` supplied this is the "device connects on site" flow: the
    dossier is created at version 1 and an open assignment opens on the slot
    in one journaled transaction.
    """

    parsed = CraneParams.from_dict(params)
    equipment_id = equipment_id.strip() or f"eq-{uuid.uuid4().hex[:12]}"
    serial = serial.strip()
    if not serial:
        raise RegistryValidationError("serial is required")
    moment = parse_iso(at) if at else datetime.now(timezone.utc)
    with registry._lock:  # noqa: SLF001 - module-level functions share registry state
        if equipment_id in registry.records:
            raise EquipmentExists(f"equipment already registered: {equipment_id}")
        for record in registry.records.values():
            if record.serial == serial:
                raise EquipmentExists(f"serial number already on file: {serial}")
        if slot_id:
            _ensure_slot(registry, slot_id, slot_name)
            if registry.open_assignment(slot_id) is not None:
                raise SlotOccupied(f"{slot_id} already runs a machine; swap it instead")
            _assert_after_last_slot_event(registry, slot_id, moment)
        event = _append_event(
            registry,
            ACTION_REGISTER,
            moment.isoformat(),
            slot_id=slot_id,
            equipment_id=equipment_id,
            version_no=1,
            operator=operator,
            reason=reason or "设备建档",
        )
        version = ArchiveVersion(
            version_no=1,
            params=parsed,
            reason=event.reason,
            changed_fields=(),
            effective_at=moment.isoformat(),
            created_at=now_iso(),
            event_id=event.event_id,
        )
        registry.records[equipment_id] = EquipmentRecord(
            equipment_id=equipment_id,
            serial=serial,
            versions=[version],
            registered_at=moment.isoformat(),
        )
        if slot_id:
            registry.assignments[slot_id].append(
                Assignment(
                    assignment_id=uuid.uuid4().hex,
                    slot_id=slot_id,
                    equipment_id=equipment_id,
                    version_no=1,
                    start_at=moment.isoformat(),
                    end_at=None,
                    start_event_id=event.event_id,
                    end_event_id=None,
                )
            )
    return registry.records[equipment_id], event


def _close_assignment(
    registry: EquipmentRegistry,
    assignment: Assignment,
    at: str,
    event_id: str,
) -> None:
    items = registry.assignments[assignment.slot_id]
    index = items.index(assignment)
    items[index] = Assignment(
        assignment_id=assignment.assignment_id,
        slot_id=assignment.slot_id,
        equipment_id=assignment.equipment_id,
        version_no=assignment.version_no,
        start_at=assignment.start_at,
        end_at=at,
        start_event_id=assignment.start_event_id,
        end_event_id=event_id,
    )


def mount_equipment(
    registry: EquipmentRegistry,
    slot_id: str,
    equipment_id: str,
    *,
    slot_name: str = "",
    version_no: int = 0,
    at: str = "",
    operator: str = "",
    reason: str = "",
) -> tuple[Assignment, RegistryEvent]:
    """Mount an existing (and free) machine onto an empty slot."""

    slot_id = slot_id.strip()
    equipment_id = equipment_id.strip()
    if not slot_id or not equipment_id:
        raise RegistryValidationError("slot_id and equipment_id are required")
    moment = parse_iso(at) if at else datetime.now(timezone.utc)
    with registry._lock:  # noqa: SLF001
        record = registry.records.get(equipment_id)
        if record is None:
            raise EquipmentNotFound(f"equipment not registered: {equipment_id}")
        _ensure_slot(registry, slot_id, slot_name)
        open_on_slot = registry.open_assignment(slot_id)
        if open_on_slot is not None:
            raise SlotOccupied(f"{slot_id} already runs {open_on_slot.equipment_id}; swap it instead")
        other_slot = registry.slot_of(equipment_id)
        if other_slot is not None:
            raise EquipmentMountedElsewhere(f"{equipment_id} is already mounted on {other_slot}")
        _assert_after_last_slot_event(registry, slot_id, moment)
        target = record.current() if version_no <= 0 else record.version(version_no)
        if target is None:
            raise RegistryValidationError(f"version {version_no} not found on {equipment_id}")
        if parse_iso(target.effective_at) > moment:
            raise RegistryValidationError(
                f"version {target.version_no} only takes effect at {target.effective_at}"
            )
        event = _append_event(
            registry,
            ACTION_MOUNT,
            moment.isoformat(),
            slot_id=slot_id,
            equipment_id=equipment_id,
            version_no=target.version_no,
            operator=operator,
            reason=reason or f"{equipment_id} 上机位 {slot_id}",
        )
        assignment = Assignment(
            assignment_id=uuid.uuid4().hex,
            slot_id=slot_id,
            equipment_id=equipment_id,
            version_no=target.version_no,
            start_at=moment.isoformat(),
            end_at=None,
            start_event_id=event.event_id,
            end_event_id=None,
        )
        registry.assignments[slot_id].append(assignment)
    return assignment, event


def swap_equipment(
    registry: EquipmentRegistry,
    slot_id: str,
    equipment_id: str,
    *,
    slot_name: str = "",
    version_no: int = 0,
    at: str = "",
    operator: str = "",
    reason: str = "",
) -> tuple[list[RegistryEvent], Assignment]:
    """Replace the machine on a slot, closing the old interval first.

    Returns the closing event (old machine unmounted), the opening event and
    the new open assignment. Mounting the same machine is rejected; use
    :func:`revise_params` for parameter changes.
    """

    slot_id = slot_id.strip()
    equipment_id = equipment_id.strip()
    moment = parse_iso(at) if at else datetime.now(timezone.utc)
    with registry._lock:  # noqa: SLF001
        current_assignment = registry.open_assignment(slot_id)
        if current_assignment is None:
            raise SlotEmpty(f"{slot_id} has no machine to swap; mount one first")
        if current_assignment.equipment_id == equipment_id:
            raise RegistryValidationError(
                f"{equipment_id} already runs {slot_id}; revise its parameters instead"
            )
        record = registry.records.get(equipment_id)
        if record is None:
            raise EquipmentNotFound(f"equipment not registered: {equipment_id}")
        if registry.slot_of(equipment_id) is not None:
            raise EquipmentMountedElsewhere(f"{equipment_id} is mounted on another slot")
        _ensure_slot(registry, slot_id, slot_name)
        _assert_after_last_slot_event(registry, slot_id, moment)
        target = record.current() if version_no <= 0 else record.version(version_no)
        if target is None:
            raise RegistryValidationError(f"version {version_no} not found on {equipment_id}")
        if parse_iso(target.effective_at) > moment:
            raise RegistryValidationError(
                f"version {target.version_no} only takes effect at {target.effective_at}"
            )
        close_event = _append_event(
            registry,
            ACTION_UNMOUNT,
            moment.isoformat(),
            slot_id=slot_id,
            equipment_id=current_assignment.equipment_id,
            version_no=current_assignment.version_no,
            operator=operator,
            reason=reason or f"换机：{current_assignment.equipment_id} 下机位",
        )
        _close_assignment(registry, current_assignment, moment.isoformat(), close_event.event_id)
        open_event = _append_event(
            registry,
            ACTION_SWAP,
            moment.isoformat(),
            slot_id=slot_id,
            equipment_id=equipment_id,
            version_no=target.version_no,
            operator=operator,
            reason=reason or f"换机：{equipment_id} 上机位 {slot_id}",
        )
        assignment = Assignment(
            assignment_id=uuid.uuid4().hex,
            slot_id=slot_id,
            equipment_id=equipment_id,
            version_no=target.version_no,
            start_at=moment.isoformat(),
            end_at=None,
            start_event_id=open_event.event_id,
            end_event_id=None,
        )
        registry.assignments[slot_id].append(assignment)
        events = [close_event, open_event]
    return events, assignment


def revise_params(
    registry: EquipmentRegistry,
    equipment_id: str,
    params: Mapping[str, Any],
    *,
    at: str = "",
    operator: str = "",
    reason: str = "",
) -> tuple[ArchiveVersion, RegistryEvent, Assignment | None]:
    """Append a new parameter version to a dossier.

    If the machine currently occupies a slot, its open interval is rotated to
    the new version (old interval closed, new interval opened) so the slot
    timeline shows the exact parameters in force at every moment.
    """

    equipment_id = equipment_id.strip()
    parsed = CraneParams.from_dict(params)
    moment = parse_iso(at) if at else datetime.now(timezone.utc)
    with registry._lock:  # noqa: SLF001
        record = registry.records.get(equipment_id)
        if record is None:
            raise EquipmentNotFound(f"equipment not registered: {equipment_id}")
        latest = record.current()
        if parse_iso(latest.effective_at) >= moment:
            raise RegistryValidationError(
                f"effective time must be later than version {latest.version_no} at {latest.effective_at}"
            )
        previous = latest.params.to_dict()
        upcoming = parsed.to_dict()
        changed = tuple(
            name for name in (*PARAM_FIELDS, "extras") if previous.get(name) != upcoming.get(name)
        )
        if not changed:
            raise RegistryValidationError("nothing changed; a revision must alter at least one parameter")
        slot_id = registry.slot_of(equipment_id)
        if slot_id is not None:
            _assert_after_last_slot_event(registry, slot_id, moment)
        event = _append_event(
            registry,
            ACTION_REVISE,
            moment.isoformat(),
            slot_id=slot_id or "",
            equipment_id=equipment_id,
            version_no=latest.version_no + 1,
            operator=operator,
            reason=reason or "运行参数变更",
        )
        version = ArchiveVersion(
            version_no=latest.version_no + 1,
            params=parsed,
            reason=reason or "运行参数变更",
            changed_fields=changed,
            effective_at=moment.isoformat(),
            created_at=now_iso(),
            event_id=event.event_id,
        )
        record.versions.append(version)
        new_assignment: Assignment | None = None
        if slot_id is not None:
            open_assignment = registry.open_assignment(slot_id)
            assert open_assignment is not None
            _close_assignment(registry, open_assignment, moment.isoformat(), event.event_id)
            new_assignment = Assignment(
                assignment_id=uuid.uuid4().hex,
                slot_id=slot_id,
                equipment_id=equipment_id,
                version_no=version.version_no,
                start_at=moment.isoformat(),
                end_at=None,
                start_event_id=event.event_id,
                end_event_id=None,
            )
            registry.assignments[slot_id].append(new_assignment)
    return version, event, new_assignment


def unmount_equipment(
    registry: EquipmentRegistry,
    slot_id: str,
    *,
    at: str = "",
    operator: str = "",
    reason: str = "",
) -> tuple[Assignment, RegistryEvent]:
    """Take the running machine off a slot, closing its open interval."""

    slot_id = slot_id.strip()
    moment = parse_iso(at) if at else datetime.now(timezone.utc)
    with registry._lock:  # noqa: SLF001
        assignment = registry.open_assignment(slot_id)
        if assignment is None:
            raise SlotEmpty(f"{slot_id} has no mounted machine")
        _assert_after_last_slot_event(registry, slot_id, moment)
        event = _append_event(
            registry,
            ACTION_UNMOUNT,
            moment.isoformat(),
            slot_id=slot_id,
            equipment_id=assignment.equipment_id,
            version_no=assignment.version_no,
            operator=operator,
            reason=reason or f"{assignment.equipment_id} 下机位 {slot_id}",
        )
        _close_assignment(registry, assignment, moment.isoformat(), event.event_id)
    return assignment, event


def effective_assignment(
    registry: EquipmentRegistry, slot_id: str, at: str | None = None
) -> tuple[Assignment, ArchiveVersion] | None:
    """Return the dossier version that governed a slot at ``at`` (default now).

    The assignment pins both the machine and the version number, so a later
    parameter revision cannot change what a historical query reports.
    """

    moment = parse_iso(at) if at else datetime.now(timezone.utc)
    with registry._lock:  # noqa: SLF001
        for assignment in registry.assignments.get(slot_id, ()):
            if not assignment.covers(moment):
                continue
            record = registry.records.get(assignment.equipment_id)
            if record is None:
                continue
            version = record.version(assignment.version_no)
            if version is not None:
                return assignment, version
    return None


def query_events(
    registry: EquipmentRegistry,
    *,
    slot_id: str = "",
    equipment_id: str = "",
    action: str = "",
    limit: int = 50,
) -> list[RegistryEvent]:
    """Return journal entries newest first, optionally filtered."""

    events = registry.events_snapshot()
    collected: list[RegistryEvent] = []
    for event in reversed(events):
        if slot_id and event.slot_id != slot_id:
            continue
        if equipment_id and event.equipment_id != equipment_id:
            continue
        if action and event.action != action:
            continue
        collected.append(event)
        if len(collected) >= limit > 0:
            break
    return collected


# ----------------------------------------------------------------- views


def equipment_view(registry: EquipmentRegistry, equipment_id: str) -> dict[str, object] | None:
    with registry._lock:  # noqa: SLF001
        record = registry.records.get(equipment_id)
        if record is None:
            return None
        payload = record.to_dict()
        payload["mounted_slot"] = registry.slot_of(equipment_id)
        return payload


def slot_view(registry: EquipmentRegistry, slot_id: str, at: str = "") -> dict[str, object]:
    """Render a slot's status, current (or historical) dossier and timeline."""

    moment = parse_iso(at) if at else datetime.now(timezone.utc)
    with registry._lock:  # noqa: SLF001
        name = registry.slots.get(slot_id, slot_id)
        known = slot_id in registry.slots
        timeline: list[dict[str, object]] = []
        for assignment in registry.assignments.get(slot_id, ()):
            record = registry.records.get(assignment.equipment_id)
            version = record.version(assignment.version_no) if record else None
            entry = dict(assignment.to_dict())
            entry["serial"] = record.serial if record else None
            entry["model"] = version.params.model if version else None
            timeline.append(entry)
        effective_payload: dict[str, object] | None = None
        for assignment in registry.assignments.get(slot_id, ()):
            if not assignment.covers(moment):
                continue
            record = registry.records.get(assignment.equipment_id)
            if record is None:
                continue
            version = record.version(assignment.version_no)
            if version is None:
                continue
            effective_payload = {
                "assignment": assignment.to_dict(),
                "equipment_id": assignment.equipment_id,
                "serial": record.serial,
                "version_no": version.version_no,
                "params": version.params.to_dict(),
            }
            break
        open_assignment = next(
            (item for item in reversed(registry.assignments.get(slot_id, ())) if item.end_at is None),
            None,
        )
    return {
        "slot_id": slot_id,
        "name": name,
        "known": known,
        "at": moment.isoformat(),
        "open": open_assignment.to_dict() if open_assignment else None,
        "effective": effective_payload,
        "timeline": timeline,
    }


def registry_view(registry: EquipmentRegistry) -> dict[str, object]:
    """Render the full register for the console."""

    with registry._lock:  # noqa: SLF001
        slots = []
        for slot_id in sorted(registry.slots):
            open_assignment = registry.open_assignment(slot_id)
            entry: dict[str, object] = {
                "slot_id": slot_id,
                "name": registry.slots[slot_id],
                "equipment_id": None,
                "model": None,
                "version_no": None,
                "since": None,
            }
            if open_assignment is not None:
                record = registry.records.get(open_assignment.equipment_id)
                version = record.version(open_assignment.version_no) if record else None
                entry.update(
                    equipment_id=open_assignment.equipment_id,
                    serial=record.serial if record else None,
                    model=version.params.model if version else None,
                    version_no=open_assignment.version_no,
                    since=open_assignment.start_at,
                )
            slots.append(entry)
        equipment = [
            {
                "equipment_id": record.equipment_id,
                "serial": record.serial,
                "model": record.current().params.model,
                "version_count": len(record.versions),
                "current_version": record.current().version_no,
                "mounted_slot": registry.slot_of(record.equipment_id),
                "registered_at": record.registered_at,
            }
            for record in (registry.records[key] for key in sorted(registry.records))
        ]
    return {
        "slots": slots,
        "equipment": equipment,
        "slot_count": len(slots),
        "equipment_count": len(equipment),
        "event_count": len(registry.events_snapshot()),
    }


# ------------------------------------------------------------- persistence


def persist(registry: EquipmentRegistry, store: Store) -> None:
    """Write the whole register (journal included) as one durable snapshot."""

    with registry._lock:  # noqa: SLF001
        snapshot = {
            "version": SNAPSHOT_VERSION,
            "slots": dict(registry.slots),
            "records": [record.to_dict() for record in registry.records.values()],
            "assignments": [
                assignment.to_dict()
                for items in registry.assignments.values()
                for assignment in items
            ],
            "events": [event.to_dict() for event in registry.events],
        }
    store.save(REGISTRY_KEY, json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def restore(registry: EquipmentRegistry, store: Store) -> None:
    """Reload the register on boot; a missing key simply leaves it empty."""

    try:
        payload = store.load(REGISTRY_KEY)
    except StoreNotFound:
        return
    snapshot = json.loads(payload.decode("utf-8"))
    with registry._lock:  # noqa: SLF001
        registry.slots = dict(snapshot.get("slots", {}))
        registry.records = {}
        for item in snapshot.get("records", []):
            record = EquipmentRecord.from_dict(item)
            registry.records[record.equipment_id] = record
        registry.assignments = {}
        for item in snapshot.get("assignments", []):
            assignment = Assignment.from_dict(item)
            registry.assignments.setdefault(assignment.slot_id, []).append(assignment)
        for items in registry.assignments.values():
            items.sort(key=lambda value: value.start_at)
        registry.events = [RegistryEvent.from_dict(item) for item in snapshot.get("events", [])]
        registry.events.sort(key=lambda value: value.seq)
