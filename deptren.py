"""Приказ о переименовании подразделения: после подписи название меняется в штатном расписании и у работающих сотрудников."""
import json
from datetime import date

from flask import abort, flash, redirect, render_template, request, url_for

import db
from extras import roles_required, route

M = None


def setup(app_module):
    global M
    M = app_module


def validate(con, d, order_id=0):
    """Список ошибок для данных приказа (используется при сохранении и при подписи)."""
    errs = []
    old, new = d.get("department", ""), d.get("new_name", "")
    if con.execute("SELECT 1 FROM departments WHERE name=?", (old,)).fetchone() is None:
        errs.append(f"Подразделения «{old}» нет в штатном расписании (возможно, оно уже переименовано).")
    if not new:
        errs.append("Укажите новое название подразделения.")
    elif new == old:
        errs.append("Новое название совпадает со старым.")
    elif con.execute("SELECT 1 FROM departments WHERE lower(name)=lower(?) AND name<>?", (new, old)).fetchone():
        errs.append(f"Подразделение «{new}» уже есть в штатном расписании.")
    if len(new) > 120:
        errs.append("Название слишком длинное (до 120 знаков).")
    try:
        date.fromisoformat(d.get("effective_date", ""))
    except ValueError:
        errs.append("Укажите дату, с которой действует новое название.")
    return errs


def data_for(old, new, eff):
    """Данные приказа. Поля ФИО заполнены «старое → новое», чтобы приказ показывался в списках."""
    return {"department": old, "new_name": new, "effective_date": eff, "tabel_no": "", "position": "",
            "last_name": f"«{old}» → «{new}»", "first_name": "", "middle_name": ""}


def _form(order):
    con = db.connect()
    try:
        deps = [r["name"] for r in con.execute("SELECT name FROM departments ORDER BY name")]
        if order is not None:
            f = json.loads(order["data"])
            f["basis"] = order["basis"]
        else:
            f = {"effective_date": date.today().isoformat(), "basis": ""}
        if request.method == "POST":
            fm = request.form
            old = fm.get("department", "").strip()
            new = " ".join(fm.get("new_name", "").split())
            eff = fm.get("effective_date", "").strip()
            basis = fm.get("basis", "").strip()
            d = data_for(old, new, eff)
            errors = validate(con, d, order["id"] if order else 0)
            f = {**d, "basis": basis}
            if not errors:
                data = json.dumps(d, ensure_ascii=False)
                if order is not None:
                    con.execute("UPDATE orders SET data=?, basis=?, status='draft', return_comment='' WHERE id=? AND status IN ('draft','returned')",
                                (data, basis, order["id"]))
                    new_id = order["id"]
                else:
                    me = M._user_row(con)
                    cur = con.execute(
                        """INSERT INTO orders(kind, status, data, basis, created_by, created_by_name, created_by_title, created_at)
                           VALUES('dept_rename','draft',?,?,?,?,?,?)""",
                        (data, basis, me["login"], me["full_name"] or me["login"], me["title"], db.now_local().strftime("%Y-%m-%d %H:%M")))
                    new_id = cur.lastrowid
                M._apply_group(con, new_id, request.form)
                flash("Проект приказа сохранён. Название изменится после подписи директора.", "ok")
                return redirect(url_for("order_view", order_id=new_id))
            for e in errors:
                flash(e, "error")
    finally:
        con.close()
    return render_template("dept_rename.html", f=f, deps=deps, order=order, groups=M._groups(), **M._chain_context(order))


@route("/order/new/dept-rename", ("GET", "POST"))
@roles_required("hr", "admin")
def dept_rename_new():
    return _form(None)


@route("/order/<int:order_id>/dept-rename-edit", ("GET", "POST"))
@roles_required("hr", "admin")
def dept_rename_edit(order_id):
    con = db.connect()
    try:
        order = M._get_order(con, order_id)
    finally:
        con.close()
    if order["kind"] != "dept_rename" or order["status"] not in ("draft", "returned") or order["deleted_at"]:
        abort(404)
    return _form(order)
