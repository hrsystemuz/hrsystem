"""Общие документы: заголовок, несколько пунктов с форматированием, связанные документы, файлы,
таблица подписантов (согласование → подпись → исполнение / к сведению)."""
import json
import re
import secrets

from flask import abort, flash, redirect, render_template, request, send_file, session, url_for

import db
import extras
from extras import login_required, route

M = None

DDL = """
CREATE TABLE IF NOT EXISTS gdocs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL DEFAULT 'Приказ',
    title          TEXT NOT NULL DEFAULT '',
    items          TEXT NOT NULL DEFAULT '[]',
    related        TEXT NOT NULL DEFAULT '[]',
    author         TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'draft',
    number         TEXT,
    created_at     TEXT NOT NULL DEFAULT '',
    submitted_at   TEXT,
    finished_at    TEXT,
    return_comment TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS gdoc_steps (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id   INTEGER NOT NULL REFERENCES gdocs(id) ON DELETE CASCADE,
    ord      INTEGER NOT NULL DEFAULT 0,
    login    TEXT NOT NULL,
    name     TEXT NOT NULL DEFAULT '',
    title    TEXT NOT NULL DEFAULT '',
    action   TEXT NOT NULL DEFAULT 'approve',
    status   TEXT NOT NULL DEFAULT 'pending',
    comment  TEXT NOT NULL DEFAULT '',
    acted_at TEXT,
    due      TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS gdoc_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      INTEGER NOT NULL REFERENCES gdocs(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    stored      TEXT NOT NULL UNIQUE,
    size        INTEGER NOT NULL DEFAULT 0,
    uploaded_by TEXT NOT NULL DEFAULT '',
    uploaded_at TEXT NOT NULL DEFAULT ''
);
"""

COMMISSION = "Приказ о комиссии"
KINDS = ["Приказ", COMMISSION, "Распоряжение", "Служебная записка", "Протокол", "Письмо", "Акт", "Другое"]
CM_ROLES = {"chair": "Председатель комиссии", "member": "Член комиссии"}
ACTIONS = {"approve": "Согласование", "sign": "Подпись (утверждает)", "execute": "На исполнение", "info": "К сведению"}
STATUS_RU = {"draft": "черновик", "review": "на согласовании", "returned": "возвращён на доработку",
             "signed": "подписан", "rejected": "отказано", "done": "исполнен"}
MAX_ITEMS = 30


def setup(app_module):
    global M
    M = app_module
    con = db.connect()
    try:
        con.executescript(DDL)
        if "due" not in {r["name"] for r in con.execute("PRAGMA table_info(gdoc_steps)")}:
            con.execute("ALTER TABLE gdoc_steps ADD COLUMN due TEXT NOT NULL DEFAULT ''")
        have = {r["name"] for r in con.execute("PRAGMA table_info(gdoc_steps)")}
        for col in ("instruction", "assigned_by"):
            if col not in have:
                con.execute(f"ALTER TABLE gdoc_steps ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        if "accepted_at" not in have:
            con.execute("ALTER TABLE gdoc_steps ADD COLUMN accepted_at TEXT")
        if "commission" not in {r["name"] for r in con.execute("PRAGMA table_info(gdocs)")}:
            con.execute("ALTER TABLE gdocs ADD COLUMN commission TEXT NOT NULL DEFAULT ''")
    finally:
        con.close()


def _now():
    return db.now_local().strftime("%Y-%m-%d %H:%M")


# ---------- общие функции ----------

def _doc(con, doc_id):
    d = con.execute("SELECT * FROM gdocs WHERE id=?", (doc_id,)).fetchone()
    if d is None:
        abort(404)
    return d


def can_see(con, d):
    """Видимость: автор и администратор — всегда; участники — только когда до них дошла очередь
    (или они уже действовали). После подписания документ виден всем участникам, кадровикам и директору."""
    me, role = session["user"], session.get("role")
    if role == "admin" or d["author"] == me:
        return True
    mine = con.execute("SELECT * FROM gdoc_steps WHERE doc_id=? AND login=?", (d["id"], me)).fetchall()
    if d["status"] in ("signed", "done"):
        return role in ("hr", "director") or bool(mine)
    if d["status"] == "draft":
        return False
    cur_ids = {c["id"] for c in current_steps(con, d["id"])} if d["status"] == "review" else set()
    for st in mine:
        if st["action"] in ("approve", "sign") and (st["status"] != "pending" or st["id"] in cur_ids):
            return True
    return False


def current_steps(con, doc_id):
    """Кто сейчас должен согласовать или подписать: все ожидающие шаги с самым маленьким номером очереди."""
    first = con.execute(
        "SELECT MIN(ord) FROM gdoc_steps WHERE doc_id=? AND action IN ('approve','sign') AND status='pending'",
        (doc_id,)).fetchone()[0]
    if first is None:
        return []
    return con.execute(
        "SELECT * FROM gdoc_steps WHERE doc_id=? AND action IN ('approve','sign') AND status='pending' AND ord=? "
        "ORDER BY id", (doc_id, first)).fetchall()


def my_step(con, doc_id):
    for st in current_steps(con, doc_id):
        if st["login"] == session["user"]:
            return st
    return None


def waiting_for_me(con, d):
    me = session["user"]
    if d["status"] == "review":
        return my_step(con, d["id"]) is not None
    if d["status"] == "returned":
        return d["author"] == me
    if d["status"] == "signed":
        if con.execute("SELECT 1 FROM gdoc_steps WHERE doc_id=? AND login=? AND action='execute' "
                       "AND status='pending'", (d["id"], me)).fetchone():
            return True
        # исполнитель отказался: кадровик или администратор должен переназначить
        return session.get("role") in ("hr", "admin") and bool(con.execute(
            "SELECT 1 FROM gdoc_steps WHERE doc_id=? AND action='execute' AND status='rejected'", (d["id"],)).fetchone())
    return False


def pending(con, login_):
    """Сколько общих документов ждёт действия пользователя (для значка в меню)."""
    n = con.execute(
        "SELECT COUNT(*) FROM gdocs g JOIN gdoc_steps s ON s.doc_id=g.id WHERE g.status='review' AND s.login=? "
        "AND s.action IN ('approve','sign') AND s.status='pending' AND s.ord=(SELECT MIN(ord) FROM gdoc_steps "
        "WHERE doc_id=g.id AND action IN ('approve','sign') AND status='pending')",
        (login_,)).fetchone()[0]
    n += con.execute("SELECT COUNT(*) FROM gdocs WHERE author=? AND status='returned'", (login_,)).fetchone()[0]
    n += con.execute(
        "SELECT COUNT(*) FROM gdocs g JOIN gdoc_steps s ON s.doc_id=g.id WHERE g.status='signed' AND s.login=? "
        "AND s.action='execute' AND s.status='pending'", (login_,)).fetchone()[0]
    return n


def log(con, doc_id, what, comment=""):
    extras.log_event(con, "gdoc", doc_id, what, comment)


def flow(con, d):
    """Данные для области «Ход документа»: у кого сейчас, кто дальше, что уже произошло."""
    steps = con.execute("SELECT * FROM gdoc_steps WHERE doc_id=? ORDER BY ord, id", (d["id"],)).fetchall()

    def row(st, action=None):
        return {"who": st["name"], "title": st["title"], "action": action or ACTIONS[st["action"]], "due": st["due"]}
    now, nxt = [], []
    if d["status"] == "review":
        cur = {c["id"] for c in current_steps(con, d["id"])}
        for st in steps:
            if st["action"] in ("approve", "sign") and st["status"] == "pending":
                (now if st["id"] in cur else nxt).append(row(st))
        for st in steps:
            if st["action"] == "execute" and st["status"] == "pending":
                nxt.append(row(st, "На исполнение (после подписи)"))
    elif d["status"] == "returned" or d["status"] == "draft":
        now.append({"who": extras.user_name(con, d["author"]), "title": "", "action": "Автор: доработка и отправка", "due": ""})
    elif d["status"] == "signed":
        for st in steps:
            if st["action"] == "execute" and st["status"] == "pending":
                now.append(row(st, "Принял к исполнению" if st["accepted_at"] else "На исполнении"))
            elif st["action"] == "execute" and st["status"] == "rejected":
                now.append(row(st, "Отказался от исполнения — нужно переназначить"))
    return {"flow_now": now, "flow_next": nxt, "flow_events": extras.log_events(con, "gdoc", d["id"]),
            "flow_today": db.now_local().strftime("%Y-%m-%d")}


def _users(con):
    return extras.people(con)


def _groups(con):
    out = []
    for g in con.execute("SELECT * FROM signer_groups ORDER BY name"):
        rows = []
        for m in con.execute("SELECT login, action FROM signer_group_members WHERE group_id=? ORDER BY ord, id", (g["id"],)):
            rows.append({"login": m["login"], "action": {"rozilik": "approve", "ijro": "execute", "info": "info"}.get(m["action"], "approve")})
        out.append({"id": g["id"], "name": g["name"], "rows": rows})
    return out


def _parse_form(con):
    """Заголовок, пункты, связанные документы и участники из формы. Возвращает (данные, ошибки)."""
    import appl
    errors = []
    title = request.form.get("title", "").strip()[:300]
    kind = request.form.get("kind", "")
    if kind not in KINDS:
        kind = KINDS[0]
    items = [appl.sanitize(x) for x in request.form.getlist("item")][:MAX_ITEMS]
    items = [x for x in items if appl.plain(x)]
    related = []
    for tok in request.form.getlist("related"):
        m = re.fullmatch(r"(order|app|gdoc):(\d+)", tok.strip())
        if m and (m.group(1), int(m.group(2))) not in [(r["src"], r["id"]) for r in related]:
            related.append({"src": m.group(1), "id": int(m.group(2))})
    steps, seen, q = [], set(), 0
    for lg, act, queue, due in extras.parse_steps(request.form):
        if act not in ACTIONS or (lg, act) in seen:
            continue
        seen.add((lg, act))
        if not con.execute("SELECT 1 FROM users WHERE login=? AND role<>'employee'", (lg,)).fetchone():
            errors.append(f"Пользователь «{lg}» не найден.")
            continue
        if act in ("approve", "sign"):
            if queue is None:
                queue = q + 1
            q = max(q, queue)
        else:
            queue = 0
        steps.append((lg, act, queue, due))
    commission = None
    if kind == COMMISSION:
        if session.get("role") not in ("hr", "admin"):
            errors.append("Приказ о комиссии составляют кадровая служба или администратор.")
        members, seen_m = [], set()
        for lg, act, _q, _d in extras.parse_steps(request.form, "cm"):
            if act not in CM_ROLES or lg in seen_m:
                continue
            if not con.execute("SELECT 1 FROM users WHERE login=? AND role<>'employee'", (lg,)).fetchone():
                errors.append(f"Пользователь «{lg}» не найден.")
                continue
            seen_m.add(lg)
            members.append([lg, act])
        commission = {"name": " ".join(request.form.get("cm_name", "").split())[:150], "members": members}
    return {"title": title, "kind": kind, "items": items, "related": related, "steps": steps,
            "commission": commission}, errors


def _validate_submit(data):
    errors = []
    if not data["title"]:
        errors.append("Введите заголовок документа.")
    if not data["items"]:
        errors.append("Напишите текст хотя бы в одном пункте.")
    if data["kind"] == COMMISSION:
        cm = data.get("commission") or {"name": "", "members": []}
        if not cm["name"]:
            errors.append("Введите название комиссии, например «Комиссия по надбавкам».")
        if sum(1 for m in cm["members"] if m[1] == "chair") != 1:
            errors.append("В составе комиссии должен быть ровно один председатель.")
        if len(cm["members"]) < 2:
            errors.append("В составе комиссии должно быть не меньше двух человек.")
    signs = [s for s in data["steps"] if s[1] == "sign"]
    if len(signs) != 1:
        errors.append("В таблице подписантов должен быть ровно один человек с действием «Подпись (утверждает)».")
    elif any(s[2] >= signs[0][2] for s in data["steps"] if s[1] == "approve"):
        errors.append("Подписывающий должен идти последним: номер его очереди должен быть больше, чем у всех согласующих.")
    return errors


def _save_steps(con, doc_id, steps):
    """Участники: согласующие и подписант по номеру очереди, потом исполнение и «к сведению»."""
    con.execute("DELETE FROM gdoc_steps WHERE doc_id=?", (doc_id,))
    for lg, act, queue, due in steps:
        u = con.execute("SELECT full_name, title FROM users WHERE login=?", (lg,)).fetchone()
        con.execute("INSERT INTO gdoc_steps(doc_id, ord, login, name, title, action, status, due) VALUES(?,?,?,?,?,?,?,?)",
                    (doc_id, queue, lg, (u["full_name"] or lg) if u else lg, u["title"] if u else "", act,
                     "done" if act == "info" else "pending", due))


def related_info(con, related):
    out = []
    for r in related:
        if r["src"] == "order":
            o = con.execute("SELECT id, number, kind FROM orders WHERE id=? AND deleted_at IS NULL", (r["id"],)).fetchone()
            if o:
                out.append({"label": f"Приказ {o['number'] or 'проект №' + str(o['id'])} ({M.KIND_RU[o['kind']].lower()})",
                            "url": url_for("order_view", order_id=o["id"])})
        elif r["src"] == "app":
            a = con.execute("SELECT id, number, type_name FROM applications WHERE id=?", (r["id"],)).fetchone()
            if a:
                out.append({"label": f"Заявление {a['number'] or '№' + str(a['id'])}: {a['type_name']}",
                            "url": url_for("ap_view", app_id=a["id"])})
        else:
            g = con.execute("SELECT id, number, title FROM gdocs WHERE id=?", (r["id"],)).fetchone()
            if g:
                out.append({"label": f"Документ {g['number'] or '№' + str(g['id'])}: {g['title']}",
                            "url": url_for("gd_view", doc_id=g["id"])})
    return out


def _doc_choices(con):
    """Что можно связать с документом: последние приказы, заявления и общие документы."""
    out = []
    for o in con.execute("SELECT id, number, kind FROM orders WHERE deleted_at IS NULL ORDER BY id DESC LIMIT 200"):
        out.append({"v": f"order:{o['id']}", "t": f"Приказ {o['number'] or 'проект №' + str(o['id'])} — {M.KIND_RU[o['kind']].lower()}"})
    for a in con.execute("SELECT id, number, type_name FROM applications WHERE status<>'draft' ORDER BY id DESC LIMIT 200"):
        out.append({"v": f"app:{a['id']}", "t": f"Заявление {a['number'] or a['id']} — {a['type_name']}"})
    for g in con.execute("SELECT id, number, title FROM gdocs WHERE status<>'draft' ORDER BY id DESC LIMIT 200"):
        out.append({"v": f"gdoc:{g['id']}", "t": f"Документ {g['number'] or g['id']} — {g['title']}"})
    return out


def _save_commission(con, doc_id, data):
    cm = data.get("commission")
    con.execute("UPDATE gdocs SET commission=? WHERE id=?",
                (json.dumps(cm, ensure_ascii=False) if data["kind"] == COMMISSION and cm else "", doc_id))


def _form_page(con, d, data, errors=None):
    rows = [{"login": s[0], "action": s[1], "queue": s[2], "due": s[3]} for s in data["steps"]]
    cmd = data.get("commission") or {"name": "", "members": []}
    cm_rows = [{"login": m[0], "action": m[1], "queue": 0, "due": ""} for m in cmd["members"]]
    return render_template("gd_form.html", d=d, data=data, cm_name=cmd["name"], cm_rows=cm_rows, CM_ROLES=CM_ROLES,
                           COMMISSION=COMMISSION, is_hr=session.get("role") in ("hr", "admin"), users=_users(con), groups=_groups(con),
                           choices=_doc_choices(con), ACTIONS=ACTIONS, KINDS=KINDS, step_rows=rows,
                           files=con.execute("SELECT * FROM gdoc_files WHERE doc_id=?", (d["id"],)).fetchall() if d else [],
                           errors=errors or [])


def _blank():
    return {"title": "", "kind": KINDS[0], "items": [""], "related": [], "steps": [], "commission": None}


def _stored_data(con, d):
    steps = [(s["login"], s["action"], s["ord"], s["due"]) for s in
             con.execute("SELECT login, action, ord, due FROM gdoc_steps WHERE doc_id=? ORDER BY ord, id", (d["id"],))]
    return {"title": d["title"], "kind": d["kind"], "items": json.loads(d["items"]) or [""],
            "related": json.loads(d["related"]), "steps": steps,
            "commission": json.loads(d["commission"]) if d["commission"] else None}


def _forbid_employee():
    if session.get("role") == "employee":
        abort(403)


# ---------- создание и правка ----------

@route("/gd/new", ("GET", "POST"))
@login_required
def gd_new():
    _forbid_employee()
    con = db.connect()
    try:
        if request.method == "POST":
            data, errors = _parse_form(con)
            go = request.form.get("do", "save")
            if go == "submit":
                errors += _validate_submit(data)
            if errors:
                return _form_page(con, None, data, errors)
            con.execute("BEGIN IMMEDIATE")
            try:
                cur = con.execute(
                    "INSERT INTO gdocs(kind, title, items, related, author, status, created_at) VALUES(?,?,?,?,?,?,?)",
                    (data["kind"], data["title"], json.dumps(data["items"] or [""]), json.dumps(data["related"]),
                     session["user"], "draft", _now()))
                doc_id = cur.lastrowid
                _save_steps(con, doc_id, data["steps"])
                _save_commission(con, doc_id, data)
                log(con, doc_id, "Создан документ")
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            if go == "submit":
                _submit(con, doc_id)
                flash("Документ отправлен на согласование.", "ok")
                return redirect(url_for("gd_view", doc_id=doc_id))
            flash("Черновик сохранён. Теперь можно прикрепить файлы.", "ok")
            return redirect(url_for("gd_edit", doc_id=doc_id))
        blank = _blank()
        if request.args.get("kind") == "commission" and session.get("role") in ("hr", "admin"):
            blank["kind"] = COMMISSION
            blank["title"] = "О создании комиссии"
        return _form_page(con, None, blank)
    finally:
        con.close()


@route("/gd/<int:doc_id>/edit", ("GET", "POST"))
@login_required
def gd_edit(doc_id):
    _forbid_employee()
    con = db.connect()
    try:
        d = _doc(con, doc_id)
        if d["author"] != session["user"] or d["status"] not in ("draft", "returned"):
            abort(403)
        if request.method == "POST":
            data, errors = _parse_form(con)
            go = request.form.get("do", "save")
            if go == "submit":
                errors += _validate_submit(data)
            if errors:
                return _form_page(con, d, data, errors)
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute("UPDATE gdocs SET kind=?, title=?, items=?, related=? WHERE id=?",
                            (data["kind"], data["title"], json.dumps(data["items"] or [""]),
                             json.dumps(data["related"]), doc_id))
                _save_steps(con, doc_id, data["steps"])
                _save_commission(con, doc_id, data)
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            if go == "submit":
                _submit(con, doc_id)
                flash("Документ отправлен на согласование.", "ok")
                return redirect(url_for("gd_view", doc_id=doc_id))
            flash("Черновик сохранён.", "ok")
            return redirect(url_for("gd_edit", doc_id=doc_id))
        return _form_page(con, d, _stored_data(con, d))
    finally:
        con.close()


def _submit(con, doc_id):
    con.execute("UPDATE gdoc_steps SET status='pending', comment='', acted_at=NULL WHERE doc_id=? AND action<>'info'", (doc_id,))
    con.execute("UPDATE gdocs SET status='review', submitted_at=?, return_comment='' WHERE id=?", (_now(), doc_id))
    log(con, doc_id, "Отправлен на согласование")


# ---------- просмотр и действия ----------

@route("/gd/<int:doc_id>")
@login_required
def gd_view(doc_id):
    _forbid_employee()
    con = db.connect()
    try:
        d = _doc(con, doc_id)
        if not can_see(con, d):
            abort(403)
        steps = con.execute("SELECT * FROM gdoc_steps WHERE doc_id=? ORDER BY CASE action WHEN 'approve' THEN 0 WHEN 'sign' THEN 1 "
                            "WHEN 'execute' THEN 2 ELSE 3 END, ord, id", (doc_id,)).fetchall()
        files = con.execute("SELECT * FROM gdoc_files WHERE doc_id=? ORDER BY id", (doc_id,)).fetchall()
        cur_list = current_steps(con, doc_id) if d["status"] == "review" else []
        cur_ids = {c["id"] for c in cur_list}
        me = session["user"]
        mine_step = next((c for c in cur_list if c["login"] == me), None)
        can_decide = mine_step is not None
        cur = mine_step
        my_exec = d["status"] == "signed" and bool(con.execute(
            "SELECT 1 FROM gdoc_steps WHERE doc_id=? AND login=? AND action='execute' AND status='pending'", (doc_id, me)).fetchone())
        my_exec_step = con.execute(
            "SELECT * FROM gdoc_steps WHERE doc_id=? AND login=? AND action='execute' AND status='pending'", (doc_id, me)).fetchone() \
            if d["status"] == "signed" else None
        can_delegate = (d["status"] == "signed" and my_exec_step is not None) or \
            (d["status"] in ("signed", "done") and session.get("role") in ("hr", "admin"))
        author = con.execute("SELECT full_name, title FROM users WHERE login=?", (d["author"],)).fetchone()
        commission = None
        if d["commission"]:
            cmj = json.loads(d["commission"])
            commission = {"name": cmj.get("name", ""), "template": cmj.get("template", ""), "members": [
                {"role": CM_ROLES.get(m[1], m[1]), "name": extras.user_name(con, m[0]),
                 "title": (con.execute("SELECT title FROM users WHERE login=?", (m[0],)).fetchone() or {"title": ""})["title"]}
                for m in cmj.get("members", [])]}
        org = db.get_settings(con)
        return render_template("gd_view.html", d=d, items=json.loads(d["items"]), steps=steps, files=files, cur=cur, cur_ids=cur_ids, today=db.now_local().strftime("%Y-%m-%d"),
                               can_decide=can_decide, my_exec=my_exec, my_exec_step=my_exec_step,
                               can_delegate=can_delegate, commission=commission, people=_users(con) if can_delegate else [], ACTIONS=ACTIONS, STATUS_RU=STATUS_RU,
                               related=related_info(con, json.loads(d["related"])), author=author, org=org, **flow(con, d),
                               is_author=d["author"] == me, is_admin=session.get("role") == "admin")
    finally:
        con.close()


@route("/gd/<int:doc_id>/act", ("POST",))
@login_required
def gd_act(doc_id):
    _forbid_employee()
    act = request.form.get("act", "")
    comment = request.form.get("comment", "").strip()[:1000]
    me = session["user"]
    con = db.connect()
    try:
        d = _doc(con, doc_id)
        if not can_see(con, d):
            abort(403)
        con.execute("BEGIN IMMEDIATE")
        try:
            msg = None
            d = _doc(con, doc_id)
            if act in ("approve", "return", "reject"):
                cur = my_step(con, doc_id) if d["status"] == "review" else None
                if not cur:
                    msg = ("error", "Сейчас этот документ ждёт не вас.")
                elif act in ("return", "reject") and not comment:
                    msg = ("error", "Напишите комментарий: он нужен для возврата или отказа.")
                elif act == "approve":
                    con.execute("UPDATE gdoc_steps SET status='approved', comment=?, acted_at=? WHERE id=?",
                                (comment, _now(), cur["id"]))
                    if cur["action"] == "sign":
                        year = db.now_local().year
                        n = db.next_counter(con, f"gdoc:{year}")
                        number = f"{n:04d}/{str(year)[2:]}"
                        has_exec = con.execute("SELECT 1 FROM gdoc_steps WHERE doc_id=? AND action='execute'", (doc_id,)).fetchone()
                        con.execute("UPDATE gdocs SET status=?, number=?, finished_at=? WHERE id=?",
                                    ("signed", number, _now(), doc_id))
                        if not has_exec:
                            con.execute("UPDATE gdocs SET status='done' WHERE id=?", (doc_id,))
                        log(con, doc_id, f"Подписал. Присвоен номер {number}", comment)
                        msg = ("ok", f"Документ подписан. Номер {number}.")
                        if d["kind"] == COMMISSION and d["commission"]:
                            tname = _make_commission_template(con, d, number)
                            if tname:
                                log(con, doc_id, f"Создан шаблон служебной записки «{tname}»")
                                msg = ("ok", f"Приказ подписан. Номер {number}. Создан шаблон «{tname}»: теперь по нему можно писать служебные записки комиссии.")
                    else:
                        log(con, doc_id, "Согласовал", comment)
                        msg = ("ok", "Вы согласовали документ. Он передан дальше по цепочке.")
                elif act == "return":
                    con.execute("UPDATE gdoc_steps SET status='returned', comment=?, acted_at=? WHERE id=?",
                                (comment, _now(), cur["id"]))
                    con.execute("UPDATE gdocs SET status='returned', return_comment=? WHERE id=?",
                                (f"{cur['name']}: {comment}", doc_id))
                    log(con, doc_id, "Вернул на доработку", comment)
                    msg = ("ok", "Документ возвращён автору на доработку.")
                else:
                    con.execute("UPDATE gdoc_steps SET status='rejected', comment=?, acted_at=? WHERE id=?",
                                (comment, _now(), cur["id"]))
                    con.execute("UPDATE gdocs SET status='rejected', return_comment=?, finished_at=? WHERE id=?",
                                (f"{cur['name']}: {comment}", _now(), doc_id))
                    log(con, doc_id, "Отказал", comment)
                    msg = ("ok", "Вы отказали в документе.")
            elif act == "execute":
                st = con.execute("SELECT id FROM gdoc_steps WHERE doc_id=? AND login=? AND action='execute' AND status='pending'",
                                 (doc_id, me)).fetchone()
                if d["status"] != "signed" or not st:
                    msg = ("error", "Этот документ не ждёт вашего исполнения.")
                else:
                    con.execute("UPDATE gdoc_steps SET status='approved', comment=?, acted_at=? WHERE id=?",
                                (comment, _now(), st["id"]))
                    left = con.execute("SELECT 1 FROM gdoc_steps WHERE doc_id=? AND action='execute' "
                                       "AND status IN ('pending','rejected')", (doc_id,)).fetchone()
                    if not left:
                        con.execute("UPDATE gdocs SET status='done' WHERE id=?", (doc_id,))
                    log(con, doc_id, "Отметил: исполнено", comment)
                    msg = ("ok", "Отмечено: исполнено.")
            elif act in ("accept", "decline", "justify"):
                st = con.execute("SELECT * FROM gdoc_steps WHERE doc_id=? AND login=? AND action='execute' AND status='pending'",
                                 (doc_id, me)).fetchone()
                if d["status"] != "signed" or not st:
                    msg = ("error", "Этот документ не ждёт вашего исполнения.")
                elif act == "accept":
                    if st["accepted_at"]:
                        msg = ("error", "Вы уже приняли этот документ к исполнению.")
                    else:
                        con.execute("UPDATE gdoc_steps SET accepted_at=? WHERE id=?", (_now(), st["id"]))
                        log(con, doc_id, "Принял к исполнению", comment)
                        msg = ("ok", "Вы приняли документ к исполнению.")
                elif not comment:
                    msg = ("error", "Напишите комментарий: для обоснования или отказа он обязателен.")
                elif act == "decline":
                    con.execute("UPDATE gdoc_steps SET status='rejected', comment=?, acted_at=? WHERE id=?",
                                (comment, _now(), st["id"]))
                    log(con, doc_id, "Отказался от исполнения", comment)
                    msg = ("ok", "Вы отказались от исполнения. Кадровая служба увидит причину и переназначит исполнителя.")
                else:
                    import datetime
                    nd = request.form.get("new_due", "").strip()
                    try:
                        datetime.date.fromisoformat(nd)
                        what = "Обоснование. Просит продлить срок до " + datetime.date.fromisoformat(nd).strftime("%d.%m.%Y")
                    except ValueError:
                        what = "Обоснование"
                    con.execute("UPDATE gdoc_steps SET comment=? WHERE id=?", (comment, st["id"]))
                    log(con, doc_id, what, comment)
                    msg = ("ok", "Обоснование записано в ход документа.")
            elif act == "delegate":
                has_mine = con.execute("SELECT 1 FROM gdoc_steps WHERE doc_id=? AND login=? AND action='execute' "
                                       "AND status='pending'", (doc_id, me)).fetchone()
                allowed = (d["status"] == "signed" and has_mine) or \
                    (d["status"] in ("signed", "done") and session.get("role") in ("hr", "admin"))
                if not allowed:
                    msg = ("error", "Поручить исполнение сейчас нельзя.")
                else:
                    instr = request.form.get("instruction", "").strip()[:1000]
                    added = []
                    byname = extras.user_name(con, me)
                    for lg, _a, _q, due in extras.parse_steps(request.form):
                        u = con.execute("SELECT full_name, title FROM users WHERE login=? AND role<>'employee'", (lg,)).fetchone()
                        if not u or lg in [x[0] for x in added] or con.execute(
                                "SELECT 1 FROM gdoc_steps WHERE doc_id=? AND login=? AND action='execute' AND status='pending'",
                                (doc_id, lg)).fetchone():
                            continue
                        con.execute("INSERT INTO gdoc_steps(doc_id, ord, login, name, title, action, status, due, instruction, assigned_by) "
                                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                                    (doc_id, 0, lg, u["full_name"] or lg, u["title"], "execute", "pending", due, instr, byname))
                        added.append((lg, u["full_name"] or lg, due))
                    if not added:
                        msg = ("error", "Выберите хотя бы одного нового исполнителя (тот, кто уже исполняет, не добавляется повторно).")
                    else:
                        con.execute("UPDATE gdoc_steps SET status='reassigned' WHERE doc_id=? AND action='execute' AND status='rejected'", (doc_id,))
                        if d["status"] == "done":
                            con.execute("UPDATE gdocs SET status='signed' WHERE id=?", (doc_id,))
                        log(con, doc_id, "Поручил исполнение: " + ", ".join(
                            n + (f" (срок {x[8:10]}.{x[5:7]}.{x[:4]})" if x else "") for _l, n, x in added), instr)
                        msg = ("ok", "Поручение отправлено: " + ", ".join(n for _l, n, _x in added) + ".")
            elif act == "withdraw":
                if d["author"] != me or d["status"] != "review":
                    msg = ("error", "Отозвать можно только свой документ, который ещё на согласовании.")
                else:
                    con.execute("UPDATE gdocs SET status='draft' WHERE id=?", (doc_id,))
                    log(con, doc_id, "Автор отозвал документ с согласования")
                    msg = ("ok", "Документ отозван. Его можно исправить и отправить снова.")
            elif act == "delete":
                if (d["author"] != me and session.get("role") != "admin") or d["status"] not in ("draft", "returned", "rejected"):
                    msg = ("error", "Удалить можно только черновик, возвращённый или отклонённый документ.")
                else:
                    for f in con.execute("SELECT stored FROM gdoc_files WHERE doc_id=?", (doc_id,)).fetchall():
                        try:
                            (extras.files_dir() / f["stored"]).unlink()
                        except OSError:
                            pass
                    con.execute("DELETE FROM gdocs WHERE id=?", (doc_id,))
                    con.execute("DELETE FROM doc_log WHERE src='gdoc' AND doc_id=?", (doc_id,))
                    con.execute("COMMIT")
                    flash("Документ удалён.", "ok")
                    return redirect(url_for("docs"))
            else:
                abort(400)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    if msg:
        flash(msg[1], msg[0])
    return redirect(url_for("gd_view", doc_id=doc_id))


def _make_commission_template(con, d, number):
    """После подписания приказа о комиссии: создать шаблон служебной записки, где подписанты — состав комиссии
    (члены согласуют одновременно, председатель подписывает последним)."""
    cm = json.loads(d["commission"])
    members = cm.get("members", [])
    if not cm.get("name") or not members:
        return ""
    name = cm["name"]
    if con.execute("SELECT 1 FROM doc_templates WHERE name=?", (name,)).fetchone():
        name = f"{cm['name']} (приказ № {number})"
        n = 2
        while con.execute("SELECT 1 FROM doc_templates WHERE name=?", (name,)).fetchone():
            name = f"{cm['name']} (приказ № {number}, {n})"
            n += 1
    esc = lambda x: x.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    items = ["<p><b>Тема:</b> {Тема}</p>", "<p>{Суть обращения}</p>",
             f"<p>Прошу рассмотреть вопрос на заседании комиссии «{esc(cm['name'])}» (создана приказом № {esc(number)}).</p>",
             "<p>{Автор}, {Дата}</p>"]
    fields = [{"label": "Тема", "type": "text", "required": True}, {"label": "Суть обращения", "type": "area", "required": True}]
    steps = [[lg, "approve", 1, ""] for lg, role in members if role != "chair"] + \
            [[lg, "sign", 2, ""] for lg, role in members if role == "chair"]
    con.execute("INSERT INTO doc_templates(name, description, kind, title, items, fields, steps, active, created_by, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (name, f"Служебная записка в комиссию. Состав определён приказом № {number}.", "Служебная записка", "{Тема}",
                 json.dumps(items, ensure_ascii=False), json.dumps(fields, ensure_ascii=False),
                 json.dumps(steps, ensure_ascii=False), 1, session["user"], _now()))
    cm["template"] = name
    con.execute("UPDATE gdocs SET commission=? WHERE id=?", (json.dumps(cm, ensure_ascii=False), d["id"]))
    return name


# ---------- файлы ----------

@route("/gd/<int:doc_id>/files", ("POST",))
@login_required
def gd_file_upload(doc_id):
    _forbid_employee()
    f = request.files.get("file")
    con = db.connect()
    try:
        d = _doc(con, doc_id)
        if d["author"] != session["user"] or d["status"] not in ("draft", "returned"):
            abort(403)
        if f is None or not f.filename:
            flash("Выберите файл.", "error")
        else:
            name = extras._clean_name(f.filename)
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            blob = f.read(extras.MAX_FILE + 1)
            if ext not in extras.ALLOWED_EXT:
                flash("Такой тип файла нельзя: разрешены pdf, jpg, png, doc, docx, xls, xlsx.", "error")
            elif len(blob) > extras.MAX_FILE:
                flash("Файл больше 10 МБ.", "error")
            elif not blob:
                flash("Файл пустой.", "error")
            else:
                stored = secrets.token_hex(16) + "." + ext
                (extras.files_dir() / stored).write_bytes(blob)
                con.execute("INSERT INTO gdoc_files(doc_id, filename, stored, size, uploaded_by, uploaded_at) VALUES(?,?,?,?,?,?)",
                            (doc_id, name, stored, len(blob), session["user"], _now()))
                flash("Файл прикреплён.", "ok")
    finally:
        con.close()
    return redirect(url_for("gd_edit", doc_id=doc_id))


@route("/gd/file/<int:file_id>")
@login_required
def gd_file(file_id):
    _forbid_employee()
    con = db.connect()
    try:
        row = con.execute("SELECT * FROM gdoc_files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            abort(404)
        if not can_see(con, _doc(con, row["doc_id"])):
            abort(403)
    finally:
        con.close()
    path = extras.files_dir() / row["stored"]
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=row["filename"])


@route("/gd/file/<int:file_id>/delete", ("POST",))
@login_required
def gd_file_delete(file_id):
    _forbid_employee()
    con = db.connect()
    try:
        row = con.execute("SELECT * FROM gdoc_files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            abort(404)
        d = _doc(con, row["doc_id"])
        if d["author"] != session["user"] or d["status"] not in ("draft", "returned"):
            abort(403)
        con.execute("DELETE FROM gdoc_files WHERE id=?", (file_id,))
        try:
            (extras.files_dir() / row["stored"]).unlink()
        except OSError:
            pass
    finally:
        con.close()
    flash("Файл удалён.", "ok")
    return redirect(url_for("gd_edit", doc_id=row["doc_id"]))


# ---------- копирование ----------

@route("/gd/<int:doc_id>/copy", ("POST",))
@login_required
def gd_copy(doc_id):
    """Новый черновик с тем же текстом, связями и участниками (без номера, подписей и сроков)."""
    _forbid_employee()
    con = db.connect()
    try:
        d = _doc(con, doc_id)
        if not can_see(con, d):
            abort(403)
        con.execute("BEGIN IMMEDIATE")
        try:
            cur = con.execute(
                "INSERT INTO gdocs(kind, title, items, related, author, status, created_at, commission) VALUES(?,?,?,?,?,?,?,?)",
                (d["kind"], d["title"], d["items"], d["related"], session["user"], "draft", _now(),
                 json.dumps({**json.loads(d["commission"]), "template": ""}, ensure_ascii=False) if d["commission"] else ""))
            new_id = cur.lastrowid
            for st in con.execute("SELECT * FROM gdoc_steps WHERE doc_id=? ORDER BY ord, id", (doc_id,)).fetchall():
                con.execute("INSERT INTO gdoc_steps(doc_id, ord, login, name, title, action, status, due) VALUES(?,?,?,?,?,?,?,?)",
                            (new_id, st["ord"], st["login"], st["name"], st["title"], st["action"],
                             "done" if st["action"] == "info" else "pending", ""))
            for f in con.execute("SELECT * FROM gdoc_files WHERE doc_id=?", (doc_id,)).fetchall():
                src = extras.files_dir() / f["stored"]
                if src.exists():
                    stored = secrets.token_hex(16) + ("." + f["stored"].rsplit(".", 1)[-1] if "." in f["stored"] else "")
                    (extras.files_dir() / stored).write_bytes(src.read_bytes())
                    con.execute("INSERT INTO gdoc_files(doc_id, filename, stored, size, uploaded_by, uploaded_at) VALUES(?,?,?,?,?,?)",
                                (new_id, f["filename"], stored, f["size"], session["user"], _now()))
            log(con, new_id, f"Создан копированием из документа {d['number'] or '№' + str(d['id'])}")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    flash("Копия создана как черновик. Исправьте, что нужно, и отправьте на согласование.", "ok")
    return redirect(url_for("gd_edit", doc_id=new_id))
