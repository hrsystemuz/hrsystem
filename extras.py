"""Дополнительные разделы: файлы и связи приказов, группы согласующих,
табель, график отпусков, выгрузки в Excel (CSV).

Маршруты регистрируются из app.py вызовом extras.setup(app_module).
"""
import calendar
import csv
import io
import json
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Response, abort, flash, redirect, render_template, request, send_file, session, url_for

import db

M = None            # модуль app.py; задаётся в setup()
_ROUTES = []

ALLOWED_EXT = {"pdf", "jpg", "jpeg", "png", "doc", "docx", "xls", "xlsx"}
MAX_FILE = 10 * 1024 * 1024

# праздники Узбекистана с постоянной датой (переменные даты хайитов добавляются в настройках)
FIXED_HOLIDAYS = {(1, 1), (3, 8), (3, 21), (5, 9), (9, 1), (10, 1), (12, 8)}

VAC_CODE = {"annual": "О", "educational": "У", "unpaid": "ДО", "childcare": "ОЖ", "social": "ДО"}
MANUAL_CODES = {"Я": "Явка", "Б": "Больничный", "ПР": "Прогул", "ДО": "Отпуск без сохранения",
                "О": "Отпуск ежегодный", "У": "Учебный отпуск", "К": "Командировка", "В": "Выходной"}
CODE_NAMES = {**MANUAL_CODES, "ОЖ": "Отпуск по уходу за ребёнком"}


# ---------- вспомогательное ----------

def route(rule, methods=("GET",)):
    def deco(view):
        _ROUTES.append((rule, view, list(methods)))
        return view
    return deco


def login_required(view):
    @wraps(view)
    def w(*a, **k):
        return M.login_required(view)(*a, **k)
    return w


def roles_required(*roles):
    def deco(view):
        @wraps(view)
        def w(*a, **k):
            return M.roles_required(*roles)(view)(*a, **k)
        return w
    return deco


def admin_required(view):
    @wraps(view)
    def w(*a, **k):
        return M.admin_required(view)(*a, **k)
    return w


def setup(app_module):
    global M
    M = app_module
    app = M.app
    app.config["MAX_CONTENT_LENGTH"] = MAX_FILE + 2 * 1024 * 1024
    for rule, view, methods in _ROUTES:
        app.add_url_rule(rule, view.__name__, view, methods=methods)
    con = db.connect()
    try:
        con.executescript(DOC_LOG_DDL)
    finally:
        con.close()


DOC_LOG_DDL = """
CREATE TABLE IF NOT EXISTS doc_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    src     TEXT NOT NULL,
    doc_id  INTEGER NOT NULL,
    at      TEXT NOT NULL,
    actor   TEXT NOT NULL DEFAULT '',
    what    TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_doc_log ON doc_log(src, doc_id);
"""


def user_name(con, login_):
    u = con.execute("SELECT full_name FROM users WHERE login=?", (login_,)).fetchone()
    return (u["full_name"] if u and u["full_name"] else login_)


def log_event(con, src, doc_id, what, comment=""):
    """Записать действие в «Ход документа»: кто (текущий пользователь), что и когда."""
    con.execute("INSERT INTO doc_log(src, doc_id, at, actor, what, comment) VALUES(?,?,?,?,?,?)",
                (src, doc_id, db.now_local().strftime("%Y-%m-%d %H:%M"), user_name(con, session["user"]), what, comment or ""))


def log_events(con, src, doc_id):
    return [{"at": r["at"], "who": r["actor"], "what": r["what"], "comment": r["comment"]}
            for r in con.execute("SELECT * FROM doc_log WHERE src=? AND doc_id=? ORDER BY id", (src, doc_id))]


def people(con):
    """Кого можно выбрать подписантом или исполнителем: пользователи с данными сотрудника (подразделение, табельный номер)."""
    out = []
    for u in con.execute(
            """SELECT u.login, u.full_name, u.title, u.role, e.department, e.position, e.tabel_no,
                      e.last_name, e.first_name, e.middle_name
               FROM users u LEFT JOIN employees e ON e.id = u.employee_id
               WHERE u.role <> 'employee' ORDER BY u.full_name, u.login"""):
        fio = " ".join(x for x in (u["last_name"], u["first_name"], u["middle_name"]) if x)
        out.append({"login": u["login"], "name": u["full_name"] or fio or u["login"],
                    "title": u["title"] or u["position"] or "", "role": u["role"],
                    "dept": u["department"] or "", "tabel": u["tabel_no"] or ""})
    return out


def parse_steps(form, prefix="step"):
    """Строки таблицы участников из формы: [(login, action, queue|None, due)]."""
    import datetime
    logins, actions = form.getlist(prefix + "_login"), form.getlist(prefix + "_action")
    queues, dues = form.getlist(prefix + "_queue"), form.getlist(prefix + "_due")
    rows = []
    for i, (lg, act) in enumerate(zip(logins, actions)):
        lg = lg.strip()
        if not lg:
            continue
        q = None
        if i < len(queues) and queues[i].strip().isdigit():
            q = min(int(queues[i].strip()), 9999)
        due = dues[i].strip() if i < len(dues) else ""
        try:
            datetime.date.fromisoformat(due)
        except ValueError:
            due = ""
        rows.append((lg, act, q, due))
    return rows


def files_dir():
    p = db.DB_PATH.parent / "files"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _order_open(con, order_id):
    order = M._get_order(con, order_id)
    return order


def csv_response(name, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(header)
    w.writerows(rows)
    data = "﻿" + buf.getvalue()          # BOM: Excel правильно читает кириллицу
    return Response(data, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---------- файлы и связи приказов ----------

def _clean_name(name):
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", name or "").strip(" .")
    return name[-120:] or "file"


@route("/order/<int:order_id>/files", ("POST",))
@roles_required("hr", "admin")
def order_file_upload(order_id):
    f = request.files.get("file")
    con = db.connect()
    try:
        order = M._get_order(con, order_id)
        if f is None or not f.filename:
            flash("Выберите файл.", "error")
        else:
            name = _clean_name(f.filename)
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            blob = f.read(MAX_FILE + 1)
            if ext not in ALLOWED_EXT:
                flash("Такой тип файла нельзя: разрешены pdf, jpg, png, doc, docx, xls, xlsx.", "error")
            elif len(blob) > MAX_FILE:
                flash("Файл больше 10 МБ.", "error")
            elif not blob:
                flash("Файл пустой.", "error")
            else:
                stored = secrets.token_hex(16) + "." + ext
                (files_dir() / stored).write_bytes(blob)
                con.execute("INSERT INTO order_files(order_id, filename, stored, size, uploaded_by, uploaded_at) "
                            "VALUES(?,?,?,?,?,?)",
                            (order_id, name, stored, len(blob), session["user"],
                             db.now_local().strftime("%Y-%m-%d %H:%M")))
                flash("Файл прикреплён.", "ok")
    finally:
        con.close()
    return redirect(url_for("order_view", order_id=order_id))


@route("/file/<int:file_id>")
@login_required
def order_file(file_id):
    con = db.connect()
    try:
        row = con.execute("SELECT * FROM order_files WHERE id=?", (file_id,)).fetchone()
    finally:
        con.close()
    path = files_dir() / row["stored"] if row else None
    if row is None or not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=row["filename"])


@route("/file/<int:file_id>/delete", ("POST",))
@roles_required("hr", "admin")
def order_file_delete(file_id):
    con = db.connect()
    try:
        row = con.execute("SELECT f.*, o.status FROM order_files f JOIN orders o ON o.id=f.order_id WHERE f.id=?",
                          (file_id,)).fetchone()
        if row is None:
            abort(404)
        if row["status"] not in ("draft", "returned"):
            flash("Файлы подписанного приказа убрать нельзя.", "error")
        else:
            con.execute("DELETE FROM order_files WHERE id=?", (file_id,))
            try:
                (files_dir() / row["stored"]).unlink()
            except OSError:
                pass
            flash("Файл убран.", "ok")
        oid = row["order_id"]
    finally:
        con.close()
    return redirect(url_for("order_view", order_id=oid))


@route("/order/<int:order_id>/link", ("POST",))
@roles_required("hr", "admin")
def order_link(order_id):
    other = request.form.get("other", type=int)
    con = db.connect()
    try:
        M._get_order(con, order_id)
        if not other or other == order_id or con.execute("SELECT 1 FROM orders WHERE id=?", (other,)).fetchone() is None:
            flash("Выберите другой приказ.", "error")
        else:
            a, b = sorted((order_id, other))
            con.execute("INSERT OR IGNORE INTO order_links(order_id, linked_order_id) VALUES(?,?)", (a, b))
            flash("Приказы связаны.", "ok")
    finally:
        con.close()
    return redirect(url_for("order_view", order_id=order_id))


@route("/order/<int:order_id>/unlink", ("POST",))
@roles_required("hr", "admin")
def order_unlink(order_id):
    other = request.form.get("other", type=int) or 0
    a, b = sorted((order_id, other))
    con = db.connect()
    try:
        con.execute("DELETE FROM order_links WHERE order_id=? AND linked_order_id=?", (a, b))
        flash("Связь убрана.", "ok")
    finally:
        con.close()
    return redirect(url_for("order_view", order_id=order_id))


# ---------- группы согласующих ----------

@route("/groups", ("GET", "POST"))
@admin_required
def groups():
    con = db.connect()
    try:
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add_group":
                name = " ".join(request.form.get("name", "").split())
                if not name:
                    flash("Укажите название группы.", "error")
                else:
                    try:
                        con.execute("INSERT INTO signer_groups(name) VALUES(?)", (name,))
                        flash(f"Группа «{name}» создана. Добавьте в неё участников.", "ok")
                    except sqlite3.IntegrityError:
                        flash("Такая группа уже есть.", "error")
            elif action == "delete_group":
                con.execute("DELETE FROM signer_group_members WHERE group_id=?", (request.form.get("id", type=int),))
                con.execute("DELETE FROM signer_groups WHERE id=?", (request.form.get("id", type=int),))
                flash("Группа удалена. В уже составленных приказах согласующие остаются.", "ok")
            elif action == "add_member":
                gid = request.form.get("group_id", type=int)
                login_ = request.form.get("login", "")
                act = request.form.get("action_kind", "rozilik")
                if act not in db.STEP_ACTIONS:
                    act = "rozilik"
                user = con.execute("SELECT * FROM users WHERE login=?", (login_,)).fetchone()
                if user is None or con.execute("SELECT 1 FROM signer_groups WHERE id=?", (gid,)).fetchone() is None:
                    flash("Выберите группу и пользователя.", "error")
                elif user["role"] == "hr":
                    flash("Кадровик составляет приказы, его нельзя назначить согласующим.", "error")
                else:
                    nxt = con.execute("SELECT COALESCE(MAX(ord), 0) + 1 FROM signer_group_members WHERE group_id=?",
                                      (gid,)).fetchone()[0]
                    con.execute("INSERT INTO signer_group_members(group_id, login, action, ord) VALUES(?,?,?,?)",
                                (gid, login_, act, nxt))
                    flash("Участник добавлен.", "ok")
            elif action == "remove_member":
                con.execute("DELETE FROM signer_group_members WHERE id=?", (request.form.get("id", type=int),))
                flash("Участник убран из группы.", "ok")
            return redirect(url_for("groups"))
        rows = []
        for g in con.execute("SELECT * FROM signer_groups ORDER BY name"):
            members = con.execute(
                """SELECT m.*, u.full_name, u.title, u.role FROM signer_group_members m
                   LEFT JOIN users u ON u.login=m.login WHERE m.group_id=? ORDER BY m.ord, m.id""",
                (g["id"],)).fetchall()
            rows.append({"g": g, "members": members})
        users = con.execute("SELECT * FROM users WHERE role<>'hr' ORDER BY full_name, login").fetchall()
    finally:
        con.close()
    return render_template("groups.html", rows=rows, users=users, actions=db.STEP_ACTIONS)


# ---------- табель ----------

def _month_arg():
    text = request.args.get("month", "")
    if re.fullmatch(r"\d{4}-\d{2}", text):
        y, m = int(text[:4]), int(text[5:])
        if 1 <= m <= 12:
            return y, m
    t = date.today()
    return t.year, t.month


def _shift(y, m, delta):
    m += delta
    while m > 12:
        m -= 12; y += 1
    while m < 1:
        m += 12; y -= 1
    return f"{y:04d}-{m:02d}"


def holiday_set(settings):
    extra = {x.strip() for x in settings.get("extra_holidays", "").split(",") if x.strip()}
    return extra


def build_timesheet(con, y, m, department=""):
    """Табель за месяц: [{employee, codes: [код/''], итоги}] и список дней."""
    settings = db.get_settings(con)
    six = settings.get("work_days_per_week") == "6"
    extra = holiday_set(settings)
    try:
        ov = {r["day"]: r["kind"] for r in con.execute("SELECT day, kind FROM work_calendar")}
    except Exception:
        ov = {}
    days = [date(y, m, d) for d in range(1, calendar.monthrange(y, m)[1] + 1)]
    first, last = days[0].isoformat(), days[-1].isoformat()
    q = ("SELECT * FROM employees WHERE hire_date<=? AND (dismissal_date IS NULL OR dismissal_date>=?)")
    args = [last, first]
    if department:
        q += " AND department=?"; args.append(department)
    emps = con.execute(q + " ORDER BY department, last_name, first_name", args).fetchall()
    manual = {}
    for r in con.execute("SELECT * FROM timesheet WHERE day>=? AND day<=?", (first, last)):
        manual[(r["employee_id"], r["day"])] = r["code"]
    absence = {}
    for r, d in db.absence_rows(con):
        if d["end_date"] < first or d["start_date"] > last or d.get("days", 1) <= 0:
            continue
        code = VAC_CODE.get(d.get("vacation_type"), "ДО") if r["kind"] == "vacation" else "К"
        cur = max(date.fromisoformat(d["start_date"]), days[0])
        end = min(date.fromisoformat(d["end_date"]), days[-1])
        while cur <= end:
            absence[(r["employee_id"], cur.isoformat())] = code
            cur += timedelta(days=1)
    rows = []
    for e in emps:
        codes = []
        for day in days:
            iso = day.isoformat()
            if iso < e["hire_date"] or (e["dismissal_date"] and iso > e["dismissal_date"]):
                codes.append("")
                continue
            code = manual.get((e["id"], iso)) or absence.get((e["id"], iso))
            if not code:
                off = day.weekday() == 6 or (day.weekday() == 5 and not six)
                is_off = ov[iso] == "off" if iso in ov else (off or (day.month, day.day) in FIXED_HOLIDAYS or iso in extra)
                if is_off:
                    code = "В"
                else:
                    code = "Я"
            codes.append(code)
        worked = sum(1 for c in codes if c in ("Я", "К"))
        rows.append({"e": e, "codes": codes, "worked": worked, "hours": worked * 8,
                     "vacation": sum(1 for c in codes if c in ("О", "У", "ДО", "ОЖ")),
                     "other": sum(1 for c in codes if c in ("Б", "ПР"))})
    return days, rows


@route("/timesheet", ("GET", "POST"))
@login_required
def timesheet():
    y, m = _month_arg()
    department = request.args.get("department", "")
    con = db.connect()
    try:
        if request.method == "POST":
            if session.get("role") not in ("hr", "admin"):
                abort(403)
            emp_id = request.form.get("employee_id", type=int)
            code = request.form.get("code", "")
            try:
                d1 = date.fromisoformat(request.form.get("from", ""))
                d2 = date.fromisoformat(request.form.get("to", "") or request.form.get("from", ""))
            except ValueError:
                d1 = d2 = None
            emp = con.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
            if emp is None or d1 is None or d2 < d1 or (code not in MANUAL_CODES and code != "auto"):
                flash("Выберите сотрудника, даты и отметку.", "error")
            elif (d2 - d1).days > 366:
                flash("Слишком длинный период.", "error")
            else:
                cur = d1
                while cur <= d2:
                    if code == "auto":
                        con.execute("DELETE FROM timesheet WHERE employee_id=? AND day=?", (emp_id, cur.isoformat()))
                    else:
                        con.execute("INSERT INTO timesheet(employee_id, day, code) VALUES(?,?,?) "
                                    "ON CONFLICT(employee_id, day) DO UPDATE SET code=excluded.code",
                                    (emp_id, cur.isoformat(), code))
                    cur += timedelta(days=1)
                flash("Табель обновлён.", "ok")
            return redirect(url_for("timesheet", month=f"{y:04d}-{m:02d}", department=department))
        days, rows = build_timesheet(con, y, m, department)
        deps = [r["name"] for r in con.execute("SELECT name FROM departments ORDER BY name")]
    finally:
        con.close()
    return render_template("timesheet.html", days=days, rows=rows, deps=deps, department=department,
                           month=f"{y:04d}-{m:02d}", prev=_shift(y, m, -1), next=_shift(y, m, 1),
                           codes=MANUAL_CODES, names=CODE_NAMES, today=date.today())


# ---------- график отпусков ----------

@route("/vacations", ("GET", "POST"))
@login_required
def vacations():
    year = request.args.get("year", type=int) or date.today().year
    con = db.connect()
    try:
        if request.method == "POST":
            if session.get("role") not in ("hr", "admin"):
                abort(403)
            action = request.form.get("action")
            if action == "add":
                emp = con.execute("SELECT * FROM employees WHERE id=? AND status=?",
                                  (request.form.get("employee_id", type=int), db.ACTIVE)).fetchone()
                try:
                    s_ = date.fromisoformat(request.form.get("start_date", ""))
                    e_ = date.fromisoformat(request.form.get("end_date", ""))
                except ValueError:
                    s_ = e_ = None
                if emp is None or s_ is None or e_ < s_:
                    flash("Выберите сотрудника и верные даты.", "error")
                elif s_.year != year:
                    flash(f"Даты должны быть в {year} году.", "error")
                else:
                    con.execute("INSERT INTO vacation_plan(employee_id, year, start_date, end_date, note) VALUES(?,?,?,?,?)",
                                (emp["id"], year, s_.isoformat(), e_.isoformat(), request.form.get("note", "").strip()))
                    flash("Отпуск внесён в график.", "ok")
            elif action == "delete":
                con.execute("DELETE FROM vacation_plan WHERE id=?", (request.form.get("id", type=int),))
                flash("Строка убрана из графика.", "ok")
            return redirect(url_for("vacations", year=year))
        plan = con.execute(
            """SELECT p.*, e.last_name, e.first_name, e.middle_name, e.tabel_no, e.department, e.position
               FROM vacation_plan p JOIN employees e ON e.id=p.employee_id WHERE p.year=?
               ORDER BY p.start_date, e.last_name""", (year,)).fetchall()
        plan = [{**dict(p), "days": (date.fromisoformat(p["end_date"]) - date.fromisoformat(p["start_date"])).days + 1}
                for p in plan]
        done = []
        used = {}
        for r, d in db.absence_rows(con, ("vacation",)):
            if d["start_date"][:4] == str(year):
                done.append({"id": r["id"], "number": r["number"], "d": d})
                if d.get("vacation_type") == "annual":
                    used[r["employee_id"]] = used.get(r["employee_id"], 0) + int(d.get("days", 0))
        emps = con.execute("SELECT * FROM employees WHERE status=? ORDER BY last_name, first_name",
                           (db.ACTIVE,)).fetchall()
        norm = int(db.get_settings(con).get("annual_leave_days") or 21)
        balance = [{"e": e, "norm": norm, "used": used.get(e["id"], 0), "left": norm - used.get(e["id"], 0)} for e in emps]
        planned = {p["employee_id"] for p in plan}
        deps = sorted({e["department"] for e in emps})
    finally:
        con.close()
    fq = request.args.get("q", "").strip().lower()
    fdep = request.args.get("department", "")
    if fdep:
        plan = [p for p in plan if p["department"] == fdep]
        done = [x for x in done if x["d"].get("department") == fdep]
        balance = [b for b in balance if b["e"]["department"] == fdep]
    if fq:
        balance = [b for b in balance if fq in f'{b["e"]["last_name"]} {b["e"]["first_name"]} {b["e"]["middle_name"]} {b["e"]["tabel_no"]}'.lower()]
        def hit(fio, extra):
            return fq in (fio + " " + extra).lower()
        plan = [p for p in plan if hit(f'{p["last_name"]} {p["first_name"]} {p["middle_name"]}', p["tabel_no"])]
        done = [x for x in done if hit(f'{x["d"].get("last_name","")} {x["d"].get("first_name","")}', x["d"].get("tabel_no", ""))]
    return render_template("vacations.html", year=year, plan=plan, done=done, emps=emps, planned=planned, balance=balance,
                           deps=deps, fq=request.args.get("q", ""), fdep=fdep, filtered=bool(fq or fdep))


# ---------- выгрузки в Excel (CSV) ----------

@route("/reports")
@roles_required("hr", "admin", "director")
def reports():
    y, m = _month_arg()
    return render_template("reports.html", month=f"{y:04d}-{m:02d}", year=date.today().year)


@route("/export/<what>.csv")
@roles_required("hr", "admin", "director")
def export_csv(what):
    con = db.connect()
    try:
        if what == "employees":
            rows = con.execute("SELECT * FROM employees ORDER BY tabel_no").fetchall()
            return csv_response("sotrudniki.csv",
                ["Табельный №", "Договор №", "Папка", "Трудовая книжка №", "Фамилия", "Имя", "Отчество",
                 "Подразделение", "Должность", "Дата приёма", "Статус", "Дата увольнения", "Причина"],
                [[r["tabel_no"], r["contract_no"], r["folder_no"] or r["old_folder_no"] or "", r["workbook_no"] or "",
                  r["last_name"], r["first_name"], r["middle_name"], r["department"], r["position"],
                  r["hire_date"], r["status"], r["dismissal_date"] or "", r["dismissal_reason"]] for r in rows])
        if what == "orders":
            out = []
            for r in con.execute("SELECT * FROM orders WHERE deleted_at IS NULL ORDER BY id"):
                d = json.loads(r["data"])
                fio = " ".join(x for x in (d["last_name"], d["first_name"], d.get("middle_name", "")) if x)
                out.append([r["number"] or "проект", r["order_date"] or "", M.KIND_RU[r["kind"]], fio,
                            d.get("tabel_no", ""), r["status"], r["created_by_name"], r["signed_by_name"] or "",
                            r["signed_at"] or "", r["cancel_reason"]])
            return csv_response("prikazy.csv",
                ["Номер", "Дата", "Вид", "Сотрудник", "Табельный №", "Статус", "Составил", "Подписал",
                 "Время подписи", "Причина отмены"], out)
        if what == "timesheet":
            y, m = _month_arg()
            days, rows = build_timesheet(con, y, m, request.args.get("department", ""))
            return csv_response(f"tabel-{y:04d}-{m:02d}.csv",
                ["Табельный №", "ФИО", "Подразделение"] + [str(d.day) for d in days] + ["Дни явки", "Часы", "Отпуск", "Прочие неявки"],
                [[r["e"]["tabel_no"], f'{r["e"]["last_name"]} {r["e"]["first_name"]} {r["e"]["middle_name"]}'.strip(),
                  r["e"]["department"]] + r["codes"] + [r["worked"], r["hours"], r["vacation"], r["other"]] for r in rows])
        if what == "vacations":
            year = request.args.get("year", type=int) or date.today().year
            out = []
            for r, d in db.absence_rows(con, ("vacation",)):
                if d["start_date"][:4] == str(year):
                    out.append([r["number"], d["tabel_no"], f'{d["last_name"]} {d["first_name"]} {d.get("middle_name", "")}'.strip(),
                                M.VAC_TYPES[d["vacation_type"]][0], d["start_date"], d["end_date"], d["days"],
                                "продлён" if d["extended"] else "", "отозван" if d["recalled"] else ""])
            return csv_response(f"otpuska-{year}.csv",
                ["Приказ", "Табельный №", "Сотрудник", "Вид", "С", "По", "Дней", "Продление", "Отзыв"], out)
        if what == "vac_balance":
            year = request.args.get("year", type=int) or date.today().year
            norm = int(db.get_settings(con).get("annual_leave_days") or 21)
            used = {}
            for r, d in db.absence_rows(con, ("vacation",)):
                if d["start_date"][:4] == str(year) and d.get("vacation_type") == "annual":
                    used[r["employee_id"]] = used.get(r["employee_id"], 0) + int(d.get("days", 0))
            out = [[e["tabel_no"], f'{e["last_name"]} {e["first_name"]} {e["middle_name"]}'.strip(), e["department"], norm,
                    used.get(e["id"], 0), norm - used.get(e["id"], 0)]
                   for e in con.execute("SELECT * FROM employees WHERE status=? ORDER BY department, last_name, first_name", (db.ACTIVE,))]
            return csv_response(f"ostatok-otpuska-{year}.csv",
                ["Табельный №", "Сотрудник", "Подразделение", "Положено дней", "Использовано", "Остаток"], out)
    finally:
        con.close()
    abort(404)


# ---------- сводка (аналог файла Jamlanma) ----------

MONTHS_UZ = ["Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun", "Iyul", "Avgust",
             "Sentyabr", "Oktyabr", "Noyabr", "Dekabr"]
MONTHS_RU = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август",
             "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

# причины увольнения: (название, ключевые слова в тексте причины)
DISMISS_REASONS = [
    ("Низкая зарплата", ["ish haqi", "зарплат"]),
    ("Переход на другую работу", ["boshqa ishga o", "другую работу"]),
    ("Выезд за границу", ["chet el", "yevropa", "koreya", "за границ"]),
    ("Прогул (отсутствие без причины)", ["sababsiz", "прогул"]),
    ("Не смог привыкнуть к графику/сменам", ["smenali", "grafik", "график"]),
    ("По состоянию здоровья", ["sog'lig", "sog‘lig", "здоровь"]),
    ("Достиг пенсионного возраста", ["pensiya", "пенси"]),
    ("Нарушение трудовой дисциплины", ["intizom", "дисциплин", "oʻg'ri", "o'g'ri"]),
]
AGE_BANDS = [("16-19", 16, 19), ("20-24", 20, 24), ("25-30", 25, 30), ("31-34", 31, 34), ("35-39", 35, 39),
             ("40-44", 40, 44), ("45-49", 45, 49), ("50-54", 50, 54), ("55-59", 55, 59), ("60 и старше", 60, 200)]
TENURE_BANDS = [("0-1 год", 0, 1), ("2-5 лет", 2, 5), ("6-10 лет", 6, 10), ("11-14 лет", 11, 14), ("15 лет и больше", 15, 200)]
EDU_ORDER = ["Umumiy o'rta", "O'rta maxsus", "Bakalavr", "Magistr", "PhD", "DSc"]
CATEGORY_RU = {"AUP": "АУП (управленческий персонал)", "ITR": "ИТР (инженерно-технические)",
               "Ishchi": "Рабочие и технический персонал"}


def _years_between(d1, d2):
    return d2.year - d1.year - ((d2.month, d2.day) < (d1.month, d1.day))


def build_summary(con, year, month):
    """Все цифры сводки: движение кадров за год и месяц + состояние штата на сегодня."""
    ys = str(year)
    hires = [0] * 12
    dism = [0] * 12
    trans = [0] * 12
    for r in con.execute("SELECT hire_date FROM employees WHERE substr(hire_date,1,4)=?", (ys,)):
        hires[int(r[0][5:7]) - 1] += 1
    reasons_m = {name: [0] * 12 for name, _ in DISMISS_REASONS}
    reasons_m["Прочие причины"] = [0] * 12
    for r in con.execute("SELECT dismissal_date, dismissal_reason FROM employees "
                         "WHERE dismissal_date IS NOT NULL AND substr(dismissal_date,1,4)=?", (ys,)):
        i = int(r[0][5:7]) - 1
        dism[i] += 1
        text = (r[1] or "").lower()
        for name, keys in DISMISS_REASONS:
            if any(k in text for k in keys):
                reasons_m[name][i] += 1
                break
        else:
            reasons_m["Прочие причины"][i] += 1
    for r in con.execute("SELECT transfer_date FROM transfers WHERE substr(transfer_date,1,4)=?", (ys,)):
        trans[int(r[0][5:7]) - 1] += 1
    months = [{"n": i + 1, "uz": MONTHS_UZ[i], "ru": MONTHS_RU[i], "hire": hires[i], "dism": dism[i], "trans": trans[i]}
              for i in range(12)]
    totals = {"hire": sum(hires), "dism": sum(dism), "trans": sum(trans)}
    reasons = [{"name": n, "months": v, "year": sum(v), "month": v[month - 1]}
               for n, v in reasons_m.items() if sum(v) or n != "Прочие причины"]

    # состояние штата на сегодня
    today = date.today()
    pos = con.execute("SELECT d.name AS dep, p.name, p.rate, p.category FROM positions p "
                      "JOIN departments d ON d.id=p.department_id").fetchall()
    cat_of = {(r["dep"], r["name"]): (r["category"] or "") for r in pos}
    emps = con.execute("SELECT * FROM employees WHERE status=?", (db.ACTIVE,)).fetchall()
    occ = {}
    for e in emps:
        occ[(e["department"], e["position"])] = occ.get((e["department"], e["position"]), 0) + 1
    deps = {}
    total_rate = 0.0
    for r in pos:
        total_rate += r["rate"]
        dd = deps.setdefault(r["dep"], {"name": r["dep"], "rate": 0.0, "occ": 0})
        dd["rate"] += r["rate"]
        dd["occ"] += occ.get((r["dep"], r["name"]), 0)
    # занятые места вне штатного расписания (должности, которых в расписании нет)
    for (dn, pn), n in occ.items():
        if (dn, pn) not in cat_of:
            dd = deps.setdefault(dn, {"name": dn, "rate": 0.0, "occ": 0})
            dd["occ"] += n
    dep_rows = sorted(deps.values(), key=lambda x: x["name"])
    for x in dep_rows:
        x["free"] = max(x["rate"] - x["occ"], 0)
    busy = len(emps)
    free = sum(x["free"] for x in dep_rows)
    gender = {"M": 0, "F": 0, "": 0}
    cats = {"AUP": 0, "ITR": 0, "Ishchi": 0, "": 0}
    ages = {b[0]: 0 for b in AGE_BANDS}
    tenure = {b[0]: 0 for b in TENURE_BANDS}
    edu = {k: 0 for k in EDU_ORDER}
    edu[""] = 0
    for e in emps:
        gender[e["gender"] if e["gender"] in ("M", "F") else ""] += 1
        c = cat_of.get((e["department"], e["position"]), "")
        cats[c if c in cats else ""] += 1
        try:
            a = _years_between(date.fromisoformat(e["birth_date"]), today)
            for name, lo, hi in AGE_BANDS:
                if lo <= a <= hi:
                    ages[name] += 1
                    break
        except ValueError:
            pass
        try:
            t = _years_between(date.fromisoformat(e["hire_date"]), today)
            for name, lo, hi in TENURE_BANDS:
                if lo <= t <= hi:
                    tenure[name] += 1
                    break
        except ValueError:
            pass
        edu[e["education"] if e["education"] in edu else ""] += 1
    staff = {"rate": total_rate, "busy": busy, "free": free, "gender": gender, "cats": cats,
             "ages": ages, "tenure": tenure, "edu": edu, "deps": dep_rows}
    return {"year": year, "month": month, "months": months, "totals": totals, "reasons": reasons,
            "cur": months[month - 1], "staff": staff, "today": today}


def _sum_args():
    t = date.today()
    year = request.args.get("year", type=int) or t.year
    month = request.args.get("month", type=int) or t.month
    if not 1 <= month <= 12:
        month = t.month
    return year, month


@route("/summary")
@roles_required("hr", "admin", "director")
def summary():
    year, month = _sum_args()
    con = db.connect()
    try:
        s = build_summary(con, year, month)
    finally:
        con.close()
    return render_template("summary.html", s=s, years=list(range(date.today().year - 5, date.today().year + 1)),
                           CATEGORY_RU=CATEGORY_RU, EDU_ORDER=EDU_ORDER)


@route("/export/summary.csv")
@roles_required("hr", "admin", "director")
def export_summary():
    year, month = _sum_args()
    con = db.connect()
    try:
        s = build_summary(con, year, month)
    finally:
        con.close()
    out = [["ДВИЖЕНИЕ КАДРОВ", year, "", "", ""], ["Месяц", "Принято", "Уволено", "Переведено", ""]]
    for m in s["months"]:
        out.append([m["ru"], m["hire"], m["dism"], m["trans"], ""])
    t = s["totals"]
    out.append(["Итого за год", t["hire"], t["dism"], t["trans"], ""])
    out.append([""] * 5)
    out.append(["ПРИЧИНЫ УВОЛЬНЕНИЯ", "За год", MONTHS_RU[month - 1], "", ""])
    for r in s["reasons"]:
        out.append([r["name"], r["year"], r["month"], "", ""])
    st = s["staff"]
    out += [[""] * 5, ["ШТАТ НА " + s["today"].strftime("%d.%m.%Y"), "", "", "", ""],
            ["Штатных единиц", st["rate"], "", "", ""], ["Занято", st["busy"], "", "", ""],
            ["Вакансий", st["free"], "", "", ""], ["Мужчины", st["gender"]["M"], "", "", ""],
            ["Женщины", st["gender"]["F"], "", "", ""], ["Пол не указан", st["gender"][""], "", "", ""]]
    for k, v in CATEGORY_RU.items():
        out.append([v, st["cats"][k], "", "", ""])
    for k, v in st["ages"].items():
        out.append(["Возраст " + k, v, "", "", ""])
    for k, v in st["tenure"].items():
        out.append(["Стаж " + k, v, "", "", ""])
    for k in EDU_ORDER:
        out.append(["Образование " + k, st["edu"][k], "", "", ""])
    out += [[""] * 5, ["ПО ПОДРАЗДЕЛЕНИЯМ", "Штат", "Занято", "Вакансий", ""]]
    for d in st["deps"]:
        out.append([d["name"], d["rate"], d["occ"], d["free"], ""])
    return csv_response(f"svodka-{year}-{month:02d}.csv", ["Показатель", "Значение", "", "", ""], out)


# ---------- тестовый штат ----------

@route("/demo", methods=("GET", "POST"))
@admin_required
def demo():
    con = db.connect()
    try:
        if request.method == "POST":
            action = request.form.get("action")
            if action == "load":
                res = db.load_demo(con)
                if res is None:
                    flash("Тестовые данные уже загружены.", "error")
                else:
                    flash(f"Загружено: принято {res[0]}, из них уволено {res[1]}, переведено {res[2]}.", "ok")
            elif action == "remove":
                n = db.remove_demo(con)
                flash(f"Тестовые данные удалены ({n} сотрудников), нумерация возвращена.", "ok")
            return redirect(url_for("demo"))
        loaded = db.demo_loaded(con)
        total = con.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
    finally:
        con.close()
    return render_template("demo.html", loaded=loaded, total=total)


@route("/employee/<int:emp_id>/profile", methods=("POST",))
@roles_required("hr", "admin")
def employee_profile(emp_id):
    gender = request.form.get("gender", "")
    edu = request.form.get("education", "")
    if gender not in ("", "M", "F") or edu not in [""] + EDU_ORDER:
        abort(400)
    con = db.connect()
    try:
        con.execute("UPDATE employees SET gender=?, education=? WHERE id=?", (gender, edu, emp_id))
    finally:
        con.close()
    flash("Сохранено.", "ok")
    return redirect(url_for("employee", emp_id=emp_id))


@route("/order/<int:order_id>/steps", methods=("POST",))
@roles_required("hr", "admin")
def order_steps_save(order_id):
    """Изменить участников и подписанта приказа, не открывая сам приказ. Уже данные согласия сохраняются."""
    con = db.connect()
    try:
        order = M._get_order(con, order_id)
        if order["status"] not in ("draft", "returned") or order["deleted_at"]:
            flash("У подписанного приказа участников менять нельзя.", "error")
            return redirect(url_for("order_view", order_id=order_id))
        form = request.form
        err = db.replace_steps(con, order_id, parse_steps(form))
        if err:
            flash(err, "error")
        else:
            M._set_signer(con, order_id, form.get("signer_login", ""))
            con.execute("UPDATE orders SET group_name='' WHERE id=?", (order_id,))
            flash("Участники и подписант сохранены.", "ok")
    finally:
        con.close()
    return redirect(url_for("order_view", order_id=order_id))
