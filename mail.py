"""Внутренняя почта: письма одному, нескольким, подразделению или всем; файлы; «прочитано»; ответы."""
import secrets
from datetime import datetime

from flask import abort, flash, redirect, render_template, request, send_file, session, url_for

import db
import extras
from extras import login_required, route

M = None

DDL = """
CREATE TABLE IF NOT EXISTS mail_msgs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id INTEGER, sender TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    broadcast TEXT NOT NULL DEFAULT '', sender_deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS mail_rcpt (
    id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id INTEGER NOT NULL REFERENCES mail_msgs(id) ON DELETE CASCADE,
    login TEXT NOT NULL, read_at TEXT, deleted INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_mail_rcpt_login ON mail_rcpt(login, deleted);
CREATE TABLE IF NOT EXISTS mail_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id INTEGER NOT NULL REFERENCES mail_msgs(id) ON DELETE CASCADE,
    filename TEXT NOT NULL, stored TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0);
"""
BROADCAST_ROLES = ("hr", "admin", "director")
MAX_FILES = 5


def setup(app_module):
    global M
    M = app_module
    con = db.connect()
    try:
        con.executescript(DDL)
    finally:
        con.close()

    @M.app.context_processor
    def _unread():
        if "user" not in session:
            return {}
        con = db.connect()
        try:
            n = con.execute("SELECT COUNT(*) FROM mail_rcpt WHERE login=? AND read_at IS NULL AND deleted=0",
                            (session["user"],)).fetchone()[0]
        finally:
            con.close()
        return {"mail_unread": n}


def _now():
    return db.now_local().strftime("%Y-%m-%d %H:%M")


def _users(con):
    """Все пользователи с подразделением (если привязаны к карточке сотрудника)."""
    out = []
    for u in con.execute("""SELECT u.login, u.full_name, u.title, u.role, e.department, e.position
                            FROM users u LEFT JOIN employees e ON e.id = u.employee_id AND e.status=?
                            ORDER BY u.full_name, u.login""", (db.ACTIVE,)):
        out.append({"login": u["login"], "name": u["full_name"] or u["login"],
                    "title": u["title"] or u["position"] or "", "role": u["role"], "dept": u["department"] or ""})
    return out


def _allowed_targets(con, me_role):
    """Кому можно писать: сотрудник — только кадровикам и администраторам."""
    us = [u for u in _users(con) if u["login"] != session["user"]]
    if me_role == "employee":
        us = [u for u in us if u["role"] in ("hr", "admin")]
    return us


def _names(con):
    return {r["login"]: (r["full_name"] or r["login"]) for r in con.execute("SELECT login, full_name FROM users")}


@route("/mail")
@login_required
def mail_inbox():
    box = "sent" if request.args.get("box") == "sent" else "in"
    me = session["user"]
    con = db.connect()
    try:
        names = _names(con)
        if box == "in":
            rows = con.execute("""SELECT m.*, r.read_at FROM mail_rcpt r JOIN mail_msgs m ON m.id=r.msg_id
                                  WHERE r.login=? AND r.deleted=0 ORDER BY m.id DESC LIMIT 300""", (me,)).fetchall()
        else:
            rows = con.execute("""SELECT m.*, (SELECT COUNT(*) FROM mail_rcpt WHERE msg_id=m.id) AS n_to,
                                  (SELECT COUNT(*) FROM mail_rcpt WHERE msg_id=m.id AND read_at IS NOT NULL) AS n_read,
                                  (SELECT group_concat(login) FROM mail_rcpt WHERE msg_id=m.id) AS tos
                                  FROM mail_msgs m WHERE m.sender=? AND m.sender_deleted=0 ORDER BY m.id DESC LIMIT 300""", (me,)).fetchall()
        files = {r[0] for r in con.execute("SELECT DISTINCT msg_id FROM mail_files")}
    finally:
        con.close()
    return render_template("mail_list.html", box=box, rows=rows, names=names, files=files)


@route("/mail/new", ("GET", "POST"))
@login_required
def mail_new():
    role = session.get("role")
    con = db.connect()
    try:
        targets = _allowed_targets(con, role)
        allowed = {u["login"] for u in targets}
        reply_to = request.values.get("re", type=int)
        parent = None
        if reply_to:
            parent = con.execute("SELECT m.* FROM mail_msgs m WHERE m.id=? AND (m.sender=? OR EXISTS "
                                 "(SELECT 1 FROM mail_rcpt WHERE msg_id=m.id AND login=?))",
                                 (reply_to, session["user"], session["user"])).fetchone()
            if parent is None:
                abort(404)
        if request.method == "POST":
            f = request.form
            subject = " ".join(f.get("subject", "").split())[:200]
            body = f.get("body", "").strip()[:20000]
            chosen = set(f.getlist("to"))
            can_bc = role in BROADCAST_ROLES
            bc_all = can_bc and f.get("all") == "1"
            bc_depts = set(f.getlist("dept")) if can_bc else set()
            rcpt = set(l for l in chosen if l in allowed)
            label = ""
            if bc_all:
                rcpt |= allowed; label = "всем"
            if bc_depts:
                rcpt |= {u["login"] for u in targets if u["dept"] in bc_depts}
                label = label or "подразделениям: " + ", ".join(sorted(bc_depts))
            if not rcpt:
                flash("Выберите хотя бы одного получателя.", "error")
            elif not subject:
                flash("Укажите тему письма.", "error")
            elif not body:
                flash("Напишите текст письма.", "error")
            else:
                uploads = [x for x in request.files.getlist("files") if x and x.filename][:MAX_FILES]
                blobs, bad = [], ""
                for x in uploads:
                    name = extras._clean_name(x.filename)
                    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                    blob = x.read(extras.MAX_FILE + 1)
                    if ext not in extras.ALLOWED_EXT:
                        bad = f"Файл «{name}»: такой тип нельзя (разрешены pdf, jpg, png, doc, docx, xls, xlsx)."
                    elif len(blob) > extras.MAX_FILE:
                        bad = f"Файл «{name}» больше 10 МБ."
                    elif not blob:
                        bad = f"Файл «{name}» пустой."
                    else:
                        blobs.append((name, ext, blob))
                if bad:
                    flash(bad, "error")
                else:
                    cur = con.execute("INSERT INTO mail_msgs(thread_id, sender, subject, body, created_at, broadcast) VALUES(?,?,?,?,?,?)",
                                      (parent["thread_id"] or parent["id"] if parent else None, session["user"], subject, body, _now(), label))
                    mid = cur.lastrowid
                    if not parent:
                        con.execute("UPDATE mail_msgs SET thread_id=? WHERE id=?", (mid, mid))
                    for l in sorted(rcpt):
                        con.execute("INSERT INTO mail_rcpt(msg_id, login) VALUES(?,?)", (mid, l))
                    for name, ext, blob in blobs:
                        stored = secrets.token_hex(16) + "." + ext
                        (extras.files_dir() / stored).write_bytes(blob)
                        con.execute("INSERT INTO mail_files(msg_id, filename, stored, size) VALUES(?,?,?,?)", (mid, name, stored, len(blob)))
                    flash(f"Письмо отправлено: получателей {len(rcpt)}.", "ok")
                    return redirect(url_for("mail_inbox", box="sent"))
            return render_template("mail_new.html", targets=targets, depts=sorted({u["dept"] for u in targets if u["dept"]}),
                                   can_bc=can_bc, parent=parent, f=f, chosen=chosen)
        pre = request.args.get("to", "")
        chosen = {pre} if pre in allowed else set()
        subj, to_sel = "", chosen
        if parent:
            subj = parent["subject"] if parent["subject"].lower().startswith("re:") else "Re: " + parent["subject"]
            others = {parent["sender"]} if parent["sender"] != session["user"] else {
                r["login"] for r in con.execute("SELECT login FROM mail_rcpt WHERE msg_id=?", (parent["id"],))}
            to_sel = {l for l in others if l in allowed}
        return render_template("mail_new.html", targets=targets, depts=sorted({u["dept"] for u in targets if u["dept"]}),
                               can_bc=role in BROADCAST_ROLES, parent=parent, f={"subject": subj}, chosen=to_sel)
    finally:
        con.close()


@route("/mail/<int:msg_id>")
@login_required
def mail_view(msg_id):
    me = session["user"]
    con = db.connect()
    try:
        m = con.execute("SELECT * FROM mail_msgs WHERE id=?", (msg_id,)).fetchone()
        if m is None:
            abort(404)
        mine = con.execute("SELECT * FROM mail_rcpt WHERE msg_id=? AND login=?", (msg_id, me)).fetchone()
        if m["sender"] != me and mine is None:
            abort(403)
        thread = con.execute("""SELECT m.* FROM mail_msgs m WHERE m.thread_id=? AND (m.sender=? OR EXISTS
                                (SELECT 1 FROM mail_rcpt r WHERE r.msg_id=m.id AND r.login=?)) ORDER BY m.id""",
                             (m["thread_id"] or m["id"], me, me)).fetchall()
        ids = [t["id"] for t in thread]
        con.execute("UPDATE mail_rcpt SET read_at=? WHERE login=? AND read_at IS NULL AND msg_id IN (%s)" % ",".join("?" * len(ids)),
                    [_now(), me] + ids)
        files, rc = {}, {}
        for r in con.execute("SELECT * FROM mail_files WHERE msg_id IN (%s) ORDER BY id" % ",".join("?" * len(ids)), ids):
            files.setdefault(r["msg_id"], []).append(r)
        for r in con.execute("SELECT * FROM mail_rcpt WHERE msg_id IN (%s) ORDER BY id" % ",".join("?" * len(ids)), ids):
            rc.setdefault(r["msg_id"], []).append(r)
        names = _names(con)
    finally:
        con.close()
    return render_template("mail_view.html", m=m, thread=thread, files=files, rc=rc, names=names, me=me, last=thread[-1])


@route("/mail/file/<int:file_id>")
@login_required
def mail_file(file_id):
    me = session["user"]
    con = db.connect()
    try:
        row = con.execute("SELECT f.*, m.sender FROM mail_files f JOIN mail_msgs m ON m.id=f.msg_id WHERE f.id=?", (file_id,)).fetchone()
        if row is None:
            abort(404)
        if row["sender"] != me and con.execute("SELECT 1 FROM mail_rcpt WHERE msg_id=? AND login=?", (row["msg_id"], me)).fetchone() is None:
            abort(403)
    finally:
        con.close()
    path = extras.files_dir() / row["stored"]
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=row["filename"])


@route("/mail/<int:msg_id>/delete", ("POST",))
@login_required
def mail_delete(msg_id):
    me = session["user"]
    con = db.connect()
    try:
        m = con.execute("SELECT sender FROM mail_msgs WHERE id=?", (msg_id,)).fetchone()
        if m is None:
            abort(404)
        con.execute("UPDATE mail_rcpt SET deleted=1 WHERE msg_id=? AND login=?", (msg_id, me))
        if m["sender"] == me:
            con.execute("UPDATE mail_msgs SET sender_deleted=1 WHERE id=?", (msg_id,))
    finally:
        con.close()
    flash("Письмо удалено из вашего списка.", "ok")
    return redirect(url_for("mail_inbox"))


def system_send(con, sender, rcpts, subject, body):
    """Служебное письмо от имени sender (из других модулей, например при поручении по входящему письму)."""
    rcpts = sorted({r for r in rcpts if r and r != sender})
    if not rcpts:
        return
    cur = con.execute("INSERT INTO mail_msgs(sender, subject, body, created_at, broadcast) VALUES(?,?,?,?, '')",
                      (sender, subject[:200], body[:20000], _now()))
    mid = cur.lastrowid
    con.execute("UPDATE mail_msgs SET thread_id=? WHERE id=?", (mid, mid))
    for r in rcpts:
        con.execute("INSERT INTO mail_rcpt(msg_id, login) VALUES(?,?)", (mid, r))
