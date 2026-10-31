"""Сборка Windows x64 из официальных дистрибутивов CPython, без запуска установщика."""

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile


def assemble(installer, embedded, output):
    from refinery.units.formats.archive.xtcab import xtcab
    from refinery.units.formats.msi import xtmsi
    from refinery.lib.cab import Cabinet

    output = Path(output).resolve()
    runtime = output / "runtime"
    runtime.mkdir(exist_ok=False)
    with ZipFile(embedded) as archive:
        archive.extractall(runtime)
    data = Path(installer).read_bytes()
    offsets = []
    position = 0
    while (position := data.find(b"MSCF", position)) >= 0:
        size = struct.unpack_from("<I", data, position + 8)[0]
        if 36 <= size <= len(data) - position:
            offsets.append((position, size))
        position += 4
    # Контейнеры WiX содержат манифест и MSI; имена сверяем с манифестом.
    cabinets = []
    manifest = None
    for offset, size in offsets:
        try:
            items = {x.path: x.get_data() for x in xtcab().unpack(data[offset:offset + size])}
        except Exception:
            continue
        cabinets.append(items)
        if "0" in items:
            try:
                candidate = ET.fromstring(items["0"])
                if candidate.tag.endswith("BurnManifest"):
                    manifest = candidate
            except ET.ParseError:
                pass
    if manifest is None:
        raise ValueError("Не найден манифест официального установщика")
    payload = next(e for e in manifest.iter() if e.tag.endswith("Payload") and e.get("Id") == "tcltk_AllUsers")
    msi = next(items[payload.get("SourcePath")] for items in cabinets if payload.get("SourcePath") in items)
    if hashlib.sha1(msi).hexdigest().upper() != payload.get("Hash"):
        raise ValueError("Не совпала контрольная сумма tcltk.msi из манифеста")
    streams = {x.path: x.get_data() for x in xtmsi(nocab=True).unpack(msi)}
    tables = json.loads(streams["MsiTables.json"])
    dirs = {r["Directory"]: r for r in tables["Directory"]}
    components = {r["Component"]: r for r in tables["Component"]}

    def directory(key):
        if key in ("TARGETDIR", "InstallDirectory"):
            return Path()
        row = dirs[key]
        name = row["DefaultDir"].split(":")[0].split("|")[-1]
        return directory(row["Directory_Parent"]) / ("" if name == "." else name)

    files = {r["File"]: r for r in tables["File"]}
    count = 0
    extracted = [(item.name, item.extractor()) for item in Cabinet(memoryview(streams["cab1.cab"])).process().get_files()]
    for file_id, extract in extracted:
        row = files[file_id]
        target = runtime / directory(components[row["Component"]]["Directory"]) / row["FileName"].split("|")[-1]
        # В комплекте достаточно Tkinter и его библиотек; IDLE и заголовки не нужны.
        relative = target.relative_to(runtime)
        if relative.parts[0] not in ("DLLs", "tcl") and relative.parts[:2] != ("Lib", "tkinter"):
            continue
        if target.suffix == ".lib":
            continue
        raw = bytes(extract())
        if len(raw) != row["FileSize"]:
            raise ValueError(f"Неверная длина {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        count += 1

    site = runtime / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    for distribution_name, package_name in (("openpyxl", "openpyxl"), ("et_xmlfile", "et_xmlfile")):
        distribution = importlib.metadata.distribution(distribution_name)
        package = Path(distribution.locate_file(package_name))
        shutil.copytree(package, site / package_name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        metadata = Path(distribution._path)
        shutil.copytree(metadata, site / metadata.name, ignore=shutil.ignore_patterns("__pycache__"))
    (runtime / "python312._pth").write_text("python312.zip\n.\nDLLs\nLib\nLib/site-packages\n../app\n", encoding="utf-8")
    required = ("DLLs/_tkinter.pyd", "DLLs/tcl86t.dll", "DLLs/tk86t.dll", "Lib/tkinter/__init__.py",
                "tcl/tcl8.6/init.tcl", "tcl/tk8.6/tk.tcl", "Lib/site-packages/openpyxl/__init__.py")
    for name in required:
        if not (runtime / name).is_file():
            raise ValueError(f"В комплекте нет {name}")
    provenance = {"python_version": "3.12.10", "architecture": "amd64", "tcltk_files": count,
                  "sources": [{"file": Path(p).name, "sha256": hashlib.sha256(Path(p).read_bytes()).hexdigest(),
                               "url": "https://www.python.org/ftp/python/3.12.10/" + Path(p).name}
                              for p in (installer, embedded)],
                  "packages": {n: importlib.metadata.version(n) for n in ("openpyxl", "et_xmlfile")}}
    (output / "runtime-provenance.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    env = dict(os.environ, GOOS="windows", GOARCH="amd64", CGO_ENABLED="0")
    cache = Path.cwd() / "work" / "go-cache"
    cache.mkdir(parents=True, exist_ok=True)
    env["GOCACHE"], env["GOMODCACHE"] = str(cache), str(cache / "modules")
    subprocess.run(["go", "build", "-trimpath", "-ldflags=-s -w -H=windowsgui", "-o", str(output / "PulsarLoader.exe"),
                    str(output / "source" / "launcher_windows.go")], env=env, check=True)
    print(json.dumps(provenance, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--embedded", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    assemble(args.installer, args.embedded, args.output)
