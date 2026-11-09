"""Справочники. Сейчас: причины увольнения со статьями Трудового кодекса РУз.
При выборе причины в приказе об увольнении статья и формулировка подставляются сами."""
from flask import abort, flash, redirect, render_template, request, url_for

import db
from extras import roles_required, route

M = None

DDL = """
CREATE TABLE IF NOT EXISTS dismiss_reasons (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE,
    article TEXT NOT NULL DEFAULT '',
    text_uz TEXT NOT NULL DEFAULT '',
    active  INTEGER NOT NULL DEFAULT 1,
    ord     INTEGER NOT NULL DEFAULT 0
);
"""

# Новый Трудовой кодекс РУз: основания прекращения трудового договора
SEED = [
    ("По соглашению сторон", "157-modda", "tomonlarning kelishuviga ko‘ra"),
    ("Истечение срока трудового договора", "158-modda", "mehnat shartnomasi muddatining tugashi munosabati bilan"),
    ("По инициативе работника (по собственному желанию)", "160-modda", "xodimning tashabbusiga ko‘ra"),
    ("По инициативе работодателя", "161-modda", "ish beruvchining tashabbusiga ko‘ra"),
    ("Обстоятельства, не зависящие от воли сторон", "168-modda", "tomonlarning irodasiga bog‘liq bo‘lmagan holatlar tufayli"),
]


def setup(app_module):
    global M
    M = app_module
    con = db.connect()
    try:
        con.executescript(DDL)
        if con.execute("SELECT COUNT(*) FROM dismiss_reasons").fetchone()[0] == 0:
            for i, (n, a, t) in enumerate(SEED, 1):
                con.execute("INSERT INTO dismiss_reasons(name, article, text_uz, ord) VALUES(?,?,?,?)", (n, a, t, i))
    finally:
        con.close()


def active_reasons(con):
    return [dict(r) for r in con.execute(
        "SELECT id, name, article, text_uz FROM dismiss_reasons WHERE active=1 ORDER BY ord, id")]


@route("/refs/dismiss", ("GET", "POST"))
@roles_required("admin")
def refs_dismiss():
    con = db.connect()
    try:
        if request.method == "POST":
            act = request.form.get("act", "")
            rid = request.form.get("id", type=int)
            name = " ".join(request.form.get("name", "").split())[:150]
            article = " ".join(request.form.get("article", "").split())[:60]
            text_uz = " ".join(request.form.get("text_uz", "").split())[:200]
            try:
                if act == "add":
                    if not name or not article:
                        flash("Укажите причину и статью Трудового кодекса.", "error")
                    else:
                        con.execute("INSERT INTO dismiss_reasons(name, article, text_uz, ord) VALUES(?,?,?,"
                                    "(SELECT COALESCE(MAX(ord), 0) + 1 FROM dismiss_reasons))", (name, article, text_uz))
                        flash("Причина добавлена.", "ok")
                elif act == "save":
                    if not name or not article:
                        flash("Укажите причину и статью Трудового кодекса.", "error")
                    else:
                        con.execute("UPDATE dismiss_reasons SET name=?, article=?, text_uz=?, active=? WHERE id=?",
                                    (name, article, text_uz, 1 if request.form.get("active") else 0, rid))
                        flash("Сохранено.", "ok")
                elif act == "delete":
                    con.execute("DELETE FROM dismiss_reasons WHERE id=?", (rid,))
                    flash("Причина удалена из списка. Уже подписанные приказы не изменились.", "ok")
                else:
                    abort(400)
            except Exception as ex:  # повтор названия
                if "UNIQUE" in str(ex):
                    flash("Такая причина уже есть в списке.", "error")
                else:
                    raise
            return redirect(url_for("refs_dismiss"))
        rows = con.execute("SELECT * FROM dismiss_reasons ORDER BY ord, id").fetchall()
    finally:
        con.close()
    return render_template("refs_dismiss.html", rows=rows)
