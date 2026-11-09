"""Входящая корреспонденция и контроль исполнения: регистрация с номером, направление исполнителям,
отчёты об исполнении, приёмка или возврат на доработку, продление сроков, напоминания, сводка."""
import secrets
from datetime import date, datetime

from flask import abort, flash, redirect, render_template, request, send_file, session, url_for

import db
import extras
import mail
from extras import csv_response, login_required, route

M = None

DDL = """
CREATE TABLE IF NOT EXISTS corr_in (
    id INTEGER PRIMARY KEY AUTOINCREMENT, reg_no TEXT NOT NULL UNIQUE, seq INTEGER NOT NULL, reg_year INTEGER NOT NULL,
    reg_date TEXT NOT NULL, sender_org TEXT NOT NULL, out_no TEXT NOT NULL DEFAULT '', out_date TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'letter', subject TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', pages INTEGER NOT NULL DEFAULT 0,
    deadline TEXT NOT NULL DEFAULT '', resolution TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'registered',
    answer_no TEXT NOT NULL DEFAULT '', answer_date TEXT NOT NULL DEFAULT '', close_note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL, created_at TEXT NOT NULL, closed_at TEXT);
CREATE TABLE IF NOT EXISTS corr_exec (
    id INTEGER PRIMARY KEY AUTOINCREMENT, corr_id INTEGER NOT NULL REFERENCES corr_in(id) ON DELETE CASCADE,
    login TEXT NOT NULL, is_main INTEGER NOT NULL DEFAULT 0, task TEXT NOT NULL DEFAULT '', deadline TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'assigned', result TEXT NOT NULL DEFAULT '', report TEXT NOT NULL DEFAULT '',
    reported_at TEXT, remark TEXT NOT NULL DEFAULT '', rounds INTEGER NOT NULL DEFAULT 0,
    assigned_by TEXT NOT NULL, assigned_at TEXT NOT NULL, accepted_at TEXT);
CREATE INDEX IF NOT EXISTS ix_corr_exec_login ON corr_exec(login, status);
CREATE TABLE IF NOT EXISTS corr_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT, corr_id INTEGER NOT NULL REFERENCES corr_in(id) ON DELETE CASCADE,
    exec_id INTEGER, what TEXT NOT NULL DEFAULT 'letter', filename TEXT NOT NULL, stored TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0, uploaded_by TEXT NOT NULL, uploaded_at TEXT NOT NULL);
"""
MANAGE = ("clerk", "admin", "director")
NUM_FMT = "KR-{seq:04d}/{yy}"
KINDS = {"letter": "Письмо", "request": "Запрос / обращение", "order_up": "Поручение вышестоящей организации",
         "claim": "Жалоба / заявление", "notice": "Уведомление / извещение", "other": "Другое"}
RESULTS = {"done": "Выполнено полностью", "partial": "Выполнено частично", "not_done": "Не выполнено"}
ST_EXEC = {"assigned": "На исполнении", "reported": "На проверке", "accepted": "Принято", "returned": "Возвращено на доработку"}
ST_CORR = {"registered": "Зарегистрировано", "in_work": "На исполнении", "review": "На проверке",
           "done": "Исполнено, ждёт закрытия", "closed": "Закрыто"}
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
    def _corr_badge():
        if "user" not in session or session.get("role") == "employee":
            return {}
        con = db.connect()
        try:
            me = session["user"]
            mine = con.execute("SELECT COUNT(*) FROM corr_exec x JOIN corr_in c ON c.id=x.corr_id "
                               "WHERE x.login=? AND x.status IN ('assigned','returned') AND c.status<>'closed'", (me,)).fetchone()[0]
            rev = 0
            if session.get("role") in MANAGE:
                rev = con.execute("SELECT COUNT(*) FROM corr_exec WHERE status='reported'").fetchone()[0]
        finally:
            con.close()
        return {"corr_mine": mine, "corr_review": rev, "corr_badge": mine + rev}

    @M.app.template_global()
    def corr_manage():
        return session.get("role") in MANAGE


def _now():
    return db.now_local().strftime("%Y-%m-%d %H:%M")


def _today():
    return db.now_local().strftime("%Y-%m-%d")


def _manage():
    return session.get("role") in MANAGE


def _need_manage():
    if not _manage():
        abort(403)


def _d(s):
    s = (s or "").strip()
    if not s:
        return ""
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None
    return s


def _log(con, cid, what, comment=""):
    extras.log_event(con, "corr", cid, what, comment)


def _name(con, login_):
    return extras.user_name(con, login_)


def _recalc(con, cid):
    """Статус письма по статусам исполнителей (закрытое не трогаем)."""
    c = con.execute("SELECT status FROM corr_in WHERE id=?", (cid,)).fetchone()
    if c is None or c["status"] == "closed":
        return
    st = [r["status"] for r in con.execute("SELECT status FROM corr_exec WHERE corr_id=?", (cid,))]
    if not st:
        new = "registered"
    elif all(s == "accepted" for s in st):
        new = "done"
    elif any(s in ("assigned", "returned") for s in st):
        new = "in_work"
    else:
        new = "review"
    con.execute("UPDATE corr_in SET status=? WHERE id=?", (new, cid))


def _is_overdue(dl, status):
    return bool(dl) and dl < _today() and status in ("assigned", "returned")


def _visible(con, cid):
    """Письмо может открыть управляющий или исполнитель."""
    if _manage():
        return True
    return con.execute("SELECT 1 FROM corr_exec WHERE corr_id=? AND login=?", (cid, session["user"])).fetchone() is not None


def _get(con, cid):
    c = con.execute("SELECT * FROM corr_in WHERE id=?", (cid,)).fetchone()
    if c is None:
        abort(404)
    if not _visible(con, cid):
        abort(403)
    return c


def _save_files(con, cid, exec_id, what, field="files"):
    """Принять файлы из формы. Возвращает текст ошибки или ''. Файлы пишутся только если все прошли проверку."""
    blobs = []
    for x in [x for x in request.files.getlist(field) if x and x.filename][:MAX_FILES]:
        name = extras._clean_name(x.filename)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        blob = x.read(extras.MAX_FILE + 1)
        if ext not in extras.ALLOWED_EXT:
            return f"Файл «{name}»: такой тип нельзя (разрешены pdf, jpg, png, doc, docx, xls, xlsx)."
        if len(blob) > extras.MAX_FILE:
            return f"Файл «{name}» больше 10 МБ."
        if not blob:
            return f"Файл «{name}» пустой."
        blobs.append((name, ext, blob))
    for name, ext, blob in blobs:
        stored = secrets.token_hex(16) + "." + ext
        (extras.files_dir() / stored).write_bytes(blob)
        con.execute("INSERT INTO corr_files(corr_id, exec_id, what, filename, stored, size, uploaded_by, uploaded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (cid, exec_id, what, name, stored, len(blob), session["user"], _now()))
    return ""


def _notify(con, rcpts, subject, body):
    mail.system_send(con, session["user"], rcpts, subject, body)


def _next_reg(con, reg_date):
    y = int(reg_date[:4])
    n = db.next_counter(con, f"incoming:{y}")
    return n, y, NUM_FMT.format(seq=n, yy=str(y)[2:])


# ---------- список ----------

def _rows(con, scope, q, year):
    me = session["user"]
    sql = ["SELECT c.* FROM corr_in c WHERE 1=1"]
    args = []
    if not _manage():
        sql.append("AND EXISTS (SELECT 1 FROM corr_exec x WHERE x.corr_id=c.id AND x.login=?)")
        args.append(me)
    if year:
        sql.append("AND c.reg_year=?"); args.append(year)
    if q:
        like = f"%{q}%"
        sql.append("AND (c.reg_no LIKE ? OR c.sender_org LIKE ? OR c.subject LIKE ? OR c.out_no LIKE ? OR c.summary LIKE ?)")
        args += [like] * 5
    sql.append("ORDER BY c.id DESC LIMIT 1000")
    out = []
    for c in con.execute(" ".join(sql), args).fetchall():
        ex = con.execute("SELECT x.*, u.full_name FROM corr_exec x LEFT JOIN users u ON u.login=x.login WHERE x.corr_id=? ORDER BY x.is_main DESC, x.id", (c["id"],)).fetchall()
        late = c["status"] != "closed" and any(_is_overdue(x["deadline"] or c["deadline"], x["status"]) for x in ex)
        mine = any(x["login"] == me and x["status"] in ("assigned", "returned") for x in ex)
        out.append({"c": c, "ex": ex, "late": late, "mine": mine})
    if scope == "mine":
        out = [r for r in out if r["mine"]]
    elif scope == "late":
        out = [r for r in out if r["late"]]
    elif scope == "review":
        out = [r for r in out if r["c"]["status"] == "review" or any(x["status"] == "reported" for x in r["ex"])]
    elif scope == "new":
        out = [r for r in out if r["c"]["status"] == "registered"]
    elif scope == "closed":
        out = [r for r in out if r["c"]["status"] == "closed"]
    elif scope == "open":
        out = [r for r in out if r["c"]["status"] != "closed"]
    return out


@route("/corr")
@login_required
def corr_list():
    scope = request.args.get("scope") or ("open" if _manage() else "mine")
    q = " ".join(request.args.get("q", "").split())[:100]
    year = request.args.get("year", type=int)
    con = db.connect()
    try:
        rows = _rows(con, scope, q, year)
        years = [r[0] for r in con.execute("SELECT DISTINCT reg_year FROM corr_in ORDER BY 1 DESC")]
        names = {u["login"]: u["name"] for u in extras.people(con)}
    finally:
        con.close()
    return render_template("corr_list.html", rows=rows, scope=scope, q=q, year=year, years=years, names=names,
                           KINDS=KINDS, ST_CORR=ST_CORR, today=_today())


@route("/corr/export.csv")
@login_required
def corr_export():
    _need_manage()
    year = request.args.get("year", type=int)
    con = db.connect()
    try:
        rows = _rows(con, "all", "", year)
        out = []
        for r in rows:
            c = r["c"]
            ex = "; ".join(f"{x['full_name'] or x['login']}{' (отв.)' if x['is_main'] else ''} — {ST_EXEC[x['status']]}" for x in r["ex"])
            out.append([c["reg_no"], c["reg_date"], c["sender_org"], c["out_no"], c["out_date"], KINDS.get(c["kind"], c["kind"]),
                        c["subject"], c["pages"], c["deadline"], ex, ST_CORR[c["status"]], "да" if r["late"] else "",
                        c["answer_no"], c["answer_date"]])
    finally:
        con.close()
    return csv_response("incoming_register.csv",
                        ["Рег. №", "Дата регистрации", "От кого", "Исх. №", "Исх. дата", "Вид", "Тема", "Листов", "Срок",
                         "Исполнители", "Состояние", "Просрочено", "Ответ №", "Ответ дата"], out)


# ---------- регистрация ----------

def _form_vals(f):
    return {k: " ".join(f.get(k, "").split()) if k not in ("summary", "resolution") else f.get(k, "").strip()
            for k in ("reg_date", "sender_org", "out_no", "out_date", "kind", "subject", "summary", "pages", "deadline", "resolution")}


def _validate(v):
    errs = []
    if _d(v["reg_date"]) in (None, ""):
        errs.append("Дата регистрации: укажите дату.")
    if not v["sender_org"]:
        errs.append("Укажите, от кого пришло письмо.")
    if not v["subject"]:
        errs.append("Укажите тему (краткое содержание).")
    if _d(v["out_date"]) is None:
        errs.append("Исходящая дата письма указана неверно.")
    if _d(v["deadline"]) is None:
        errs.append("Срок исполнения указан неверно.")
    if v["pages"] and not v["pages"].isdigit():
        errs.append("Количество листов — число.")
    if v["kind"] not in KINDS:
        v["kind"] = "letter"
    return errs


@route("/corr/new", ("GET", "POST"))
@login_required
def corr_new():
    _need_manage()
    v = {"reg_date": _today(), "kind": "letter"}
    if request.method == "POST":
        v = _form_vals(request.form)
        errs = _validate(v)
        if errs:
            for e in errs:
                flash(e, "error")
        else:
            con = db.connect()
            try:
                con.execute("BEGIN IMMEDIATE")
                try:
                    seq, y, reg = _next_reg(con, v["reg_date"])
                    cur = con.execute("""INSERT INTO corr_in(reg_no, seq, reg_year, reg_date, sender_org, out_no, out_date, kind, subject,
                                         summary, pages, deadline, resolution, created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                      (reg, seq, y, v["reg_date"], v["sender_org"], v["out_no"], v["out_date"], v["kind"], v["subject"],
                                       v["summary"], int(v["pages"] or 0), v["deadline"], v["resolution"], session["user"], _now()))
                    cid = cur.lastrowid
                    err = _save_files(con, cid, None, "letter")
                    if err:
                        raise ValueError(err)
                    _log(con, cid, f"Зарегистрировано под номером {reg}")
                    con.execute("COMMIT")
                except Exception as e:                                           # noqa: BLE001
                    con.execute("ROLLBACK")
                    flash(str(e) if isinstance(e, ValueError) else "Не удалось зарегистрировать письмо.", "error")
                    return render_template("corr_form.html", v=v, KINDS=KINDS, edit=None)
            finally:
                con.close()
            flash(f"Письмо зарегистрировано: № {reg}. Теперь направьте его исполнителям.", "ok")
            return redirect(url_for("corr_view", cid=cid))
    return render_template("corr_form.html", v=v, KINDS=KINDS, edit=None)


@route("/corr/<int:cid>/edit", ("GET", "POST"))
@login_required
def corr_edit(cid):
    _need_manage()
    con = db.connect()
    try:
        c = _get(con, cid)
        if c["status"] == "closed":
            flash("Закрытое письмо менять нельзя. Сначала откройте его заново.", "error")
            return redirect(url_for("corr_view", cid=cid))
        v = dict(c)
        if request.method == "POST":
            v = _form_vals(request.form)
            v["reg_date"] = c["reg_date"]                     # номер и дата регистрации не меняются
            errs = _validate(v)
            if errs:
                for e in errs:
                    flash(e, "error")
            else:
                err = _save_files(con, cid, None, "letter")
                if err:
                    flash(err, "error")
                else:
                    con.execute("""UPDATE corr_in SET sender_org=?, out_no=?, out_date=?, kind=?, subject=?, summary=?, pages=?, deadline=?, resolution=?
                                   WHERE id=?""", (v["sender_org"], v["out_no"], v["out_date"], v["kind"], v["subject"], v["summary"],
                                                    int(v["pages"] or 0), v["deadline"], v["resolution"], cid))
                    _log(con, cid, "Изменены данные письма")
                    flash("Сохранено.", "ok")
                    return redirect(url_for("corr_view", cid=cid))
    finally:
        con.close()
    return render_template("corr_form.html", v=v, KINDS=KINDS, edit=c)


# ---------- карточка ----------

@route("/corr/<int:cid>")
@login_required
def corr_view(cid):
    me = session["user"]
    con = db.connect()
    try:
        c = _get(con, cid)
        ex = []
        files_ex = {}
        for f in con.execute("SELECT * FROM corr_files WHERE corr_id=? ORDER BY id", (cid,)):
            files_ex.setdefault(f["exec_id"], []).append(f)
        for x in con.execute("SELECT * FROM corr_exec WHERE corr_id=? ORDER BY is_main DESC, id", (cid,)):
            d = dict(x)
            d["name"] = _name(con, x["login"])
            d["late"] = _is_overdue(x["deadline"] or c["deadline"], x["status"])
            d["files"] = files_ex.get(x["id"], [])
            d["mine"] = x["login"] == me
            ex.append(d)
        used = {x["login"] for x in ex}
        people = [p for p in extras.people(con) if p["login"] not in used] if _manage() else []
        log = extras.log_events(con, "corr", cid)
        letter_files = files_ex.get(None, [])
    finally:
        con.close()
    return render_template("corr_view.html", c=c, ex=ex, people=people, log=log, letter_files=letter_files, KINDS=KINDS, ST_CORR=ST_CORR,
                           ST_EXEC=ST_EXEC, RESULTS=RESULTS, manage=_manage(), today=_today(),
                           late=c["status"] != "closed" and any(x["late"] for x in ex))


@route("/corr/<int:cid>/card")
@login_required
def corr_card(cid):
    con = db.connect()
    try:
        c = _get(con, cid)
        ex = [dict(x, name=_name(con, x["login"])) for x in con.execute("SELECT * FROM corr_exec WHERE corr_id=? ORDER BY is_main DESC, id", (cid,))]
    finally:
        con.close()
    return render_template("corr_card.html", c=c, ex=ex, KINDS=KINDS, ST_EXEC=ST_EXEC)


@route("/corr/file/<int:fid>")
@login_required
def corr_file(fid):
    con = db.connect()
    try:
        f = con.execute("SELECT * FROM corr_files WHERE id=?", (fid,)).fetchone()
        if f is None:
            abort(404)
        _get(con, f["corr_id"])
    finally:
        con.close()
    path = extras.files_dir() / f["stored"]
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=f["filename"])


@route("/corr/<int:cid>/files", ("POST",))
@login_required
def corr_add_files(cid):
    _need_manage()
    con = db.connect()
    try:
        c = _get(con, cid)
        err = _save_files(con, cid, None, "letter")
        flash(err or "Файлы добавлены.", "error" if err else "ok")
        if not err:
            _log(con, cid, "Добавлены файлы к письму")
    finally:
        con.close()
    return redirect(url_for("corr_view", cid=cid))


# ---------- направление исполнителям ----------

@route("/corr/<int:cid>/route", ("POST",))
@login_required
def corr_route(cid):
    _need_manage()
    f = request.form
    con = db.connect()
    try:
        c = _get(con, cid)
        if c["status"] == "closed":
            flash("Письмо закрыто.", "error")
            return redirect(url_for("corr_view", cid=cid))
        allowed = {p["login"] for p in extras.people(con)}
        logins = [l for l in dict.fromkeys(f.getlist("who")) if l in allowed]
        dl = _d(f.get("deadline")) 
        task = f.get("task", "").strip()[:2000]
        main = f.get("main", "")
        if not logins:
            flash("Выберите хотя бы одного исполнителя.", "error")
        elif dl is None:
            flash("Срок указан неверно.", "error")
        elif not (dl or c["deadline"]):
            flash("Укажите срок исполнения (для письма или для исполнителя).", "error")
        else:
            has_main = con.execute("SELECT 1 FROM corr_exec WHERE corr_id=? AND is_main=1", (cid,)).fetchone() is not None
            if main not in logins:
                main = logins[0] if not has_main else ""
            added = []
            for l in logins:
                if con.execute("SELECT 1 FROM corr_exec WHERE corr_id=? AND login=?", (cid, l)).fetchone():
                    continue
                con.execute("INSERT INTO corr_exec(corr_id, login, is_main, task, deadline, assigned_by, assigned_at) VALUES(?,?,?,?,?,?,?)",
                            (cid, l, 1 if l == main else 0, task, dl or "", session["user"], _now()))
                added.append(l)
                _log(con, cid, f"Направлено на исполнение: {_name(con, l)}{' (ответственный)' if l == main else ''}",
                     f"срок {dl or c['deadline']}" + (f"; {task}" if task else ""))
            if f.get("resolution", "").strip():
                con.execute("UPDATE corr_in SET resolution=? WHERE id=?", (f["resolution"].strip()[:2000], cid))
            if added:
                _recalc(con, cid)
                _notify(con, added, f"На исполнение: {c['reg_no']} — {c['subject']}"[:200],
                        f"Вам направлено входящее письмо {c['reg_no']} от {c['sender_org']}.\nТема: {c['subject']}\n"
                        f"Срок: {dl or c['deadline'] or 'не указан'}\n" + (f"Поручение: {task}\n" if task else "") +
                        "\nОткройте раздел «Контроль исполнения», прочитайте письмо и пришлите отчёт.")
                flash(f"Направлено исполнителям: {len(added)}.", "ok")
            else:
                flash("Эти исполнители уже назначены.", "error")
    finally:
        con.close()
    return redirect(url_for("corr_view", cid=cid))


@route("/corr/exec/<int:eid>/remove", ("POST",))
@login_required
def corr_exec_remove(eid):
    _need_manage()
    con = db.connect()
    try:
        x = con.execute("SELECT * FROM corr_exec WHERE id=?", (eid,)).fetchone()
        if x is None:
            abort(404)
        c = _get(con, x["corr_id"])
        if c["status"] == "closed" or x["status"] == "accepted":
            flash("Нельзя убрать исполнителя: письмо закрыто или его работа уже принята.", "error")
        else:
            con.execute("DELETE FROM corr_files WHERE exec_id=?", (eid,))
            con.execute("DELETE FROM corr_exec WHERE id=?", (eid,))
            _log(con, c["id"], f"Снят с исполнения: {_name(con, x['login'])}")
            if x["is_main"]:
                nxt = con.execute("SELECT id FROM corr_exec WHERE corr_id=? ORDER BY id LIMIT 1", (c["id"],)).fetchone()
                if nxt:
                    con.execute("UPDATE corr_exec SET is_main=1 WHERE id=?", (nxt["id"],))
            _recalc(con, c["id"])
            flash("Исполнитель убран.", "ok")
    finally:
        con.close()
    return redirect(url_for("corr_view", cid=c["id"]))


# ---------- отчёт исполнителя ----------

@route("/corr/exec/<int:eid>/report", ("POST",))
@login_required
def corr_report(eid):
    f = request.form
    con = db.connect()
    try:
        x = con.execute("SELECT * FROM corr_exec WHERE id=?", (eid,)).fetchone()
        if x is None:
            abort(404)
        if x["login"] != session["user"]:
            abort(403)
        c = _get(con, x["corr_id"])
        text = f.get("report", "").strip()[:5000]
        res = f.get("result", "")
        if c["status"] == "closed" or x["status"] not in ("assigned", "returned"):
            flash("Отчёт сейчас отправить нельзя.", "error")
        elif res not in RESULTS:
            flash("Выберите результат: выполнено, частично или не выполнено.", "error")
        elif not text:
            flash("Напишите, что сделано.", "error")
        else:
            err = _save_files(con, c["id"], eid, "report")
            if err:
                flash(err, "error")
            else:
                con.execute("UPDATE corr_exec SET status='reported', result=?, report=?, reported_at=?, remark='' WHERE id=?",
                            (res, text, _now(), eid))
                _log(con, c["id"], f"Отчёт об исполнении: {RESULTS[res]}", text[:300])
                _recalc(con, c["id"])
                boss = {x["assigned_by"], c["created_by"]}
                _notify(con, boss, f"Отчёт по {c['reg_no']}: {RESULTS[res]}"[:200],
                        f"{_name(con, session['user'])} прислал отчёт по письму {c['reg_no']} ({c['subject']}).\nРезультат: {RESULTS[res]}\n{text}\n\n"
                        "Откройте письмо в «Контроле исполнения», чтобы принять отчёт или вернуть на доработку.")
                flash("Отчёт отправлен контролёру.", "ok")
    finally:
        con.close()
    return redirect(url_for("corr_view", cid=c["id"]))


# ---------- проверка отчёта: принять или вернуть ----------

@route("/corr/exec/<int:eid>/review", ("POST",))
@login_required
def corr_review(eid):
    _need_manage()
    f = request.form
    con = db.connect()
    try:
        x = con.execute("SELECT * FROM corr_exec WHERE id=?", (eid,)).fetchone()
        if x is None:
            abort(404)
        c = _get(con, x["corr_id"])
        act = f.get("action")
        comment = f.get("comment", "").strip()[:2000]
        dl = _d(f.get("deadline"))
        if c["status"] == "closed" or x["status"] != "reported":
            flash("Этот отчёт сейчас проверять не нужно.", "error")
        elif act == "accept":
            con.execute("UPDATE corr_exec SET status='accepted', accepted_at=?, remark=? WHERE id=?", (_now(), comment, eid))
            _log(con, c["id"], f"Принято исполнение: {_name(con, x['login'])}", comment)
            _recalc(con, c["id"])
            _notify(con, [x["login"]], f"Принято: {c['reg_no']}"[:200], f"Ваше исполнение по письму {c['reg_no']} принято.\n{comment}")
            flash("Исполнение принято.", "ok")
        elif act == "return":
            if not comment:
                flash("Напишите, что именно нужно доработать.", "error")
            elif dl is None:
                flash("Новый срок указан неверно.", "error")
            elif dl and dl < _today():
                flash("Новый срок не может быть в прошлом.", "error")
            else:
                con.execute("UPDATE corr_exec SET status='returned', remark=?, rounds=rounds+1, deadline=CASE WHEN ?<>'' THEN ? ELSE deadline END WHERE id=?",
                            (comment, dl or "", dl or "", eid))
                _log(con, c["id"], f"Возвращено на доработку: {_name(con, x['login'])}", comment + (f" Новый срок: {dl}" if dl else ""))
                _recalc(con, c["id"])
                _notify(con, [x["login"]], f"Доработать: {c['reg_no']} — {c['subject']}"[:200],
                        f"Ваш отчёт по письму {c['reg_no']} возвращён на доработку.\nЗамечание: {comment}\n"
                        f"Срок: {dl or x['deadline'] or c['deadline'] or 'не указан'}")
                flash("Возвращено исполнителю на доработку.", "ok")
        else:
            flash("Выберите действие.", "error")
    finally:
        con.close()
    return redirect(url_for("corr_view", cid=c["id"]))


@route("/corr/exec/<int:eid>/extend", ("POST",))
@login_required
def corr_extend(eid):
    _need_manage()
    f = request.form
    con = db.connect()
    try:
        x = con.execute("SELECT * FROM corr_exec WHERE id=?", (eid,)).fetchone()
        if x is None:
            abort(404)
        c = _get(con, x["corr_id"])
        dl = _d(f.get("deadline"))
        if c["status"] == "closed" or x["status"] not in ("assigned", "returned"):
            flash("Срок сейчас менять не нужно.", "error")
        elif not dl:
            flash("Укажите новый срок.", "error")
        else:
            con.execute("UPDATE corr_exec SET deadline=? WHERE id=?", (dl, eid))
            _log(con, c["id"], f"Срок продлён: {_name(con, x['login'])}", f"новый срок {dl}; " + f.get("reason", "").strip()[:500])
            _notify(con, [x["login"]], f"Новый срок: {c['reg_no']}"[:200], f"Срок исполнения письма {c['reg_no']} изменён: {dl}.")
            flash("Срок изменён.", "ok")
    finally:
        con.close()
    return redirect(url_for("corr_view", cid=c["id"]))


@route("/corr/<int:cid>/remind", ("POST",))
@login_required
def corr_remind(cid):
    _need_manage()
    con = db.connect()
    try:
        c = _get(con, cid)
        who = [x["login"] for x in con.execute("SELECT login FROM corr_exec WHERE corr_id=? AND status IN ('assigned','returned')", (cid,))]
        if c["status"] == "closed" or not who:
            flash("Напоминать некому.", "error")
        else:
            _notify(con, who, f"Напоминание: {c['reg_no']} — {c['subject']}"[:200],
                    f"Напоминаем об исполнении письма {c['reg_no']} ({c['subject']}). Срок: {c['deadline'] or 'см. поручение'}.")
            _log(con, cid, f"Отправлено напоминание: {len(who)} чел.")
            flash(f"Напоминание отправлено: {len(who)}.", "ok")
    finally:
        con.close()
    return redirect(url_for("corr_view", cid=cid))


# ---------- закрытие ----------

@route("/corr/<int:cid>/close", ("POST",))
@login_required
def corr_close(cid):
    _need_manage()
    f = request.form
    con = db.connect()
    try:
        c = _get(con, cid)
        st = [r["status"] for r in con.execute("SELECT status FROM corr_exec WHERE corr_id=?", (cid,))]
        ad = _d(f.get("answer_date"))
        note = f.get("close_note", "").strip()[:1000]
        if c["status"] == "closed":
            flash("Уже закрыто.", "error")
        elif any(s != "accepted" for s in st):
            flash("Закрыть нельзя: не все исполнители сдали и не по всем принято исполнение.", "error")
        elif not st and not note:
            flash("Письмо никому не направлялось. Напишите причину закрытия (например, «к сведению»).", "error")
        elif ad is None:
            flash("Дата ответа указана неверно.", "error")
        else:
            con.execute("UPDATE corr_in SET status='closed', closed_at=?, answer_no=?, answer_date=?, close_note=? WHERE id=?",
                        (_now(), f.get("answer_no", "").strip()[:60], ad or "", note, cid))
            _log(con, cid, "Письмо закрыто (снято с контроля)", (f"Ответ № {f.get('answer_no', '').strip()} от {ad}. " if f.get("answer_no") else "") + note)
            flash("Письмо закрыто.", "ok")
    finally:
        con.close()
    return redirect(url_for("corr_view", cid=cid))


@route("/corr/<int:cid>/reopen", ("POST",))
@login_required
def corr_reopen(cid):
    _need_manage()
    con = db.connect()
    try:
        c = _get(con, cid)
        if c["status"] != "closed":
            flash("Письмо не закрыто.", "error")
        else:
            con.execute("UPDATE corr_in SET status='registered', closed_at=NULL WHERE id=?", (cid,))
            _recalc(con, cid)
            _log(con, cid, "Письмо открыто заново")
            flash("Письмо снова на контроле.", "ok")
    finally:
        con.close()
    return redirect(url_for("corr_view", cid=cid))


# ---------- сводка по исполнителям ----------

@route("/corr/summary")
@login_required
def corr_summary():
    _need_manage()
    year = request.args.get("year", type=int)
    con = db.connect()
    try:
        args = []
        yc = ""
        if year:
            yc = "AND c.reg_year=?"; args.append(year)
        data = {}
        for x in con.execute(f"""SELECT x.*, c.deadline AS cdl, c.status AS cst FROM corr_exec x JOIN corr_in c ON c.id=x.corr_id
                                 WHERE 1=1 {yc}""", args):
            d = data.setdefault(x["login"], {"login": x["login"], "total": 0, "work": 0, "review": 0, "done": 0, "late": 0, "returned": 0})
            d["total"] += 1
            d["returned"] += x["rounds"]
            if x["status"] in ("assigned", "returned"):
                d["work"] += 1
                if _is_overdue(x["deadline"] or x["cdl"], x["status"]):
                    d["late"] += 1
            elif x["status"] == "reported":
                d["review"] += 1
            else:
                d["done"] += 1
        for d in data.values():
            d["name"] = _name(con, d["login"])
        rows = sorted(data.values(), key=lambda d: (-d["late"], -d["work"], d["name"]))
        years = [r[0] for r in con.execute("SELECT DISTINCT reg_year FROM corr_in ORDER BY 1 DESC")]
        tot = {"all": con.execute(f"SELECT COUNT(*) FROM corr_in c WHERE 1=1 {yc}", args).fetchone()[0],
               "closed": con.execute(f"SELECT COUNT(*) FROM corr_in c WHERE c.status='closed' {yc}", args).fetchone()[0],
               "new": con.execute(f"SELECT COUNT(*) FROM corr_in c WHERE c.status='registered' {yc}", args).fetchone()[0]}
    finally:
        con.close()
    return render_template("corr_summary.html", rows=rows, years=years, year=year, tot=tot)
