import argparse
import csv
import hashlib
from io import BytesIO
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import struct
import sys
import time
from datetime import datetime, timezone


PROFILE = "pulsar-standard-a1-v1"
VERSION = "1.0.1"
REQUEST_PAUSE_SECONDS = 0.05
FIELDS = (
    ("P00C0.F32", "Вес импульса входа 1", "parameter", 0x00C0, "м³/имп"),
    ("P00C3.F32", "Вес импульса входа 2", "parameter", 0x00C3, "м³/имп"),
    ("K9.F32", "ХВС: импульсный вход 1", "channel", 9, "м³"),
    ("K10.F32", "ГВС: импульсный вход 2", "channel", 10, "м³"),
)
ERROR_CODES = {
    1: "ERROR_CODE_FUNCTION", 2: "ERROR_CODE_BITMASK", 3: "ERROR_CODE_LENGTH",
    4: "ERROR_CODE_PARAMETER", 5: "ERROR_CODE_LOCKED", 6: "ERROR_CODE_RANGE",
    7: "ERROR_CODE_ARCHIVE", 8: "ERROR_CODE_LIMIT", 9: "ERROR_CODE_VERIFY",
    128: "ERROR_CODE_NO_ANSWER",
}


class ProtocolError(Exception):
    pass


class DeviceError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Прибор: {ERROR_CODES.get(code, 'ERROR_CODE_UNKNOWN')} ({code})")


class VerificationError(Exception):
    pass


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def serial_number(value):
    if isinstance(value, bool):
        raise ValueError("Неверный номер прибора")
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value != int(value):
            raise ValueError("Номер прибора должен быть целым")
        value = str(int(value))
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,8}", value.strip()):
        raise ValueError("Номер прибора должен содержать от 1 до 8 цифр")
    value = value.strip().zfill(8)
    if int(value) <= 1:
        raise ValueError("Нулевой и широковещательный адреса не разрешены")
    return value


def float32(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Ожидалось числовое значение, получено {value!r}")
    if not math.isfinite(value) or value < 0:
        raise ValueError("Значение должно быть конечным неотрицательным числом")
    try:
        result = struct.unpack("<f", struct.pack("<f", value))[0]
    except (OverflowError, struct.error) as error:
        raise ValueError("Значение не помещается в Float32") from error
    if not math.isfinite(result):
        raise ValueError("Значение не помещается в Float32")
    return result


def crc16(data):
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def pack_frame(meter, command, payload, request_id):
    address = bytes.fromhex(serial_number(meter))
    length = len(payload) + 10
    if not 10 <= length <= 255:
        raise ValueError("Недопустимая длина пакета")
    body = address + bytes([command, length]) + payload + struct.pack("<H", request_id)
    return body + struct.pack("<H", crc16(body))


def unpack_response(frame, meter, command, request_id):
    if len(frame) < 10 or frame[5] != len(frame):
        raise ProtocolError("Неверная длина ответа")
    if crc16(frame[:-2]) != int.from_bytes(frame[-2:], "little"):
        raise ProtocolError("Неверная CRC ответа")
    if frame[:4] != bytes.fromhex(serial_number(meter)):
        raise ProtocolError("Ответ от другого адреса")
    if int.from_bytes(frame[-4:-2], "little") != request_id:
        raise ProtocolError("Не совпал идентификатор запроса")
    payload = frame[6:-4]
    if frame[4] == 0:
        if len(payload) != 1 or payload[0] == 0:
            raise ProtocolError("Неверный ответ с кодом ошибки")
        raise DeviceError(payload[0])
    if frame[4] != command:
        raise ProtocolError("Не совпал код команды ответа")
    return payload


class PulsarClient:
    def __init__(self, ip, port, meter, timeout=5.0, log=None):
        self.ip, self.port, self.meter = ip, port, serial_number(meter)
        self.timeout = timeout
        self.log = log or (lambda **event: None)
        self.sock = None
        self.request_id = secrets.randbelow(65535)

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def request(self, command, payload):
        if self.sock is None:
            self.sock = socket.create_connection((self.ip, self.port), self.timeout)
        time.sleep(REQUEST_PAUSE_SECONDS)
        self.request_id = self.request_id % 65535 + 1
        frame = pack_frame(self.meter, command, payload, self.request_id)
        self.log(event="tx", command=command, hex=frame.hex(" "))
        deadline = time.monotonic() + self.timeout
        self.sock.settimeout(self.timeout)
        self.sock.sendall(frame)

        def receive(size):
            data = bytearray()
            while len(data) < size:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Истекло время ожидания ответа")
                self.sock.settimeout(remaining)
                part = self.sock.recv(size - len(data))
                if not part:
                    raise ConnectionError("Соединение закрыто до полного ответа")
                data.extend(part)
            return bytes(data)

        header = receive(6)
        if not 10 <= header[5] <= 255:
            raise ProtocolError("Недопустимая длина в заголовке ответа")
        response = header + receive(header[5] - 6)
        self.log(event="rx", command=command, hex=response.hex(" "))
        return unpack_response(response, self.meter, command, self.request_id)

    def read_parameter(self, parameter):
        payload = self.request(0x0A, struct.pack("<H", parameter))
        if len(payload) not in (4, 8):
            raise ProtocolError("Ожидалось 4 или 8 байт значения параметра")
        return payload

    def verify_serial(self):
        actual = struct.unpack("<I", self.read_parameter(0x0003)[:4])[0]
        if actual != int(self.meter):
            raise VerificationError(f"Заводской номер {actual} не совпал с Excel {self.meter}")
        self.log(event="identity_verified", factory_number=actual)

    def read_field(self, field):
        _, _, kind, address, _ = field
        if kind == "parameter":
            raw = self.read_parameter(address)[:4]
        else:
            mask = 1 << (address - 1)
            payload = self.request(0x01, struct.pack("<I", mask))
            if len(payload) != 4:
                raise ProtocolError(f"Ожидалось 4 байта Float32 в ответе канала, получено {len(payload)}")
            raw = payload
        value = struct.unpack("<f", raw)[0]
        if not math.isfinite(value) or value < 0:
            raise ProtocolError(f"Прибор вернул недопустимое значение {field[0]}: {value}")
        return value

    def write_field(self, field, value):
        _, _, kind, address, _ = field
        raw = struct.pack("<f", float32(value))
        if kind == "parameter":
            payload = struct.pack("<H", address) + raw + bytes(4)
            answer = self.request(0x0B, payload)
            if len(answer) != 2:
                raise ProtocolError("Неверная длина подтверждения параметра")
            if int.from_bytes(answer, "little") != 0:
                raise DeviceError(9)
        else:
            mask = struct.pack("<I", 1 << (address - 1))
            answer = self.request(0x03, mask + raw)
            if answer != mask:
                raise ProtocolError("Не совпала маска подтверждения записи канала")


def endpoint_port(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Порт K должен быть числом")
    if not math.isfinite(value) or value != int(value) or not 1 <= value <= 65535:
        raise ValueError("Порт K должен быть целым числом от 1 до 65535")
    return int(value)


def build_plan(path, corrections=None):
    from openpyxl import load_workbook

    corrections = corrections or {}
    for old, new in corrections.items():
        if not re.fullmatch(r"[0-9]+", old) or serial_number(new) != new:
            raise ValueError("Исправление номера должно иметь вид OLD=NEW, NEW — до 8 цифр")
    applied_corrections = []

    def meter_at(cell):
        value = cell.value
        raw = str(int(value)) if isinstance(value, (int, float)) and math.isfinite(value) and value == int(value) else str(value).strip()
        if raw in corrections:
            applied_corrections.append({"cell": cell.coordinate, "original": raw, "corrected": corrections[raw]})
            value = corrections[raw]
        return serial_number(value)

    path = Path(path)
    raw_source = path.read_bytes()
    workbook = load_workbook(BytesIO(raw_source), data_only=False)
    values = load_workbook(BytesIO(raw_source), data_only=True)
    try:
        if "Лист1" not in workbook.sheetnames:
            raise ValueError("Не найден лист «Лист1»")
        sheet, cached = workbook["Лист1"], values["Лист1"]
        headers = {"A": "Секция", "B": "Кв", "C": "ТЭ", "D": "ХВС", "E": "ГВС",
                   "G": "УМ", "H": "Канал", "I": "IP", "J": "Порт", "N": "ХВС", "O": "ГВС"}
        for column, expected in headers.items():
            if sheet[f"{column}1"].value != expected:
                raise ValueError(f"Изменён формат: {column}1 должно быть {expected!r}")
        targets, readings, reading_errors, invalid_reading_keys = {}, {}, {}, []
        for row in range(2, sheet.max_row + 1):
            cell = sheet[f"M{row}"]
            if cell.value is None:
                continue
            try:
                meter = meter_at(cell)
            except ValueError as error:
                invalid_reading_keys.append({"sheet": "Лист1", "row": row,
                                             "message": f"M{row}={cell.value!r}: {error}"})
                continue
            if meter in readings:
                raise ValueError(f"Повтор номера в M: {meter}, строки {readings[meter]['row']} и {row}")
            entry = {"row": row}
            readings[meter] = entry
            for column, name in (("N", "cold"), ("O", "hot")):
                cell = sheet[f"{column}{row}"]
                try:
                    if cell.data_type in ("e", "f"):
                        raise ValueError(f"ошибка/формула {cell.value!r}; требуется исходное число")
                    float32(cell.value)
                    entry[name] = cell.value
                except ValueError as error:
                    reading_errors.setdefault(meter, []).append(f"{column}{row}: {error}")
        jobs, skipped, warnings = [], [], invalid_reading_keys
        for row in range(2, sheet.max_row + 1):
            if all(sheet[f"{c}{row}"].value is None for c in ("B", "C", "I", "K")):
                continue
            context = {"sheet": "Лист1", "row": row, "section": sheet[f"A{row}"].value,
                       "apartment": sheet[f"B{row}"].value}
            try:
                if sheet[f"C{row}"].data_type == "f":
                    raise ValueError("Номер прибора C не должен быть формулой")
                try:
                    meter = meter_at(sheet[f"C{row}"])
                except ValueError as error:
                    raise ValueError(f"C{row}={sheet[f'C{row}'].value!r}: {error}") from error
                context["meter"] = meter
                if meter in targets:
                    raise ValueError(f"Повтор номера в C: {meter}")
                targets[meter] = row
                if meter not in readings:
                    raise ValueError("Нет показаний с таким номером в M")
                if meter in reading_errors:
                    raise ValueError("; ".join(reading_errors[meter]))
                if sheet[f"K{row}"].data_type == "f":
                    raise ValueError("Порт K не должен быть формулой")
                port = endpoint_port(sheet[f"K{row}"].value)
                ip = str(ipaddress.IPv4Address(str(sheet[f"I{row}"].value).strip()))
                entry = readings[meter]
                job = dict(context, ip=ip, port=port, readings_row=entry["row"],
                           source_meter=sheet[f"C{row}"].value,
                           values={"P00C0.F32": 0.01, "P00C3.F32": 0.01,
                                   "K9.F32": entry["cold"], "K10.F32": entry["hot"]})
                jobs.append(job)
                j_port = cached[f"J{row}"].value
                if j_port is not None and j_port != port:
                    warnings.append(dict(context, message=f"J={j_port}, K={port}: используется K по указанию пользователя"))
                if entry["cold"] == entry["hot"] == 0:
                    warnings.append(dict(context, message="Оба показания равны нулю; нули сохранены"))
            except ValueError as error:
                skipped.append(dict(context, reason=str(error)))
        # Повторный номер исключает и первую запись, чтобы порядок строк не выбирал победителя.
        duplicate_meters = {x["meter"] for x in skipped if "Повтор номера в C" in x["reason"]}
        for job in jobs[:]:
            if job["meter"] in duplicate_meters:
                jobs.remove(job)
                skipped.append({k: job[k] for k in ("sheet", "row", "section", "apartment", "meter")} |
                               {"reason": "Повтор номера в C"})
        orphaned = sorted(set(readings) - set(targets))
        if orphaned:
            warnings.append({"message": "В M есть показания без прибора в C", "meters": orphaned})
        other_sheets = []
        for other in workbook:
            if other.title != "Лист1":
                count = sum(other[f"C{r}"].value is not None for r in range(2, other.max_row + 1))
                other_sheets.append({"sheet": other.title, "meter_count": count,
                                     "reason": "Лист не входит в подтверждённый профиль A1; подключения и значения не назначены"})
        return {"profile": PROFILE, "created_at": timestamp(), "source_file": path.name,
                "source_sha256": hashlib.sha256(raw_source).hexdigest(), "units": "m3",
                "connection": "tcp-client", "port_column": "K", "jobs": jobs,
                "skipped": skipped, "warnings": warnings, "other_sheets": other_sheets,
                "serial_corrections": corrections, "applied_corrections": applied_corrections}
    finally:
        workbook.close()
        values.close()


def load_plan(path):
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    if (plan.get("profile"), plan.get("units"), plan.get("connection"), plan.get("port_column")) != (
            PROFILE, "m3", "tcp-client", "K"):
        raise ValueError("Не совпал профиль плана")
    seen = set()
    for job in plan["jobs"]:
        job["meter"] = serial_number(job["meter"])
        if job["meter"] in seen:
            raise ValueError("В плане повторяется номер прибора")
        seen.add(job["meter"])
        job["ip"] = str(ipaddress.IPv4Address(job["ip"]))
        job["port"] = endpoint_port(job["port"])
        if set(job["values"]) != {field[0] for field in FIELDS}:
            raise ValueError("В плане должны быть ровно четыре согласованных поля")
        for value in job["values"].values():
            float32(value)
        for key in ("P00C0.F32", "P00C3.F32"):
            if job["values"][key] != 0.01:
                raise ValueError("Подтверждённый вес импульса — 0.01 м³/имп")
    return plan


def apply_field(client, field, before, target, log):
    target = float32(target)
    record = {"status": "write_unverified", "before": before, "requested": target, "after": None}
    if before == target:
        return {"status": "already_matches", "before": before, "requested": target, "after": before}
    log(event="write_intent", field=field[0], before=before, requested=target)
    uncertain = None
    try:
        try:
            client.write_field(field, target)
        except (OSError, ProtocolError) as error:
            uncertain = str(error)
            record["response_error"] = uncertain
            log(event="write_response_uncertain", field=field[0], error=uncertain)
            client.close()
            # Повторяем только чтение и идентификацию; команда записи второй раз не отправляется.
            client.verify_serial()
        after = client.read_field(field)
        record["after"] = after
        log(event="readback", field=field[0], after=after, requested=target)
        if after != target:
            record["status"] = "verification_mismatch"
            record["error"] = f"После записи {after}, ожидалось {target}; повторная запись не выполнялась"
        else:
            record["status"] = "verified_after_uncertain_response" if uncertain else "written_verified"
    except DeviceError as error:
        record.update(status="device_error", error=str(error), device_error_code=error.code)
    except (OSError, ProtocolError, VerificationError) as error:
        record["error"] = str(error)
    return record


def process_job(job, write, timeout, log, client_factory=PulsarClient):
    result = {"meter": job["meter"], "ip": job["ip"], "port": job["port"],
              "sheet": job["sheet"], "row": job["row"], "apartment": job.get("apartment"),
              "status": "failed", "fields": {}, "started_at": timestamp()}
    client = client_factory(job["ip"], job["port"], job["meter"], timeout=timeout, log=log)
    try:
        client.verify_serial()
        before = {field[0]: client.read_field(field) for field in FIELDS}
        result["before"] = before
        log(event="before", values=before, targets=job["values"])
        for field in FIELDS:
            key = field[0]
            result["active_field"] = key
            if write:
                result["fields"][key] = apply_field(client, field, before[key], job["values"][key], log)
            else:
                result["fields"][key] = {"status": "read_only", "before": before[key],
                                          "requested": float32(job["values"][key])}
            log(event="field_result", field=key, **result["fields"][key])
            if result["fields"][key]["status"] in ("device_error", "verification_mismatch", "write_unverified"):
                result["error"] = result["fields"][key].get("error", "Запись не подтверждена")
                break
        else:
            result.pop("active_field", None)
            result["status"] = "verified" if write else "read_only"
    except (OSError, DeviceError, ProtocolError, VerificationError) as error:
        result["error"] = str(error)
        if isinstance(error, DeviceError):
            result["device_error_code"] = error.code
    finally:
        client.close()
    if result["status"] == "failed":
        result["completed_fields"] = [key for key, item in result["fields"].items()
                                      if item["status"] in ("already_matches", "written_verified", "verified_after_uncertain_response")]
    result["finished_at"] = timestamp()
    log(event="meter_result", result=result)
    return result


def write_json(path, data):
    with Path(path).open("x", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write("\n")


def run_plan(plan, jobs, write, output_dir, timeout):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    write_json(output_dir / "plan.json", plan)
    results = []
    with (output_dir / "events.jsonl").open("x", encoding="utf-8") as events:
        def emit(**event):
            event["time"] = timestamp()
            events.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
            events.flush()
            if event["event"] == "write_intent":
                os.fsync(events.fileno())
        emit(event="run_started", tool_version=VERSION, mode="apply" if write else "read", count=len(jobs))
        try:
            for index, job in enumerate(jobs, 1):
                def log(**event):
                    emit(meter=job["meter"], ip=job["ip"], port=job["port"], **event)
                print(f"[{index}/{len(jobs)}] {job['meter']} {job['ip']}:{job['port']}", flush=True)
                result = process_job(job, write, timeout, log)
                results.append(result)
                print(f"  {result['status']}: {result.get('error', 'OK')}", flush=True)
        except KeyboardInterrupt:
            emit(event="interrupted", message="Операция прервана. Незавершённую запись проверить чтением.")
            raise
        finally:
            write_json(output_dir / "results.json", results)
            with (output_dir / "results.csv").open("x", encoding="utf-8-sig", newline="") as output:
                writer = csv.writer(output, delimiter=";")
                writer.writerow(["Прибор", "Лист", "Строка", "Квартира", "IP", "Порт", "Результат",
                                 "Поле", "До", "План Float32", "После", "Состояние поля", "Ошибка"])
                for result in results:
                    for field in FIELDS:
                        item = result["fields"].get(field[0], {})
                        writer.writerow([result["meter"], result["sheet"], result["row"], result["apartment"],
                                         result["ip"], result["port"], result["status"], field[0],
                                         item.get("before", result.get("before", {}).get(field[0], "")),
                                         item.get("requested", ""), item.get("after", ""),
                                         item.get("status", "not_completed"), result.get("error", "")])
    return 2 if any(r["status"] == "failed" for r in results) else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Пакетная настройка «Пульсар теплосчетчик стандартный» из A1 XLSX")
    parser.add_argument("--version", action="version", version=f"Pulsar Excel Loader {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("plan", help="Проверить Excel и сохранить план; без сети")
    command.add_argument("xlsx", type=Path)
    command.add_argument("--out", required=True, type=Path)
    command.add_argument("--serial-correction", action="append", default=[], metavar="OLD=NEW",
                         help="Явно исправить номер в C и M, сохранив исходный Excel")
    for name in ("read", "apply"):
        command = commands.add_parser(name, help="Только чтение" if name == "read" else "Запись четырёх полей и проверка")
        command.add_argument("plan", type=Path)
        selection = command.add_mutually_exclusive_group(required=True)
        selection.add_argument("--meter", action="append", help="Номер прибора; можно повторить")
        selection.add_argument("--all", action="store_true", help="Все готовые записи плана")
        command.add_argument("--report-dir", type=Path, required=True, help="Новая папка для журнала и результатов")
        command.add_argument("--timeout", type=float, default=5.0, help="Время ожидания запроса, секунды")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            corrections = {}
            for item in args.serial_correction:
                old, separator, new = item.partition("=")
                if not separator or old in corrections:
                    raise ValueError("Исправления задаются как OLD=NEW без повторов OLD")
                corrections[old] = serial_number(new)
            plan = build_plan(args.xlsx, corrections)
            write_json(args.out, plan)
            print(f"Готово: {len(plan['jobs'])}; пропущено: {len(plan['skipped'])}; замечаний: {len(plan['warnings'])}.")
            for item in plan["skipped"]:
                print(f"  Пропущено {item['sheet']}:{item['row']}: {item['reason']}")
            for item in plan["other_sheets"]:
                print(f"  Лист {item['sheet']}: исключено приборов {item['meter_count']}.")
            for item in plan["applied_corrections"]:
                print(f"  Исправление {item['cell']}: {item['original']} → {item['corrected']}.")
            print(f"План: {args.out.resolve()}. Сетевых запросов не было.")
            return 0
        if not math.isfinite(args.timeout) or args.timeout <= 0:
            raise ValueError("Timeout должен быть положительным конечным числом")
        plan = load_plan(args.plan)
        jobs = plan["jobs"]
        if args.meter:
            selected = {serial_number(meter) for meter in args.meter}
            missing = selected - {job["meter"] for job in jobs}
            if missing:
                raise ValueError(f"Нет готовых заданий для: {', '.join(sorted(missing))}")
            jobs = [job for job in jobs if job["meter"] in selected]
        if not jobs:
            raise ValueError("В плане нет готовых заданий")
        print(f"Режим: {'ЗАПИСЬ' if args.command == 'apply' else 'ТОЛЬКО ЧТЕНИЕ'}. Приборов: {len(jobs)}.")
        print(f"Пропущенных строк в плане: {len(plan['skipped'])}.")
        code = run_plan(plan, jobs, args.command == "apply", args.report_dir, args.timeout)
        print(f"Отчёт: {args.report_dir.resolve()}")
        return code
    except ModuleNotFoundError as error:
        print(f"Не найдена библиотека {error.name}. Выполните: py -3 -m pip install -r requirements.txt", file=sys.stderr)
        return 2
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Остановлено. Проверяйте журнал перед повторным запуском.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
