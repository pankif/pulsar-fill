import os
from pathlib import Path
import tempfile
import time
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

import pulsar_loader as core
from test_pulsar_loader import Device, fixture


@unittest.skipUnless(os.environ.get("PULSAR_GUI_TEST") == "1", "Нужен графический сеанс")
class GuiTests(unittest.TestCase):
    def setUp(self):
        from pulsar_gui import PulsarApp
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.path = Path(self.temp.name) / "Дом.xlsx"
        fixture(self.path)
        self.root = tk.Tk()
        self.app = PulsarApp(self.root)
        self.app.report_root.set(str(Path(self.temp.name) / "reports"))

    def tearDown(self):
        self.root.destroy()
        self.temp.cleanup()

    def until_idle(self):
        deadline = time.monotonic() + 8
        while self.app.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.root.update()
        self.assertFalse(self.app.busy)

    def test_load_select_and_read_from_gui_without_real_network(self):
        with patch.object(core.socket, "create_connection", side_effect=AssertionError("Импорт обращается к сети")):
            self.app.open_file(self.path)
            self.until_idle()
        self.assertEqual(len(self.app.plan["jobs"]), 2)
        self.assertEqual(self.app.checked, set())
        self.app.select_all()
        self.assertEqual(len(self.app.checked), 2)
        self.app.select_none()
        self.root.update()
        x, y, width, height = self.app.table.bbox("12345678", "check")
        self.app.table.event_generate("<Button-1>", x=x + width // 2, y=y + height // 2)
        self.root.update()
        self.assertEqual(self.app.checked, {"12345678"})
        device = Device()
        with patch.object(core.socket, "create_connection", device.connect), patch.object(core.time, "sleep"):
            self.app.start_run(False)
            self.until_idle()
        self.assertEqual(self.app.results["12345678"]["status"], "read_only")
        self.assertFalse(any(frame[4] in (3, 11) for frame in device.requests))
        self.assertTrue((Path(self.app.last_report) / "results.json").exists())

    def test_write_preview_cancel_does_not_connect(self):
        self.app.open_file(self.path)
        self.until_idle()
        self.app.toggle_row("12345678")
        with patch.object(core.socket, "create_connection", side_effect=AssertionError("Сеть до подтверждения")):
            self.app.start_run(True)
            self.root.update()
            self.assertIsNotNone(self.app.preview)
            self.assertFalse(self.app.busy)
            self.app.preview.destroy()

    def test_confirmed_write_uses_values_from_window(self):
        self.app.open_file(self.path)
        self.until_idle()
        self.app.toggle_row("12345678")
        self.app.weights[0].set("0,02")
        self.app.weights[1].set("0.03")
        self.app.start_run(True)
        self.root.update()

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)

        confirm = next(w for w in descendants(self.app.preview)
                       if isinstance(w, ttk.Button)
                       and w.cget("text") == "Записать эти приборы")
        device = Device()
        with patch.object(core.socket, "create_connection", device.connect), patch.object(core.time, "sleep"):
            confirm.invoke()
            self.until_idle()
        self.assertEqual(device.parameters[0x00C0], core.float32(0.02))
        self.assertEqual(device.parameters[0x00C3], core.float32(0.03))
        self.assertEqual(self.app.results["12345678"]["status"], "verified")


if __name__ == "__main__":
    unittest.main()
