"""Tests for the equipment registry: dossiers, swaps, revisions and history."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from cranefleet.errors import (
    EquipmentExists,
    EquipmentMountedElsewhere,
    EquipmentNotFound,
    RegistryValidationError,
    SlotEmpty,
    SlotOccupied,
)
from cranefleet.registry import (
    EquipmentRegistry,
    CraneParams,
    effective_assignment,
    mount_equipment,
    parse_iso,
    persist,
    query_events,
    register_equipment,
    restore,
    revise_params,
    slot_view,
    swap_equipment,
    unmount_equipment,
)
from cranefleet.store import MemoryStore

PARAMS_A = {
    "model": "QTZ80",
    "manufacturer": "中天",
    "rated_capacity_t": 8.0,
    "jib_length_m": 55.0,
    "tip_capacity_t": 1.3,
    "mast_height_m": 45.0,
    "max_slew_speed_rpm": 0.8,
    "rated_wind_speed_ms": 20.0,
}
PARAMS_B = {
    "model": "QTZ125",
    "manufacturer": "中联",
    "rated_capacity_t": 10.0,
    "jib_length_m": 60.0,
    "tip_capacity_t": 2.0,
    "mast_height_m": 50.0,
    "max_slew_speed_rpm": 0.7,
    "rated_wind_speed_ms": 20.0,
}
T0 = datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(days=60)
T2 = T1 + timedelta(days=30)


def iso(moment: datetime) -> str:
    return moment.isoformat()


class ParamValidationTests(unittest.TestCase):
    def test_model_required(self) -> None:
        with self.assertRaises(RegistryValidationError):
            CraneParams.from_dict({})

    def test_tip_cannot_exceed_rated(self) -> None:
        payload = dict(PARAMS_A, tip_capacity_t=9.0)
        with self.assertRaises(RegistryValidationError):
            CraneParams.from_dict(payload)

    def test_positive_dimensions(self) -> None:
        payload = dict(PARAMS_A, jib_length_m=0)
        with self.assertRaises(RegistryValidationError):
            CraneParams.from_dict(payload)

    def test_extras_kept_and_unknown_number_coerced(self) -> None:
        params = CraneParams.from_dict(dict(PARAMS_A, extras={"吊钩倍率": "4"}))
        self.assertEqual(params.extras["吊钩倍率"], "4")
        self.assertEqual(params.model, "QTZ80")

    def test_timestamps_must_be_timezone_aware(self) -> None:
        with self.assertRaises(RegistryValidationError):
            parse_iso("2026-03-01T08:00:00")
        parsed = parse_iso("2026-03-01T08:00:00Z")
        self.assertEqual(parsed.tzinfo, timezone.utc)
        with self.assertRaises(RegistryValidationError):
            parse_iso("not-a-date")


class RegistryFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = EquipmentRegistry()
        self.record_a, _ = register_equipment(
            self.registry,
            PARAMS_A,
            equipment_id="eq-a",
            serial="SN-A-001",
            slot_id="crane-1",
            slot_name="一号机位",
            at=iso(T0),
        )

    def test_register_mounts_version_one_on_slot(self) -> None:
        self.assertEqual(self.registry.slot_of("eq-a"), "crane-1")
        assignment, version = effective_assignment(self.registry, "crane-1", iso(T0 + timedelta(days=1)))
        self.assertEqual(assignment.equipment_id, "eq-a")
        self.assertEqual(version.version_no, 1)
        self.assertEqual(version.params.model, "QTZ80")

    def test_duplicate_id_and_serial_rejected(self) -> None:
        with self.assertRaises(EquipmentExists):
            register_equipment(self.registry, PARAMS_B, equipment_id="eq-a", serial="SN-X", at=iso(T1))
        with self.assertRaises(EquipmentExists):
            register_equipment(self.registry, PARAMS_B, equipment_id="eq-b", serial="SN-A-001", at=iso(T1))

    def test_mount_requires_registered_free_machine_and_empty_slot(self) -> None:
        with self.assertRaises(EquipmentNotFound):
            mount_equipment(self.registry, "crane-2", "eq-missing")
        with self.assertRaises(SlotOccupied):
            mount_equipment(self.registry, "crane-1", "eq-a")
        record_b, _ = register_equipment(
            self.registry, PARAMS_B, equipment_id="eq-b", serial="SN-B-002", at=iso(T1)
        )
        with self.assertRaises(SlotOccupied):
            mount_equipment(self.registry, "crane-1", "eq-b", at=iso(T1))
        # A machine already mounted cannot take a second slot.
        unmount_equipment(self.registry, "crane-1", at=iso(T1))
        mount_equipment(self.registry, "crane-2", "eq-b", at=iso(T1))
        with self.assertRaises(EquipmentMountedElsewhere):
            mount_equipment(self.registry, "crane-9", "eq-b", at=iso(T1 + timedelta(days=1)))

    def test_swap_closes_old_interval_and_opens_new_one(self) -> None:
        register_equipment(
            self.registry, PARAMS_B, equipment_id="eq-b", serial="SN-B-002", at=iso(T1)
        )
        events, assignment = swap_equipment(
            self.registry, "crane-1", "eq-b", at=iso(T1), reason="老塔退场"
        )
        self.assertEqual([event.action for event in events], ["unmount", "swap"])
        self.assertEqual(assignment.equipment_id, "eq-b")

        # Before the swap the old dossier governed the slot; afterwards the new one.
        before = effective_assignment(self.registry, "crane-1", iso(T1 - timedelta(minutes=1)))
        after = effective_assignment(self.registry, "crane-1", iso(T1 + timedelta(minutes=1)))
        self.assertEqual(before[0].equipment_id, "eq-a")
        self.assertEqual(before[1].params.model, "QTZ80")
        self.assertEqual(after[0].equipment_id, "eq-b")
        self.assertEqual(after[1].params.model, "QTZ125")

        timeline = slot_view(self.registry, "crane-1")["timeline"]
        self.assertEqual(len(timeline), 2)
        self.assertIsNotNone(timeline[0]["end_at"])
        self.assertIsNone(timeline[1]["end_at"])

    def test_swap_same_machine_rejected_and_empty_slot_rejected(self) -> None:
        with self.assertRaises(RegistryValidationError):
            swap_equipment(self.registry, "crane-1", "eq-a", at=iso(T1))
        with self.assertRaises(SlotEmpty):
            swap_equipment(self.registry, "crane-empty", "eq-a", at=iso(T1))

    def test_revise_appends_version_and_rotates_open_interval(self) -> None:
        new_params = dict(PARAMS_A, max_slew_speed_rpm=0.6, rated_wind_speed_ms=18.0)
        version, event, assignment = revise_params(
            self.registry, "eq-a", new_params, at=iso(T1), reason="降速运行"
        )
        self.assertEqual(version.version_no, 2)
        self.assertEqual(version.changed_fields, ("max_slew_speed_rpm", "rated_wind_speed_ms"))
        self.assertIsNotNone(assignment)

        old = effective_assignment(self.registry, "crane-1", iso(T1 - timedelta(minutes=1)))
        new = effective_assignment(self.registry, "crane-1", iso(T1 + timedelta(minutes=1)))
        self.assertEqual(old[1].version_no, 1)
        self.assertEqual(old[1].params.max_slew_speed_rpm, 0.8)
        self.assertEqual(new[1].version_no, 2)
        self.assertEqual(new[1].params.max_slew_speed_rpm, 0.6)

        view = slot_view(self.registry, "crane-1")
        self.assertEqual(view["effective"]["version_no"], 2)
        self.assertEqual(len(view["timeline"]), 2)
        self.assertEqual(event.action, "revise")

    def test_revise_requires_a_change_and_monotonic_time(self) -> None:
        with self.assertRaises(RegistryValidationError):
            revise_params(self.registry, "eq-a", PARAMS_A, at=iso(T1))
        with self.assertRaises(RegistryValidationError):
            revise_params(
                self.registry, "eq-a", dict(PARAMS_A, jib_length_m=50), at=iso(T0 - timedelta(days=1))
            )
        with self.assertRaises(EquipmentNotFound):
            revise_params(self.registry, "eq-nope", PARAMS_B, at=iso(T1))

    def test_backdated_change_must_follow_last_slot_event(self) -> None:
        # The slot first changed at T0; an earlier mount is refused.
        register_equipment(
            self.registry, PARAMS_B, equipment_id="eq-b", serial="SN-B-002", at=iso(T0 + timedelta(days=2))
        )
        unmount_equipment(self.registry, "crane-1", at=iso(T0 + timedelta(days=3)))
        with self.assertRaises(RegistryValidationError):
            mount_equipment(
                self.registry, "crane-1", "eq-b", at=iso(T0 + timedelta(days=1))
            )

    def test_unmount_leaves_effective_empty_afterwards(self) -> None:
        unmount_equipment(self.registry, "crane-1", at=iso(T1))
        self.assertIsNone(self.registry.slot_of("eq-a"))
        self.assertIsNone(effective_assignment(self.registry, "crane-1", iso(T2)))
        # The history is still there.
        self.assertEqual(
            effective_assignment(self.registry, "crane-1", iso(T0 + timedelta(days=2)))[0].equipment_id,
            "eq-a",
        )

    def test_revised_dossier_rides_along_a_later_swap(self) -> None:
        revise_params(
            self.registry, "eq-a", dict(PARAMS_A, max_slew_speed_rpm=0.5), at=iso(T1)
        )
        register_equipment(
            self.registry, PARAMS_B, equipment_id="eq-b", serial="SN-B-002", at=iso(T2)
        )
        swap_equipment(self.registry, "crane-1", "eq-b", at=iso(T2))
        # eq-a interval on the slot keeps pointing at v2 while it was running.
        history = [item for item in slot_view(self.registry, "crane-1")["timeline"] if item["equipment_id"] == "eq-a"]
        self.assertEqual(history[-1]["version_no"], 2)

    def test_events_journal_filters_and_persistence_round_trip(self) -> None:
        register_equipment(
            self.registry, PARAMS_B, equipment_id="eq-b", serial="SN-B-002", at=iso(T1)
        )
        swap_equipment(self.registry, "crane-1", "eq-b", at=iso(T1 + timedelta(days=1)))
        revise_params(
            self.registry, "eq-b", dict(PARAMS_B, rated_wind_speed_ms=16), at=iso(T2)
        )
        swaps = query_events(self.registry, slot_id="crane-1", action="swap")
        self.assertEqual(len(swaps), 1)
        self.assertEqual(swaps[0].equipment_id, "eq-b")
        seqs = [event.seq for event in query_events(self.registry, limit=0)]
        self.assertEqual(sorted(seqs), list(range(1, len(seqs) + 1)))

        store = MemoryStore()
        persist(self.registry, store)
        rebuilt = EquipmentRegistry()
        restore(rebuilt, store)
        self.assertEqual(rebuilt.equipment_ids(), ["eq-a", "eq-b"])
        historical = effective_assignment(rebuilt, "crane-1", iso(T1 - timedelta(hours=1)))
        self.assertEqual(historical[1].params.model, "QTZ80")
        current = effective_assignment(rebuilt, "crane-1", iso(T2 + timedelta(days=1)))
        self.assertEqual(current[0].equipment_id, "eq-b")
        self.assertEqual(current[1].version_no, 2)
        self.assertEqual(current[1].params.rated_wind_speed_ms, 16)
        self.assertEqual(len(query_events(rebuilt, limit=0)), 5)


if __name__ == "__main__":
    unittest.main()
