"""Tests for the equipment archive ledger and its console routes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cranefleet import archive as archive_module
from cranefleet.archive import (
    ACTION_AMEND,
    ACTION_REGISTER,
    ACTION_REPLACE,
    ArchiveLedger,
    amend,
    parse_iso,
    register,
    replace,
    restore,
    persist,
)
from cranefleet.console import Console, Request
from cranefleet.errors import (
    ArchiveNotFound,
    DuplicateArchive,
    InvalidEffectiveTime,
)
from cranefleet.store import MemoryStore
from cranefleet.wiring import build_app

WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


def dispatch(console: Console, method: str, path: str, body: dict | None = None, query: dict | None = None):  # noqa: ANN201
    request = Request(
        method=method,
        path=path,
        query=query or {},
        body=body or {},
        body_valid=True,
    )
    return console.dispatch(request)


FIRST_MACHINE = {
    "model": "QTZ80",
    "serial_number": "T-2018-001",
    "manufacturer": "中天机械",
    "effective_from": "2026-03-01T08:00:00+08:00",
    "parameters": {
        "rated_capacity_t": 8.0,
        "max_radius_m": 55.0,
        "free_height_m": 45.0,
        "max_wind_speed_mps": 20.0,
    },
    "reason": "设备进场接入",
    "operator": "张工",
}


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = ArchiveLedger()

    def test_register_creates_first_revision(self) -> None:
        revision = register(self.ledger, "crane-1", FIRST_MACHINE)
        self.assertEqual(revision.seq, 1)
        self.assertEqual(revision.change, ACTION_REGISTER)
        self.assertEqual(revision.model, "QTZ80")
        self.assertEqual(revision.supersedes, "")
        self.assertTrue(self.ledger.is_registered("crane-1"))
        self.assertEqual(self.ledger.current("crane-1").revision_id, revision.revision_id)

    def test_duplicate_registration_rejected(self) -> None:
        register(self.ledger, "crane-1", FIRST_MACHINE)
        with self.assertRaises(DuplicateArchive):
            register(self.ledger, "crane-1", FIRST_MACHINE)

    def test_amend_carries_identity_and_merges_parameters(self) -> None:
        register(self.ledger, "crane-1", FIRST_MACHINE)
        revision = amend(
            self.ledger,
            "crane-1",
            {
                "parameters": {"max_wind_speed_mps": 16.0},
                "effective_from": "2026-05-10T09:00:00+08:00",
                "reason": "接长起重臂后重新核定抗风参数",
            },
        )
        self.assertEqual(revision.change, ACTION_AMEND)
        self.assertEqual(revision.seq, 2)
        # Same physical machine: identity carries over.
        self.assertEqual(revision.serial_number, "T-2018-001")
        self.assertEqual(revision.manufacturer, "中天机械")
        # Untouched parameters survive; the amended one is overwritten.
        self.assertEqual(revision.parameters["rated_capacity_t"], 8.0)
        self.assertEqual(revision.parameters["max_wind_speed_mps"], 16.0)
        self.assertEqual(revision.supersedes, self.ledger.revisions_for("crane-1")[0].revision_id)

    def test_replace_opens_a_new_machine_revision(self) -> None:
        register(self.ledger, "crane-1", FIRST_MACHINE)
        revision = replace(
            self.ledger,
            "crane-1",
            {
                "model": "QTZ125",
                "serial_number": "T-2026-042",
                "manufacturer": "中联重科",
                "effective_from": "2026-07-01T07:30:00+08:00",
                "parameters": {"rated_capacity_t": 10.0, "max_radius_m": 60.0},
                "reason": "老塔退场，新塔接管同一机位",
            },
        )
        self.assertEqual(revision.change, ACTION_REPLACE)
        self.assertEqual(revision.seq, 2)
        self.assertEqual(revision.serial_number, "T-2026-042")
        # A replacement does not inherit the old machine's ratings.
        self.assertEqual(set(revision.parameters), {"rated_capacity_t", "max_radius_m"})

    def test_amend_or_replace_unknown_crane_rejected(self) -> None:
        with self.assertRaises(ArchiveNotFound):
            amend(self.ledger, "crane-x", {"parameters": {"rated_capacity_t": 6}})
        with self.assertRaises(ArchiveNotFound):
            replace(self.ledger, "crane-x", {"model": "QTZ80", "serial_number": "S"})

    def test_effective_at_backtraces_which_archive_was_running(self) -> None:
        register(self.ledger, "crane-1", FIRST_MACHINE)
        amend(
            self.ledger,
            "crane-1",
            {"parameters": {"max_wind_speed_mps": 16.0},
             "effective_from": "2026-05-10T09:00:00+08:00"},
        )
        replace(
            self.ledger,
            "crane-1",
            {
                "model": "QTZ125",
                "serial_number": "T-2026-042",
                "effective_from": "2026-07-01T07:30:00+08:00",
                "parameters": {"rated_capacity_t": 10.0},
            },
        )
        # April: the original QTZ80 running its original parameters.
        april = parse_iso("2026-04-15T12:00:00+08:00")
        self.assertEqual(self.ledger.effective_at("crane-1", april).serial_number, "T-2018-001")
        self.assertEqual(
            self.ledger.effective_at("crane-1", april).parameters["max_wind_speed_mps"], 20.0
        )
        # June: same machine, amended wind rating.
        june = parse_iso("2026-06-15T12:00:00+08:00")
        june_revision = self.ledger.effective_at("crane-1", june)
        self.assertEqual(june_revision.serial_number, "T-2018-001")
        self.assertEqual(june_revision.parameters["max_wind_speed_mps"], 16.0)
        # August: the replacement QTZ125.
        august = parse_iso("2026-08-01T00:00:00+08:00")
        self.assertEqual(self.ledger.effective_at("crane-1", august).model, "QTZ125")
        # Before onboarding there is no archive.
        with self.assertRaises(ArchiveNotFound):
            self.ledger.effective_at("crane-1", parse_iso("2026-01-01T00:00:00+08:00"))

    def test_effective_from_must_not_predate_current(self) -> None:
        register(self.ledger, "crane-1", FIRST_MACHINE)
        with self.assertRaises(InvalidEffectiveTime):
            amend(
                self.ledger,
                "crane-1",
                {"parameters": {"rated_capacity_t": 6},
                 "effective_from": "2026-02-01T00:00:00+08:00"},
            )

    def test_validation_rules(self) -> None:
        with self.assertRaises(archive_module.ArchiveError):
            register(self.ledger, "crane-1", {"serial_number": "S"})  # no model
        with self.assertRaises(archive_module.ArchiveError):
            register(self.ledger, "crane-1", {"model": "QTZ80"})  # no serial
        with self.assertRaises(archive_module.ArchiveError):
            register(self.ledger, "crane-1", {"model": "QTZ80", "serial_number": "S"})  # no params
        with self.assertRaises(archive_module.ArchiveError):
            register(
                self.ledger,
                "crane-1",
                {"model": "QTZ80", "serial_number": "S", "parameters": {"x": "lots"}},
            )
        with self.assertRaises(InvalidEffectiveTime):
            register(self.ledger, "crane-9", {"model": "M", "serial_number": "S",
                                              "parameters": {"a": 1},
                                              "effective_from": "not-a-date"})

    def test_noop_amendment_rejected(self) -> None:
        register(self.ledger, "crane-1", FIRST_MACHINE)
        with self.assertRaises(archive_module.ArchiveError):
            amend(self.ledger, "crane-1", {"parameters": {"rated_capacity_t": 8.0}})

    def test_revisions_are_immutable_history(self) -> None:
        first = register(self.ledger, "crane-1", FIRST_MACHINE)
        amend(
            self.ledger,
            "crane-1",
            {"parameters": {"rated_capacity_t": 6.0},
             "effective_from": "2026-05-10T09:00:00+08:00"},
        )
        history = self.ledger.revisions_for("crane-1")
        self.assertEqual(len(history), 2)
        # The original revision still shows the original rating.
        self.assertEqual(history[0].revision_id, first.revision_id)
        self.assertEqual(history[0].parameters["rated_capacity_t"], 8.0)
        self.assertEqual(history[1].parameters["rated_capacity_t"], 6.0)

    def test_persistence_round_trip(self) -> None:
        register(self.ledger, "crane-1", FIRST_MACHINE)
        replace(
            self.ledger,
            "crane-1",
            {
                "model": "QTZ125",
                "serial_number": "T-2026-042",
                "effective_from": "2026-07-01T07:30:00+08:00",
                "parameters": {"rated_capacity_t": 10.0},
            },
        )
        store = MemoryStore()
        persist(self.ledger, store)
        rebuilt = ArchiveLedger()
        restore(rebuilt, store)
        self.assertEqual(rebuilt.crane_ids(), ["crane-1"])
        self.assertEqual(rebuilt.current("crane-1").model, "QTZ125")
        june = parse_iso("2026-06-01T00:00:00+08:00")
        self.assertEqual(rebuilt.effective_at("crane-1", june).model, "QTZ80")


class ConsoleArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = build_app("memory", web_root=str(WEB_ROOT))
        self.console = self.app.console

    def tearDown(self) -> None:
        self.app.shutdown()

    def _register(self, crane_id: str = "crane-1", **overrides) -> tuple[int, dict]:  # noqa: ANN001
        body = dict(FIRST_MACHINE)
        body["crane_id"] = crane_id
        body.update(overrides)
        return dispatch(self.console, "POST", "/api/archives/register", body)

    def test_register_list_get_and_history_routes(self) -> None:
        status, payload = self._register()
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["seq"], 1)

        status, payload = dispatch(self.console, "GET", "/api/archives")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["total"], 1)
        self.assertEqual(payload["data"]["cranes"][0]["model"], "QTZ80")

        status, payload = dispatch(
            self.console, "GET", "/api/archives/crane", query={"crane_id": ["crane-1"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["revision_count"], 1)
        self.assertEqual(payload["data"]["current"]["serial_number"], "T-2018-001")

        status, payload = dispatch(
            self.console, "GET", "/api/archives/history", query={"crane_id": ["crane-1"]}
        )
        self.assertEqual([item["change"] for item in payload["data"]], [ACTION_REGISTER])

    def test_full_machine_swap_lifecycle(self) -> None:
        self._register()
        status, payload = dispatch(
            self.console,
            "POST",
            "/api/archives/amend",
            {
                "crane_id": "crane-1",
                "parameters": {"max_wind_speed_mps": 16.0},
                "effective_from": "2026-05-10T09:00:00+08:00",
                "reason": "重新核定",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["change"], ACTION_AMEND)

        status, payload = dispatch(
            self.console,
            "POST",
            "/api/archives/replace",
            {
                "crane_id": "crane-1",
                "model": "QTZ125",
                "serial_number": "T-2026-042",
                "manufacturer": "中联重科",
                "effective_from": "2026-07-01T07:30:00+08:00",
                "parameters": {"rated_capacity_t": 10.0, "max_radius_m": 60.0},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["change"], ACTION_REPLACE)

        # Point-in-time backtrace through the API.
        status, payload = dispatch(
            self.console,
            "GET",
            "/api/archives/effective",
            query={"crane_id": ["crane-1"], "at": ["2026-06-15T12:00:00+08:00"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["serial_number"], "T-2018-001")
        self.assertEqual(payload["data"]["parameters"]["max_wind_speed_mps"], 16.0)

        status, payload = dispatch(
            self.console,
            "GET",
            "/api/archives/effective",
            query={"crane_id": ["crane-1"], "at": ["2026-08-15T12:00:00+08:00"]},
        )
        self.assertEqual(payload["data"]["model"], "QTZ125")
        self.assertEqual(payload["data"]["seq"], 3)

        # The change trail is visible in the audit log.
        actions = [entry.action for entry in self.console.audit.snapshot()]
        self.assertIn("archive-register", actions)
        self.assertIn("archive-amend", actions)
        self.assertIn("archive-replace", actions)

    def test_revision_lookup_route(self) -> None:
        status, payload = self._register()
        revision_id = payload["data"]["revision_id"]
        status, payload = dispatch(
            self.console,
            "GET",
            "/api/archives/revision",
            query={"revision_id": [revision_id]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["revision_id"], revision_id)

    def test_error_responses(self) -> None:
        status, payload = dispatch(
            self.console, "GET", "/api/archives/crane", query={"crane_id": ["ghost"]}
        )
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])

        self._register()
        status, payload = self._register()
        self.assertEqual(status, 409)

        status, _ = dispatch(self.console, "POST", "/api/archives/register", {"crane_id": "crane-9"})
        self.assertEqual(status, 400)

        status, _ = dispatch(
            self.console,
            "POST",
            "/api/archives/amend",
            {"crane_id": "crane-1", "parameters": {},
             "effective_from": "2026-01-01T00:00:00+08:00"},
        )
        self.assertEqual(status, 400)

        status, _ = dispatch(
            self.console,
            "GET",
            "/api/archives/effective",
            query={"crane_id": ["crane-1"], "at": ["yesterday"]},
        )
        self.assertEqual(status, 400)

        status, _ = dispatch(
            self.console,
            "GET",
            "/api/archives/effective",
            query={"crane_id": ["crane-1"], "at": ["2026-01-01T00:00:00+08:00"]},
        )
        self.assertEqual(status, 404)


class ArchiveBootTests(unittest.TestCase):
    def test_archive_history_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = build_app(directory, web_root=str(WEB_ROOT))
            dispatch(
                first.console,
                "POST",
                "/api/archives/register",
                {"crane_id": "crane-1", **FIRST_MACHINE},
            )
            dispatch(
                first.console,
                "POST",
                "/api/archives/replace",
                {
                    "crane_id": "crane-1",
                    "model": "QTZ125",
                    "serial_number": "T-2026-042",
                    "effective_from": "2026-07-01T07:30:00+08:00",
                    "parameters": {"rated_capacity_t": 10.0},
                },
            )
            first.shutdown()

            second = build_app(directory, web_root=str(WEB_ROOT))
            ledger = second.console.archive
            self.assertEqual(ledger.current("crane-1").serial_number, "T-2026-042")
            self.assertEqual(len(ledger.revisions_for("crane-1")), 2)
            backthen = ledger.effective_at(
                "crane-1", parse_iso("2026-04-01T00:00:00+08:00")
            )
            self.assertEqual(backthen.model, "QTZ80")
            second.shutdown()


if __name__ == "__main__":
    unittest.main()
