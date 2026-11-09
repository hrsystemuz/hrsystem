"""Резервные копии: ежедневная автоматическая (база) и ручная (база + все прикреплённые файлы)."""
import io
import re
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from flask import abort, flash, redirect, render_template, request, send_file, url_for

import db
import extras
from extras import admin_required, route

M = None
KEEP = {"auto": 14, "manual": 10}
_last_day = {"d": ""}


def setup(app_module):
    global M
    M = app_module

    @M.app.before_request
    def _daily():
        d = db.now_local().strftime("%Y-%m-%d")
        if _last_day["d"] == d:
            return
        _last_day["d"] = d
        try:
            if not any(p.name.startswith("auto_" + d) for p in _dir().glob("auto_*.zip")):
                make("auto")
        except Exception:                                                      # noqa: BLE001  копия не должна ломать работу
            pass


def _dir():
    p = db.DB_PATH.parent / "backups"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _snapshot(path):
    """Согласованный снимок базы (безопасно, даже если в базу сейчас пишут)."""
    src = sqlite3.connect(db.DB_PATH)
    dst = sqlite3.connect(path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def make(kind):
    """Создать архив. Возвращает путь."""
    stamp = db.now_local().strftime("%Y-%m-%d_%H%M%S")
    out = _dir() / f"{kind}_{stamp}.zip"
    with tempfile.TemporaryDirectory() as td:
        snap = Path(td) / "hr.db"
        _snapshot(snap)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, "hr.db")
            if kind == "manual":
                fd = extras.files_dir()
                for f in fd.iterdir():
                    if f.is_file():
                        z.write(f, "files/" + f.name)
            z.writestr("README.txt", "Копия кадровой программы от " + db.now_local().strftime("%d.%m.%Y %H:%M") +
                       ".\nhr.db - база данных (поместить в папку data программы).\nfiles - прикреплённые файлы (в папку data\\files).\n")
    for k, n in KEEP.items():                      # старые копии удаляем
        old = sorted(_dir().glob(f"{k}_*.zip"), reverse=True)[n:]
        for p in old:
            p.unlink(missing_ok=True)
    return out


def _list():
    rows = []
    for p in sorted(_dir().glob("*.zip"), reverse=True):
        m = re.match(r"(auto|manual)_(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})", p.name)
        if not m:
            continue
        rows.append({"name": p.name, "kind": "авто" if m.group(1) == "auto" else "вручную",
                     "when": f"{m.group(4)}.{m.group(3)}.{m.group(2)} {m.group(5)}:{m.group(6)}",
                     "size": f"{p.stat().st_size / 1048576:.1f} МБ" if p.stat().st_size > 1048576 else f"{max(p.stat().st_size // 1024, 1)} КБ"})
    return rows


@route("/backup")
@admin_required
def backup_page():
    return render_template("backup.html", rows=_list(), folder=str(_dir()), keep=KEEP)


@route("/backup/make", ("POST",))
@admin_required
def backup_make():
    p = make("manual")
    flash("Копия создана: " + p.name, "ok")
    return redirect(url_for("backup_download", name=p.name))


@route("/backup/download/<name>")
@admin_required
def backup_download(name):
    if not re.fullmatch(r"(auto|manual)_[0-9_-]+\.zip", name):
        abort(404)
    p = _dir() / name
    if not p.exists():
        abort(404)
    return send_file(p, as_attachment=True, download_name="kadry_" + name)
