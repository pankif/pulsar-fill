"""Графическое приложение для теплосчётчика «Пульсар стандартный»."""

import copy
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pulsar_app as service
import pulsar_loader as core


def number(value):
    return "—" if value is None else f"{value:.8g}"


class PulsarApp:
    def __init__(self, root):
        self.root = root
        root.title(f"Пульсар · Загрузка из Excel {core.VERSION}")
        width = min(1240, root.winfo_screenwidth() - 60)
        height = min(820, root.winfo_screenheight() - 90)
        root.geometry(f"{max(1000, width)}x{max(700, height)}")
        root.minsize(1000, 700)
        self.plan, self.jobs, self.results = None, {}, {}
        self.checked = set()
        self.busy, self.running, self.closing = False, False, False
        self.preview, self.last_report = None, None
        self.messages, self.stop_event = queue.Queue(), threading.Event()
        self.path = tk.StringVar()
        self.sheet = tk.StringVar()
        self.weights = [tk.StringVar(value="0.01"), tk.StringVar(value="0.01")]
        self.timeout = tk.StringVar(value="5")
        self.report_root = tk.StringVar(value=str(Path.home() / "Documents" / "PulsarLoader" / "Reports"))
        self.search = tk.StringVar()
        self.status = tk.StringVar(value="Откройте Excel с IP, портами и начальными показаниями.")
        self.selection_status = tk.StringVar(value="Приборы не загружены")
        self.correction_text = ""
        self.controls = []
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self.callback_error
        self.search.trace_add("write", lambda *_: self.render())
        root.after(80, self.pump)

    def _build(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f5f8")
        style.configure("TLabel", background="#f3f5f8", foreground="#203045")
        style.configure("TLabelframe", background="#f3f5f8")
        style.configure("TLabelframe.Label", background="#f3f5f8", foreground="#203045")
        style.configure("TButton", padding=(10, 7))
        style.configure("Accent.TButton", background="#1c648c", foreground="white")
        style.map("Accent.TButton", background=[("active", "#287ba7"), ("disabled", "#b6c3ce")])
        style.configure("Treeview", rowheight=29, background="white", fieldbackground="white", foreground="#203045")
        style.configure("Treeview.Heading", padding=(4, 8), background="#e5ebf1")
        self.root.configure(background="#f3f5f8")
        top = tk.Frame(self.root, background="#18334a", padx=20, pady=15)
        top.pack(fill="x")
        tk.Label(top, text="ПУЛЬСАР  /  Загрузка из Excel", font=("Segoe UI", 18, "bold"),
                 background="#18334a", foreground="white").pack(anchor="w")
        tk.Label(top, text="Теплосчётчик стандартный · TCP/IP · ХВС → вход 1 · ГВС → вход 2",
                 font=("Segoe UI", 10), background="#18334a", foreground="#c7dae7").pack(anchor="w", pady=(5, 0))
        body = ttk.Frame(self.root, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(3, weight=1)
        source = ttk.LabelFrame(body, text="1. Источник", padding=10)
        source.grid(row=0, column=0, sticky="ew")
        source.columnconfigure(1, weight=1)
        self._button(source, "Открыть Excel…", self.choose_file).grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(source, textvariable=self.path, state="readonly").grid(row=0, column=1, sticky="ew")
        ttk.Label(source, text="Лист").grid(row=0, column=2, padx=(12, 6))
        self.sheet_box = ttk.Combobox(source, textvariable=self.sheet, state="readonly", width=17)
        self.sheet_box.grid(row=0, column=3)
        self.sheet_box.bind("<<ComboboxSelected>>", lambda _: self.recheck())
        self.controls.append(self.sheet_box)
        self._button(source, "Проверить заново", self.recheck).grid(row=0, column=4, padx=(8, 0))
        ttk.Label(source, text="Формат: C — номер · I — IP · K — TCP-порт · M — номер для N/O · N/O — показания в м³").grid(
            row=1, column=0, columnspan=5, sticky="w", pady=(8, 0))

        settings = ttk.LabelFrame(body, text="2. Параметры записи", padding=10)
        settings.grid(row=1, column=0, sticky="ew", pady=10)
        for index in range(2):
            ttk.Label(settings, text=f"Вес импульса входа {index + 1}").grid(row=0, column=index * 3, padx=(0, 5))
            entry = ttk.Entry(settings, textvariable=self.weights[index], width=9)
            entry.grid(row=0, column=index * 3 + 1)
            self.controls.append(entry)
            ttk.Label(settings, text="м³/имп").grid(row=0, column=index * 3 + 2, padx=(5, 16))
        ttk.Label(settings, text="Таймаут, с").grid(row=0, column=6, padx=(0, 5))
        entry = ttk.Entry(settings, textvariable=self.timeout, width=6)
        entry.grid(row=0, column=7)
        self.controls.append(entry)
        settings.columnconfigure(8, weight=1)
        self._button(settings, "Исправления номеров…", self.corrections_dialog).grid(row=0, column=9, padx=(12, 0))

        select = ttk.Frame(body)
        select.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(select, text="3. Приборы").pack(side="left")
        self._button(select, "Выбрать видимые", self.select_all).pack(side="left", padx=(12, 6))
        self._button(select, "Снять выбор", self.select_none).pack(side="left")
        ttk.Label(select, textvariable=self.selection_status).pack(side="left", padx=12)
        search = ttk.Entry(select, textvariable=self.search, width=24)
        search.pack(side="right")
        ttk.Label(select, text="Поиск: номер / квартира / IP  ").pack(side="right")

        self.tabs = ttk.Notebook(body)
        self.tabs.grid(row=3, column=0, sticky="nsew")
        page = ttk.Frame(self.tabs)
        page.rowconfigure(0, weight=1)
        page.columnconfigure(0, weight=1)
        self.tabs.add(page, text="Приборы")
        columns = ("check", "row", "apartment", "meter", "endpoint", "cold", "hot", "currentcold", "currenthot", "status")
        self.table = ttk.Treeview(page, columns=columns, show="headings", selectmode="browse")
        headings = ("✓", "Строка", "Квартира", "Номер Пульсар", "IP : порт K", "ХВС Excel", "ГВС Excel", "ХВС прибор", "ГВС прибор", "Состояние")
        widths = (34, 53, 70, 105, 172, 85, 85, 85, 85, 145)
        for col, title, width in zip(columns, headings, widths):
            self.table.heading(col, text=title)
            self.table.column(col, width=width, minwidth=width, stretch=col in ("endpoint", "status"))
        self.table.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(page, orient="vertical", command=self.table.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.table.configure(yscrollcommand=scrollbar.set)
        horizontal = ttk.Scrollbar(page, orient="horizontal", command=self.table.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table.configure(xscrollcommand=horizontal.set)
        self.table.tag_configure("failed", foreground="#a52929")
        self.table.tag_configure("verified", foreground="#237343")
        self.table.bind("<Button-1>", self.table_click)
        self.table.bind("<space>", self.table_space)
        self.table.bind("<<TreeviewSelect>>", lambda _: self.show_detail())
        self.detail = tk.Text(page, height=4, wrap="word", background="#edf2f6", relief="flat", padx=10, pady=8,
                              font=("Segoe UI", 10))
        self.detail.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self._replace(self.detail, "Отметьте приборы в первой колонке. При выборе строки здесь появятся параметры и результат.")
        self.issues = self._text_tab("Проверка Excel")
        self.journal = self._text_tab("Журнал")

        footer = ttk.Frame(body)
        footer.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        footer.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(footer, mode="determinate")
        self.progress.grid(row=0, column=0, columnspan=5, sticky="ew", pady=(0, 8))
        ttk.Label(footer, textvariable=self.status, wraplength=1120).grid(row=1, column=0, columnspan=5, sticky="w", pady=(0, 8))
        self.read_button = self._button(footer, "Прочитать выбранные", lambda: self.start_run(False))
        self.read_button.grid(row=2, column=0, sticky="w")
        self.write_button = self._button(footer, "Записать выбранные…", lambda: self.start_run(True), "Accent.TButton")
        self.write_button.grid(row=2, column=1, padx=8)
        self.stop_button = ttk.Button(footer, text="Стоп после текущего", command=self.stop)
        self.stop_button.grid(row=2, column=2)
        self.report_button = ttk.Button(footer, text="Открыть отчёт", command=self.open_report)
        self.report_button.grid(row=2, column=3, padx=8)
        self._button(footer, "Папка отчётов…", self.choose_reports).grid(row=2, column=4)
        self._update_buttons()

    def _button(self, parent, text, command, style="TButton"):
        button = ttk.Button(parent, text=text, command=command, style=style)
        self.controls.append(button)
        return button

    def _text_tab(self, label):
        page = ttk.Frame(self.tabs)
        self.tabs.add(page, text=label)
        text = tk.Text(page, wrap="word", padx=12, pady=10, background="white", relief="flat", font=("Segoe UI", 10))
        scroll = ttk.Scrollbar(page, command=text.yview)
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        text.configure(yscrollcommand=scroll.set, state="disabled")
        return text

    @staticmethod
    def _replace(widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")

    def append_log(self, text):
        self.journal.configure(state="normal")
        self.journal.insert("end", text + "\n")
        if int(self.journal.index("end-1c").split(".")[0]) > 1500:
            self.journal.delete("1.0", "200.0")
        self.journal.see("end")
        self.journal.configure(state="disabled")

    def _update_buttons(self):
        for button in (self.read_button, self.write_button):
            button.state(["!disabled"] if self.checked and not self.busy else ["disabled"])
        self.stop_button.state(["!disabled"] if self.running and not self.stop_event.is_set() else ["disabled"])
        self.report_button.state(["!disabled"] if self.last_report else ["disabled"])
        self.selection_status.set(f"Готово: {len(self.jobs)} · Выбрано: {len(self.checked)}")

    def _busy(self, value):
        self.busy = value
        for widget in self.controls:
            widget.state(["disabled"] if value else ["!disabled"])
        self._update_buttons()

    def _launch(self, kind, work):
        self._busy(True)

        def worker():
            try:
                self.messages.put({"type": "done", "kind": kind, "value": work()})
            except Exception as error:
                self.messages.put({"type": "error", "kind": kind, "error": str(error), "trace": traceback.format_exc()})

        threading.Thread(target=worker, name="PulsarWorker", daemon=True).start()

    def choose_file(self):
        path = filedialog.askopenfilename(title="Excel с приборами", filetypes=[("Excel", "*.xlsx"), ("Все файлы", "*.*")])
        if path:
            self.open_file(path)

    def open_file(self, path):
        if self.busy:
            return
        self.path.set(str(Path(path).resolve()))
        self.plan, self.jobs, self.results, self.checked = None, {}, {}, set()
        self.correction_text = ""
        self.search.set("")
        self._replace(self.issues, "Читаю новый файл…")
        self._replace(self.detail, "")
        self.render()
        self.status.set("Читаю список листов…")
        self._launch("sheets", lambda: service.workbook_sheets(path))

    def options(self):
        weights = [service.positive_number(w.get(), f"Вес входа {i + 1}", True) for i, w in enumerate(self.weights)]
        timeout = service.positive_number(self.timeout.get(), "Таймаут")
        return weights, timeout

    def recheck(self):
        if self.busy or not self.path.get():
            return
        try:
            weights, _ = self.options()
            corrections = service.parse_corrections(self.correction_text)
        except ValueError as error:
            messagebox.showerror("Параметры", str(error), parent=self.root)
            return
        path, sheet = self.path.get(), self.sheet.get()
        self.plan, self.jobs, self.results, self.checked = None, {}, {}, set()
        self._replace(self.issues, "Проверяю выбранный лист…")
        self._replace(self.detail, "")
        self.render()
        self.status.set("Проверяю Excel и сопоставляю показания по номеру…")
        self._launch("plan", lambda: service.load_xlsx(path, sheet, corrections, weights))

    def accept_plan(self, plan):
        self.plan = plan
        self.jobs = {j["meter"]: j for j in plan["jobs"]}
        self.results, self.checked = {}, set()
        issues = [f"Файл: {plan['source_file']} · Лист: {plan['selected_sheet']}",
                  f"Готово: {len(self.jobs)} · Исключено: {len(plan['skipped'])}", ""]
        for item in plan["skipped"]:
            issues.append(f"ИСКЛЮЧЕНО · строка {item['row']} · кв. {item.get('apartment', '—')}: {item['reason']}")
        for item in plan["warnings"]:
            issues.append(f"Замечание · строка {item.get('row', '—')}: {item['message']}")
        for item in plan["applied_corrections"]:
            issues.append(f"Исправлено в плане: {item['cell']} · {item['original']} → {item['corrected']}")
        for item in plan["other_sheets"]:
            issues.append(f"Другой лист: {item['sheet']} · приборов {item['meter_count']} · не выбран")
        self._replace(self.issues, "\n".join(issues))
        self.render()
        self.status.set(f"Загружено {len(self.jobs)} приборов. Исключено строк: {len(plan['skipped'])}. Отметьте нужные приборы.")
        self.append_log(self.status.get())

    def visible_jobs(self):
        query = self.search.get().strip().lower()
        return [j for j in self.jobs.values() if not query or query in
                f"{j['meter']} {j.get('apartment', '')} {j.get('section', '')} {j['ip']}:{j['port']}".lower()]

    def render(self):
        if not hasattr(self, "table"):
            return
        focus = self.table.selection()
        children = self.table.get_children()
        if children:
            self.table.delete(*children)
        for job in self.visible_jobs():
            self._insert_row(job)
        if focus and self.table.exists(focus[0]):
            self.table.selection_set(focus[0])
        self._update_buttons()

    def _insert_row(self, job):
        meter = job["meter"]
        result = self.results.get(meter, {})
        status = result.get("status", "ready")
        labels = {"ready": "Готов", "pending": "Ожидает", "working": "Подключение…", "read_only": "Прочитан",
                  "verified": "Запись проверена", "failed": "Ошибка", "not_started": "Не начат", "uncertain": "Проверить чтением"}
        actual = {k: f.get("after", f.get("before")) for k, f in result.get("fields", {}).items()}
        actual = result.get("before", {}) | {k: v for k, v in actual.items() if v is not None}
        values = ("☑" if meter in self.checked else "☐", job["row"], job.get("apartment", ""), meter,
                  f"{job['ip']}:{job['port']}", number(job["values"]["K9.F32"]), number(job["values"]["K10.F32"]),
                  number(actual.get("K9.F32")), number(actual.get("K10.F32")), labels.get(status, status))
        if self.table.exists(meter):
            self.table.item(meter, values=values, tags=(status,))
        else:
            self.table.insert("", "end", iid=meter, values=values, tags=(status,))

    def toggle_row(self, meter):
        if self.busy or meter not in self.jobs:
            return
        self.checked.symmetric_difference_update({meter})
        self._insert_row(self.jobs[meter])
        self._update_buttons()

    def select_all(self):
        if not self.busy:
            self.checked.update(j["meter"] for j in self.visible_jobs())
            self.render()

    def select_none(self):
        if not self.busy:
            self.checked.clear()
            self.render()

    def table_click(self, event):
        if self.table.identify_column(event.x) == "#1":
            self.toggle_row(self.table.identify_row(event.y))

    def table_space(self, _):
        selected = self.table.selection()
        if selected:
            self.toggle_row(selected[0])
        return "break"

    def show_detail(self):
        selection = self.table.selection()
        if not selection or selection[0] not in self.jobs:
            return
        meter = selection[0]
        job, result = self.jobs[meter], self.results.get(meter, {})
        lines = [f"Прибор {meter} · секция {job.get('section', '—')} · кв. {job.get('apartment', '—')} · строка показаний {job['readings_row']}"]
        for key, label, _, _, unit in core.FIELDS:
            item = result.get("fields", {}).get(key, {})
            before = item.get("before", result.get("before", {}).get(key))
            lines.append(f"{label}: план {number(job['values'][key])} {unit} · до {number(before)} · после {number(item.get('after'))}")
        if result.get("error"):
            lines.append("Ошибка: " + result["error"])
        self._replace(self.detail, "\n".join(lines))

    def corrections_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Явные исправления номеров")
        dialog.geometry("540x330")
        dialog.transient(self.root)
        dialog.grab_set()
        ttk.Label(dialog, text="По одному СТАРЫЙ=НОВЫЙ на строку.\nИсправление применяется в C и M только для этого файла.\nИсходный Excel не изменяется.", padding=12).pack(anchor="w")
        text = tk.Text(dialog, height=7, padx=10, pady=8)
        text.pack(fill="both", expand=True, padx=12)
        text.insert("1.0", self.correction_text)

        def save():
            candidate = text.get("1.0", "end").strip()
            try:
                service.parse_corrections(candidate)
            except ValueError as error:
                messagebox.showerror("Номер", str(error), parent=dialog)
                return
            self.correction_text = candidate
            dialog.destroy()
            self.recheck()

        ttk.Button(dialog, text="Применить и проверить Excel", command=save).pack(pady=12)

    def start_run(self, write):
        if self.busy or not self.plan or not self.checked:
            return
        try:
            weights, timeout = self.options()
            if not self.report_root.get().strip():
                raise ValueError("Выберите папку отчётов")
        except ValueError as error:
            messagebox.showerror("Параметры", str(error), parent=self.root)
            return
        plan = copy.deepcopy(self.plan)
        for job in plan["jobs"]:
            job["values"]["P00C0.F32"], job["values"]["P00C3.F32"] = weights
            self.jobs[job["meter"]]["values"] = dict(job["values"])
        jobs = [j for j in plan["jobs"] if j["meter"] in self.checked]
        output_root = self.report_root.get()
        self.show_detail()
        if write:
            self.write_preview(plan, jobs, timeout, output_root)
        else:
            self.begin_run(plan, jobs, False, timeout, output_root)

    def write_preview(self, plan, jobs, timeout, output_root):
        dialog = tk.Toplevel(self.root)
        self.preview = dialog
        dialog.title("Проверка перед записью")
        dialog.geometry("850x560")
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"Будут записаны {len(jobs)} приборов", font=("Segoe UI", 15, "bold")).pack(anchor="w")
        ttk.Label(frame, text=f"{plan['source_file']} · {plan['selected_sheet']}\nСначала веса обоих входов, затем ХВС и ГВС в м³. После каждого поля — чтение для проверки.", wraplength=800).pack(anchor="w", pady=10)
        preview = tk.Text(frame, wrap="none", padx=10, pady=8)
        scroll = ttk.Scrollbar(frame, command=preview.yview)
        scroll.pack(side="right", fill="y")
        preview.pack(fill="both", expand=True)
        preview.configure(yscrollcommand=scroll.set)
        lines = ["Квартира | Номер | IP:порт | Вес 1 | Вес 2 | ХВС | ГВС"]
        for job in jobs:
            values = job["values"]
            lines.append(f"{job.get('apartment', '—')} | {job['meter']} | {job['ip']}:{job['port']} | " +
                         " | ".join(number(values[f[0]]) for f in core.FIELDS))
        self._replace(preview, "\n".join(lines))
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(12, 0))

        def confirm():
            dialog.destroy()
            self.preview = None
            self.begin_run(plan, jobs, True, timeout, output_root)

        ttk.Button(actions, text="Отмена", command=dialog.destroy).pack(side="right")
        ttk.Button(actions, text="Записать эти приборы", style="Accent.TButton", command=confirm).pack(side="right", padx=10)

    def begin_run(self, plan, jobs, write, timeout, output_root):
        self.running = True
        self.stop_event.clear()
        for job in jobs:
            self.results[job["meter"]] = {"status": "pending"}
        self.render()
        self.progress.configure(maximum=len(jobs), value=0)
        self.status.set(f"{'Запись' if write else 'Чтение'} · приборов {len(jobs)}")
        self.append_log(self.status.get())
        self._launch("run", lambda: service.run_batch(plan, jobs, write, output_root, timeout, self.stop_event, self.messages.put))

    def stop(self):
        self.stop_event.set()
        self.status.set("Останавливаю после текущего прибора. Его чтение и проверка записи будут завершены.")
        self._update_buttons()

    def pump(self):
        try:
            for _ in range(200):
                event = self.messages.get_nowait()
                kind = event["type"]
                if kind == "done":
                    self.running = False
                    self._busy(False)
                    if event["kind"] == "sheets":
                        sheets = event["value"]
                        self.sheet_box.configure(values=sheets)
                        self.sheet.set("Лист1" if "Лист1" in sheets else sheets[0])
                        self.recheck()
                    elif event["kind"] == "plan":
                        self.accept_plan(event["value"])
                    else:
                        summary = event["value"]
                        for index, meter in enumerate(summary["not_started"]):
                            status = "uncertain" if index == 0 and summary["status"] == "error" else "not_started"
                            self.results[meter] = {"status": status, "error": summary.get("error", "")}
                            if self.table.exists(meter):
                                self._insert_row(self.jobs[meter])
                        label = {"finished": "Завершено", "stopped": "Остановлено", "error": "Ошибка выполнения"}[summary["status"]]
                        self.status.set(f"{label} · обработано {summary['completed']}/{summary['total']} · ошибок {summary['failed']} · не начато {len(summary['not_started'])}")
                        self.append_log(self.status.get() + "\nОтчёт: " + summary["report_dir"])
                        if summary.get("error"):
                            self.append_log(summary["error"] + "\n" + summary["notice"])
                            messagebox.showerror("Выполнение остановлено", summary["error"] + "\n" + summary["notice"], parent=self.root)
                elif kind == "error":
                    self.running = False
                    self._busy(False)
                    self.status.set("Ошибка: " + event["error"])
                    self.append_log(event["trace"])
                    messagebox.showerror("Пульсар", event["error"], parent=self.root)
                elif kind == "report_created":
                    self.last_report = event["path"]
                    self._update_buttons()
                elif kind == "job_started":
                    job = event["job"]
                    self.results[job["meter"]] = {"status": "working"}
                    if self.table.exists(job["meter"]):
                        self._insert_row(self.jobs[job["meter"]])
                        self.table.see(job["meter"])
                    self.status.set(f"{event['index']}/{event['total']} · {job['meter']} · {job['ip']}:{job['port']}")
                elif kind == "job_finished":
                    result = event["result"]
                    self.results[result["meter"]] = result
                    if self.table.exists(result["meter"]):
                        self._insert_row(self.jobs[result["meter"]])
                    self.progress.configure(value=event["index"])
                    self.append_log(f"{event['index']}/{event['total']} · {result['meter']} · " +
                                    {"read_only": "Прочитан", "verified": "Запись проверена", "failed": "Ошибка"}.get(result["status"], result["status"]) +
                                    (": " + result["error"] if result.get("error") else ""))
                    self.show_detail()
                elif kind == "protocol" and event["data"]["event"] == "write_response_uncertain":
                    data = event["data"]
                    self.append_log(f"{data['meter']} · Нет подтверждения {data['field']}: проверяю чтением. Повторной записи нет.")
        except queue.Empty:
            pass
        if self.closing and not self.busy:
            self.root.destroy()
            return
        self.root.after(80, self.pump)

    def choose_reports(self):
        path = filedialog.askdirectory(title="Папка отчётов")
        if path:
            self.report_root.set(path)
            self.status.set("Отчёты будут сохраняться в " + path)

    def open_report(self):
        if self.last_report:
            if sys.platform == "win32":
                os.startfile(self.last_report)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", self.last_report])

    def close(self):
        if self.busy:
            if messagebox.askyesno("Работа продолжается", "Дождаться текущего прибора и закрыть приложение?", parent=self.root):
                self.closing = True
                self.stop()
        else:
            self.root.destroy()

    def callback_error(self, exc_type, exc_value, tb):
        detail = "".join(traceback.format_exception(exc_type, exc_value, tb))
        self.append_log(detail)
        if sys.stderr:
            sys.stderr.write(detail)
        messagebox.showerror("Ошибка интерфейса", str(exc_value), parent=self.root)


def main():
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.SetProcessDPIAware()
    root = tk.Tk()
    PulsarApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
