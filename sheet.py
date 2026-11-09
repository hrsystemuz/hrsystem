"""Лист согласования: печатная страница со всеми визами и подписями документа, заявления или приказа."""
import json

from flask import abort, render_template, session

import db
import extras
from extras import login_required, route

M = None

ORDER_ACTION = {"rozilik": "Согласование", "ijro": "Исполнение", "info": "Для сведения"}
GD_ACTION = {"approve": "Согласование", "sign": "Подпись", "execute": "Исполнение", "info": "Для сведения"}
STATE = {"approved": "согласовано", "rejected": "отказано", "pending": "ожидает", "done": "исполнено",
         "accepted": "принято", "reassigned": "передано другому", "signed": "подписано"}


def setup(app_module):
    global M
    M = app_module


def _page(title, subtitle, rows, log, extra=None):
    return render_template("sheet.html", title=title, subtitle=subtitle, rows=rows, log=log, extra=extra or [],
                           now_str=db.now_local().strftime("%d.%m.%Y %H:%M"))


@route("/ap/<int:app_id>/sheet")
@login_required
def sheet_app(app_id):
    import appl
    con = db.connect()
    try:
        a = con.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        if a is None:
            abort(404)
        if not appl.can_see(con, a):
            abort(403)
        emp = con.execute("SELECT * FROM employees WHERE id=?", (a["employee_id"],)).fetchone()
        steps = con.execute("SELECT * FROM app_steps WHERE app_id=? ORDER BY ord, id", (app_id,)).fetchall()
        logs = con.execute("SELECT at, who, text FROM app_log WHERE app_id=? ORDER BY id", (app_id,)).fetchall()
    finally:
        con.close()
    rows = [{"name": s["name"], "title": s["title"], "action": s["label"] or "Согласование",
             "state": STATE.get(s["status"], s["status"]), "at": s["at"] or "", "comment": s["comment"]} for s in steps]
    extra = [("Заявитель", f'{appl.fio_of(emp)}, {emp["position"]}, {emp["department"]}'),
             ("Подано", a["submitted_at"] or "—"), ("Статус", appl.STATUS_RU.get(a["status"], a["status"]))]
    if a["result_note"]:
        extra.append(("Результат", a["result_note"]))
    return _page(f'Лист согласования заявления {a["number"] or "№" + str(a["id"])}', a["type_name"], rows,
                 [(l["at"], l["who"], l["text"]) for l in logs], extra)


@route("/order/<int:order_id>/sheet")
@login_required
def sheet_order(order_id):
    con = db.connect()
    try:
        o = M._get_order(con, order_id)
        steps = db.steps_for(con, order_id)
        if not M._order_visible(con, o, steps):
            abort(403)
        d = json.loads(o["data"])
    finally:
        con.close()
    rows = [{"name": s["name"], "title": s["title"], "action": ORDER_ACTION.get(s["action"], s["action"]),
             "state": STATE.get(s["status"], s["status"]), "at": s["at"] or "", "comment": s["comment"]} for s in steps]
    if o["status"] == "signed" or o["signed_at"]:
        rows.append({"name": o["signed_by_name"], "title": o["signed_by_title"], "action": "Подпись приказа",
                     "state": "подписано", "at": o["signed_at"] or "", "comment": ""})
    extra = [("Вид приказа", M.KIND_RU.get(o["kind"], o["kind"])), ("Номер", o["number"] or "проект"),
             ("Составил", f'{o["created_by_name"]}, {o["created_by_title"]}'.strip(", "))]
    fio = " ".join(x for x in (d.get("last_name"), d.get("first_name"), d.get("middle_name")) if x)
    if fio:
        extra.append(("Сотрудник", fio))
    if o["status"] == "cancelled":
        extra.append(("Отменён", f'{o["cancelled_at"]}: {o["cancel_reason"]}'))
    return _page(f'Лист согласования приказа {o["number"] or "(проект №" + str(o["id"]) + ")"}', M.KIND_RU.get(o["kind"], ""), rows, [], extra)


@route("/gd/<int:doc_id>/sheet")
@login_required
def sheet_gdoc(doc_id):
    import gdoc
    extras_ = extras
    con = db.connect()
    try:
        d = gdoc._doc(con, doc_id)
        if not gdoc.can_see(con, d):
            abort(403)
        steps = con.execute("SELECT * FROM gdoc_steps WHERE doc_id=? ORDER BY CASE action WHEN 'approve' THEN 0 WHEN 'sign' THEN 1 "
                            "WHEN 'execute' THEN 2 ELSE 3 END, ord, id", (doc_id,)).fetchall()
        log = extras_.log_events(con, "gd", doc_id)
    finally:
        con.close()
    rows = [{"name": s["name"], "title": s["title"], "action": GD_ACTION.get(s["action"], s["action"]),
             "state": STATE.get(s["status"], s["status"]), "at": s["acted_at"] or "", "comment": s["comment"]} for s in steps]
    extra = [("Вид", d["kind"]), ("Номер", d["number"] or "проект"), ("Автор", d["author"])]
    return _page(f'Лист согласования документа {d["number"] or "(проект №" + str(d["id"]) + ")"}', d["title"], rows,
                 [(l["at"], l["who"], l["what"] + (": " + l["comment"] if l["comment"] else "")) for l in log], extra)
