"""Вернуть на рассмотрение документ, который вернули или отклонили (по ошибке или после исправления).

Два способа:
  one — только тому, кто вернул/отказал; кто уже подписал, остаётся подписанным;
  all — начать согласование заново, все прежние подписи сбрасываются.
"""
from flask import abort, flash, redirect, request, session, url_for

import db
import extras
from extras import login_required, route

M = None


def setup(app_module):
    global M
    M = app_module
    app_module.app.jinja_env.globals["restore_info"] = info


def _is_boss():
    return session.get("role") in ("hr", "admin")


def _party(con, kind, row):
    """Свой ли человек в этом документе: автор, участник цепочки, кадровик, администратор, директор."""
    me = session["user"]
    if _is_boss() or session.get("role") == "director":
        return True
    if kind == "order":
        return row["created_by"] == me or bool(con.execute(
            "SELECT 1 FROM order_steps WHERE order_id=? AND login=?", (row["id"], me)).fetchone())
    if kind == "app":
        return row["author"] == me or bool(con.execute(
            "SELECT 1 FROM app_steps WHERE app_id=? AND login=?", (row["id"], me)).fetchone())
    return row["author"] == me or bool(con.execute(
        "SELECT 1 FROM gdoc_steps WHERE doc_id=? AND login=?", (row["id"], me)).fetchone())


def _who_order(con, o):
    if o["status"] != "returned" or o["deleted_at"]:
        return None
    login = o["returned_by"] if "returned_by" in o.keys() else ""
    if not login:
        return None
    name = extras.user_name(con, login)
    ok = _party(con, "order", o)
    return (name, ok)


def _stuck_app(con, a):
    if a["status"] not in ("returned", "rejected"):
        return None
    return con.execute("SELECT * FROM app_steps WHERE app_id=? AND status='rejected' ORDER BY id DESC LIMIT 1", (a["id"],)).fetchone()


def _stuck_gd(con, d):
    if d["status"] not in ("returned", "rejected"):
        return None
    return con.execute("SELECT * FROM gdoc_steps WHERE doc_id=? AND action<>'execute' AND status IN ('returned','rejected') "
                       "ORDER BY id DESC LIMIT 1", (d["id"],)).fetchone()


def info(kind, row):
    """Для шаблона: None или {url, who} — показывать ли кнопку «Вернуть на рассмотрение»."""
    if "user" not in session:
        return None
    con = db.connect()
    try:
        if kind == "order":
            r = _who_order(con, row)
            if not r or not r[1]:
                return None
            return {"url": url_for("rework_order", order_id=row["id"]), "who": r[0]}
        if kind == "app":
            st = _stuck_app(con, row)
            if st is None or not _party(con, "app", row):
                return None
            return {"url": url_for("ap_restore", app_id=row["id"]), "who": st["name"] or st["login"]}
        if kind == "gd":
            st = _stuck_gd(con, row)
            if st is None or not _party(con, "gd", row):
                return None
            return {"url": url_for("rework_gd", doc_id=row["id"]), "who": st["name"] or st["login"]}
    finally:
        con.close()
    return None


def _mode():
    return "all" if request.form.get("mode") == "all" else "one"


@route("/order/<int:order_id>/rework", ("POST",))
@login_required
def rework_order(order_id):
    mode = _mode()
    con = db.connect()
    try:
        o = M._get_order(con, order_id)
        r = _who_order(con, o)
        if not r or not r[1]:
            abort(403)
        con.execute("BEGIN IMMEDIATE")
        try:
            login = o["returned_by"]
            con.execute("UPDATE orders SET status='draft', return_comment='', returned_by='' WHERE id=?", (order_id,))
            if mode == "all":
                db.reset_steps(con, order_id)
                extras.log_event(con, "order", order_id, "Вернул на рассмотрение: согласование начато заново (все подписи сброшены)")
            else:
                con.execute("UPDATE order_steps SET status='pending', at=NULL, comment='' WHERE order_id=? AND login=? AND action<>'info'",
                            (order_id, login))
                extras.log_event(con, "order", order_id, f"Вернул на рассмотрение: {r[0]} (остальные подписи сохранены)")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    flash("Приказ снова на рассмотрении." + (" Все подписи сброшены." if mode == "all" else " Прежние подписи сохранены."), "ok")
    return redirect(url_for("order_view", order_id=order_id))


@route("/ap/<int:app_id>/rework", ("POST",))
@login_required
def ap_restore(app_id):
    import appl
    mode = _mode()
    con = db.connect()
    try:
        a = con.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        if a is None:
            abort(404)
        st = _stuck_app(con, a)
        if st is None or not _party(con, "app", a):
            abort(403)
        con.execute("BEGIN IMMEDIATE")
        try:
            if mode == "all":
                con.execute("UPDATE app_steps SET status='pending', at=NULL, comment='' WHERE app_id=?", (app_id,))
            else:
                con.execute("UPDATE app_steps SET status='pending', at=NULL, comment='' WHERE id=?", (st["id"],))
            con.execute("UPDATE applications SET status='review', return_comment='', finished_at=NULL WHERE id=?", (app_id,))
            who = st["name"] or st["login"]
            appl.log(con, app_id, session["user"],
                     "Возвращено на рассмотрение: согласование начато заново" if mode == "all"
                     else f"Возвращено на рассмотрение: {who} (остальные согласия сохранены)")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    flash("Заявление снова на рассмотрении.", "ok")
    return redirect(url_for("ap_view", app_id=app_id))


@route("/gd/<int:doc_id>/rework", ("POST",))
@login_required
def rework_gd(doc_id):
    import gdoc
    mode = _mode()
    con = db.connect()
    try:
        d = con.execute("SELECT * FROM gdocs WHERE id=?", (doc_id,)).fetchone()
        if d is None:
            abort(404)
        st = _stuck_gd(con, d)
        if st is None or not _party(con, "gd", d):
            abort(403)
        con.execute("BEGIN IMMEDIATE")
        try:
            if mode == "all":
                con.execute("UPDATE gdoc_steps SET status='pending', comment='', acted_at=NULL WHERE doc_id=? AND action NOT IN ('info','execute')", (doc_id,))
            else:
                con.execute("UPDATE gdoc_steps SET status='pending', comment='', acted_at=NULL WHERE id=?", (st["id"],))
            con.execute("UPDATE gdocs SET status='review', return_comment='', finished_at=NULL WHERE id=?", (doc_id,))
            gdoc.log(con, doc_id, "Возвращён на рассмотрение: согласование начато заново" if mode == "all"
                     else f"Возвращён на рассмотрение: {st['name'] or st['login']} (остальные согласия сохранены)")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    flash("Документ снова на рассмотрении.", "ok")
    return redirect(url_for("gd_view", doc_id=doc_id))
