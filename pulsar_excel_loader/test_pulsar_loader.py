import importlib.util
import hashlib
import os
import io
import json
from pathlib import Path
import socket
import struct
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from openpyxl import Workbook


ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("pulsar_loader", ROOT / "pulsar_loader.py")
loader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(loader)


def fixture(path):
    w = Workbook()
    s = w.active
    s.title = "Лист1"
    for col, val in {"A": "Секция", "B": "Кв", "C": "ТЭ", "D": "ХВС", "E": "ГВС",
                     "G": "УМ", "H": "Канал", "I": "IP", "J": "Порт", "M": 291,
                     "N": "ХВС", "O": "ГВС"}.items():
        s[f"{col}1"] = val
    for row, meter in enumerate([12345678, 12345679], 2):
        for col, val in {"A": 1, "B": row - 1, "C": meter, "D": "Вода1", "E": "Вода2",
                         "G": 1, "H": 1, "I": "127.0.0.1", "J": 50003, "K": 50002,
                         "M": meter, "N": 1.25, "O": 2.5}.items():
            s[f"{col}{row}"] = val
    other = w.create_sheet("Офисы")
    other["C1"] = "ТЭ"
    other["C2"] = 22345678
    w.save(path)
    return w


class Device:
    def __init__(self):
        self.serial = 12345678
        self.parameters = {0x00C0: 1.0, 0x00C3: 1.0}
        self.channels = {9: 2.0, 10: 3.0}
        self.requests = []
        self.locked = None
        self.drop_ack = False
        self.ignore_channel = None
        self.connections = 0

    def connect(self, endpoint, timeout):
        self.connections += 1
        return FakeSocket(self)

    def handle(self, frame):
        self.requests.append(frame)
        assert loader.crc16(frame) == 0
        command, payload = frame[4], frame[6:-4]
        if command == 0x0A:
            parameter = int.from_bytes(payload, "little")
            data = (struct.pack("<I", self.serial) if parameter == 3
                    else struct.pack("<f", self.parameters[parameter])) + bytes(4)
        elif command == 1:
            mask = int.from_bytes(payload, "little")
            channel = mask.bit_length()
            data = struct.pack("<f", self.channels[channel])
        elif command == 0x0B:
            parameter = int.from_bytes(payload[:2], "little")
            assert len(payload) == 10 and payload[-4:] == bytes(4)
            if parameter == self.locked:
                return loader.pack_frame("12345678", 0, b"\x05", int.from_bytes(frame[-4:-2], "little"))
            self.parameters[parameter] = struct.unpack("<f", payload[2:6])[0]
            data = b"\x00\x00"
        elif command == 3:
            mask = int.from_bytes(payload[:4], "little")
            channel = mask.bit_length()
            if channel != self.ignore_channel:
                self.channels[channel] = struct.unpack("<f", payload[4:])[0]
            data = payload[:4]
        else:
            raise AssertionError(command)
        if command in (3, 0x0B) and self.drop_ack:
            self.drop_ack = False
            return None
        return loader.pack_frame("12345678", command, data, int.from_bytes(frame[-4:-2], "little"))


class FakeSocket:
    def __init__(self, device):
        self.device = device
        self.buffer = b""
        self.closed = False

    def settimeout(self, timeout):
        pass

    def sendall(self, frame):
        self.buffer = self.device.handle(frame)

    def recv(self, size):
        if self.buffer is None:
            raise socket.timeout("Потеряно подтверждение")
        # Ответ специально разбивается на части, как в реальном TCP-потоке.
        size = min(size, 2)
        part, self.buffer = self.buffer[:size], self.buffer[size:]
        return part

    def close(self):
        self.closed = True


def job():
    return {"meter": "12345678", "ip": "127.0.0.1", "port": 50002,
            "sheet": "Лист1", "row": 2, "apartment": 1,
            "values": {"P00C0.F32": 0.01, "P00C3.F32": 0.01, "K9.F32": 1.25, "K10.F32": 2.5}}


class WorkbookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.path = Path(self.temp.name) / "source.xlsx"

    def tearDown(self):
        self.temp.cleanup()

    def test_port_K_weights_and_offices(self):
        fixture(self.path)
        original = self.path.read_bytes()
        plan = loader.build_plan(self.path)
        self.assertEqual(len(plan["jobs"]), 2)
        self.assertEqual(plan["jobs"][0]["port"], 50002)
        self.assertEqual(plan["jobs"][0]["values"], job()["values"])
        self.assertEqual(plan["other_sheets"][0]["meter_count"], 1)
        self.assertEqual(self.path.read_bytes(), original)

    def test_join_by_meter_not_row(self):
        w = fixture(self.path)
        s = w["Лист1"]
        s["M2"], s["M3"] = s["M3"].value, s["M2"].value
        s["N2"], s["N3"] = 9.0, 7.0
        w.save(self.path)
        plan = loader.build_plan(self.path)
        self.assertEqual(plan["jobs"][0]["values"]["K9.F32"], 7.0)
        self.assertEqual(plan["jobs"][0]["readings_row"], 3)

    def test_error_is_skipped_but_zero_is_retained(self):
        w = fixture(self.path)
        s = w["Лист1"]
        s["O2"] = "#VALUE!"
        s["N3"], s["O3"] = 0, 0
        w.save(self.path)
        plan = loader.build_plan(self.path)
        self.assertEqual(len(plan["skipped"]), 1)
        self.assertIn("O2", plan["skipped"][0]["reason"])
        self.assertEqual(plan["jobs"][0]["values"]["K9.F32"], 0)

    def test_duplicate_readings_rejected(self):
        w = fixture(self.path)
        w["Лист1"]["M3"] = w["Лист1"]["M2"].value
        w.save(self.path)
        with self.assertRaisesRegex(ValueError, "Повтор номера в M"):
            loader.build_plan(self.path)

    def test_duplicate_targets_both_skipped(self):
        w = fixture(self.path)
        w["Лист1"]["C3"] = w["Лист1"]["C2"].value
        w.save(self.path)
        plan = loader.build_plan(self.path)
        self.assertEqual(plan["jobs"], [])
        self.assertEqual(len(plan["skipped"]), 2)

    def test_formula_readings_and_bad_port_rejected(self):
        w = fixture(self.path)
        s = w["Лист1"]
        s["N2"] = "=1/0"
        s["K3"] = 0
        w.save(self.path)
        plan = loader.build_plan(self.path)
        self.assertEqual(len(plan["skipped"]), 2)
        self.assertEqual(plan["jobs"], [])

    def test_nine_digit_serial_skips_only_that_meter(self):
        w = fixture(self.path)
        s = w["Лист1"]
        s["C2"], s["M2"] = 123456788, 123456788
        w.save(self.path)
        plan = loader.build_plan(self.path)
        self.assertEqual(len(plan["jobs"]), 1)
        self.assertEqual(len(plan["skipped"]), 1)
        self.assertIn("123456788", plan["skipped"][0]["reason"])

    def test_explicit_serial_correction_preserves_source(self):
        w = fixture(self.path)
        s = w["Лист1"]
        s["C2"], s["M2"] = 123456788, 123456788
        w.save(self.path)
        original = self.path.read_bytes()
        plan = loader.build_plan(self.path, {"123456788": "12345678"})
        self.assertEqual(plan["jobs"][0]["meter"], "12345678")
        self.assertEqual([item["cell"] for item in plan["applied_corrections"]], ["M2", "C2"])
        self.assertEqual(plan["skipped"], [])
        self.assertEqual(self.path.read_bytes(), original)

    def test_optional_local_workbook(self):
        path = Path(os.environ.get('PULSAR_TEST_XLSX', 'missing-source.xlsx'))
        if not path.exists():
            self.skipTest("Локальная книга не задана; данные объекта не входят в Git")
        original = path.read_bytes()
        plan = loader.build_plan(path)
        self.assertGreater(len(plan["jobs"]), 0)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(plan["source_sha256"], hashlib.sha256(original).hexdigest())


class ProtocolTests(unittest.TestCase):
    def test_replay_synthetic_single_channel_read(self):
        capture = json.loads((ROOT / "fixtures/synthetic-read.json").read_text(encoding="utf-8"))

        class ReplaySocket:
            def __init__(self):
                self.pairs = list(capture["pairs"])
                self.buffer = b""

            def settimeout(self, timeout):
                pass

            def sendall(self, frame):
                pair = self.pairs.pop(0)
                if frame != bytes.fromhex(pair["request"]):
                    raise AssertionError("Запрос отличается от синтетического эталона")
                self.buffer = bytes.fromhex(pair["response"])

            def recv(self, size):
                size = min(size, 2)
                part, self.buffer = self.buffer[:size], self.buffer[size:]
                return part

            def close(self):
                pass

        replay = ReplaySocket()
        client = loader.PulsarClient("127.0.0.1", 50001, capture["meter"])
        client.request_id = capture["initial_request_id"]
        with patch.object(loader.socket, "create_connection", return_value=replay), patch.object(loader, "REQUEST_PAUSE_SECONDS", 0):
            try:
                client.verify_serial()
                self.assertAlmostEqual(client.read_field(loader.FIELDS[0]), 0.01, places=8)
                self.assertAlmostEqual(client.read_field(loader.FIELDS[1]), 0.01, places=8)
                value = client.read_field(loader.FIELDS[2])
            except loader.ProtocolError as error:
                self.fail(f"Синтетический ответ чтения канала отвергнут: {error}")
            finally:
                client.close()
        self.assertAlmostEqual(value, 1.25, places=7)
        self.assertEqual(replay.pairs, [])

    def test_manual_golden_packets(self):
        first = bytes.fromhex("05 29 67 25 04 0A AC 00 21 14")
        second = bytes.fromhex("05 29 67 25 0A 0C 02 00 AD 00 CD 09")
        self.assertEqual(loader.pack_frame("5296725", 4, b"", 0x00AC), first)
        self.assertEqual(loader.pack_frame("5296725", 10, b"\x02\x00", 0x00AD), second)
        self.assertEqual(loader.crc16(first), 0)

    def test_crc_address_id_and_function_rejected(self):
        packet = loader.pack_frame("12345678", 1, bytes.fromhex("00 01 00 00 00 00 80 3F"), 7)
        bad = bytearray(packet)
        bad[6] ^= 1
        with self.assertRaisesRegex(loader.ProtocolError, "CRC"):
            loader.unpack_response(bytes(bad), "12345678", 1, 7)
        for meter, command, token in [("12345680", 1, 7), ("12345678", 3, 7), ("12345678", 1, 8)]:
            with self.assertRaises(loader.ProtocolError):
                loader.unpack_response(packet, meter, command, token)

    def test_error_code_locked(self):
        packet = loader.pack_frame("12345678", 0, b"\x05", 7)
        with self.assertRaises(loader.DeviceError) as caught:
            loader.unpack_response(packet, "12345678", 3, 7)
        self.assertEqual(caught.exception.code, 5)

    def test_broadcast_and_nonfinite_values_rejected(self):
        for value in [0, 1, "1", True, 12345678.5]:
            with self.assertRaises(ValueError):
                loader.serial_number(value)
        for value in [float("nan"), float("inf"), -1, True, "0.01"]:
            with self.assertRaises(ValueError):
                loader.float32(value)


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.pause = patch.object(loader, "REQUEST_PAUSE_SECONDS", 0)
        self.pause.start()
        self.addCleanup(self.pause.stop)

    def run_device(self, device, write=True):
        self.events = []
        with patch.object(loader.socket, "create_connection", side_effect=device.connect):
            return loader.process_job(job(), write, 1, lambda **event: self.events.append(event))

    def test_read_does_not_write(self):
        device = Device()
        result = self.run_device(device, False)
        self.assertEqual(result["status"], "read_only")
        self.assertTrue(all(frame[4] in (1, 10) for frame in device.requests))

    def test_four_writes_order_and_payloads(self):
        device = Device()
        result = self.run_device(device)
        self.assertEqual(result["status"], "verified")
        writes = [frame for frame in device.requests if frame[4] in (3, 11)]
        self.assertEqual([frame[4] for frame in writes], [11, 11, 3, 3])
        self.assertEqual(writes[0][6:-4], bytes.fromhex("C0 00 0A D7 23 3C 00 00 00 00"))
        self.assertEqual(writes[1][6:8], b"\xC3\x00")
        self.assertEqual(writes[2][6:10], b"\x00\x01\x00\x00")
        self.assertEqual(writes[3][6:10], b"\x00\x02\x00\x00")
        self.assertTrue(all(item["after"] == item["requested"] for item in result["fields"].values()))

    def test_wrong_serial_prevents_all_writes(self):
        device = Device()
        device.serial += 1
        result = self.run_device(device)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(device.requests), 1)

    def test_locked_weight_prevents_counter_writes(self):
        device = Device()
        device.locked = 0x00C3
        result = self.run_device(device)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["fields"]["P00C3.F32"]["device_error_code"], 5)
        self.assertFalse(any(frame[4] == 3 for frame in device.requests))
        self.assertIn("P00C0.F32", result["completed_fields"])

    def test_lost_ack_reconnects_reads_and_never_rewrites(self):
        device = Device()
        device.drop_ack = True
        result = self.run_device(device)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(device.connections, 2)
        writes = [frame for frame in device.requests if frame[4] == 11 and frame[6:8] == b"\xC0\x00"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(result["fields"]["P00C0.F32"]["status"], "verified_after_uncertain_response")

    def test_readback_mismatch_preserved_and_stops_meter(self):
        device = Device()
        device.ignore_channel = 9
        result = self.run_device(device)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["fields"]["K9.F32"]["after"], 2.0)
        self.assertNotIn("K10.F32", result["fields"])

    def test_second_run_does_not_repeat_matching_values(self):
        device = Device()
        self.run_device(device)
        device.requests.clear()
        result = self.run_device(device)
        self.assertEqual(result["status"], "verified")
        self.assertFalse(any(frame[4] in (3, 11) for frame in device.requests))

    def test_report_contains_failed_field_and_partial_success(self):
        device = Device()
        device.locked = 0x00C3
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            target = Path(directory) / "report"
            with patch.object(loader.socket, "create_connection", side_effect=device.connect):
                code = loader.run_plan({"jobs": [job()]}, [job()], True, target, 1)
            self.assertEqual(code, 2)
            results = json.loads((target / "results.json").read_text())
            self.assertEqual(results[0]["active_field"], "P00C3.F32")
            lines = [json.loads(line) for line in (target / "events.jsonl").read_text().splitlines()]
            self.assertEqual(sum(e["event"] == "write_intent" for e in lines), 2)


class CliTests(unittest.TestCase):
    def test_version_is_available_without_network(self):
        output = io.StringIO()
        with patch.object(loader.socket, "create_connection", side_effect=AssertionError("Запрещено подключение")), redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            loader.main(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("1.0.1", output.getvalue())

    def test_apply_requires_explicit_selection(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            loader.main(["apply", "plan.json", "--report-dir", "unused"])
        self.assertEqual(caught.exception.code, 2)

    def test_plan_command_is_offline_and_applies_correction(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            source, target = Path(directory) / "test.xlsx", Path(directory) / "plan.json"
            w = fixture(source)
            w["Лист1"]["C2"], w["Лист1"]["M2"] = 123456788, 123456788
            w.save(source)
            with patch.object(loader.socket, "create_connection", side_effect=AssertionError("Запрещено подключение")), redirect_stdout(io.StringIO()):
                code = loader.main(["plan", str(source), "--out", str(target),
                                    "--serial-correction", "123456788=12345678"])
            self.assertEqual(code, 0)
            self.assertEqual(loader.load_plan(target)["jobs"][0]["meter"], "12345678")

    def test_other_fields_or_changed_weights_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            target = Path(directory) / "plan.json"
            plan = {"profile": loader.PROFILE, "units": "m3", "connection": "tcp-client",
                    "port_column": "K", "jobs": [job()]}
            plan["jobs"][0]["values"]["P00C0.F32"] = 1
            target.write_text(json.dumps(plan))
            with self.assertRaisesRegex(ValueError, "0.01"):
                loader.load_plan(target)


if __name__ == "__main__":
    unittest.main(verbosity=2)
