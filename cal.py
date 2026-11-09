"""Рабочий календарь (праздники, выходные, переносы) и простые справочники (вузы, национальности, языки)."""
import calendar as _cal
from datetime import date, datetime

from flask import abort, flash, redirect, render_template, request, session, url_for

import db
import extras
from extras import FIXED_HOLIDAYS, login_required, roles_required, route

M = None

DDL = """
CREATE TABLE IF NOT EXISTS work_calendar (
    day  TEXT PRIMARY KEY,
    kind TEXT NOT NULL,               -- 'off' нерабочий (праздник, перенос) или 'work' рабочий (перенесённая суббота и т.п.)
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ref_items (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ref    TEXT NOT NULL,
    name   TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(ref, name)
);
"""
HOLIDAY_NAMES = {(1, 1): "Новый год", (3, 8): "Международный женский день", (3, 21): "Навруз",
                 (5, 9): "День памяти и почестей", (9, 1): "День независимости", (10, 1): "День учителя",
                 (12, 8): "День Конституции"}
MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
REFS = {"universities": "Учебные заведения", "nationalities": "Национальности", "languages": "Языки"}
SEED = {
    "nationalities": ["O‘zbek", "Rus", "Tojik", "Qozoq", "Qoraqalpoq", "Tatar", "Qirg‘iz", "Koreys", "Ukrain", "Turkman", "Boshqa"],
    "languages": ["O‘zbek", "Rus", "Ingliz", "Qoraqalpoq", "Tojik", "Nemis", "Fransuz", "Xitoy", "Turk", "Koreys"],
    "universities": ["Toshkent davlat texnika universiteti", "Toshkent axborot texnologiyalari universiteti",
                     "O‘zbekiston Milliy universiteti", "Toshkent davlat iqtisodiyot universiteti", "Samarqand davlat universiteti"],
}


def setup(app_module):
    global M
    M = app_module
    con = db.connect()
    try:
        con.executescript(DDL)
        for ref, items in SEED.items():
            if con.execute("SELECT COUNT(*) FROM ref_items WHERE ref=?", (ref,)).fetchone()[0] == 0:
                for it in items:
                    con.execute("INSERT INTO ref_items(ref, name) VALUES(?,?)", (ref, it))
    finally:
        con.close()
    M.app.jinja_env.globals["ref_list"] = ref_list


def ref_list(ref):
    """Названия активных элементов справочника (для подсказок в формах)."""
    con = db.connect()
    try:
        return [r["name"] for r in con.execute("SELECT name FROM ref_items WHERE ref=? AND active=1 ORDER BY name", (ref,))]
    finally:
        con.close()


def overrides(con):
    return {r["day"]: r["kind"] for r in con.execute("SELECT day, kind FROM work_calendar")}


def is_off(day, six, extra, ov):
    """Нерабочий ли день: учитывает выходные, праздники, дополнительные дни из настроек и записи календаря."""
    iso = day.isoformat()
    if ov.get(iso) == "work":
        return False
    if ov.get(iso) == "off":
        return True
    return day.weekday() == 6 or (day.weekday() == 5 and not six) or (day.month, day.day) in FIXED_HOLIDAYS or iso in extra


@route("/calendar", ("GET", "POST"))
@login_required
def work_calendar():
    if session.get("role") == "employee":
        abort(403)
    year = request.values.get("year", type=int) or date.today().year
    year = min(max(year, 2000), 2100)
    con = db.connect()
    try:
        if request.method == "POST":
            if session.get("role") not in ("hr", "admin"):
                abort(403)
            act = request.form.get("act", "")
            day = request.form.get("day", "")
            try:
                datetime.strptime(day, "%Y-%m-%d")
            except ValueError:
                flash("Укажите правильную дату.", "error")
                return redirect(url_for("work_calendar", year=year))
            if act == "add":
                kind = "work" if request.form.get("kind") == "work" else "off"
                note = " ".join(request.form.get("note", "").split())[:100]
                con.execute("INSERT INTO work_calendar(day, kind, note) VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET kind=excluded.kind, note=excluded.note",
                            (day, kind, note))
                flash("Запись календаря сохранена. Табель пересчитан.", "ok")
            elif act == "delete":
                con.execute("DELETE FROM work_calendar WHERE day=?", (day,))
                flash("Запись удалена.", "ok")
            else:
                abort(400)
            return redirect(url_for("work_calendar", year=int(day[:4])))
        settings = db.get_settings(con)
        six = settings.get("work_days_per_week") == "6"
        extra = extras.holiday_set(settings)
        ov = overrides(con)
        notes = {r["day"]: r for r in con.execute("SELECT * FROM work_calendar WHERE day LIKE ? ORDER BY day", (f"{year}-%",))}
    finally:
        con.close()
    months, total = [], 0
    for m in range(1, 13):
        weeks, work = [], 0
        for wk in _cal.Calendar(0).monthdatescalendar(year, m):
            row = []
            for d in wk:
                if d.month != m:
                    row.append(None)
                    continue
                off = is_off(d, six, extra, ov)
                work += 0 if off else 1
                iso = d.isoformat()
                kind = "work" if ov.get(iso) == "work" else "off" if ov.get(iso) == "off" else \
                    "hol" if ((d.month, d.day) in FIXED_HOLIDAYS or iso in extra) else "we" if off else ""
                row.append({"n": d.day, "kind": kind, "iso": iso,
                            "tip": notes[iso]["note"] if iso in notes and notes[iso]["note"] else HOLIDAY_NAMES.get((d.month, d.day), "")})
            weeks.append(row)
        months.append({"name": MONTHS[m - 1], "weeks": weeks, "work": work})
        total += work
    fixed = sorted(((m, d, n) for (m, d), n in HOLIDAY_NAMES.items()))
    return render_template("calendar.html", year=year, months=months, total=total, notes=list(notes.values()), fixed=fixed,
                           six=six, can_edit=session.get("role") in ("hr", "admin"))


@route("/refs/<ref>", ("GET", "POST"))
@roles_required("admin")
def refs_items(ref):
    if ref not in REFS:
        abort(404)
    con = db.connect()
    try:
        if request.method == "POST":
            act = request.form.get("act", "")
            rid = request.form.get("id", type=int)
            name = " ".join(request.form.get("name", "").split())[:150]
            try:
                if act == "add":
                    if not name:
                        flash("Введите название.", "error")
                    else:
                        con.execute("INSERT INTO ref_items(ref, name) VALUES(?,?)", (ref, name))
                        flash("Добавлено.", "ok")
                elif act == "save":
                    if not name:
                        flash("Название не может быть пустым.", "error")
                    else:
                        con.execute("UPDATE ref_items SET name=?, active=? WHERE id=? AND ref=?",
                                    (name, 1 if request.form.get("active") else 0, rid, ref))
                        flash("Сохранено.", "ok")
                elif act == "delete":
                    con.execute("DELETE FROM ref_items WHERE id=? AND ref=?", (rid, ref))
                    flash("Удалено из списка. Уже заполненные карточки не изменились.", "ok")
                else:
                    abort(400)
            except Exception as ex:
                if "UNIQUE" in str(ex):
                    flash("Такое название уже есть.", "error")
                else:
                    raise
            return redirect(url_for("refs_items", ref=ref))
        rows = con.execute("SELECT * FROM ref_items WHERE ref=? ORDER BY name", (ref,)).fetchall()
    finally:
        con.close()
    return render_template("refs_items.html", ref=ref, title=REFS[ref], rows=rows, REFS=REFS)
