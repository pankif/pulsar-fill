import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import pulsar_loader as core
import pulsar_app as app
from test_pulsar_loader import Device, fixture, job


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.path = self.root / "Другой объект.xlsx"

    def tearDown(self):
        self.temp.cleanup()

    def test_arbitrary_filename_sheet_and_runtime_weights(self):
        w = fixture(self.path)
        w.active.title = "Дом Б"
        w.save(self.path)
        source = self.path.read_bytes()
        with patch.object(core.socket, "create_connection", side_effect=AssertionError("Сеть при импорте")):
            self.assertEqual(app.workbook_sheets(self.path), ["Дом Б", "Офисы"])
            plan = app.load_xlsx(self.path, "Дом Б", {}, (0.02, 0.03))
        self.assertEqual(len(plan["jobs"]), 2)
        first = plan["jobs"][0]
        self.assertEqual(first["sheet"], "Дом Б")
        self.assertEqual(first["port"], 50002)
        self.assertEqual(first["values"]["P00C0.F32"], 0.02)
        self.assertEqual(first["values"]["P00C3.F32"], 0.03)
        self.assertEqual(self.path.read_bytes(), source)

    def test_corrections_are_explicit_and_dont_leak_into_other_imports(self):
        w = fixture(self.path)
        w.active["C2"] = w.active["M2"] = 123456788
        w.save(self.path)
        corrected = app.load_xlsx(self.path, "Лист1", app.parse_corrections("123456788 = 12345678"), (0.01, 0.01))
        self.assertEqual(len(corrected["jobs"]), 2)
        self.assertEqual(len(app.load_xlsx(self.path, "Лист1", {}, (0.01, 0.01))["jobs"]), 1)

    def test_invalid_options_before_any_device_access(self):
        for value in ("0", "-1", "nan", "inf", "1e-100", "1e100", "abc"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.positive_number(value, "Вес", as_float32=True)
        self.assertEqual(app.positive_number("0,01", "Вес", as_float32=True), 0.01)
        with self.assertRaises(ValueError):
            app.parse_corrections("123=456\n123=789")


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        self.device = Device()
        self.plan = {"source_file": "test.xlsx", "jobs": [job()], "skipped": [], "warnings": []}

    def tearDown(self):
        self.temp.cleanup()

    def run_batch(self, write=False, stop=None, callback=lambda e: None, jobs=None):
        with patch.object(core.socket, "create_connection", self.device.connect), patch.object(core.time, "sleep"):
            return app.run_batch(self.plan, jobs or self.plan["jobs"], write, self.root, 1,
                                 stop or threading.Event(), callback)

    def test_read_reports_actual_values_and_never_sends_write(self):
        summary = self.run_batch()
        self.assertEqual(summary["completed"], 1)
        self.assertFalse(any(f[4] in (3, 11) for f in self.device.requests))
        report = Path(summary["report_dir"])
        results = json.loads((report / "results.json").read_text())
        self.assertEqual(results[0]["before"]["K9.F32"], 2)
        self.assertTrue((report / "results.csv").read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_write_uses_core_order_and_journals_before_network_write(self):
        events = []
        original = self.device.handle

        def handle(frame):
            if frame[4] in (3, 11):
                journal = next(self.root.glob("*/events.jsonl")).read_text()
                self.assertIn('"event": "write_intent"', journal)
            return original(frame)

        self.device.handle = handle
        summary = self.run_batch(True, callback=events.append)
        self.assertEqual(summary["failed"], 0)
        writes = [f for f in self.device.requests if f[4] in (3, 11)]
        self.assertEqual([f[4] for f in writes], [11, 11, 3, 3])
        self.assertEqual([e["type"] for e in events if e["type"] == "job_finished"], ["job_finished"])
        saved = json.loads((Path(summary["report_dir"]) / "plan.json").read_text())
        self.assertEqual(saved["selected_jobs"], [job()])

    def test_stop_finishes_current_meter_and_records_unstarted(self):
        stop = threading.Event()
        second = dict(copy.deepcopy(job()), meter="12345679", row=3)

        def progress(event):
            if event["type"] == "job_finished":
                stop.set()

        summary = self.run_batch(True, stop, progress, [job(), second])
        self.assertEqual(summary["completed"], 1)
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(summary["not_started"], ["12345679"])
        self.assertEqual(self.device.channels[10], core.float32(2.5))

    def test_failure_is_reported_and_new_run_has_new_directory(self):
        self.device.locked = 0x00C0
        one = self.run_batch(True)
        two = self.run_batch(False)
        self.assertEqual(one["failed"], 1)
        self.assertNotEqual(one["report_dir"], two["report_dir"])
        self.assertFalse(any(f[4] == 3 for f in self.device.requests))


if __name__ == "__main__":
    unittest.main()
