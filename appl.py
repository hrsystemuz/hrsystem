"""Заявления сотрудников: шаблоны с форматированием, цепочка согласования, передача кадровику."""
import html
import json
import re
import secrets
from html.parser import HTMLParser

from flask import abort, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

import db
import extras
from extras import login_required, roles_required, route

M = None   # модуль app (его задаёт setup)

STATUS_RU = {"draft": "черновик", "review": "на согласовании", "returned": "возвращено на доработку",
             "approved": "согласовано, у кадровика", "rejected": "отказано", "done": "исполнено"}
KINDS = {"none": "Свободное заявление (без приказа)", "aid": "Материальная помощь (без приказа)",
         "vacation": "Отпуск → приказ об отпуске", "transfer": "Перевод → приказ о переводе",
         "dismiss": "Увольнение → приказ об увольнении", "trip": "Командировка → приказ о командировке"}
FIELD_TYPES = {"text": "Текст", "area": "Длинный текст", "date": "Дата", "number": "Число"}
BASE_TOKENS = [("fio", "ФИО"), ("position", "Должность"), ("department", "Подразделение"),
               ("tabel", "Табельный №"), ("date", "Сегодняшняя дата"), ("org", "Организация"),
               ("org_form", "Форма организации")]


def setup(app_module):
    global M
    M = app_module
    con = db.connect()
    try:
        if con.execute("SELECT COUNT(*) FROM app_types").fetchone()[0] == 0:
            seed_types(con)
    finally:
        con.close()


# ---------- безопасный HTML из редактора ----------

ALLOWED_TAGS = {"p", "br", "b", "strong", "i", "em", "u", "s", "strike", "ul", "ol", "li", "div", "span",
                "h1", "h2", "h3", "h4", "blockquote", "sub", "sup"}
SKIP_CONTENT = {"script", "style", "iframe", "object", "embed", "head", "title"}
STYLE_OK = {
    "text-align": re.compile(r"^(left|right|center|justify)$"),
    "font-size": re.compile(r"^(xx-small|x-small|small|medium|large|x-large|xx-large|\d{1,2}(\.\d)?(px|pt|em))$"),
    "font-weight": re.compile(r"^(bold|normal|[1-9]00)$"),
    "font-style": re.compile(r"^(italic|normal)$"),
    "text-decoration": re.compile(r"^(underline|line-through|none)( (underline|line-through))?$"),
    "text-indent": re.compile(r"^\d{1,3}(px|pt|em)$"),
    "margin-left": re.compile(r"^\d{1,3}(px|pt|em)$"),
    "font-family": re.compile(r"^[A-Za-z0-9 ,'\"-]{1,60}$"),
}
FONT_SIZES = {"1": "x-small", "2": "small", "3": "medium", "4": "large", "5": "x-large", "6": "xx-large", "7": "xx-large"}


def _clean_style(text):
    out = []
    for part in (text or "").split(";"):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        k, v = k.strip().lower(), v.strip().lower() if k.strip().lower() != "font-family" else v.strip()
        if k in STYLE_OK and STYLE_OK[k].match(v):
            out.append(f"{k}: {v}")
    return "; ".join(out)


class _Cleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.skip = 0
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_CONTENT:
            self.skip += 1
            return
        if self.skip:
            return
        a = dict(attrs)
        if tag == "font":                       # старые браузеры: <font size=5> → span со стилем
            size = FONT_SIZES.get(str(a.get("size", "")))
            self.out.append(f'<span style="font-size: {size}">' if size else "<span>")
            self.stack.append("span")
            return
        if tag not in ALLOWED_TAGS:
            return
        style = _clean_style(a.get("style", ""))
        self.out.append(f"<{tag}" + (f' style="{html.escape(style, quote=True)}"' if style else "") + ">")
        if tag != "br":
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self.out.append("<br>")

    def handle_endtag(self, tag):
        if tag in SKIP_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "font":
            tag = "span"
        if tag in ALLOWED_TAGS and tag in self.stack:
            while self.stack:
                t = self.stack.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data))

    def result(self):
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitize(text):
    """Оставить в тексте только безопасное форматирование."""
    p = _Cleaner()
    p.feed(text or "")
    p.close()
    return p.result()


def plain(text):
    """Текст без разметки (для проверки «не пусто»)."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


# ---------- шаблоны по умолчанию ----------

def _tpl(title_top, body_lines):
    head = ('<p style="text-align: right">«{org}» {org_form}<br>Bosh direktoriga<br>{department}<br>'
            '{position} {fio} tomonidan</p>')
    lines = "".join(f'<p style="text-align: justify; text-indent: 30px">{x}</p>' for x in body_lines)
    foot = '<p>{date}<span style="margin-left: 120px">Imzo: ______________</span></p>'
    return f'{head}<h3 style="text-align: center">{title_top}</h3>{lines}{foot}'


DEFAULT_TYPES = [
    ("Moddiy yordam uchun ariza (Материальная помощь)", "aid",
     _tpl("ARIZA", ["Men, {fio}, {position}, {reason} munosabati bilan menga moddiy yordam ko‘rsatishingizni so‘rayman."]),
     [("reason", "Причина (свадьба, болезнь, смерть близких и др.)", "area", "yes")]),
    ("Ish haqi saqlanmaydigan ta’til (Отпуск без содержания)", "vacation",
     _tpl("ARIZA", ["Men, {fio}, {position}, {start_date}dan {end_date}gacha ish haqi saqlanmaydigan ta’til "
                    "berishingizni so‘rayman.", "Sabab: {reason}."]),
     [("start_date", "Первый день отпуска", "date", "yes"), ("end_date", "Последний день отпуска", "date", "yes"),
      ("reason", "Причина", "text", "yes")]),
    ("Yillik mehnat ta’tili (Ежегодный отпуск)", "vacation",
     _tpl("ARIZA", ["Men, {fio}, {position}, {start_date}dan {end_date}gacha yillik mehnat ta’tilini "
                    "berishingizni so‘rayman."]),
     [("start_date", "Первый день отпуска", "date", "yes"), ("end_date", "Последний день отпуска", "date", "yes")]),
    ("Boshqa lavozimga o‘tkazish (Перевод на другую должность)", "transfer",
     _tpl("ARIZA", ["Men, {fio}, {position}, {transfer_date}dan boshlab {to_department} {to_position} "
                    "lavozimiga o‘tkazishingizni so‘rayman.", "Sabab: {reason}."]),
     [("transfer_date", "Желаемая дата перевода", "date", "yes"), ("to_department", "В какое подразделение", "text", "yes"),
      ("to_position", "На какую должность", "text", "yes"), ("reason", "Причина", "text", "no")]),
    ("Mehnat shartnomasini bekor qilish (Увольнение по собственному желанию)", "dismiss",
     _tpl("ARIZA", ["Men, {fio}, {position}, o‘z xohishimga ko‘ra {dismissal_date}dan mehnat shartnomasini "
                    "bekor qilishingizni so‘rayman."]),
     [("dismissal_date", "Последний рабочий день", "date", "yes")]),
    ("Xizmat safari (Командировка)", "trip",
     _tpl("ARIZA", ["Men, {fio}, {position}, {start_date}dan {end_date}gacha {destination}ga xizmat safariga "
                    "yuborishingizni so‘rayman.", "Safar maqsadi: {purpose}."]),
     [("start_date", "Дата начала", "date", "yes"), ("end_date", "Дата окончания", "date", "yes"),
      ("destination", "Куда", "text", "yes"), ("purpose", "Цель командировки", "text", "yes")]),
    ("Erkin ariza (Свободное заявление)", "none",
     _tpl("ARIZA", ["{text}"]), [("text", "Суть заявления", "area", "yes")]),
]


def seed_types(con):
    for i, (name, kind, body, fields) in enumerate(DEFAULT_TYPES):
        fl = [{"key": k, "label": lbl, "type": t, "required": r == "yes"} for k, lbl, t, r in fields]
        cur = con.execute("INSERT INTO app_types(name, kind, body, fields, ord) VALUES(?,?,?,?,?)",
                          (name, kind, sanitize(body), json.dumps(fl, ensure_ascii=False), i))
        con.execute("INSERT INTO app_route(type_id, ord, who, label) VALUES(?,?,?,?)",
                    (cur.lastrowid, 0, "head", "Начальник подразделения"))


# ---------- общие помощники ----------

def now():
    return db.now_local().strftime("%Y-%m-%d %H:%M")


def log(con, app_id, who, text):
    con.execute("INSERT INTO app_log(app_id, at, who, text) VALUES(?,?,?,?)", (app_id, now(), who, text))


def fio_of(e):
    return " ".join(x for x in (e["last_name"], e["first_name"], e["middle_name"]) if x)


def my_employee(con):
    row = con.execute("SELECT employee_id FROM users WHERE login=?", (session["user"],)).fetchone()
    if not row or not row["employee_id"]:
        return None
    return con.execute("SELECT * FROM employees WHERE id=? AND status=?", (row["employee_id"], db.ACTIVE)).fetchone()


def type_fields(t):
    try:
        return json.loads(t["fields"])
    except ValueError:
        return []


def render_body(template, emp, values, settings):
    """Подставить в шаблон данные сотрудника и значения полей. Незаполненное — прочерк."""
    uzdate = M.uzdate
    base = {"fio": fio_of(emp), "position": emp["position"], "department": emp["department"],
            "tabel": emp["tabel_no"], "date": uzdate(db.now_local().strftime("%Y-%m-%d")),
            "org": settings.get("org_name", ""), "org_form": settings.get("org_form", "")}

    def sub(m):
        k = m.group(1)
        v = base.get(k)
        if v is None:
            v = values.get(k, "")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v or ""):
                v = uzdate(v)
        v = (v or "").strip()
        return html.escape(v) if v else "________"
    return re.sub(r"\{(\w+)\}", sub, template)


def _values(form, fields):
    return {f["key"]: (form.get("f_" + f["key"], "") or "").strip()[:2000] for f in fields}


def _check(values, fields):
    errs = []
    for f in fields:
        v = values.get(f["key"], "")
        if f.get("required", True) and not v:
            errs.append(f"Заполните поле «{f['label']}».")
        elif v and f["type"] == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            errs.append(f"Неверная дата в поле «{f['label']}».")
        elif v and f["type"] == "number" and not re.fullmatch(r"\d+([.,]\d+)?", v):
            errs.append(f"В поле «{f['label']}» нужно число.")
    return errs


def head_of(con, department):
    r = con.execute("SELECT login FROM dept_heads WHERE department=?", (department,)).fetchone()
    return r["login"] if r else None


def user_row(con, login_):
    return con.execute("SELECT login, full_name, title FROM users WHERE login=?", (login_,)).fetchone()


def build_steps(con, app_row, emp):
    """Цепочка для заявления: [(login, label)] и список предупреждений (что пропущено)."""
    steps, notes = [], []
    for r in con.execute("SELECT * FROM app_route WHERE type_id=? ORDER BY ord, id", (app_row["type_id"],)):
        if r["who"] == "head":
            login_ = head_of(con, emp["department"])
            label = r["label"] or "Начальник подразделения"
            if not login_:
                notes.append(f"Для подразделения «{emp['department']}» не назначен начальник — шаг пропущен.")
                continue
        else:
            login_, label = r["login"], r["label"]
        if login_ == app_row["author"]:
            notes.append(f"Шаг «{label or login_}» пропущен: заявитель сам является согласующим.")
            continue
        if not user_row(con, login_):
            notes.append(f"Пользователь «{login_}» не найден — шаг пропущен.")
            continue
        if steps and steps[-1][0] == login_:
            continue
        steps.append((login_, label))
    return steps, notes


def submit(con, app_id):
    """Отправить заявление по цепочке. Возвращает список предупреждений."""
    a = con.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    emp = con.execute("SELECT * FROM employees WHERE id=?", (a["employee_id"],)).fetchone()
    steps, notes = build_steps(con, a, emp)
    con.execute("DELETE FROM app_steps WHERE app_id=?", (app_id,))
    for i, (login_, label) in enumerate(steps):
        u = user_row(con, login_)
        con.execute("INSERT INTO app_steps(app_id, ord, login, name, title, label) VALUES(?,?,?,?,?,?)",
                    (app_id, i, login_, u["full_name"] or login_, u["title"], label))
    number = a["number"]
    if not number:
        y = db.now_local().year
        number = f"Z-{y}-{db.next_counter(con, f'app:{y}'):04d}"
    status = "review" if steps else "approved"
    con.execute("UPDATE applications SET status=?, number=?, submitted_at=?, return_comment='' WHERE id=?",
                (status, number, now(), app_id))
    log(con, app_id, session["user"], "Заявление отправлено на согласование" if steps
        else "Заявление отправлено кадровику (согласующих нет)")
    for n in notes:
        log(con, app_id, "система", n)
    return notes


def current_step(con, app_id):
    return con.execute("SELECT * FROM app_steps WHERE app_id=? AND status='pending' ORDER BY ord, id LIMIT 1",
                       (app_id,)).fetchone()


def pending(con, login_, role):
    """Сколько заявлений ждёт действия пользователя (для значка в меню)."""
    n = con.execute(
        "SELECT COUNT(*) FROM applications a JOIN app_steps s ON s.app_id=a.id WHERE a.status='review' AND "
        "s.login=? AND s.status='pending' AND s.ord=(SELECT MIN(ord) FROM app_steps WHERE app_id=a.id AND status='pending')",
        (login_,)).fetchone()[0]
    n += con.execute("SELECT COUNT(*) FROM applications WHERE author=? AND status='returned'", (login_,)).fetchone()[0]
    if role in ("hr", "admin"):
        n += con.execute("SELECT COUNT(*) FROM applications WHERE status='approved'").fetchone()[0]
    return n


def can_see(con, a):
    """Автор, кадровики, директор и администратор видят всегда; согласующие — когда до них дошла очередь или они уже действовали."""
    role = session.get("role")
    if role in ("hr", "admin", "director") or a["author"] == session["user"]:
        return True
    mine = con.execute("SELECT * FROM app_steps WHERE app_id=? AND login=?", (a["id"], session["user"])).fetchall()
    if a["status"] == "draft":
        return False
    cur = current_step(con, a["id"]) if a["status"] == "review" else None
    return any(st["status"] != "pending" or (cur is not None and st["ord"] == cur["ord"]) for st in mine)


def flow(con, a):
    """Данные для области «Ход документа» заявления."""
    steps = con.execute("SELECT * FROM app_steps WHERE app_id=? ORDER BY ord, id", (a["id"],)).fetchall()
    now, nxt = [], []
    if a["status"] == "review":
        cur = current_step(con, a["id"])
        for st in steps:
            if st["status"] == "pending":
                (now if cur is not None and st["id"] == cur["id"] else nxt).append(
                    {"who": st["name"], "title": st["title"], "action": "Согласование", "due": ""})
    elif a["status"] == "approved":
        now.append({"who": "Отдел кадров", "title": "", "action": "Оформление и исполнение", "due": ""})
    elif a["status"] in ("draft", "returned"):
        now.append({"who": extras.user_name(con, a["author"]), "title": "", "action": "Автор: доработка и отправка", "due": ""})
    ev = [{"at": r["at"], "who": extras.user_name(con, r["who"]), "what": r["text"], "comment": ""}
          for r in con.execute("SELECT * FROM app_log WHERE app_id=? ORDER BY id", (a["id"],))]
    return {"flow_now": now, "flow_next": nxt, "flow_events": ev, "flow_today": now_date()}


def now_date():
    return db.now_local().strftime("%Y-%m-%d")


def _items(rows, con):
    out = []
    for a in rows:
        e = con.execute("SELECT * FROM employees WHERE id=?", (a["employee_id"],)).fetchone()
        cur = current_step(con, a["id"]) if a["status"] == "review" else None
        out.append({"a": a, "fio": fio_of(e) if e else "?", "dept": e["department"] if e else "",
                    "cur": cur})
    return out


# ---------- сотрудник: свои заявления ----------

@route("/my")
@login_required
def ap_my():
    con = db.connect()
    try:
        emp = my_employee(con)
        rows = con.execute("SELECT * FROM applications WHERE author=? ORDER BY id DESC", (session["user"],)).fetchall()
        items = _items(rows, con)
    finally:
        con.close()
    return render_template("ap_my.html", items=items, emp=emp, STATUS_RU=STATUS_RU)


@route("/my/new")
@login_required
def ap_new():
    con = db.connect()
    try:
        emp = my_employee(con)
        types = con.execute("SELECT * FROM app_types WHERE active=1 ORDER BY ord, id").fetchall()
    finally:
        con.close()
    if emp is None:
        flash("Ваш логин не привязан к карточке работающего сотрудника. Обратитесь в отдел кадров.", "error")
        return redirect(url_for("ap_my"))
    return render_template("ap_pick.html", types=types)


def _form_page(t, a, emp, values, body, con):
    return render_template("ap_form.html", t=t, a=a, emp=emp, fields=type_fields(t), values=values, body=body)


@route("/my/new/<int:type_id>", methods=("GET", "POST"))
@login_required
def ap_form(type_id):
    return _edit(None, type_id)


@route("/my/<int:app_id>/edit", methods=("GET", "POST"))
@login_required
def ap_edit(app_id):
    return _edit(app_id, None)


def _edit(app_id, type_id):
    con = db.connect()
    try:
        a = None
        if app_id:
            a = con.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
            if a is None:
                abort(404)
            if a["author"] != session["user"]:
                abort(403)
            if a["status"] not in ("draft", "returned"):
                flash("Это заявление уже нельзя править.", "error")
                return redirect(url_for("ap_view", app_id=app_id))
            type_id = a["type_id"]
        t = con.execute("SELECT * FROM app_types WHERE id=?", (type_id,)).fetchone()
        if t is None or (not t["active"] and a is None):
            abort(404)
        emp = my_employee(con)
        if emp is None:
            flash("Ваш логин не привязан к карточке работающего сотрудника.", "error")
            return redirect(url_for("ap_my"))
        fields = type_fields(t)
        settings = db.get_settings(con)
        if request.method == "POST":
            values = _values(request.form, fields)
            body = sanitize(request.form.get("body", ""))
            action = request.form.get("do", "save")
            errors = []
            if action == "submit":
                errors += _check(values, fields)
                if not plain(body):
                    errors.append("Текст заявления пустой.")
            if not errors:
                if a is None:
                    cur = con.execute(
                        "INSERT INTO applications(type_id, type_name, kind, employee_id, author, fields, body, created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)", (t["id"], t["name"], t["kind"], emp["id"], session["user"],
                                                    json.dumps(values, ensure_ascii=False), body, now()))
                    app_id = cur.lastrowid
                    log(con, app_id, session["user"], "Заявление создано")
                else:
                    con.execute("UPDATE applications SET fields=?, body=? WHERE id=?",
                                (json.dumps(values, ensure_ascii=False), body, app_id))
                    log(con, app_id, session["user"], "Заявление изменено")
                if action == "submit":
                    notes = submit(con, app_id)
                    for n in notes:
                        flash(n, "error")
                    flash("Заявление отправлено.", "ok")
                else:
                    flash("Черновик сохранён.", "ok")
                return redirect(url_for("ap_view", app_id=app_id))
            for e in errors:
                flash(e, "error")
        else:
            values = json.loads(a["fields"]) if a else {}
            body = a["body"] if a else render_body(t["body"], emp, values, settings)
        return _form_page(t, a, emp, values, body, con)
    finally:
        con.close()


@route("/my/render/<int:type_id>", methods=("POST",))
@login_required
def ap_render(type_id):
    """Текст заявления по шаблону и введённым полям (кнопка «Обновить текст по полям»)."""
    con = db.connect()
    try:
        t = con.execute("SELECT * FROM app_types WHERE id=?", (type_id,)).fetchone()
        emp = my_employee(con)
        if t is None or emp is None:
            abort(404)
        values = _values(request.form, type_fields(t))
        return jsonify(html=render_body(t["body"], emp, values, db.get_settings(con)))
    finally:
        con.close()


# ---------- просмотр и действия ----------

@route("/ap/<int:app_id>")
@login_required
def ap_view(app_id):
    con = db.connect()
    try:
        a = con.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        if a is None:
            abort(404)
        if not can_see(con, a):
            abort(403)
        emp = con.execute("SELECT * FROM employees WHERE id=?", (a["employee_id"],)).fetchone()
        steps = con.execute("SELECT * FROM app_steps WHERE app_id=? ORDER BY ord, id", (app_id,)).fetchall()
        logs = con.execute("SELECT * FROM app_log WHERE app_id=? ORDER BY id", (app_id,)).fetchall()
        cur = current_step(con, app_id) if a["status"] == "review" else None
        settings = db.get_settings(con)
        flow_data = flow(con, a)
        app_orders = con.execute("SELECT id, number, status FROM orders WHERE app_id=? AND deleted_at IS NULL "
                                 "AND status<>'cancelled' ORDER BY id", (app_id,)).fetchall() if session.get("role") in ("hr", "admin") else []
    finally:
        con.close()
    role = session.get("role")
    mine = cur is not None and cur["login"] == session["user"]
    return render_template(
        "ap_view.html", a=a, emp=emp, steps=steps, logs=logs, cur=cur, s=settings, STATUS_RU=STATUS_RU,
        fio=fio_of(emp), app_orders=app_orders, mine=mine, is_author=a["author"] == session["user"],
        is_hr=role in ("hr", "admin"), kinds=KINDS, **flow_data)


@route("/ap/<int:app_id>/act", methods=("POST",))
@login_required
def ap_act(app_id):
    action = request.form.get("act", "")
    comment = request.form.get("comment", "").strip()[:1000]
    con = db.connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        a = con.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        if a is None:
            con.execute("ROLLBACK")
            abort(404)
        me = session["user"]
        msg, cat = None, "ok"
        if action in ("approve", "return", "reject"):
            cur = current_step(con, app_id) if a["status"] == "review" else None
            if cur is None or cur["login"] != me:
                msg, cat = "Сейчас не ваша очередь согласовывать это заявление.", "error"
            elif action != "approve" and not comment:
                msg, cat = "Напишите причину.", "error"
            else:
                u = user_row(con, me)
                con.execute("UPDATE app_steps SET status=?, at=?, comment=?, name=?, title=? WHERE id=?",
                            ("approved" if action == "approve" else "rejected", now(), comment,
                             u["full_name"] or me, u["title"], cur["id"]))
                who = u["full_name"] or me
                if action == "approve":
                    log(con, app_id, me, f"Согласовано: {who}" + (f" — {comment}" if comment else ""))
                    if current_step(con, app_id) is None:
                        con.execute("UPDATE applications SET status='approved' WHERE id=?", (app_id,))
                        log(con, app_id, "система", "Все согласовали. Заявление передано кадровику.")
                    msg = "Ваше согласие записано."
                elif action == "return":
                    con.execute("UPDATE applications SET status='returned', return_comment=? WHERE id=?",
                                (f"{who}: {comment}", app_id))
                    log(con, app_id, me, f"Возвращено на доработку ({who}): {comment}")
                    msg = "Заявление возвращено заявителю."
                else:
                    con.execute("UPDATE applications SET status='rejected', return_comment=?, finished_at=? WHERE id=?",
                                (f"{who}: {comment}", now(), app_id))
                    log(con, app_id, me, f"Отказано ({who}): {comment}")
                    msg = "Отказ записан."
        elif action == "withdraw":
            if a["author"] != me or a["status"] not in ("review", "returned"):
                msg, cat = "Отозвать это заявление нельзя.", "error"
            else:
                con.execute("DELETE FROM app_steps WHERE app_id=?", (app_id,))
                con.execute("UPDATE applications SET status='draft' WHERE id=?", (app_id,))
                log(con, app_id, me, "Заявитель отозвал заявление в черновики")
                msg = "Заявление возвращено в черновики."
        elif action == "delete":
            if a["author"] != me or a["status"] != "draft":
                msg, cat = "Удалить можно только свой черновик.", "error"
            else:
                con.execute("DELETE FROM applications WHERE id=?", (app_id,))
                con.execute("COMMIT")
                flash("Черновик удалён.", "ok")
                return redirect(url_for("ap_my"))
        elif action == "done":
            if session.get("role") not in ("hr", "admin") or a["status"] != "approved":
                msg, cat = "Отметить исполненным может кадровик после согласования.", "error"
            else:
                con.execute("UPDATE applications SET status='done', result_note=?, finished_at=? WHERE id=?",
                            (comment, now(), app_id))
                log(con, app_id, me, "Исполнено" + (f": {comment}" if comment else ""))
                msg = "Заявление отмечено исполненным."
        else:
            msg, cat = "Неизвестное действие.", "error"
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        con.close()
    flash(msg, cat)
    return redirect(url_for("ap_view", app_id=app_id))


# ---------- очередь согласующего и общий список ----------

@route("/ap/inbox")
@login_required
def ap_inbox():
    con = db.connect()
    try:
        me = session["user"]
        turn = con.execute(
            "SELECT a.* FROM applications a JOIN app_steps s ON s.app_id=a.id WHERE a.status='review' AND s.login=? "
            "AND s.status='pending' AND s.ord=(SELECT MIN(ord) FROM app_steps WHERE app_id=a.id AND status='pending') "
            "ORDER BY a.id", (me,)).fetchall()
        waiting = con.execute(
            "SELECT DISTINCT a.* FROM applications a JOIN app_steps s ON s.app_id=a.id WHERE a.status='review' AND s.login=? "
            "AND s.status='pending' ORDER BY a.id DESC", (me,)).fetchall()
        waiting = [a for a in waiting if a["id"] not in {x["id"] for x in turn}]
        done = con.execute(
            "SELECT DISTINCT a.* FROM applications a JOIN app_steps s ON s.app_id=a.id WHERE s.login=? AND s.status<>'pending' "
            "ORDER BY a.id DESC LIMIT 50", (me,)).fetchall()
        hr_wait = []
        if session.get("role") in ("hr", "admin"):
            hr_wait = con.execute("SELECT * FROM applications WHERE status='approved' ORDER BY id").fetchall()
        mine_ret = con.execute("SELECT * FROM applications WHERE author=? AND status='returned' ORDER BY id DESC", (me,)).fetchall()
        data = {k: _items(v, con) for k, v in (("turn", turn), ("waiting", waiting), ("done", done),
                                                ("hr_wait", hr_wait), ("returned", mine_ret))}
    finally:
        con.close()
    return render_template("ap_inbox.html", STATUS_RU=STATUS_RU, **data)


@route("/applications")
@roles_required("hr", "admin", "director")
def ap_all():
    q = request.args.get("q", "").strip().lower()
    status = request.args.get("status", "")
    typ = request.args.get("type", "", type=str)
    department = request.args.get("department", "")
    con = db.connect()
    try:
        rows = con.execute("SELECT * FROM applications WHERE status<>'draft' ORDER BY id DESC").fetchall()
        items = _items(rows, con)
        types = con.execute("SELECT id, name FROM app_types ORDER BY ord, id").fetchall()
        deps = [r[0] for r in con.execute("SELECT DISTINCT department FROM employees ORDER BY department")]
    finally:
        con.close()
    total = len(items)
    if status:
        items = [i for i in items if i["a"]["status"] == status]
    if typ:
        items = [i for i in items if str(i["a"]["type_id"]) == typ]
    if department:
        items = [i for i in items if i["dept"] == department]
    if q:
        items = [i for i in items if q in (i["fio"] + " " + (i["a"]["number"] or "") + " " + i["a"]["type_name"]).lower()]
    return render_template("ap_all.html", items=items, STATUS_RU=STATUS_RU, types=types, deps=deps, q=request.args.get("q", ""),
                           status=status, typ=typ, department=department, total=total,
                           filtered=any([q, status, typ, department]))


# ---------- администратор: шаблоны, цепочки, начальники ----------

@route("/ap-types")
@roles_required("admin")
def ap_types():
    con = db.connect()
    try:
        types = con.execute("SELECT * FROM app_types ORDER BY ord, id").fetchall()
        routes = {t["id"]: con.execute("SELECT * FROM app_route WHERE type_id=? ORDER BY ord, id", (t["id"],)).fetchall()
                  for t in types}
        cnt = {t["id"]: con.execute("SELECT COUNT(*) FROM applications WHERE type_id=?", (t["id"],)).fetchone()[0]
               for t in types}
    finally:
        con.close()
    return render_template("ap_types.html", types=types, routes=routes, cnt=cnt, KINDS=KINDS)


@route("/ap-types/new", methods=("POST",))
@roles_required("admin")
def ap_type_new():
    name = " ".join(request.form.get("name", "").split())
    con = db.connect()
    try:
        if not name:
            flash("Укажите название шаблона.", "error")
            return redirect(url_for("ap_types"))
        try:
            cur = con.execute("INSERT INTO app_types(name, kind, body, fields, ord) VALUES(?,?,?,?,"
                              "(SELECT COALESCE(MAX(ord), 0) + 1 FROM app_types))",
                              (name, "none", sanitize(_tpl("ARIZA", ["{text}"])),
                               json.dumps([{"key": "text", "label": "Суть заявления", "type": "area", "required": True}],
                                          ensure_ascii=False)))
        except Exception:
            flash("Шаблон с таким названием уже есть.", "error")
            return redirect(url_for("ap_types"))
        con.execute("INSERT INTO app_route(type_id, ord, who, label) VALUES(?,?,?,?)",
                    (cur.lastrowid, 0, "head", "Начальник подразделения"))
        return redirect(url_for("ap_type_edit", type_id=cur.lastrowid))
    finally:
        con.close()


@route("/ap-types/<int:type_id>", methods=("GET", "POST"))
@roles_required("admin")
def ap_type_edit(type_id):
    con = db.connect()
    try:
        t = con.execute("SELECT * FROM app_types WHERE id=?", (type_id,)).fetchone()
        if t is None:
            abort(404)
        if request.method == "POST":
            act = request.form.get("act")
            if act == "delete":
                if con.execute("SELECT 1 FROM applications WHERE type_id=?", (type_id,)).fetchone():
                    con.execute("UPDATE app_types SET active=0 WHERE id=?", (type_id,))
                    flash("По шаблону уже есть заявления, поэтому он скрыт, а не удалён.", "ok")
                else:
                    con.execute("DELETE FROM app_types WHERE id=?", (type_id,))
                    flash("Шаблон удалён.", "ok")
                return redirect(url_for("ap_types"))
            f = request.form
            name = " ".join(f.get("name", "").split())
            kind = f.get("kind", "none")
            keys = [k.strip() for k in f.getlist("f_key")]
            fields, seen, errors = [], set(), []
            for k, lbl, typ, req in zip(keys, f.getlist("f_label"), f.getlist("f_type"), f.getlist("f_req")):
                if not k and not lbl.strip():
                    continue
                if not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", k) or k in seen or k in dict(BASE_TOKENS):
                    errors.append(f"Код поля «{k}» должен быть из латинских букв, цифр и _, не повторяться и не совпадать со стандартными.")
                    continue
                seen.add(k)
                fields.append({"key": k, "label": lbl.strip() or k, "type": typ if typ in FIELD_TYPES else "text",
                               "required": req == "yes"})
            if not name:
                errors.append("Укажите название.")
            if kind not in KINDS:
                kind = "none"
            if errors:
                for e in errors:
                    flash(e, "error")
                return redirect(url_for("ap_type_edit", type_id=type_id))
            try:
                con.execute("UPDATE app_types SET name=?, kind=?, body=?, fields=?, active=? WHERE id=?",
                            (name, kind, sanitize(f.get("body", "")), json.dumps(fields, ensure_ascii=False),
                             1 if f.get("active") else 0, type_id))
            except Exception:
                flash("Шаблон с таким названием уже есть.", "error")
                return redirect(url_for("ap_type_edit", type_id=type_id))
            rows = []
            for who, login_, label in zip(f.getlist("r_who"), f.getlist("r_login"), f.getlist("r_label")):
                if who == "head":
                    rows.append(("head", "", label.strip()[:80]))
                elif who == "user" and login_ and user_row(con, login_):
                    rows.append(("user", login_, label.strip()[:80]))
            con.execute("DELETE FROM app_route WHERE type_id=?", (type_id,))
            for i, (who, login_, label) in enumerate(rows):
                con.execute("INSERT INTO app_route(type_id, ord, who, login, label) VALUES(?,?,?,?,?)",
                            (type_id, i, who, login_, label))
            flash("Шаблон и цепочка согласования сохранены.", "ok")
            return redirect(url_for("ap_type_edit", type_id=type_id))
        route_rows = con.execute("SELECT * FROM app_route WHERE type_id=? ORDER BY ord, id", (type_id,)).fetchall()
        users = con.execute("SELECT login, full_name, title FROM users ORDER BY full_name, login").fetchall()
    finally:
        con.close()
    return render_template("ap_type_edit.html", t=t, fields=type_fields(t), route_rows=route_rows,
                           users=[dict(u) for u in users],
                           KINDS=KINDS, FIELD_TYPES=FIELD_TYPES, BASE_TOKENS=BASE_TOKENS)


@route("/ap-heads", methods=("GET", "POST"))
@roles_required("admin")
def ap_heads():
    con = db.connect()
    try:
        if request.method == "POST":
            dept = request.form.get("department", "")
            login_ = request.form.get("login", "")
            if login_ and user_row(con, login_):
                con.execute("INSERT INTO dept_heads(department, login) VALUES(?,?) "
                            "ON CONFLICT(department) DO UPDATE SET login=excluded.login", (dept, login_))
                flash(f"Начальник подразделения «{dept}» назначен.", "ok")
            else:
                con.execute("DELETE FROM dept_heads WHERE department=?", (dept,))
                flash(f"Начальник подразделения «{dept}» не назначен.", "ok")
            return redirect(url_for("ap_heads"))
        deps = sorted({r[0] for r in con.execute("SELECT name FROM departments")} |
                      {r[0] for r in con.execute("SELECT DISTINCT department FROM employees")})
        heads = {r["department"]: r["login"] for r in con.execute("SELECT * FROM dept_heads")}
        users = con.execute("SELECT login, full_name, title FROM users ORDER BY full_name, login").fetchall()
    finally:
        con.close()
    return render_template("ap_heads.html", deps=deps, heads=heads, users=users)


# ---------- доступ сотрудника в программу ----------

@route("/employee/<int:emp_id>/access", methods=("POST",))
@roles_required("hr", "admin")
def employee_access(emp_id):
    con = db.connect()
    try:
        e = con.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
        if e is None:
            abort(404)
        if e["status"] != db.ACTIVE:
            flash("Уволенному сотруднику доступ не выдаётся.", "error")
            return redirect(url_for("employee", emp_id=emp_id))
        pw = secrets.token_urlsafe(9)[:10]
        u = con.execute("SELECT login FROM users WHERE employee_id=?", (emp_id,)).fetchone()
        if u:
            con.execute("UPDATE users SET password_hash=? WHERE login=?", (generate_password_hash(pw), u["login"]))
            login_ = u["login"]
            flash(f"Новый пароль для логина {login_}: {pw} — передайте сотруднику. Позже он сможет сменить его сам.", "ok")
        else:
            login_ = "t" + e["tabel_no"]
            con.execute("INSERT INTO users(login, password_hash, full_name, role, title, employee_id) VALUES(?,?,?,?,?,?)",
                        (login_, generate_password_hash(pw), fio_of(e), "employee", e["position"], emp_id))
            flash(f"Доступ создан. Логин: {login_}  Пароль: {pw} — запишите и передайте сотруднику, пароль больше не показывается.", "ok")
    finally:
        con.close()
    return redirect(url_for("employee", emp_id=emp_id))


@route("/my/<int:app_id>/copy", methods=("POST",))
@login_required
def ap_copy(app_id):
    """Новый черновик заявления с тем же текстом (для автора заявления)."""
    con = db.connect()
    try:
        a = con.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        if a is None:
            abort(404)
        if a["author"] != session["user"]:
            abort(403)
        emp = my_employee(con)
        if emp is None:
            flash("Ваш логин не привязан к карточке работающего сотрудника.", "error")
            return redirect(url_for("ap_my"))
        cur = con.execute(
            "INSERT INTO applications(type_id, type_name, kind, employee_id, author, fields, body, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)", (a["type_id"], a["type_name"], a["kind"], emp["id"], session["user"],
                                        a["fields"], a["body"], now()))
        new_id = cur.lastrowid
        log(con, new_id, session["user"], f"Создано копированием из заявления {a['number'] or '№' + str(a['id'])}")
    finally:
        con.close()
    flash("Копия создана как черновик. Исправьте и отправьте.", "ok")
    return redirect(url_for("ap_edit", app_id=new_id))
