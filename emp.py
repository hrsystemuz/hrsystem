"""Расширенная карточка сотрудника: паспорт (срок действия), контакты, семья, образование,
предыдущая работа и стаж, объективка для печати, список паспортов с истекающим сроком."""
from datetime import date, datetime, timedelta

from flask import abort, flash, redirect, render_template, request, session, url_for

import db
from extras import login_required, roles_required, route

M = None

DDL = """
CREATE TABLE IF NOT EXISTS emp_family (
    id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    relation TEXT NOT NULL, full_name TEXT NOT NULL, birth_year TEXT NOT NULL DEFAULT '', workplace TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS emp_education (
    id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    level TEXT NOT NULL DEFAULT '', institution TEXT NOT NULL, specialty TEXT NOT NULL DEFAULT '',
    year_end TEXT NOT NULL DEFAULT '', diploma TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS emp_prior_work (
    id INTEGER PRIMARY KEY AUTOINCREMENT, employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    date_from TEXT NOT NULL, date_to TEXT NOT NULL, organization TEXT NOT NULL, position TEXT NOT NULL DEFAULT '');
"""
EXTRA_COLS = (("phone", "TEXT NOT NULL DEFAULT ''"), ("marital", "TEXT NOT NULL DEFAULT ''"),
              ("nationality", "TEXT NOT NULL DEFAULT ''"), ("passport_issuer", "TEXT NOT NULL DEFAULT ''"),
              ("passport_issued", "TEXT NOT NULL DEFAULT ''"), ("passport_until", "TEXT NOT NULL DEFAULT ''"),
              ("languages", "TEXT NOT NULL DEFAULT ''"))
RELATIONS = ["Отец", "Мать", "Супруг(а)", "Сын", "Дочь", "Брат", "Сестра", "Другое"]
LEVELS = ["Среднее", "Среднее специальное", "Бакалавр", "Магистр", "PhD", "DSc", "Курсы / переподготовка"]
MARITAL = ["", "женат / замужем", "не женат / не замужем", "разведён(а)", "вдовец / вдова"]
WARN_DAYS = 60


def setup(app_module):
    global M
    M = app_module
    con = db.connect()
    try:
        con.executescript(DDL)
        have = {r[1] for r in con.execute("PRAGMA table_info(employees)")}
        for n, ddl in EXTRA_COLS:
            if n not in have:
                con.execute(f"ALTER TABLE employees ADD COLUMN {n} {ddl}")
    finally:
        con.close()
    M.app.jinja_env.globals["emp_extra"] = emp_extra


def _d(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def span(days):
    """Дни → (лет, месяцев, дней) приблизительно (год = 365, месяц = 30)."""
    return days // 365, days % 365 // 30, days % 365 % 30


def passport_state(until, today=None):
    """('', None) нет данных; ('ok'|'soon'|'expired', дней до конца)."""
    u = _d(until)
    if not u:
        return "", None
    left = (u - (today or date.today())).days
    return ("expired" if left < 0 else "soon" if left <= WARN_DAYS else "ok"), left


def emp_extra(emp_id):
    con = db.connect()
    try:
        e = con.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
        fam = [dict(r) for r in con.execute("SELECT * FROM emp_family WHERE employee_id=? ORDER BY id", (emp_id,))]
        edu = [dict(r) for r in con.execute("SELECT * FROM emp_education WHERE employee_id=? ORDER BY year_end, id", (emp_id,))]
        prior = [dict(r) for r in con.execute("SELECT * FROM emp_prior_work WHERE employee_id=? ORDER BY date_from, id", (emp_id,))]
    finally:
        con.close()
    prior_days = 0
    for p in prior:
        a, b = _d(p["date_from"]), _d(p["date_to"])
        p["days"] = (b - a).days + 1 if a and b and b >= a else 0
        prior_days += p["days"]
    hire, end = _d(e["hire_date"]), _d(e["dismissal_date"]) or date.today()
    here = max((end - hire).days + 1, 0) if hire else 0
    state, left = passport_state(e["passport_until"])
    return {"fam": fam, "edu": edu, "prior": prior, "relations": RELATIONS, "levels": LEVELS, "marital": MARITAL,
            "prior_span": span(prior_days), "here_span": span(here), "total_span": span(prior_days + here),
            "pass_state": state, "pass_left": left}


@route("/employee/<int:emp_id>/card", methods=("POST",))
@roles_required("hr", "admin")
def employee_card(emp_id):
    f = request.form
    g = lambda k, n=150: " ".join(f.get(k, "").split())[:n]
    act = f.get("act", "")
    con = db.connect()
    try:
        if con.execute("SELECT 1 FROM employees WHERE id=?", (emp_id,)).fetchone() is None:
            abort(404)
        if act == "main":
            dates = {k: g(k, 10) for k in ("passport_issued", "passport_until")}
            if any(v and _d(v) is None for v in dates.values()):
                flash("Проверьте даты паспорта.", "error")
            elif dates["passport_issued"] and dates["passport_until"] and dates["passport_until"] < dates["passport_issued"]:
                flash("Срок действия паспорта не может быть раньше даты выдачи.", "error")
            elif g("marital") not in MARITAL:
                abort(400)
            else:
                con.execute("UPDATE employees SET phone=?, marital=?, nationality=?, passport_issuer=?, passport_issued=?, "
                            "passport_until=?, languages=? WHERE id=?", (g("phone", 40), g("marital"), g("nationality", 60),
                            g("passport_issuer"), dates["passport_issued"], dates["passport_until"],
                            ", ".join(x.strip()[:40] for x in f.getlist("languages")[:12] if x.strip()), emp_id))
                flash("Сохранено.", "ok")
        elif act == "add_family":
            if g("relation") not in RELATIONS or not g("full_name"):
                flash("Укажите степень родства и ФИО.", "error")
            else:
                con.execute("INSERT INTO emp_family(employee_id, relation, full_name, birth_year, workplace) VALUES(?,?,?,?,?)",
                            (emp_id, g("relation"), g("full_name"), g("birth_year", 4), g("workplace")))
                flash("Член семьи добавлен.", "ok")
        elif act == "add_edu":
            if not g("institution"):
                flash("Укажите учебное заведение.", "error")
            else:
                con.execute("INSERT INTO emp_education(employee_id, level, institution, specialty, year_end, diploma) VALUES(?,?,?,?,?,?)",
                            (emp_id, g("level") if g("level") in LEVELS else "", g("institution"), g("specialty"),
                             g("year_end", 4), g("diploma", 60)))
                flash("Образование добавлено.", "ok")
        elif act == "add_work":
            a, b = _d(g("date_from", 10)), _d(g("date_to", 10))
            if a is None or b is None or b < a or not g("organization"):
                flash("Укажите организацию и корректные даты начала и окончания.", "error")
            else:
                con.execute("INSERT INTO emp_prior_work(employee_id, date_from, date_to, organization, position) VALUES(?,?,?,?,?)",
                            (emp_id, a.isoformat(), b.isoformat(), g("organization"), g("position")))
                flash("Место работы добавлено.", "ok")
        elif act in ("del_family", "del_edu", "del_work"):
            tbl = {"del_family": "emp_family", "del_edu": "emp_education", "del_work": "emp_prior_work"}[act]
            con.execute(f"DELETE FROM {tbl} WHERE id=? AND employee_id=?", (f.get("id", type=int), emp_id))
            flash("Удалено.", "ok")
        else:
            abort(400)
    finally:
        con.close()
    return redirect(url_for("employee", emp_id=emp_id) + "#extra")


@route("/employee/<int:emp_id>/objective")
@roles_required("hr", "admin", "director")
def employee_objective(emp_id):
    con = db.connect()
    try:
        e = con.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
        if e is None:
            abort(404)
        tr = con.execute("SELECT * FROM transfers WHERE employee_id=? ORDER BY transfer_date, id", (emp_id,)).fetchall()
    finally:
        con.close()
    return render_template("objective.html", e=e, x=emp_extra(emp_id), transfers=tr, today=date.today())


@route("/passports")
@roles_required("hr", "admin", "director")
def passports():
    con = db.connect()
    try:
        rows = con.execute("SELECT * FROM employees WHERE status=? ORDER BY department, last_name", (db.ACTIVE,)).fetchall()
    finally:
        con.close()
    out, empty = [], 0
    for e in rows:
        st, left = passport_state(e["passport_until"])
        if not st:
            empty += 1
        elif st != "ok":
            out.append((left, e, st))
    out.sort(key=lambda t: t[0])
    return render_template("passports.html", rows=out, empty=empty, warn=WARN_DAYS)


@route("/profile")
@login_required
def my_profile():
    con = db.connect()
    try:
        u = con.execute("SELECT * FROM users WHERE login=?", (session["user"],)).fetchone()
        e = con.execute("SELECT * FROM employees WHERE id=?", (u["employee_id"],)).fetchone() if u and u["employee_id"] else None
        org = db.get_settings(con).get("org_name", "")
    finally:
        con.close()
    x = emp_extra(e["id"]) if e else None
    return render_template("profile.html", u=u, e=e, x=x, org=org, role_name=M.ROLES.get(u["role"], u["role"]))
