"""Уведомления: «Отклонённые» — свои документы, которые вернули на доработку или отклонили."""
from flask import render_template, session, url_for

import db
from extras import login_required, route

M = None


def setup(app_module):
    global M
    M = app_module

    @app_module.app.context_processor
    def _rej_badge():
        if "user" not in session:
            return {}
        con = db.connect()
        try:
            return {"rej_count": count(con, session["user"])}
        except Exception:                                        # noqa: BLE001
            return {}
        finally:
            con.close()


def count(con, login):
    n = con.execute("SELECT COUNT(*) FROM applications WHERE author=? AND status IN ('returned','rejected')", (login,)).fetchone()[0]
    n += con.execute("SELECT COUNT(*) FROM orders WHERE created_by=? AND status='returned' AND deleted_at IS NULL", (login,)).fetchone()[0]
    n += con.execute("SELECT COUNT(*) FROM gdocs WHERE author=? AND status IN ('returned','rejected')", (login,)).fetchone()[0]
    return n


def items(con, login):
    out = []
    for a in con.execute("SELECT * FROM applications WHERE author=? AND status IN ('returned','rejected') ORDER BY id DESC", (login,)):
        out.append({"kind": "Заявление", "title": a["type_name"], "number": a["number"] or f"черновик №{a['id']}",
                    "state": a["status"], "comment": a["return_comment"], "url": url_for("ap_view", app_id=a["id"]), "sort": a["finished_at"] or a["created_at"] or ""})
    for o in con.execute("SELECT * FROM orders WHERE created_by=? AND status='returned' AND deleted_at IS NULL ORDER BY id DESC", (login,)):
        out.append({"kind": "Приказ", "title": M.KIND_RU.get(o["kind"], o["kind"]), "number": o["number"] or f"проект №{o['id']}",
                    "state": "returned", "comment": o["return_comment"], "url": url_for("order_view", order_id=o["id"]), "sort": o["created_at"] or ""})
    for g in con.execute("SELECT * FROM gdocs WHERE author=? AND status IN ('returned','rejected') ORDER BY id DESC", (login,)):
        out.append({"kind": "Документ", "title": g["title"], "number": g["number"] or f"черновик №{g['id']}",
                    "state": g["status"], "comment": g["return_comment"], "url": url_for("gd_view", doc_id=g["id"]), "sort": g["finished_at"] or g["created_at"] or ""})
    out.sort(key=lambda x: x["sort"], reverse=True)
    return out


@route("/rejected")
@login_required
def ap_rejected():
    con = db.connect()
    try:
        rows = items(con, session["user"])
    finally:
        con.close()
    return render_template("rejected.html", rows=rows)
