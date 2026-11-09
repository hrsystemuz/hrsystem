"""Печатная форма табеля (по образцу Т-13) для печати и PDF."""
from flask import render_template, request

import db
import extras
from extras import login_required, route

MONTHS = ["", "январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
M = None


def setup(app_module):
    global M
    M = app_module


@route("/timesheet/print")
@login_required
def timesheet_print():
    y, m = extras._month_arg()
    department = request.args.get("department", "")
    con = db.connect()
    try:
        days, rows = extras.build_timesheet(con, y, m, department)
        s = db.get_settings(con)
        org = " ".join(x for x in (s.get("org_name", ""), s.get("org_form", "")) if x)
    finally:
        con.close()
    groups = {}
    for r in rows:
        groups.setdefault(r["e"]["department"], []).append(r)
    total = {k: sum(r[k] for r in rows) for k in ("worked", "hours", "vacation", "other")}
    return render_template("timesheet_print.html", days=days, groups=groups, org=org, department=department,
                           period=f"{MONTHS[m]} {y} г.", total=total, names=extras.CODE_NAMES,
                           title=f"Табель {MONTHS[m]} {y}")
