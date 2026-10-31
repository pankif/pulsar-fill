"""Загрузка Excel и выполнение заданий для графического приложения."""

import copy
import csv
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import uuid

import pulsar_loader as core


def positive_number(text, label, as_float32=False):
    try:
        value = float(str(text).strip().replace(",", "."))
    except ValueError as error:
        raise ValueError(f"{label}: требуется положительное число") from error
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label}: требуется положительное конечное число")
    if as_float32 and core.float32(value) <= 0:
        raise ValueError(f"{label}: значение слишком мало для прибора")
    return value


def parse_corrections(text):
    corrections = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        old, separator, new = line.partition("=")
        old, new = old.strip(), new.strip()
        if not separator or not re.fullmatch(r"[0-9]+", old) or old in corrections:
            raise ValueError("Исправления: по одному СТАРЫЙ=НОВЫЙ на строку, без повторов")
        corrections[old] = core.serial_number(new)
    return corrections


def workbook_sheets(path):
    from openpyxl import load_workbook
    workbook = load_workbook(path, read_only=True)
    try:
        return workbook.sheetnames
    finally:
        workbook.close()


def load_xlsx(path, sheet, corrections, weights):
    weights = [positive_number(w, "Вес импульса", as_float32=True) for w in weights]
    plan = core.build_plan(path, corrections, sheet_name=sheet)
    plan["selected_sheet"] = sheet
    for job in plan["jobs"]:
        job["values"]["P00C0.F32"], job["values"]["P00C3.F32"] = weights
    return plan


def save_results(directory, results):
    core.write_json(directory / "results.json", results)
    with (directory / "results.csv").open("x", encoding="utf-8-sig", newline="") as output:
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["Прибор", "Лист", "Строка", "Квартира", "IP", "Порт", "Результат",
                         "Поле", "До", "План Float32", "После", "Состояние поля", "Ошибка"])
        for result in results:
            for key, *_ in core.FIELDS:
                field = result["fields"].get(key, {})
                writer.writerow([result["meter"], result["sheet"], result["row"], result["apartment"],
                                 result["ip"], result["port"], result["status"], key,
                                 field.get("before", result.get("before", {}).get(key, "")),
                                 field.get("requested", ""), field.get("after", ""),
                                 field.get("status", "not_completed"), result.get("error", "")])


def run_batch(plan, jobs, write, output_root, timeout, stop_event, notify, client_factory=core.PulsarClient):
    """Остановка допускается между приборами; текущая запись завершается с проверкой."""
    timeout = positive_number(timeout, "Таймаут")
    if not jobs:
        raise ValueError("Не выбрано ни одного прибора")
    directory = Path(output_root) / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True, exist_ok=False)
    snapshot = copy.deepcopy(plan)
    snapshot["selected_jobs"] = copy.deepcopy(jobs)
    core.write_json(directory / "plan.json", snapshot)
    results, fatal = [], None
    summary = {"report_dir": str(directory.resolve()), "mode": "write" if write else "read",
               "started_at": core.timestamp(), "total": len(jobs)}
    notify({"type": "report_created", "path": summary["report_dir"]})
    try:
        with (directory / "events.jsonl").open("x", encoding="utf-8") as events:
            def emit(**event):
                event["time"] = core.timestamp()
                events.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
                events.flush()
                if event["event"] == "write_intent":
                    os.fsync(events.fileno())
                notify({"type": "protocol", "data": event})

            emit(event="run_started", tool_version=core.VERSION, mode=summary["mode"], count=len(jobs))
            for index, job in enumerate(jobs, 1):
                if stop_event.is_set():
                    break
                notify({"type": "job_started", "job": job, "index": index, "total": len(jobs)})

                def log(**event):
                    emit(meter=job["meter"], ip=job["ip"], port=job["port"], **event)

                result = core.process_job(job, write, timeout, log, client_factory=client_factory)
                results.append(result)
                notify({"type": "job_finished", "result": result, "index": index, "total": len(jobs)})
            emit(event="run_finished", completed=len(results), stopped=stop_event.is_set())
    except Exception as error:
        fatal = error
    finally:
        summary.update(completed=len(results), failed=sum(r["status"] == "failed" for r in results),
                       not_started=[j["meter"] for j in jobs[len(results):]], finished_at=core.timestamp(),
                       status="error" if fatal else ("stopped" if len(results) < len(jobs) else "finished"))
        if fatal:
            summary["error"] = str(fatal)
            summary["notice"] = "Текущий прибор проверить чтением перед повторной записью"
        save_results(directory, results)
        core.write_json(directory / "run.json", summary)
    return summary
