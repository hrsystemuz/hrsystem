"""Отчёты: движение персонала за период (текучесть по подразделениям), дни рождения, права ролей."""
from datetime import date, datetime

from flask import render_template, request

import db
from extras import csv_response, roles_required, route

M = None

MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]

# Что может каждая роль (описание текущих правил программы)
ROLE_MATRIX = [
    ("Войти и написать заявление, видеть свои заявления", "да", "да", "да", "да", "да", "да"),
    ("Почта и «Мой профиль»", "да", "да", "да", "да", "да", "да"),
    ("Согласовать назначенные ему документы, заявления и приказы", "—", "да", "да", "да", "да", "да"),
    ("Создавать общие документы, документы по шаблонам", "—", "да", "да", "да", "да", "да"),
    ("Подписывать приказы и документы (только если назначен подписантом)", "—", "да", "да", "да", "да", "да"),
    ("Составлять приказы (приём, перевод, увольнение, отпуск, командировка, взыскание)", "—", "—", "—", "—", "да", "да"),
    ("Карточки сотрудников и список сотрудников: смотреть", "—", "да", "да", "да", "да", "да"),
    ("Карточки сотрудников: изменять (паспорт, семья, образование)", "—", "—", "—", "—", "да", "да"),
    ("Табель, график отпусков, рабочий календарь: смотреть", "—", "да", "да", "да", "да", "да"),
    ("Табель, график отпусков, рабочий календарь: изменять", "—", "—", "—", "—", "да", "да"),
    ("Отчёты и сводка, выгрузки в Excel", "—", "—", "—", "да", "да", "да"),
    ("Выдать сотруднику доступ в программу (логин и пароль)", "—", "—", "—", "—", "да", "да"),
    ("Штатное расписание, пользователи, шаблоны, справочники, настройки", "—", "—", "—", "—", "—", "да"),
    ("Входящие письма: регистрировать, нумеровать, направлять исполнителям, принимать отчёты, возвращать на доработку", "—", "—", "да", "да", "—", "да"),
    ("Входящие письма: видеть только те, где назначен исполнителем, и слать отчёт", "—", "да", "да", "да", "да", "да"),
]
ROLE_COLS = ["Сотрудник", "Согласующий", "Делопроизводитель", "Директор", "Кадровик", "Администратор"]


def setup(app_module):
    global M
    M = app_module


def _d(s, default):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return default


def _period():
    today = date.today()
    a = _d(request.args.get("from"), date(today.year, 1, 1))
    b = _d(request.args.get("to"), today)
    if b < a:
        a, b = b, a
    return a, b, request.args.get("department", "")


def build_movement(con, a, b, department=""):
    """Принято, уволено, численность на начало и конец, текучесть — всего и по подразделениям."""
    a_iso, b_iso = a.isoformat(), b.isoformat()
    emps = con.execute("SELECT * FROM employees").fetchall()
    if department:
        emps = [e for e in emps if e["department"] == department]

    def head(day):   # численность на конец указанного дня
        return sum(1 for e in emps if e["hire_date"] <= day and not (e["dismissal_date"] and e["dismissal_date"] <= day))
    from datetime import timedelta
    start_day = (a - timedelta(days=1)).isoformat()
    hired = sorted((e for e in emps if a_iso <= e["hire_date"] <= b_iso), key=lambda e: e["hire_date"])
    dism = sorted((e for e in emps if e["dismissal_date"] and a_iso <= e["dismissal_date"] <= b_iso), key=lambda e: e["dismissal_date"])
    depts = {}
    for e in emps:
        depts.setdefault(e["department"], []).append(e)
    rows = []
    for dep, es in sorted(depts.items()):
        s = sum(1 for e in es if e["hire_date"] <= start_day and not (e["dismissal_date"] and e["dismissal_date"] <= start_day))
        f = sum(1 for e in es if e["hire_date"] <= b_iso and not (e["dismissal_date"] and e["dismissal_date"] <= b_iso))
        h = sum(1 for e in es if a_iso <= e["hire_date"] <= b_iso)
        d = sum(1 for e in es if e["dismissal_date"] and a_iso <= e["dismissal_date"] <= b_iso)
        avg = (s + f) / 2
        rows.append({"dep": dep, "start": s, "hired": h, "dism": d, "end": f, "turn": round(d / avg * 100, 1) if avg else 0.0})
    s_all, f_all = head(start_day), head(b_iso)
    avg_all = (s_all + f_all) / 2
    transfers = con.execute("SELECT COUNT(*) FROM transfers WHERE transfer_date>=? AND transfer_date<=?", (a_iso, b_iso)).fetchone()[0]
    return {"start": s_all, "end": f_all, "hired": hired, "dism": dism, "rows": rows, "transfers": transfers,
            "turn": round(len(dism) / avg_all * 100, 1) if avg_all else 0.0}


def _departments(con):
    return [r["name"] for r in con.execute("SELECT name FROM departments ORDER BY name")]


@route("/report/movement")
@roles_required("hr", "admin", "director")
def report_movement():
    a, b, dep = _period()
    con = db.connect()
    try:
        r = build_movement(con, a, b, dep)
        deps = _departments(con)
    finally:
        con.close()
    return render_template("report_movement.html", r=r, a=a, b=b, dep=dep, deps=deps)


@route("/report/movement.csv")
@roles_required("hr", "admin", "director")
def report_movement_csv():
    a, b, dep = _period()
    con = db.connect()
    try:
        r = build_movement(con, a, b, dep)
    finally:
        con.close()
    out = [["ДВИЖЕНИЕ ПЕРСОНАЛА", f"{a.strftime('%d.%m.%Y')} — {b.strftime('%d.%m.%Y')}", dep or "все подразделения", "", "", ""],
           ["На начало", r["start"], "На конец", r["end"], "Текучесть, %", r["turn"]], [""] * 6,
           ["Подразделение", "На начало", "Принято", "Уволено", "На конец", "Текучесть, %"]]
    out += [[x["dep"], x["start"], x["hired"], x["dism"], x["end"], x["turn"]] for x in r["rows"]]
    out += [[""] * 6, ["ПРИНЯТЫ", "Табельный №", "Сотрудник", "Подразделение", "Должность", "Дата приёма"]]
    out += [["", e["tabel_no"], f'{e["last_name"]} {e["first_name"]} {e["middle_name"]}'.strip(), e["department"], e["position"], e["hire_date"]] for e in r["hired"]]
    out += [[""] * 6, ["УВОЛЕНЫ", "Табельный №", "Сотрудник", "Подразделение", "Должность", "Дата увольнения", "Основание"]]
    out += [["", e["tabel_no"], f'{e["last_name"]} {e["first_name"]} {e["middle_name"]}'.strip(), e["department"], e["position"],
             e["dismissal_date"], e["dismissal_reason"] or ""] for e in r["dism"]]
    return csv_response(f"dvizhenie-{a.isoformat()}-{b.isoformat()}.csv", out[0], out[1:])


def _birthdays(con, month, dep):
    q = "SELECT * FROM employees WHERE status=?"
    args = [db.ACTIVE]
    if dep:
        q += " AND department=?"; args.append(dep)
    today = date.today()
    out = []
    for e in con.execute(q, args):
        try:
            bd = datetime.strptime(e["birth_date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        if bd.month == month:
            out.append({"e": e, "day": bd.day, "age": today.year - bd.year, "bd": bd})
    out.sort(key=lambda x: (x["day"], x["e"]["last_name"]))
    return out


@route("/report/birthdays")
@roles_required("hr", "admin", "director")
def report_birthdays():
    month = request.args.get("month", type=int) or date.today().month
    month = min(max(month, 1), 12)
    dep = request.args.get("department", "")
    con = db.connect()
    try:
        rows, deps = _birthdays(con, month, dep), _departments(con)
    finally:
        con.close()
    return render_template("report_birthdays.html", rows=rows, month=month, months=MONTHS, dep=dep, deps=deps, today=date.today())


@route("/report/birthdays.csv")
@roles_required("hr", "admin", "director")
def report_birthdays_csv():
    month = min(max(request.args.get("month", type=int) or date.today().month, 1), 12)
    dep = request.args.get("department", "")
    con = db.connect()
    try:
        rows = _birthdays(con, month, dep)
    finally:
        con.close()
    return csv_response(f"dni-rozhdeniya-{month:02d}.csv", ["День", "Сотрудник", "Подразделение", "Должность", "Дата рождения", "Исполняется лет"],
                        [[x["day"], f'{x["e"]["last_name"]} {x["e"]["first_name"]} {x["e"]["middle_name"]}'.strip(), x["e"]["department"],
                          x["e"]["position"], x["e"]["birth_date"], x["age"]] for x in rows])


@route("/roles")
@roles_required("hr", "admin", "director")
def roles_matrix():
    return render_template("roles_matrix.html", matrix=ROLE_MATRIX, cols=ROLE_COLS)
