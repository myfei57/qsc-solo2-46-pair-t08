"""Equipment archives for the cranes connected to the service.

A tower crane position on site outlives the physical machines that occupy it:
over a project the same logical crane slot ("台账里只有一个名字") is served by
several machines, each with its own model and rated parameters, and one
machine can itself be re-rated while it stands on site.  This module keeps
that history as an append-only ledger of *archive revisions*.

Every revision is an immutable snapshot of the parameters that were in force
between its ``effective_from`` and the next revision's ``effective_from``.
Nothing is ever overwritten, so a historical question ("what archive was this
crane running at time T?") is answered by scanning the ledger rather than
trusting whatever happens to be current.  Three operations exist:

* ``register`` - the very first revision of a crane slot, at equipment
  access/onboarding time;
* ``amend``    - a parameter change on the same physical machine;
* ``replace``  - a different physical machine taking over the slot.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .errors import (
    ArchiveError,
    ArchiveNotFound,
    DuplicateArchive,
    InvalidEffectiveTime,
)
from .errors import StoreNotFound
from .store import Store

ARCHIVE_KEY = "archive/ledger"

ACTION_REGISTER = "register"
ACTION_AMEND = "amend"
ACTION_REPLACE = "replace"
ARCHIVE_ACTIONS = (ACTION_REGISTER, ACTION_AMEND, ACTION_REPLACE)

# Rated parameters captured for every physical machine.  They are stored as a
# loose mapping so site-specific extras (jib extensions, counter-jib setup...)
# can be carried alongside the standard fields without a schema migration.
STANDARD_PARAMS = (
    "rated_capacity_t",
    "max_radius_m",
    "free_height_m",
    "max_slew_speed_dps",
    "max_trolley_speed_mps",
    "max_hoist_speed_mps",
    "max_wind_speed_mps",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 stamp, accepting a trailing ``Z`` as UTC."""

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InvalidEffectiveTime(f"{value!r} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class ArchiveRevision:
    """One immutable version of one crane's equipment archive."""

    revision_id: str
    crane_id: str
    seq: int
    change: str
    model: str
    serial_number: str
    manufacturer: str
    parameters: dict[str, float] = field(default_factory=dict)
    registered_at: str = ""
    effective_from: str = ""
    reason: str = ""
    operator: str = ""
    supersedes: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "revision_id": self.revision_id,
            "crane_id": self.crane_id,
            "seq": self.seq,
            "change": self.change,
            "model": self.model,
            "serial_number": self.serial_number,
            "manufacturer": self.manufacturer,
            "parameters": dict(self.parameters),
            "registered_at": self.registered_at,
            "effective_from": self.effective_from,
            "reason": self.reason,
            "operator": self.operator,
            "supersedes": self.supersedes,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ArchiveRevision":
        raw_parameters = payload.get("parameters") or {}
        parameters = {str(key): float(value) for key, value in dict(raw_parameters).items()}
        return cls(
            revision_id=str(payload["revision_id"]),
            crane_id=str(payload["crane_id"]),
            seq=int(payload["seq"]),
            change=str(payload["change"]),
            model=str(payload.get("model") or ""),
            serial_number=str(payload.get("serial_number") or ""),
            manufacturer=str(payload.get("manufacturer") or ""),
            parameters=parameters,
            registered_at=str(payload.get("registered_at") or ""),
            effective_from=str(payload.get("effective_from") or ""),
            reason=str(payload.get("reason") or ""),
            operator=str(payload.get("operator") or ""),
            supersedes=str(payload.get("supersedes") or ""),
        )


class ArchiveLedger:
    """Append-only, thread-safe collection of archive revisions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._revisions: list[ArchiveRevision] = []

    # ------------------------------------------------------------ inspection
    def all_revisions(self) -> list[ArchiveRevision]:
        with self._lock:
            return list(self._revisions)

    def revisions_for(self, crane_id: str) -> list[ArchiveRevision]:
        with self._lock:
            return [revision for revision in self._revisions if revision.crane_id == crane_id]

    def crane_ids(self) -> list[str]:
        with self._lock:
            return sorted({revision.crane_id for revision in self._revisions})

    def is_registered(self, crane_id: str) -> bool:
        with self._lock:
            return any(revision.crane_id == crane_id for revision in self._revisions)

    def current(self, crane_id: str) -> ArchiveRevision:
        """Return the newest registered (by sequence) revision of a crane."""

        with self._lock:
            revisions = [revision for revision in self._revisions if revision.crane_id == crane_id]
        if not revisions:
            raise ArchiveNotFound(crane_id)
        return max(revisions, key=lambda revision: revision.seq)

    def effective_at(self, crane_id: str, moment: datetime) -> ArchiveRevision:
        """Return the revision that was in force at ``moment``.

        Revisions take effect at their ``effective_from`` instant.  A moment
        before the crane was first registered has no archive.
        """

        moment = moment.astimezone(timezone.utc)
        with self._lock:
            revisions = [revision for revision in self._revisions if revision.crane_id == crane_id]
        if not revisions:
            raise ArchiveNotFound(crane_id)
        ordered = sorted(revisions, key=lambda revision: parse_iso(revision.effective_from))
        candidate: ArchiveRevision | None = None
        for revision in ordered:
            if parse_iso(revision.effective_from) <= moment:
                candidate = revision
            else:
                break
        if candidate is None:
            raise ArchiveNotFound(f"{crane_id} had no archive at {moment.isoformat()}")
        return candidate

    def revision(self, revision_id: str) -> ArchiveRevision:
        with self._lock:
            for revision in self._revisions:
                if revision.revision_id == revision_id:
                    return revision
        raise ArchiveNotFound(revision_id)

    # -------------------------------------------------------------- mutation
    def append(self, revision: ArchiveRevision) -> None:
        with self._lock:
            self._revisions.append(revision)

    def replace_all(self, revisions: Sequence[ArchiveRevision]) -> None:
        with self._lock:
            self._revisions = list(revisions)


def _validate_change(change: str) -> str:
    if change not in ARCHIVE_ACTIONS:
        raise ArchiveError(f"change must be one of {', '.join(ARCHIVE_ACTIONS)}")
    return change


def _extract_parameters(payload: Mapping[str, object]) -> dict[str, float]:
    raw = payload.get("parameters")
    if raw is None:
        raw = {key: payload[key] for key in STANDARD_PARAMS if key in payload}
    if not isinstance(raw, Mapping):
        raise ArchiveError("parameters must be an object")
    parameters: dict[str, float] = {}
    for key, value in dict(raw).items():
        name = str(key)
        if not name:
            raise ArchiveError("parameter name must not be empty")
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise ArchiveError(f"parameter {name} must be numeric") from exc
        parameters[name] = number
    return parameters


def _build_revision(
    ledger: ArchiveLedger,
    crane_id: str,
    change: str,
    payload: Mapping[str, object],
) -> ArchiveRevision:
    change = _validate_change(change)
    if not crane_id:
        raise ArchiveError("crane_id required")
    model = str(payload.get("model") or "").strip()
    serial_number = str(payload.get("serial_number") or "").strip()
    manufacturer = str(payload.get("manufacturer") or "").strip()
    parameters = _extract_parameters(payload)

    registered = ledger.is_registered(crane_id)
    if change == ACTION_REGISTER and registered:
        raise DuplicateArchive(crane_id)
    if change in (ACTION_AMEND, ACTION_REPLACE) and not registered:
        raise ArchiveNotFound(crane_id)
    if change in (ACTION_REGISTER, ACTION_REPLACE):
        if not model:
            raise ArchiveError("model required")
        if not serial_number:
            raise ArchiveError("serial_number required when registering or replacing equipment")
        if not parameters:
            raise ArchiveError("at least one rated parameter is required")

    if change == ACTION_AMEND:
        previous = ledger.current(crane_id)
        # An amendment keeps the same physical machine: missing identity
        # fields and parameters carry over from the revision being amended.
        model = model or previous.model
        serial_number = serial_number or previous.serial_number
        manufacturer = manufacturer or previous.manufacturer
        merged = dict(previous.parameters)
        merged.update(parameters)
        if (
            model == previous.model
            and serial_number == previous.serial_number
            and manufacturer == previous.manufacturer
            and merged == previous.parameters
        ):
            raise ArchiveError("amendment changes nothing")
        parameters = merged
        supersedes = previous.revision_id
        seq = previous.seq + 1
    elif change == ACTION_REPLACE:
        previous = ledger.current(crane_id)
        supersedes = previous.revision_id
        seq = previous.seq + 1
    else:
        supersedes = ""
        seq = 1

    raw_effective = str(payload.get("effective_from") or "").strip()
    effective_from = parse_iso(raw_effective).isoformat() if raw_effective else now_iso()
    if registered:
        last_effective = parse_iso(ledger.current(crane_id).effective_from)
        if parse_iso(effective_from) < last_effective:
            raise InvalidEffectiveTime("effective_from predates the current revision")

    return ArchiveRevision(
        revision_id=uuid.uuid4().hex,
        crane_id=crane_id,
        seq=seq,
        change=change,
        model=model,
        serial_number=serial_number,
        manufacturer=manufacturer,
        parameters=parameters,
        registered_at=now_iso(),
        effective_from=effective_from,
        reason=str(payload.get("reason") or "").strip(),
        operator=str(payload.get("operator") or "").strip(),
        supersedes=supersedes,
    )


def register(ledger: ArchiveLedger, crane_id: str, payload: Mapping[str, object]) -> ArchiveRevision:
    """Open the archive for a crane slot when its equipment is connected."""

    return _commit(ledger, _build_revision(ledger, crane_id, ACTION_REGISTER, payload))


def amend(ledger: ArchiveLedger, crane_id: str, payload: Mapping[str, object]) -> ArchiveRevision:
    """Record a parameter change on the machine currently on the slot."""

    return _commit(ledger, _build_revision(ledger, crane_id, ACTION_AMEND, payload))


def replace(ledger: ArchiveLedger, crane_id: str, payload: Mapping[str, object]) -> ArchiveRevision:
    """Record a different physical machine taking over the slot."""

    return _commit(ledger, _build_revision(ledger, crane_id, ACTION_REPLACE, payload))


def _commit(ledger: ArchiveLedger, revision: ArchiveRevision) -> ArchiveRevision:
    ledger.append(revision)
    return revision


# --------------------------------------------------------------- persistence
def persist(ledger: ArchiveLedger, store: Store) -> None:
    """Write the whole ledger; revisions themselves never change."""

    payload = json.dumps(
        [revision.to_dict() for revision in ledger.all_revisions()],
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    store.save(ARCHIVE_KEY, payload)


def restore(ledger: ArchiveLedger, store: Store) -> None:
    """Reload every revision ever recorded on boot."""

    try:
        payload = store.load(ARCHIVE_KEY)
    except StoreNotFound:
        return
    raw = json.loads(payload.decode("utf-8"))
    revisions = [ArchiveRevision.from_dict(item) for item in raw]
    ledger.replace_all(revisions)


# --------------------------------------------------------------------- views
def revision_view(revision: ArchiveRevision) -> dict[str, object]:
    return revision.to_dict()


def crane_archive_view(ledger: ArchiveLedger, crane_id: str) -> dict[str, object]:
    """Current archive plus the ordered history used to backtrace it."""

    revisions = sorted(ledger.revisions_for(crane_id), key=lambda item: item.seq)
    if not revisions:
        raise ArchiveNotFound(crane_id)
    current = revisions[-1]
    return {
        "crane_id": crane_id,
        "current": current.to_dict(),
        "revision_count": len(revisions),
        "history": [revision.to_dict() for revision in revisions],
    }


def ledger_view(ledger: ArchiveLedger) -> dict[str, object]:
    """One entry per crane slot, summarising the archive currently in force."""

    cranes: list[dict[str, object]] = []
    for crane_id in ledger.crane_ids():
        current = ledger.current(crane_id)
        cranes.append(
            {
                "crane_id": crane_id,
                "revision_id": current.revision_id,
                "seq": current.seq,
                "model": current.model,
                "serial_number": current.serial_number,
                "manufacturer": current.manufacturer,
                "effective_from": current.effective_from,
                "revision_count": len(ledger.revisions_for(crane_id)),
            }
        )
    return {"cranes": cranes, "total": len(cranes)}
