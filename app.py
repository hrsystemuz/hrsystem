"""Кадровая программа: вход, сотрудники, приказы, подпись с QR-кодом."""
import json
import re
import secrets
import socket
import sqlite3
import sys
from datetime import date
from functools import wraps

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,
                   session, url_for)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

import db
import qr

app = Flask(__name__)


def _load_secret():
    path = db.BASE_DIR / "data" / "secret.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(secrets.token_hex(32))
    return path.read_text().strip()


app.secret_key = _load_secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", TEMPLATES_AUTO_RELOAD=True)


# ---------- защита форм и вход ----------

def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.template_filter("dmy")
def dmy(value):
    """2026-03-05 -> 05.03.2026"""
    try:
        return date.fromisoformat(value).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return value or ""


UZ_MONTHS = ["yanvar", "fevral", "mart", "aprel", "may", "iyun",
             "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr"]


@app.template_filter("uzdate")
def uzdate(value):
    """2024-08-19 -> 2024-yil 19-avgust (к «dan» добавляется в тексте приказа)"""
    try:
        d = date.fromisoformat(value)
    except (TypeError, ValueError):
        return value or ""
    return f"{d.year}-yil {d.day}-{UZ_MONTHS[d.month - 1]}"


@app.template_filter("uzhead")
def uzhead(value):
    """2024-08-22 -> 2024-yil «22» avgust"""
    try:
        d = date.fromisoformat(value)
    except (TypeError, ValueError):
        return value or ""
    return f"{d.year}-yil «{d.day}» {UZ_MONTHS[d.month - 1]}"


@app.template_filter("qr")
def qr_filter(url):
    return Markup(qr.svg(url))


@app.before_request
def check_csrf():
    if request.method == "POST":
        sent = request.form.get("csrf", "")
        if not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400)


@app.before_request
def refresh_user():
    """Роль берём из базы при каждом запросе: изменения прав действуют сразу."""
    if "user" in session and request.endpoint not in ("static",):
        con = db.connect()
        try:
            row = con.execute("SELECT u.role, e.status AS estatus FROM users u LEFT JOIN employees e ON e.id=u.employee_id "
                              "WHERE u.login=?", (session["user"],)).fetchone()
        finally:
            con.close()
        if row is None or row["estatus"] == db.DISMISSED:
            session.clear()
            if row is not None:
                flash("Доступ закрыт: сотрудник уволен.", "error")
        else:
            session["role"] = row["role"]

EMPLOYEE_OK = {"login", "logout", "change_password", "static", "verify", "my_profile",
               "mail_inbox", "mail_new", "mail_view", "mail_file", "mail_delete", "sheet_app"}


@app.before_request
def employee_guard():
    """Обычный сотрудник видит только свои заявления и то, что ему нужно согласовать."""
    if session.get("role") == "employee":
        ep = request.endpoint or ""
        if ep not in EMPLOYEE_OK and not ep.startswith("ap_"):
            if request.method == "POST":
                abort(403)
            return redirect(url_for("ap_my"))


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


def roles_required(*roles):
    def deco(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if "user" not in session:
                return redirect(url_for("login"))
            if session.get("role") not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return deco


def ensure_admin():
    """При первом запуске создаём администратора со случайным паролем."""
    con = db.connect()
    try:
        if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            password = secrets.token_urlsafe(8)
            con.execute(
                "INSERT INTO users(login, password_hash, full_name, role) "
                "VALUES(?,?,?,?)",
                ("admin", generate_password_hash(password), "Администратор", "admin"),
            )
            note = db.BASE_DIR / "data" / "ПАРОЛЬ_АДМИНИСТРАТОРА.txt"
            note.write_text(f"Логин: admin\nПароль: {password}\n", encoding="utf-8")
            print("=" * 50)
            print("Создан администратор.  Логин: admin  Пароль:", password)
            print("Пароль также сохранён в файле:", note)
            print("=" * 50)
    finally:
        con.close()


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        con = db.connect()
        user = con.execute(
            "SELECT * FROM users WHERE login=?", (request.form.get("login", "").strip(),)
        ).fetchone()
        con.close()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session["user"] = user["login"]
            session["role"] = user["role"]
            return redirect(url_for("home"))
        flash("Неверный логин или пароль", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- пользователи программы ----------

def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapper


ROLES = {"employee": "Сотрудник (подаёт заявления)", "admin": "Администратор", "hr": "Кадровик", "director": "Директор (подписывает приказы)",
         "approver": "Согласующий (юрист, профсоюз, начальник и др.)",
         "clerk": "Делопроизводитель (входящие письма, контроль исполнения)"}

VAC_TYPES = {
    "annual": ("Ежегодный", "yillik mehnat ta’tili"),
    "educational": ("Учебный", "o‘quv ta’tili"),
    "unpaid": ("Без сохранения заработной платы", "ish haqi saqlanmaydigan ta’til"),
    "childcare": ("По уходу за ребёнком", "bolani parvarish qilish ta’tili"),
    "social": ("Социальный", "ijtimoiy ta’til"),
}
TRIP_KINDS = {"local": ("Местная", "mahalliy"), "foreign": ("Зарубежная", "chet el")}
app.jinja_env.globals.update(VAC_TYPES=VAC_TYPES, TRIP_KINDS=TRIP_KINDS)


@app.route("/users", methods=["GET", "POST"])
@admin_required
def users():
    con = db.connect()
    try:
        if request.method == "POST":
            action = request.form.get("action")
            login_ = request.form.get("login", "").strip()
            if action == "add":
                name = request.form.get("full_name", "").strip()
                title = request.form.get("title", "").strip()
                pw = request.form.get("password", "")
                role = request.form.get("role", "hr")
                if not re.fullmatch(r"[A-Za-z0-9._-]{3,32}", login_):
                    flash("Логин: 3–32 символа, только латинские буквы, цифры, точка, дефис, подчёркивание.", "error")
                elif len(pw) < 8:
                    flash("Пароль должен быть не короче 8 символов.", "error")
                elif role not in ROLES:
                    flash("Неизвестная роль.", "error")
                else:
                    try:
                        con.execute(
                            "INSERT INTO users(login, password_hash, full_name, role, title) VALUES(?,?,?,?,?)",
                            (login_, generate_password_hash(pw), name, role, title))
                        flash(f"Пользователь {login_} добавлен.", "ok")
                    except sqlite3.IntegrityError:
                        flash("Такой логин уже существует.", "error")
            elif action == "profile":
                name = request.form.get("full_name", "").strip()
                title = request.form.get("title", "").strip()
                role = request.form.get("role")
                if login_ == session["user"] or role not in ROLES:
                    role = None  # свою роль менять нельзя, чтобы не остаться без администратора
                tabel = request.form.get("tabel_no")
                if tabel is not None:
                    tabel = tabel.strip()
                    cur_u = con.execute("SELECT role FROM users WHERE login=?", (login_,)).fetchone()
                    if cur_u and cur_u["role"] != "employee":      # у сотрудников-заявителей связь ставится при выдаче доступа
                        if not tabel:
                            con.execute("UPDATE users SET employee_id=NULL WHERE login=?", (login_,))
                        else:
                            emp = con.execute("SELECT id FROM employees WHERE tabel_no=?", (tabel,)).fetchone()
                            if emp is None:
                                flash(f"Сотрудник с табельным номером {tabel} не найден: связь не сохранена.", "error")
                            else:
                                con.execute("UPDATE users SET employee_id=? WHERE login=?", (emp["id"], login_))
                if role:
                    con.execute("UPDATE users SET full_name=?, title=?, role=? WHERE login=?",
                                (name, title, role, login_))
                else:
                    con.execute("UPDATE users SET full_name=?, title=? WHERE login=?",
                                (name, title, login_))
                flash(f"Данные пользователя {login_} сохранены.", "ok")
            elif action == "reset":
                pw = request.form.get("password", "")
                if len(pw) < 8:
                    flash("Пароль должен быть не короче 8 символов.", "error")
                elif con.execute("UPDATE users SET password_hash=? WHERE login=?",
                                 (generate_password_hash(pw), login_)).rowcount:
                    flash(f"Пароль пользователя {login_} изменён.", "ok")
            elif action == "delete":
                if login_ == session["user"]:
                    flash("Нельзя удалить самого себя.", "error")
                else:
                    con.execute("DELETE FROM users WHERE login=?", (login_,))
                    flash(f"Пользователь {login_} удалён.", "ok")
            return redirect(url_for("users"))
        rows = con.execute("SELECT u.*, e.tabel_no AS tabel_no, e.department AS dept FROM users u "
                           "LEFT JOIN employees e ON e.id = u.employee_id ORDER BY u.id").fetchall()
    finally:
        con.close()
    return render_template("users.html", rows=rows, roles=ROLES)


@app.route("/password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "GET":
        return redirect(url_for("my_profile") + "#pwd")
    if request.method == "POST":
        old = request.form.get("old", "")
        new = request.form.get("new", "")
        con = db.connect()
        try:
            user = con.execute("SELECT * FROM users WHERE login=?", (session["user"],)).fetchone()
            if not user or not check_password_hash(user["password_hash"], old):
                flash("Старый пароль указан неверно.", "error")
            elif len(new) < 8:
                flash("Новый пароль должен быть не короче 8 символов.", "error")
            else:
                con.execute("UPDATE users SET password_hash=? WHERE login=?",
                            (generate_password_hash(new), session["user"]))
                flash("Пароль изменён.", "ok")
                return redirect(url_for("my_profile"))
        finally:
            con.close()
    return redirect(url_for("my_profile") + "#pwd")


# ---------- сотрудники ----------

def _in_process(con):
    """Люди, по которым есть приказ, который уже создан и идёт по подписям (ещё не подписан):
    новые сотрудники (приказ о приёме) и работающие/уволенные, по которым оформляется приказ."""
    out = []
    emps = {e["id"]: e for e in con.execute("SELECT * FROM employees")}
    for o in con.execute("SELECT * FROM orders WHERE status='draft' AND deleted_at IS NULL ORDER BY id DESC"):
        try:
            d = json.loads(o["data"])
        except ValueError:
            continue
        who = _waiting_on(con, o)
        label = KIND_RU.get(o["kind"], o["kind"]) + (f" · у: {who}" if who else "")
        link = url_for("order_view", order_id=o["id"])
        if o["kind"] == "hire":
            r = {"id": 0, "tabel_no": d.get("tabel_no") or "—", "last_name": d.get("last_name", ""), "first_name": d.get("first_name", ""),
                 "middle_name": d.get("middle_name", ""), "position": d.get("position", ""), "department": d.get("department", ""),
                 "contract_no": "", "folder_no": "", "old_folder_no": "", "pinfl": d.get("pinfl", ""), "hire_date": d.get("hire_date", ""),
                 "status": "в процессе"}
            r["proc"], r["url"] = label, link
            out.append(r)
        elif d.get("employee_id") in emps:
            r = dict(emps[d["employee_id"]])
            r["proc"], r["url"] = label, link
            out.append(r)
    return out


@app.route("/employees")
@login_required
def employees():
    q = request.args.get("q", "").strip().lower()
    f_tabel = request.args.get("tabel", "").strip().lower()
    f_name = request.args.get("name", "").strip().lower()
    f_contract = request.args.get("contract", "").strip().lower()
    f_folder = request.args.get("folder", "").strip().lower()
    status = request.args.get("status", "")
    department = request.args.get("department", "")
    position = request.args.get("position", "").strip().lower()
    hire_from = request.args.get("hire_from", "")
    hire_to = request.args.get("hire_to", "")
    if request.args.get("hire"):
        hire_from = hire_to = request.args["hire"]
    sort = request.args.get("sort", "tabel")
    con = db.connect()
    last_tr = {r[0]: r[1] for r in con.execute("SELECT employee_id, MAX(transfer_date) FROM transfers GROUP BY employee_id")}
    rows = [dict(r) for r in con.execute("SELECT * FROM employees")]
    for r in rows:                       # с какого числа на этой должности: последний перевод, иначе дата приёма
        r["since"] = max(last_tr.get(r["id"]) or "", r["hire_date"] or "")
    deps = [r[0] for r in con.execute("SELECT DISTINCT department FROM employees ORDER BY department")]
    proc = _in_process(con)
    for r in proc:
        r.setdefault("since", r.get("hire_date", ""))
    con.close()
    total = len(rows)
    if status == "process":
        rows = proc
    elif status in (db.ACTIVE, db.DISMISSED):
        rows = [r for r in rows if r["status"] == status]
    if department:
        rows = [r for r in rows if r["department"] == department]
    if position:
        rows = [r for r in rows if position in r["position"].lower()]
    if hire_from:
        rows = [r for r in rows if r["hire_date"] >= hire_from]
    if hire_to:
        rows = [r for r in rows if r["hire_date"] <= hire_to]
    if request.args.get("since"):
        rows = [r for r in rows if r["since"] == request.args["since"]]
    if f_tabel:
        rows = [r for r in rows if f_tabel in (r["tabel_no"] or "").lower()]
    if f_name:
        rows = [r for r in rows if all(w in " ".join([r["last_name"], r["first_name"], r["middle_name"]]).lower() for w in f_name.split())]
    if f_contract:
        rows = [r for r in rows if f_contract in (r["contract_no"] or "").lower()]
    if f_folder:
        rows = [r for r in rows if f_folder in ((r["folder_no"] or "") + " " + (r["old_folder_no"] or "")).lower()]
    if q:
        def match(r):
            hay = " ".join([r["last_name"], r["first_name"], r["middle_name"],
                            r["tabel_no"], r["pinfl"], r["folder_no"] or "",
                            r["old_folder_no"] or "", r["contract_no"]]).lower()
            return q in hay
        rows = [r for r in rows if match(r)]
    keys = {"tabel": (lambda r: r["tabel_no"], True), "name": (lambda r: (r["last_name"], r["first_name"]), False),
            "hire": (lambda r: r["hire_date"], True), "dept": (lambda r: (r["department"], r["position"]), False)}
    key, rev = keys.get(sort, keys["tabel"])
    rows = sorted(rows, key=key, reverse=rev)
    return render_template("employees.html", rows=rows, q=request.args.get("q", ""), status=status,
                           department=department, position=request.args.get("position", ""),
                           hire_from=hire_from, hire_to=hire_to, sort=sort, deps=deps, total=total,
                           f_tabel=request.args.get("tabel", ""), f_name=request.args.get("name", ""),
                           f_contract=request.args.get("contract", ""), f_folder=request.args.get("folder", ""), since=request.args.get("since", ""),
                           filtered=any([request.args.get("since"), q, f_tabel, f_name, f_contract, f_folder, status, department, position, hire_from, hire_to]))


@app.route("/employee/<int:emp_id>")
@login_required
def employee(emp_id):
    con = db.connect()
    try:
        emp = con.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
        if emp is None:
            abort(404)
        transfers = con.execute(
            "SELECT * FROM transfers WHERE employee_id=? ORDER BY transfer_date, id",
            (emp_id,)).fetchall()
        hire_order = None
        if emp["hire_order_id"]:
            hire_order = con.execute("SELECT id, number, order_date FROM orders WHERE id=?",
                                     (emp["hire_order_id"],)).fetchone()
        emp_orders = con.execute(
            "SELECT * FROM orders WHERE kind<>'hire' AND deleted_at IS NULL AND (employee_id=? OR (employee_id IS NULL "
            "AND status IN ('draft','returned') AND data LIKE ?)) ORDER BY id", (emp_id, f'%"employee_id": {emp_id},%')).fetchall()
    finally:
        con.close()
    order_ids = {o["number"]: o["id"] for o in emp_orders if o["number"]}
    con = db.connect()
    try:
        u = con.execute("SELECT login FROM users WHERE employee_id=?", (emp_id,)).fetchone()
    finally:
        con.close()
    con = db.connect()
    try:
        disc = db.discipline_rows(con, emp_id)
    finally:
        con.close()
    return render_template("employee.html", disc=disc, DISC_MEASURES=DISC_MEASURES, e=emp, transfers=transfers, hire_order=hire_order,
                           emp_orders=emp_orders, order_ids=order_ids, has_login=u["login"] if u else "")


NAME_RE = re.compile(r"[A-Za-z][A-Za-z'’ʻʼ`\- ]*")

FIELDS = ["pinfl", "last_name", "first_name", "middle_name", "birth_date",
          "passport", "address", "position", "department", "hire_date"]


def validate_hire(form, staff):
    """Возвращает (данные, список ошибок). staff — штатное расписание."""
    d = {k: form.get(k, "").strip() for k in FIELDS}
    d["has_workbook"] = form.get("has_workbook") == "1"
    errors = []

    if not re.fullmatch(r"\d{14}", d["pinfl"]):
        errors.append("ПИНФЛ должен состоять ровно из 14 цифр.")
    if not d["last_name"]:
        errors.append("Укажите фамилию.")
    if not d["first_name"]:
        errors.append("Укажите имя.")
    # ФИО вводится латиницей: от первой буквы фамилии зависит номер папки,
    # а латинская A и кириллическая А — разные символы.
    for key, label in [("last_name", "Фамилия"), ("first_name", "Имя"), ("middle_name", "Отчество")]:
        if d[key] and not NAME_RE.fullmatch(d[key]):
            errors.append(f"{label}: вводите латиницей (буквы A–Z, можно ' и дефис).")
    for key, label in [("passport", "паспорт"), ("address", "адрес")]:
        if not d[key]:
            errors.append(f"Заполните поле «{label}».")
    if d["department"] not in staff:
        errors.append("Выберите подразделение из списка.")
    elif d["position"] not in staff[d["department"]]:
        errors.append("Выберите должность из списка.")

    birth = hire = None
    try:
        birth = date.fromisoformat(d["birth_date"])
    except ValueError:
        errors.append("Укажите дату рождения.")
    try:
        hire = date.fromisoformat(d["hire_date"])
    except ValueError:
        errors.append("Укажите дату приёма.")
    if birth and hire:
        if birth >= hire:
            errors.append("Дата рождения должна быть раньше даты приёма.")
        elif (hire.year - birth.year) < 16:
            errors.append("Сотруднику на дату приёма меньше 16 лет — проверьте даты.")
    return d, errors


# ---------- приказы ----------

DEFAULT_BASIS = "Xodimning arizasi va xodim bilan tuzilgan mehnat shartnomasi."


def _user_row(con):
    return con.execute("SELECT * FROM users WHERE login=?", (session["user"],)).fetchone()


def lan_ip():
    """Адрес этого компьютера в локальной сети (для QR-кода)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def base_url(settings):
    """Адрес, который зашивается в QR-код: его должны суметь открыть те, кто сканирует."""
    if settings["public_url"]:
        return settings["public_url"].rstrip("/")
    host, _, port = request.host.partition(":")
    if host in ("localhost", "127.0.0.1", "[::1]"):
        return f"http://{lan_ip()}:{port or 80}"
    return request.host_url.rstrip("/")


def _groups():
    con = db.connect()
    try:
        return con.execute("SELECT * FROM signer_groups ORDER BY name").fetchall()
    finally:
        con.close()


def _apply_group(con, order_id, form):
    """Сохранить участников приказа (список из формы) и выбранного подписанта.

    Если в форме есть список участников — он главный; иначе (старый способ) берётся группа.
    """
    if form.get("steps_present"):
        err = db.replace_steps(con, order_id, extras.parse_steps(form))
        if err:
            flash(err, "error")
        db.reset_steps(con, order_id)      # приказ изменился — прежние согласия не действуют
        con.execute("UPDATE orders SET group_name='' WHERE id=?", (order_id,))
    else:
        gid = form.get("group_id", type=int) if hasattr(form, "get") else None
        db.set_steps_from_group(con, order_id, gid)
    if "signer_login" in form:
        _set_signer(con, order_id, form.get("signer_login", ""))


def _set_signer(con, order_id, login_):
    login_ = (login_ or "").strip()
    if login_ and not con.execute("SELECT 1 FROM users WHERE login=? AND role IN ('director','admin')",
                                  (login_,)).fetchone():
        login_ = ""
    con.execute("UPDATE orders SET signer_login=? WHERE id=?", (login_, order_id))


def _step_users(con=None):
    """Пользователи, которых можно выбрать в приказ."""
    own = con is None
    con = con or db.connect()
    try:
        return con.execute("SELECT login, full_name, title, role FROM users ORDER BY full_name, login").fetchall()
    finally:
        if own:
            con.close()


def _groups_data():
    """Группы с составом — для кнопки «добавить группу» в форме."""
    con = db.connect()
    try:
        out = []
        for g in con.execute("SELECT * FROM signer_groups ORDER BY name"):
            m = con.execute("SELECT login, action FROM signer_group_members WHERE group_id=? ORDER BY ord, id",
                            (g["id"],)).fetchall()
            out.append({"id": g["id"], "name": g["name"], "members": [[x["login"], x["action"]] for x in m]})
        return out
    finally:
        con.close()


def _chain_context(order=None):
    """Данные для блока «Участники и подписант» в формах приказа."""
    con = db.connect()
    try:
        users = extras.people(con)
        rows = []
        signer = ""
        if order is not None:
            rows = [{"login": x["login"], "action": x["action"], "queue": x["ord"], "due": x["due"]}
                    for x in db.steps_for(con, order["id"])]
            signer = order["signer_login"]
    finally:
        con.close()
    return {"chain_users": users, "chain_rows": rows, "chain_signer": signer, "chain_groups": _groups_data()}


def _custom_text(form):
    """(текст, ошибка): свой текст приказа, если включена галочка."""
    if not form.get("use_custom"):
        return "", None
    text = "\n".join(line.rstrip() for line in form.get("custom_text", "").strip().splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not text:
        return "", "Вы включили свой текст приказа, но он пустой. Впишите текст или снимите галочку."
    if len(text) > 6000:
        return "", "Текст приказа слишком длинный (больше 6000 знаков)."
    return text, None


def default_text(kind, d, tabel=None):
    """Стандартный текст приказа (то же, что печатается автоматически) — для заготовки своего текста."""
    g = lambda k, dflt="________": d.get(k) or dflt
    fio = " ".join(x for x in (d.get("last_name"), d.get("first_name"), d.get("middle_name")) if x) or "________"
    place = f"{g('department')} {g('position')}"
    t = d.get("tabel_no") or tabel or "________"
    law = "O‘zbekiston Respublikasi Mehnat kodeksining"
    if kind == "dept_rename":
        return (f"1. «{d.get('department', '________')}» bo‘linmasining nomi {uzdate(d.get('effective_date')) or '________'}dan "
                f"«{d.get('new_name', '________')}» deb o‘zgartirilsin.\n"
                f"2. Kadrlar xizmati xodimlarning kadrlar hujjatlarida bo‘linma nomini tegishli ravishda o‘zgartirsin.")
    if kind == "hire":
        return (f"1. {fio} – {uzdate(d.get('hire_date')) or '________'}dan {place} lavozimiga ishga qabul qilinsin.\n"
                f"2. Xodimga {t} tabel raqami berilsin.")
    if kind == "transfer":
        return (f"1. {fio} (tabel raqami {t}) – {uzdate(d.get('transfer_date')) or '________'}dan {place} lavozimidan "
                f"{g('to_department')} {g('to_position')} lavozimiga o‘tkazilsin.\n"
                f"2. Huquqiy asos: {law} {g('article')}.")
    if kind == "dismiss":
        reason = f" ({d['reason']})" if d.get("reason") else ""
        return (f"1. {fio} (tabel raqami {t}) bilan tuzilgan mehnat shartnomasi {uzdate(d.get('dismissal_date')) or '________'}dan "
                f"{place} lavozimida bekor qilinsin.\n2. Huquqiy asos: {law} {g('article')}{reason}.")
    if kind == "discipline":
        mu = DISC_MEASURES.get(d.get("measure"), ("", "hayfsan"))[1]
        lines = [f"1. {fio} (tabel raqami {t}) – {place} – {uzdate(d.get('offense_date')) or '________'}da {g('offense')} "
                 f"holatiga yo‘l qo‘yganligi uchun {mu} e’lon qilinsin."]
        if d.get("explanation"):
            lines.append(f"{len(lines) + 1}. Xodimdan yozma tushuntirish xati olingan.")
        if d.get("article"):
            lines.append(f"{len(lines) + 1}. Huquqiy asos: {law} {d['article']}.")
        return "\n".join(lines)
    if kind == "discipline_lift":
        mu = DISC_MEASURES.get(d.get("measure"), ("", "hayfsan"))[1]
        lines = [f"1. {fio} (tabel raqami {t}) ga {uzdate(d.get('base_date')) or '________'} dagi {d.get('base_number') or '____'}-son "
                 f"buyruq bilan e’lon qilingan {mu} olib tashlansin."]
        if d.get("article"):
            lines.append(f"2. Huquqiy asos: {law} {d['article']}.")
        return "\n".join(lines)
    if kind in db.VAC_ADJ:
        vt = VAC_TYPES.get(d.get("vacation_type"), ("", "ta’til"))[1]
        src = f"{uzdate(d.get('base_date')) or '________'} dagi {d.get('base_number') or '____'}-son buyruq bilan"
        if kind == "vac_extend":
            lines = [f"1. {fio} (tabel raqami {t}) ga {src} {uzdate(d.get('start_date')) or '________'}dan "
                     f"{uzdate(d.get('base_end_date')) or '________'}gacha berilgan {vt} {uzdate(d.get('new_end_date')) or '________'}gacha, "
                     f"jami {d.get('days_added') or '__'} kalendar kunga uzaytirilsin."]
        else:
            lines = [f"1. {fio} (tabel raqami {t}) {src} berilgan {vt}dan {uzdate(d.get('recall_date')) or '________'}dan boshlab "
                     f"chaqirib olinsin.",
                     f"2. Ta’tilning foydalanilmagan {d.get('days_unused') or '__'} kalendar kuni xodimga keyinchalik beriladi."]
        if d.get("article"):
            lines.append(f"{len(lines) + 1}. Huquqiy asos: {law} {d['article']}.")
        return "\n".join(lines)
    if kind == "vacation":
        vt = VAC_TYPES.get(d.get("vacation_type"), ("", "ta’til"))[1]
        lines = [f"1. {fio} (tabel raqami {t}) ga {place} lavozimida {uzdate(d.get('start_date')) or '________'}dan "
                 f"{uzdate(d.get('end_date')) or '________'}gacha, jami {d.get('days') or '__'} kalendar kun muddatga {vt} berilsin."]
        n = 2
        if d.get("material_aid"):
            lines.append(f"{n}. Xodimga moddiy yordam to‘lansin.")
            n += 1
        if d.get("article"):
            lines.append(f"{n}. Huquqiy asos: {law} {d['article']}.")
        return "\n".join(lines)
    tk = TRIP_KINDS.get(d.get("trip_kind"), ("", "mahalliy"))[1]
    lines = [f"1. {fio} (tabel raqami {t}) – {place} – {uzdate(d.get('start_date')) or '________'}dan "
             f"{uzdate(d.get('end_date')) or '________'}gacha, jami {d.get('days') or '__'} kalendar kun muddatga "
             f"{g('destination')}ga ({tk} xizmat safari) yuborilsin.",
             f"2. Safar maqsadi: {g('purpose')}."]
    if d.get("article"):
        lines.append(f"3. Huquqiy asos: {law} {d['article']}.")
    return "\n".join(lines)


def _order_form(order):
    """Форма приказа о приёме: новый проект или правка существующего."""
    con = db.connect()
    try:
        staff = db.staff_map(con)
        if order is not None:
            form = json.loads(order["data"])
            form["basis"] = order["basis"]
            form["group_name"] = order["group_name"]
            form["use_custom"] = bool(form.get("custom_text"))
        else:
            form = {"hire_date": date.today().isoformat(), "basis": DEFAULT_BASIS}
        if request.method == "POST":
            d, errors = validate_hire(request.form, staff)
            basis = request.form.get("basis", "").strip()
            ctext, cerr = _custom_text(request.form)
            if cerr:
                errors.append(cerr)
            elif ctext:
                d["custom_text"] = ctext
            form = {**d, "basis": basis, "group_id": request.form.get("group_id", ""),
                    "use_custom": bool(request.form.get("use_custom")),
                    "custom_text": request.form.get("custom_text", "")}
            order_id = order["id"] if order is not None else 0
            if not errors:
                if con.execute("SELECT 1 FROM employees WHERE pinfl=? AND status=?",
                               (d["pinfl"], db.ACTIVE)).fetchone():
                    errors.append("Сотрудник с таким ПИНФЛ уже работает.")
                elif con.execute(
                        "SELECT 1 FROM orders WHERE kind='hire' AND status IN ('draft','returned') AND deleted_at IS NULL AND id<>? AND data LIKE ?",
                        (order_id, f'%"pinfl": "{d["pinfl"]}"%')).fetchone():
                    errors.append("На этого человека уже есть неподписанный приказ о приёме.")
            if not errors:
                data = json.dumps(d, ensure_ascii=False)
                if order is not None:
                    con.execute("UPDATE orders SET data=?, basis=?, status='draft', return_comment='' "
                                "WHERE id=? AND status IN ('draft','returned')", (data, basis, order["id"]))
                    new_id = order["id"]
                else:
                    me = _user_row(con)
                    cur = con.execute(
                        """INSERT INTO orders(kind, status, data, basis, created_by,
                                              created_by_name, created_by_title, created_at)
                           VALUES('hire','draft',?,?,?,?,?,?)""",
                        (data, basis, me["login"], me["full_name"] or me["login"], me["title"],
                         db.now_local().strftime("%Y-%m-%d %H:%M")))
                    new_id = cur.lastrowid
                _apply_group(con, new_id, request.form)
                flash("Проект приказа сохранён. Сотрудник появится в базе после подписи директора.", "ok")
                return redirect(url_for("order_view", order_id=new_id))
            for e in errors:
                flash(e, "error")
    finally:
        con.close()
    return render_template("hire.html", f=form, staff=staff, order=order, groups=_groups(), **_chain_context(order))


@app.route("/hire", methods=["GET", "POST"])
@roles_required("hr", "admin")
def hire():
    return _order_form(None)


KIND_RU = {"hire": "О приёме на работу", "transfer": "О переводе", "dismiss": "Об увольнении",
           "vacation": "Об отпуске", "trip": "О командировке",
           "vac_extend": "О продлении отпуска", "vac_recall": "Об отзыве из отпуска",
           "discipline": "О дисциплинарном взыскании", "discipline_lift": "О снятии дисциплинарного взыскания",
           "dept_rename": "О переименовании подразделения"}
CHANGE_KINDS = ("transfer", "dismiss", "vacation", "trip", "discipline")
DISC_MEASURES = {"remark": ("Замечание", "ogohlantirish"), "reprimand": ("Выговор", "hayfsan")}
app.jinja_env.globals.update(DISC_MEASURES=DISC_MEASURES, today_iso=lambda: date.today().isoformat())
OPEN = "status IN ('draft','returned') AND deleted_at IS NULL"   # проекты, которые ещё не подписаны


@app.context_processor
def inject_kinds():
    return {"KIND_RU": KIND_RU, "pending_count": pending_count()}


def pending_count():
    """Сколько приказов ждёт действия текущего пользователя (для значка в меню)."""
    if "user" not in session:
        return 0
    con = db.connect()
    try:
        n = len(_my_queue(con)) if session.get("role") != "employee" else 0
        return n
    finally:
        con.close()


@app.context_processor
def inject_app_count():
    if "user" not in session:
        return {"ap_count": 0}
    import appl
    con = db.connect()
    try:
        n = appl.pending(con, session["user"], session.get("role"))
        if session.get("role") != "employee":
            import gdoc
            n += gdoc.pending(con, session["user"])
        return {"ap_count": n}
    finally:
        con.close()


def _my_queue(con):
    """Приказы, ожидающие действия текущего пользователя: подпись, согласие или доработка."""
    login_, role = session["user"], session.get("role")
    ids = set()
    for r in con.execute(
            "SELECT DISTINCT o.id FROM orders o JOIN order_steps s ON s.order_id=o.id "
            "WHERE o.status='draft' AND o.deleted_at IS NULL AND s.login=? AND s.status='pending' AND s.action<>'info' "
            "AND NOT (s.action='rozilik' AND EXISTS (SELECT 1 FROM order_steps x WHERE x.order_id=s.order_id "
            "AND x.action='rozilik' AND x.status<>'approved' AND x.ord<s.ord))",
            (login_,)):
        ids.add(r["id"])
    if role in ("director", "admin"):
        for r in con.execute(
                "SELECT o.id FROM orders o WHERE o.status='draft' AND o.deleted_at IS NULL AND "
                "(o.signer_login='' OR o.signer_login=?) AND NOT EXISTS "
                "(SELECT 1 FROM order_steps s WHERE s.order_id=o.id AND s.action='rozilik' AND s.status<>'approved')",
                (login_,)):
            ids.add(r["id"])
    if role in ("hr", "admin"):
        for r in con.execute("SELECT id FROM orders WHERE status='returned' AND deleted_at IS NULL AND created_by=?",
                             (login_,)):
            ids.add(r["id"])
    return ids


def _parse_date(text):
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def validate_change(kind, form, staff, emp, existing_id=0, con=None):
    """Проверка данных приказа о переводе, увольнении, отпуске, командировке. Возвращает (данные, ошибки)."""
    g = lambda k: str(form.get(k, "") or "").strip()
    errors = []
    d = {
        "employee_id": emp["id"], "tabel_no": emp["tabel_no"],
        "last_name": emp["last_name"], "first_name": emp["first_name"],
        "middle_name": emp["middle_name"],
        "department": emp["department"], "position": emp["position"],
        "article": g("article"),
    }
    if emp["status"] != db.ACTIVE:
        errors.append("Этот сотрудник уже уволен.")
    if kind in ("transfer", "dismiss") and not d["article"]:
        errors.append("Укажите статью Трудового кодекса РУз.")
    last = None
    if con is not None:
        last = con.execute("SELECT MAX(transfer_date) FROM transfers WHERE employee_id=?",
                           (emp["id"],)).fetchone()[0]
    earliest = max(emp["hire_date"], last or "")
    if kind == "transfer":
        d["to_department"], d["to_position"] = g("to_department"), g("to_position")
        d["transfer_date"] = g("transfer_date")
        if d["to_department"] not in staff:
            errors.append("Выберите подразделение из списка.")
        elif d["to_position"] not in staff[d["to_department"]]:
            errors.append("Выберите должность из списка.")
        elif (d["to_department"], d["to_position"]) == (emp["department"], emp["position"]):
            errors.append("Сотрудник уже работает в этом подразделении на этой должности.")
        if _parse_date(d["transfer_date"]) is None:
            errors.append("Укажите дату перевода.")
        elif d["transfer_date"] < earliest:
            errors.append("Дата перевода не может быть раньше приёма или предыдущего перевода.")
    elif kind == "dismiss":
        d["dismissal_date"] = g("dismissal_date")
        d["reason"] = g("reason")
        if _parse_date(d["dismissal_date"]) is None:
            errors.append("Укажите дату увольнения.")
        elif d["dismissal_date"] < earliest:
            errors.append("Дата увольнения не может быть раньше приёма или последнего перевода.")
    elif kind == "discipline":
        d["measure"] = g("measure")
        d["offense_date"], d["offense"] = g("offense_date"), g("offense")
        d["explanation"] = bool(g("explanation"))
        if d["measure"] not in DISC_MEASURES:
            errors.append("Выберите вид взыскания.")
        if _parse_date(d["offense_date"]) is None:
            errors.append("Укажите дату проступка.")
        elif d["offense_date"] > date.today().isoformat():
            errors.append("Дата проступка не может быть в будущем.")
        elif d["offense_date"] < emp["hire_date"]:
            errors.append("Дата проступка не может быть раньше даты приёма на работу.")
        if len(d["offense"]) < 5:
            errors.append("Опишите проступок (что именно нарушено).")
    elif kind == "discipline_lift":
        base_id = int(g("base_order_id")) if g("base_order_id").isdigit() else 0
        d["base_order_id"] = base_id
        base = con.execute("SELECT * FROM orders WHERE id=? AND kind='discipline' AND status='signed' AND employee_id=?",
                           (base_id, emp["id"])).fetchone() if con is not None else None
        if base is None:
            errors.append("Приказ о взыскании не найден, не подписан или отменён.")
        else:
            bd = json.loads(base["data"])
            d.update(base_number=base["number"], base_date=base["order_date"], measure=bd.get("measure", ""),
                     offense_date=bd.get("offense_date", ""))
            err = db.check_discipline_lift(con, d, existing_id)
            if err:
                errors.append(err)
    elif kind in db.VAC_ADJ:   # продление или отзыв из отпуска
        base_id = int(g("base_order_id")) if g("base_order_id").isdigit() else 0
        d["base_order_id"] = base_id
        base = con.execute("SELECT * FROM orders WHERE id=? AND kind='vacation' AND status='signed' AND employee_id=?",
                           (base_id, emp["id"])).fetchone() if con is not None else None
        if base is None:
            errors.append("Исходный приказ об отпуске не найден, не подписан или отменён.")
        else:
            bd = db.effective(json.loads(base["data"]),
                              [a for a in db.adjustments(con).get(base_id, []) if a[0] != existing_id])
            d.update(base_number=base["number"], base_date=base["order_date"], vacation_type=bd.get("vacation_type", ""),
                     start_date=bd["start_date"], base_end_date=bd["end_date"], end_date=bd["end_date"], days=bd["days"])
            if kind == "vac_extend":
                d["new_end_date"] = g("new_end_date")
                if _parse_date(d["new_end_date"]) is None:
                    errors.append("Укажите новую дату окончания отпуска.")
                elif d["new_end_date"] > bd["end_date"]:
                    d["days_added"] = db._days_between(bd["end_date"], d["new_end_date"]) - 1
                    d["end_date"], d["days"] = d["new_end_date"], int(bd["days"]) + d["days_added"]
            else:
                d["recall_date"] = g("recall_date")
                if _parse_date(d["recall_date"]) is None:
                    errors.append("Укажите, с какой даты сотрудник отзывается из отпуска.")
                elif bd["start_date"] < d["recall_date"] <= bd["end_date"]:
                    d["days_unused"] = db._days_between(d["recall_date"], bd["end_date"])
                    d["end_date"], d["days"] = db._add_days(d["recall_date"], -1), int(bd["days"]) - d["days_unused"]
            if not errors:
                err = db.check_vac_adjust(con, kind, d, existing_id)
                if err:
                    errors.append(err)
    else:   # отпуск или командировка
        d["start_date"], d["end_date"] = g("start_date"), g("end_date")
        s_, e_ = _parse_date(d["start_date"]), _parse_date(d["end_date"])
        if s_ is None or e_ is None:
            errors.append("Укажите даты начала и окончания.")
        elif e_ < s_:
            errors.append("Дата окончания не может быть раньше даты начала.")
        else:
            d["days"] = (e_ - s_).days + 1
            if d["start_date"] < emp["hire_date"]:
                errors.append("Дата начала не может быть раньше даты приёма на работу.")
            elif con is not None:
                clash = db.absence_conflict(con, emp["id"], d["start_date"], d["end_date"], existing_id)
                if clash:
                    errors.append(f"Эти даты пересекаются с подписанным приказом № {clash}.")
        if kind == "vacation":
            d["vacation_type"] = g("vacation_type")
            d["material_aid"] = bool(g("material_aid"))
            if d["vacation_type"] not in VAC_TYPES:
                errors.append("Выберите вид отпуска.")
        else:
            d["trip_kind"] = g("trip_kind")
            d["destination"], d["purpose"] = g("destination"), g("purpose")
            if d["trip_kind"] not in TRIP_KINDS:
                errors.append("Выберите вид командировки.")
            if not d["destination"] or not d["purpose"]:
                errors.append("Укажите место командировки и её цель.")
    if con is not None and not errors and kind in ("transfer", "dismiss"):
        if con.execute(
                f"SELECT 1 FROM orders WHERE kind=? AND {OPEN} AND employee_id IS NULL "
                "AND id<>? AND data LIKE ?",
                (kind, existing_id, f'%"employee_id": {emp["id"]},%')).fetchone():
            errors.append("На этого сотрудника уже есть неподписанный приказ такого вида.")
    return d, errors


def leave_info(con, d, existing_id=0):
    """(использовано, положено) дней ежегодного отпуска в году начала отпуска — для подсказки."""
    year = d["start_date"][:4]
    used = db.leave_used(con, d["employee_id"], year, "annual", existing_id)
    return used, int(db.get_settings(con).get("annual_leave_days") or 21)


def _employee_options(con, q):
    rows = con.execute("SELECT * FROM employees WHERE status=? ORDER BY last_name, first_name, tabel_no",
                       (db.ACTIVE,)).fetchall()
    q = q.strip().lower()
    if q:
        rows = [r for r in rows if q in " ".join(
            [r["last_name"], r["first_name"], r["middle_name"], r["tabel_no"],
             r["pinfl"], r["position"], r["department"]]).lower()]
    return rows


@app.route("/order/new/<kind>")
@roles_required("hr", "admin")
def order_pick(kind):
    """Шаг 1: поиск и выбор сотрудника для приказа."""
    if kind not in CHANGE_KINDS:
        abort(404)
    q = request.args.get("q", "")
    con = db.connect()
    try:
        rows = _employee_options(con, q)
    finally:
        con.close()
    total = len(rows)
    tpl = "pick_rows.html" if request.args.get("frag") else "pick.html"
    return render_template(tpl, kind=kind, rows=rows[:50], total=total, q=q)


@app.route("/order/text-preview/<kind>", methods=["POST"])
@roles_required("hr", "admin")
def order_text_preview(kind):
    """Стандартный текст приказа по данным, которые сейчас введены в форме."""
    if kind != "hire" and kind not in CHANGE_KINDS and kind not in db.VAC_ADJ and kind != "discipline_lift":
        abort(404)
    con = db.connect()
    try:
        staff = db.staff_map(con)
        if kind == "hire":
            d, _ = validate_hire(request.form, staff)
        else:
            emp = con.execute("SELECT * FROM employees WHERE id=?", (request.form.get("employee_id", type=int),)).fetchone()
            if emp is None:
                abort(404)
            d, _ = validate_change(kind, request.form, staff, emp, 0, con)
    finally:
        con.close()
    return jsonify(text=default_text(kind, d))


def _change_form(kind, order, emp_id):
    con = db.connect()
    try:
        emp = con.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
        if emp is None:
            abort(404)
        staff = db.staff_map(con)
        if emp["status"] != db.ACTIVE:
            flash("Этот сотрудник уже уволен.", "error")
            return redirect(url_for("order_pick", kind=kind))
        if order is not None:
            form = json.loads(order["data"])
            form["basis"] = order["basis"]
            form["group_name"] = order["group_name"]
            form["use_custom"] = bool(form.get("custom_text"))
        else:
            today = date.today().isoformat()
            form = {"transfer_date": today, "dismissal_date": today, "start_date": today, "end_date": today,
                    "vacation_type": "annual", "trip_kind": "local", "measure": "remark", "offense_date": today,
                    "basis": "Xodimning arizasi." if kind != "trip" else ""}
        app_link = None
        if request.method == "POST":
            app_id_ = request.form.get("app_id", type=int)
        else:
            app_id_ = request.args.get("app", type=int) or (order["app_id"] if order is not None else None)
        if app_id_:
            arow = con.execute("SELECT * FROM applications WHERE id=? AND employee_id=? AND kind=? AND status IN ('approved','done')",
                               (app_id_, emp_id, kind)).fetchone()
            if arow is not None:
                app_link = {"id": arow["id"], "number": arow["number"], "type_name": arow["type_name"]}
                if order is None and request.method == "GET":
                    fv = json.loads(arow["fields"] or "{}")
                    for k in ("transfer_date", "to_department", "to_position", "dismissal_date", "start_date", "end_date",
                              "destination", "purpose"):
                        if fv.get(k):
                            form[k] = fv[k]
                    if kind == "vacation":
                        form["vacation_type"] = "unpaid" if "saqlanmaydigan" in arow["type_name"] else "annual"
        base_info = None
        if kind in db.VAC_ADJ:
            bid = (request.form.get("base_order_id", type=int) if request.method == "POST"
                   else (request.args.get("base", type=int) or form.get("base_order_id")))
            brow = con.execute("SELECT * FROM orders WHERE id=? AND kind='vacation' AND status='signed' AND employee_id=?",
                               (bid, emp_id)).fetchone() if bid else None
            if brow is None:
                flash("Продлить или отозвать можно только подписанный приказ об отпуске этого сотрудника.", "error")
                return redirect(url_for("orders"))
            form["base_order_id"] = brow["id"]
            base_info = db.effective(json.loads(brow["data"]), [a for a in db.adjustments(con).get(brow["id"], [])
                                                                if not order or a[0] != order["id"]])
            base_info.update(number=brow["number"], order_date=brow["order_date"], order_id=brow["id"])
        if kind == "discipline_lift":
            bid = (request.form.get("base_order_id", type=int) if request.method == "POST"
                   else (request.args.get("base", type=int) or form.get("base_order_id")))
            brow = con.execute("SELECT * FROM orders WHERE id=? AND kind='discipline' AND status='signed' AND employee_id=?",
                               (bid, emp_id)).fetchone() if bid else None
            if brow is None:
                flash("Снять можно только подписанное взыскание этого сотрудника.", "error")
                return redirect(url_for("employee", emp_id=emp_id))
            form["base_order_id"] = brow["id"]
            base_info = json.loads(brow["data"])
            base_info.update(number=brow["number"], order_date=brow["order_date"], order_id=brow["id"])
        if request.method == "POST":
            d, errors = validate_change(kind, request.form, staff, emp,
                                        order["id"] if order else 0, con)
            basis = request.form.get("basis", "").strip()
            ctext, cerr = _custom_text(request.form)
            if cerr:
                errors.append(cerr)
            elif ctext:
                d["custom_text"] = ctext
            form = {**d, "basis": basis, "group_id": request.form.get("group_id", ""),
                    "use_custom": bool(request.form.get("use_custom")),
                    "custom_text": request.form.get("custom_text", "")}
            if not errors:
                data = json.dumps(d, ensure_ascii=False)
                if order is not None:
                    con.execute("UPDATE orders SET data=?, basis=?, status='draft', return_comment='' "
                                "WHERE id=? AND status IN ('draft','returned')", (data, basis, order["id"]))
                    new_id = order["id"]
                else:
                    me = _user_row(con)
                    cur = con.execute(
                        """INSERT INTO orders(kind, status, data, basis, created_by,
                                              created_by_name, created_by_title, created_at, app_id)
                           VALUES(?,'draft',?,?,?,?,?,?,?)""",
                        (kind, data, basis, me["login"], me["full_name"] or me["login"], me["title"],
                         db.now_local().strftime("%Y-%m-%d %H:%M"), app_link["id"] if app_link else None))
                    new_id = cur.lastrowid
                _apply_group(con, new_id, request.form)
                flash("Проект приказа сохранён. Он вступит в силу после подписи директора.", "ok")
                if kind == "vacation" and d["vacation_type"] == "annual":
                    used, total_days = leave_info(con, d, new_id)
                    if used + d["days"] > total_days:
                        flash(f"Внимание: ежегодного отпуска в {d['start_date'][:4]} году использовано {used} из {total_days} "
                              f"дней, с этим приказом будет {used + d['days']}.", "error")
                return redirect(url_for("order_view", order_id=new_id))
            for e in errors:
                flash(e, "error")
        info = None
        if kind == "vacation":
            y = (form.get("start_date") or date.today().isoformat())[:4]
            info = (db.leave_used(con, emp_id, y, "annual", order["id"] if order else 0),
                    int(db.get_settings(con).get("annual_leave_days") or 21), y)
        reasons = refs.active_reasons(con) if kind == "dismiss" else []
    finally:
        con.close()
    return render_template("change.html", kind=kind, e=emp, f=form, staff=staff, order=order, reasons=reasons, base=base_info, app_link=app_link,
                           groups=_groups(), leave=info, **_chain_context(order))


@app.route("/order/new/<kind>/<int:emp_id>", methods=["GET", "POST"])
@roles_required("hr", "admin")
def order_new_change(kind, emp_id):
    if kind not in CHANGE_KINDS and kind not in db.VAC_ADJ and kind != "discipline_lift":
        abort(404)
    return _change_form(kind, None, emp_id)


def _get_order(con, order_id):
    order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if order is None:
        abort(404)
    return order


@app.route("/order/<int:order_id>/edit", methods=["GET", "POST"])
@roles_required("hr", "admin")
def order_edit(order_id):
    con = db.connect()
    try:
        order = _get_order(con, order_id)
    finally:
        con.close()
    if order["status"] not in ("draft", "returned") or order["deleted_at"]:
        flash("Подписанный приказ изменить нельзя.", "error")
        return redirect(url_for("order_view", order_id=order_id))
    if order["kind"] == "dept_rename":
        return redirect(url_for("dept_rename_edit", order_id=order_id))
    if order["kind"] in CHANGE_KINDS or order["kind"] in db.VAC_ADJ or order["kind"] == "discipline_lift":
        return _change_form(order["kind"], order, json.loads(order["data"])["employee_id"])
    return _order_form(order)


@app.route("/order/<int:order_id>/delete", methods=["POST"])
@roles_required("hr", "admin")
def order_delete(order_id):
    """Проект уходит в корзину (не удаляется насовсем): его можно восстановить."""
    con = db.connect()
    try:
        _get_order(con, order_id)
        if con.execute("UPDATE orders SET deleted_at=? WHERE id=? AND status IN ('draft','returned') "
                       "AND deleted_at IS NULL", (db.now_local().strftime("%Y-%m-%d %H:%M"), order_id)).rowcount:
            flash("Проект перенесён в корзину. Его можно восстановить в разделе «Корзина».", "ok")
        else:
            flash("Подписанный приказ удалить нельзя.", "error")
    finally:
        con.close()
    return redirect(url_for("orders"))


@app.route("/order/<int:order_id>/restore", methods=["POST"])
@roles_required("hr", "admin")
def order_restore(order_id):
    con = db.connect()
    try:
        _get_order(con, order_id)
        con.execute("UPDATE orders SET deleted_at=NULL WHERE id=?", (order_id,))
        flash("Проект восстановлен.", "ok")
    finally:
        con.close()
    return redirect(url_for("order_view", order_id=order_id))


@app.route("/orders/trash")
@roles_required("hr", "admin")
def orders_trash():
    con = db.connect()
    try:
        rows = con.execute("SELECT * FROM orders WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC").fetchall()
    finally:
        con.close()
    return render_template("orders_trash.html", items=_order_items(rows))


def _order_items(rows):
    items = []
    for o in rows:
        d = json.loads(o["data"])
        items.append({"o": o, "d": d, "tabel": d.get("tabel_no", ""),
                      "fio": " ".join(x for x in (d["last_name"], d["first_name"], d.get("middle_name", "")) if x)})
    return items


def _tabel_of(con, o):
    if not o["employee_id"]:
        return None
    r = con.execute("SELECT tabel_no FROM employees WHERE id=?", (o["employee_id"],)).fetchone()
    return r["tabel_no"] if r else None


def _waiting_on(con, o):
    """У кого приказ сейчас на подписи/согласовании: ФИО согласующих, а когда все согласовали — подписанта."""
    if o["status"] != "draft":
        return ""
    steps = [s for s in db.steps_for(con, o["id"]) if s["status"] == "pending" and s["action"] in ("rozilik", "ijro")]
    names = [s["name"] or s["login"] for s in steps]
    if names:
        return ", ".join(names[:2]) + (f" +{len(names) - 2}" if len(names) > 2 else "")
    if o["signer_login"]:
        u = con.execute("SELECT full_name FROM users WHERE login=?", (o["signer_login"],)).fetchone()
        return (u["full_name"] if u and u["full_name"] else o["signer_login"])
    return "директор"


@app.route("/orders")
@login_required
def orders():
    q = request.args.get("q", "").strip().lower()
    num = request.args.get("num", "").strip().lower()
    by = request.args.get("by", "").strip().lower()
    signer = request.args.get("signer", "").strip().lower()
    kind = request.args.get("kind", "")
    status = request.args.get("status", "")
    department = request.args.get("department", "")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    if request.args.get("date"):
        date_from = date_to = request.args["date"]
    con = db.connect()
    try:
        rows = con.execute("SELECT * FROM orders WHERE deleted_at IS NULL ORDER BY id DESC").fetchall()
        mine = _my_queue(con)
        deps = [r[0] for r in con.execute("SELECT name FROM departments ORDER BY name")]
    finally:
        con.close()
    items = _order_items(rows)
    con = db.connect()
    try:
        items = [it for it in items if _order_visible(con, it["o"])]
    finally:
        con.close()
    tcon = db.connect()
    try:
        for it in items:
            it["mine"] = it["o"]["id"] in mine
            it["waiting"] = _waiting_on(tcon, it["o"])
            try:
                txt = it["d"].get("custom_text") or default_text(it["o"]["kind"], it["d"], it["tabel"] or _tabel_of(tcon, it["o"]))
            except Exception:                                                  # noqa: BLE001
                txt = ""
            it["text"] = txt
    finally:
        tcon.close()
    if num:
        items = [it for it in items if num in (it["o"]["number"] or "").lower()]
    if by:
        items = [it for it in items if by in (it["o"]["created_by_name"] or "").lower()]
    if signer:
        items = [it for it in items if signer in (it["o"]["signed_by_name"] or "").lower()]
    if q:
        items = [it for it in items if all(w in " ".join(
            [it["fio"], it["o"]["number"] or "", it["tabel"], it["o"]["created_by_name"], it["text"]]).lower() for w in q.split())]
    if kind:
        items = [it for it in items if it["o"]["kind"] == kind]
    if status == "mine":
        items = [it for it in items if it["mine"]]
    elif status:
        items = [it for it in items if it["o"]["status"] == status]
    if department:
        items = [it for it in items if it["d"].get("department") == department
                 or it["d"].get("to_department") == department]
    if date_from:
        items = [it for it in items if (it["o"]["order_date"] or it["o"]["created_at"][:10]) >= date_from]
    if date_to:
        items = [it for it in items if (it["o"]["order_date"] or it["o"]["created_at"][:10]) <= date_to]
    return render_template("orders.html", items=items, q=request.args.get("q", ""), kind=kind,
                           status=status, mine_count=len(mine), department=department, deps=deps,
                           date_from=date_from, date_to=date_to,
                           filtered=any([q, num, by, signer, kind, status, department, date_from, date_to]), num=request.args.get("num", ""),
                           by=request.args.get("by", ""), signer=request.args.get("signer", ""))


def signing_problems(me, settings):
    problems = []
    if not settings["org_name"].strip():
        problems.append("Укажите название организации в разделе «Настройки».")
    if me["full_name"].strip() in ("", "Администратор"):
        problems.append("Укажите своё ФИО в разделе «Пользователи»: оно печатается рядом с подписью.")
    if not me["title"].strip():
        problems.append("Укажите свою должность в разделе «Пользователи»: она печатается рядом с подписью.")
    return problems


def _order_visible(con, o, steps=None):
    """Кто видит приказ. Автор, кадровики и администратор — всегда; подписанные приказы — все сотрудники кадровой службы;
    проект — только тот, до кого дошла очередь (согласующие по очереди, затем подписант)."""
    me, role = session["user"], session.get("role")
    if role in ("admin", "hr") or o["created_by"] == me or o["status"] in ("signed", "cancelled"):
        return True
    steps = db.steps_for(con, o["id"]) if steps is None else steps
    roz = [x for x in steps if x["action"] == "rozilik"]
    pend = [x["ord"] for x in roz if x["status"] != "approved"]
    for x in roz:
        if x["login"] == me and (x["status"] == "approved" or x["ord"] <= (min(pend) if pend else x["ord"])):
            return True
    if role == "director" and not pend and o["signer_login"] in ("", me):
        return True
    return False


def _order_flow(con, o, steps):
    """Данные для области «Ход документа» приказа: у кого сейчас, кто дальше, что уже произошло."""
    now, nxt, ev = [], [], []

    def row(st, action):
        return {"who": st["name"], "title": st["title"], "action": action, "due": st["due"]}
    roz = [x for x in steps if x["action"] == "rozilik"]
    pend = [x for x in roz if x["status"] != "approved"]
    first = min((x["ord"] for x in pend), default=None)
    if o["status"] == "draft" and not o["deleted_at"]:
        for x in pend:
            (now if x["ord"] == first else nxt).append(row(x, "Согласие"))
        sg = None
        if o["signer_login"]:
            u = con.execute("SELECT full_name, title FROM users WHERE login=?", (o["signer_login"],)).fetchone()
            sg = {"who": (u["full_name"] if u and u["full_name"] else o["signer_login"]), "title": u["title"] if u else "",
                  "action": "Подпись", "due": ""}
        else:
            sg = {"who": "Любой директор", "title": "", "action": "Подпись", "due": ""}
        (nxt if pend else now).append(sg)
    elif o["status"] == "returned":
        now.append({"who": o["created_by_name"], "title": o["created_by_title"], "action": "Доработка и повторная отправка", "due": ""})
    elif o["status"] == "signed":
        for x in steps:
            if x["action"] == "ijro":
                now.append(row(x, "На исполнении"))
        for x in steps:
            if x["action"] == "info":
                nxt.append(row(x, "К сведению"))
    ev.append({"at": o["created_at"], "who": o["created_by_name"], "what": "Составил проект приказа", "comment": ""})
    for x in roz:
        if x["status"] == "approved" and x["at"]:
            ev.append({"at": x["at"], "who": x["name"], "what": "Согласовал", "comment": x["comment"] or ""})
    if o["signed_at"]:
        ev.append({"at": o["signed_at"], "who": o["signed_by_name"] or "", "what": f"Подписал. Номер {o['number']}", "comment": ""})
    if o["cancelled_at"]:
        ev.append({"at": o["cancelled_at"], "who": o["cancelled_by"] or "", "what": "Отменил приказ", "comment": o["cancel_reason"] or ""})
    ev += extras.log_events(con, "order", o["id"])
    ev.sort(key=lambda e: e["at"])
    return {"flow_now": now, "flow_next": nxt, "flow_events": ev, "flow_today": db.now_local().strftime("%Y-%m-%d")}


@app.route("/order/<int:order_id>")
@login_required
def order_view(order_id):
    con = db.connect()
    try:
        order = _get_order(con, order_id)
        if not _order_visible(con, order):
            abort(403)
        settings = db.get_settings(con)
        app_row = con.execute("SELECT id, number, type_name FROM applications WHERE id=?", (order["app_id"],)).fetchone() \
            if order["app_id"] else None
        data = json.loads(order["data"])
        emp = None
        emp_ref = order["employee_id"] or data.get("employee_id")
        if emp_ref:
            emp = con.execute("SELECT * FROM employees WHERE id=?", (emp_ref,)).fetchone()
        me = _user_row(con)
        steps = db.steps_for(con, order_id)
        flow = _order_flow(con, order, steps)
        files = con.execute("SELECT * FROM order_files WHERE order_id=? ORDER BY id", (order_id,)).fetchall()
        links = con.execute(
            "SELECT o.id, o.number, o.kind, o.status FROM order_links l JOIN orders o ON o.id=l.linked_order_id "
            "WHERE l.order_id=? UNION SELECT o.id, o.number, o.kind, o.status FROM order_links l "
            "JOIN orders o ON o.id=l.order_id WHERE l.linked_order_id=?", (order_id, order_id)).fetchall()
        link_choices = con.execute(
            "SELECT id, number, kind, status FROM orders WHERE id<>? AND deleted_at IS NULL ORDER BY id DESC LIMIT 60",
            (order_id,)).fetchall()
        vac_hist, eff, base_order = [], None, None
        if order["kind"] == "vacation" and order["status"] == "signed":
            adjs = db.adjustments(con).get(order_id, [])
            vac_hist = [{"id": a[0], "kind": a[1], "number": a[2], "d": a[3]} for a in adjs]
            eff = db.effective(data, adjs)
        if order["kind"] in db.VAC_ADJ or order["kind"] == "discipline_lift":
            base_order = con.execute("SELECT id, number FROM orders WHERE id=?", (data.get("base_order_id"),)).fetchone()
        disc = None
        if order["kind"] == "discipline" and order["status"] == "signed" and order["employee_id"]:
            disc = next((x for x in db.discipline_rows(con, order["employee_id"]) if x["id"] == order_id), None)
    finally:
        con.close()
    chain = _chain_context(order)
    signed = order["status"] == "signed"
    verify_url = None
    if signed or order["status"] == "cancelled":
        verify_url = f"{base_url(settings)}/verify/{order['token']}"
    role = session.get("role")
    draft = order["status"] == "draft" and not order["deleted_at"]
    pending_steps = [x for x in steps if x["action"] == "rozilik" and x["status"] != "approved"]
    my_step = any(x["login"] == session["user"] and x["status"] == "pending" and x["action"] != "info" for x in steps)
    can_sign = draft and role in ("director", "admin") and (not order["signer_login"] or order["signer_login"] == session["user"])
    signer_user = None
    if order["signer_login"]:
        signer_user = next((u for u in chain["chain_users"] if u["login"] == order["signer_login"]), None)
    editable = order["status"] in ("draft", "returned") and not order["deleted_at"]
    vacancy_warn = []
    if order["status"] in ("draft", "returned") and not order["deleted_at"] and order["kind"] in ("hire", "transfer"):
        vcon = db.connect()
        try:
            problem = db.vacancy_problem(vcon, order["kind"], data)
            if problem:
                vacancy_warn.append(problem)
        finally:
            vcon.close()
    vacancy_block = bool(vacancy_warn) and settings.get("strict_vacancy", "1") == "1"
    return render_template(
        "order.html", app_row=app_row, vacancy_warn=vacancy_warn, vacancy_block=vacancy_block, o=order, d=data, emp=emp, s=settings, verify_url=verify_url, steps=steps,
        files=files, links=links, link_choices=link_choices, vac_hist=vac_hist, eff=eff, base_order=base_order,
        can_adjust=order["kind"] == "vacation" and order["status"] == "signed" and session.get("role") in ("hr", "admin")
        and bool(emp and emp["status"] == db.ACTIVE),
        can_sign=can_sign, waiting=bool(pending_steps), can_approve=draft and my_step,
        can_return=draft and (can_sign or my_step),
        can_edit=editable and role in ("hr", "admin"),
        can_cancel=signed and role in ("hr", "admin", "director") and order["kind"] in db.ABSENCE_KINDS + db.VAC_ADJ + db.DISCIPLINE,
        disc=disc, can_lift=order["kind"] == "discipline" and order["status"] == "signed" and role in ("hr", "admin")
        and bool(disc and disc["state"] != "lifted" and emp and emp["status"] == db.ACTIVE),
        can_copy=role in ("hr", "admin") and not order["deleted_at"], **flow,
        can_upload=role in ("hr", "admin") and not order["deleted_at"],
        in_trash=bool(order["deleted_at"]), signer_user=signer_user,
        problems=signing_problems(me, settings) if can_sign else [], **chain)


def _close_application(con, order_id):
    """Приказ подписан: заявление, по которому он составлен, становится «исполнено»."""
    o = con.execute("SELECT number, order_date, app_id FROM orders WHERE id=?", (order_id,)).fetchone()
    if not o or not o["app_id"]:
        return
    a = con.execute("SELECT status FROM applications WHERE id=?", (o["app_id"],)).fetchone()
    if a and a["status"] == "approved":
        stamp = db.now_local().strftime("%Y-%m-%d %H:%M")
        note = f"приказ № {o['number']} от {o['order_date']}"
        con.execute("UPDATE applications SET status='done', result_note=?, finished_at=? WHERE id=?", (note, stamp, o["app_id"]))
        con.execute("INSERT INTO app_log(app_id, at, who, text) VALUES(?,?,?,?)",
                    (o["app_id"], stamp, "система", "Исполнено: подписан " + note))


@app.route("/order/<int:order_id>/sign", methods=["POST"])
@roles_required("director", "admin")
def order_sign(order_id):
    con = db.connect()
    try:
        order = _get_order(con, order_id)
        if order["status"] != "draft" or order["deleted_at"]:
            flash("Приказ уже подписан или недоступен для подписи.", "error")
            return redirect(url_for("order_view", order_id=order_id))
        if order["signer_login"] and order["signer_login"] != session["user"]:
            flash("Для этого приказа назначен другой подписант.", "error")
            return redirect(url_for("order_view", order_id=order_id))
        me = _user_row(con)
        problems = signing_problems(me, db.get_settings(con))
        if problems:
            for p in problems:
                flash(p, "error")
            return redirect(url_for("order_view", order_id=order_id))
        # данные приказа проверяем ещё раз: штатное расписание или сотрудник могли измениться
        d = json.loads(order["data"])
        if order["kind"] == "hire":
            form = {k: str(d.get(k, "")) for k in FIELDS}
            form["has_workbook"] = "1" if d.get("has_workbook") else ""
            _, errors = validate_hire(form, db.staff_map(con))
        elif order["kind"] == "dept_rename":
            errors = deptren.validate(con, d, order["id"])
        else:
            emp = con.execute("SELECT * FROM employees WHERE id=?", (d["employee_id"],)).fetchone()
            _, errors = validate_change(order["kind"], d, db.staff_map(con), emp, order["id"], con)
        if errors:
            for e in errors:
                flash(e, "error")
            flash("Верните приказ кадровику на исправление.", "error")
            return redirect(url_for("order_view", order_id=order_id))
        _, err = db.sign_order(
            con, order_id, {"login": me["login"], "full_name": me["full_name"].strip(), "title": me["title"].strip()})
        if err:
            flash(err, "error")
        else:
            _close_application(con, order_id)
            flash({"hire": "Приказ подписан. Сотрудник принят, номера присвоены.",
                   "transfer": "Приказ подписан. Перевод оформлен, табельный номер и папка не изменились.",
                   "dismiss": "Приказ подписан. Сотрудник уволен, номер папки освобождён.",
                   "vacation": "Приказ подписан. Отпуск оформлен и попал в табель.",
                   "trip": "Приказ подписан. Командировка оформлена и попала в табель.",
                   "discipline": "Приказ подписан. Взыскание записано в карточку сотрудника.",
                   "discipline_lift": "Приказ подписан. Взыскание снято.",
                   "dept_rename": "Приказ подписан. Название подразделения изменено в штатном расписании и у сотрудников.",
                   "vac_extend": "Приказ подписан. Отпуск продлён, табель и остаток дней обновлены.",
                   "vac_recall": "Приказ подписан. Сотрудник отозван из отпуска, табель и остаток дней обновлены."}[order["kind"]], "ok")
    finally:
        con.close()
    return redirect(url_for("order_view", order_id=order_id))


@app.route("/order/<int:order_id>/approve", methods=["POST"])
@login_required
def order_approve(order_id):
    con = db.connect()
    try:
        _get_order(con, order_id)
        err = db.approve_step(con, order_id, session["user"])
        if not err:
            extras.log_event(con, "order", order_id, "Согласовал", request.form.get("comment", "").strip()[:1000])
        flash(err or "Ваше согласие записано.", "error" if err else "ok")
    finally:
        con.close()
    return redirect(url_for("order_view", order_id=order_id))


@app.route("/order/<int:order_id>/return", methods=["POST"])
@login_required
def order_return(order_id):
    comment = request.form.get("comment", "").strip()
    con = db.connect()
    try:
        order = _get_order(con, order_id)
        mine = con.execute("SELECT 1 FROM order_steps WHERE order_id=? AND login=? AND action<>'info'",
                           (order_id, session["user"])).fetchone()
        if session.get("role") not in ("director", "admin") and not mine:
            abort(403)
        if not comment:
            flash("Напишите, что нужно исправить.", "error")
        else:
            err = db.return_order(con, order_id, session["user"], comment)
            if not err:
                extras.log_event(con, "order", order_id, "Вернул на доработку", comment)
            flash(err or "Приказ возвращён кадровику на доработку.", "error" if err else "ok")
    finally:
        con.close()
    return redirect(url_for("order_view", order_id=order_id))


@app.route("/order/<int:order_id>/copy", methods=["POST"])
@roles_required("hr", "admin")
def order_copy(order_id):
    """Новый проект приказа с теми же данными и участниками (без номера, подписей и сроков)."""
    con = db.connect()
    try:
        o = _get_order(con, order_id)
        d = json.loads(o["data"])
        if o["kind"] in CHANGE_KINDS and con.execute(
                "SELECT 1 FROM orders WHERE kind=? AND status IN ('draft','returned') AND deleted_at IS NULL AND data LIKE ?",
                (o["kind"], f'%"employee_id": {d.get("employee_id")},%')).fetchone() and d.get("employee_id"):
            flash("На этого сотрудника уже есть неподписанный приказ такого вида. Закончите или удалите его, потом копируйте.", "error")
            return redirect(url_for("order_view", order_id=order_id))
        me = _user_row(con)
        con.execute("BEGIN IMMEDIATE")
        try:
            cur = con.execute(
                """INSERT INTO orders(kind, status, data, basis, created_by, created_by_name, created_by_title, created_at, signer_login)
                   VALUES(?, 'draft', ?, ?, ?, ?, ?, ?, ?)""",
                (o["kind"], o["data"], o["basis"], me["login"], me["full_name"] or me["login"], me["title"],
                 db.now_local().strftime("%Y-%m-%d %H:%M"), o["signer_login"]))
            new_id = cur.lastrowid
            for st in db.steps_for(con, order_id):
                con.execute("INSERT INTO order_steps(order_id, ord, login, name, title, action, due) VALUES(?,?,?,?,?,?,?)",
                            (new_id, st["ord"], st["login"], st["name"], st["title"], st["action"], ""))
            extras.log_event(con, "order", new_id, f"Создан копированием из приказа {o['number'] or 'проект №' + str(o['id'])}")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    flash("Копия создана как проект приказа. Измените данные (сотрудника, даты, текст) и сохраните.", "ok")
    return redirect(url_for("order_edit", order_id=new_id))


@app.route("/order/<int:order_id>/cancel", methods=["POST"])
@roles_required("hr", "admin", "director")
def order_cancel(order_id):
    """Отмена подписанного приказа об отпуске или командировке (на кадровые данные они не влияют)."""
    reason = request.form.get("reason", "").strip()
    con = db.connect()
    try:
        order = _get_order(con, order_id)
        if order["status"] != "signed" or order["kind"] not in db.ABSENCE_KINDS + db.VAC_ADJ + db.DISCIPLINE:
            flash("Отменить можно только подписанный приказ об отпуске, командировке, продлении, отзыве или о взыскании. "
                  "Приём, перевод и увольнение исправляются новым приказом.", "error")
        elif not reason:
            flash("Укажите причину отмены.", "error")
        else:
            con.execute("UPDATE orders SET status='cancelled', cancelled_at=?, cancelled_by=?, cancel_reason=? WHERE id=?",
                        (db.now_local().strftime("%Y-%m-%d %H:%M"), session["user"], reason, order_id))
            flash("Приказ отменён. Он больше не учитывается в табеле.", "ok")
    finally:
        con.close()
    return redirect(url_for("order_view", order_id=order_id))


@app.route("/verify/<token>")
def verify(token):
    """Страница, на которую ведёт QR-код. Открыта без входа, личных данных не показывает."""
    con = db.connect()
    try:
        order = con.execute("SELECT * FROM orders WHERE token=? AND status IN ('signed','cancelled')",
                            (token,)).fetchone()
        tabel = None
        steps = []
        if order and order["employee_id"]:
            row = con.execute("SELECT tabel_no FROM employees WHERE id=?", (order["employee_id"],)).fetchone()
            tabel = row["tabel_no"] if row else None
        if order:
            steps = db.steps_for(con, order["id"])
    finally:
        con.close()
    if order is None:
        return render_template("verify.html", o=None, intact=False), 404
    intact = db.order_hash(order, tabel, steps) == order["content_hash"]
    return render_template("verify.html", o=order, intact=intact)


@app.route("/settings", methods=["GET", "POST"])
@roles_required("admin")
def settings():
    con = db.connect()
    try:
        if request.method == "POST":
            org_name = " ".join(request.form.get("org_name", "").split())
            org_form = " ".join(request.form.get("org_form", "").split())
            public_url = request.form.get("public_url", "").strip()
            fmt = request.form.get("order_number_format", "").strip()
            fmts = {"order_number_format": fmt}
            for k in db.ORDER_SETTING.values():
                if k != "order_number_format":
                    fmts[k] = request.form.get(k, "").strip() or db.SETTING_DEFAULTS[k]
            other = {}
            leave = request.form.get("annual_leave_days", "").strip() or db.SETTING_DEFAULTS["annual_leave_days"]
            week = request.form.get("work_days_per_week", "5")
            extra = [x.strip() for x in request.form.get("extra_holidays", "").replace("\n", ",").split(",") if x.strip()]
            errors = []
            if not org_name:
                errors.append("Укажите название организации.")
            if public_url and not re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", public_url):
                errors.append("Адрес для QR-кода должен начинаться с http:// или https://.")
            if not (leave.isdigit() and 1 <= int(leave) <= 365):
                errors.append("Ежегодный отпуск: число дней от 1 до 365.")
            if week not in ("5", "6"):
                errors.append("Рабочая неделя: 5 или 6 дней.")
            for x in extra:
                try:
                    date.fromisoformat(x)
                except ValueError:
                    errors.append(f"Нерабочий день «{x}»: нужен формат ГГГГ-ММ-ДД.")
                    break
            other = {"annual_leave_days": leave, "work_days_per_week": week, "extra_holidays": ",".join(sorted(set(extra))),
                     "strict_vacancy": "1" if request.form.get("strict_vacancy") else "0"}
            if not all(db.valid_number_format(v) for v in fmts.values()):
                errors.append("Формат номера приказа неверный. Допустимо, например: 01/{seq:04d}-SHT")
            if errors:
                for e in errors:
                    flash(e, "error")
            else:
                for k, v in (("org_name", org_name), ("org_form", org_form),
                             ("public_url", public_url), *fmts.items(), *other.items()):
                    db.set_setting(con, k, v)
                flash("Настройки сохранены.", "ok")
                return redirect(url_for("settings"))
        s = db.get_settings(con)
        if request.method == "POST":
            s.update(strict_vacancy="1" if request.form.get("strict_vacancy") else "0", org_name=request.form.get("org_name", ""), org_form=request.form.get("org_form", ""),
                     public_url=request.form.get("public_url", ""),
                     order_number_format=request.form.get("order_number_format", ""),
                     **{k: v for k, v in fmts.items() if k != "order_number_format"},
                     annual_leave_days=leave, work_days_per_week=week, extra_holidays=",".join(extra))
    finally:
        con.close()
    yy = f"{db.now_local().year % 100:02d}"
    example = {k: (s[k].format(seq=1, yy=yy) if db.valid_number_format(s[k]) else "")
               for k in db.ORDER_SETTING.values()}
    return render_template("settings.html", s=s, example=example, auto_url=base_url(s))


# ---------- штатное расписание ----------

@app.route("/staff", methods=["GET", "POST"])
@admin_required
def staff():
    con = db.connect()
    try:
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add_department":
                name = " ".join(request.form.get("name", "").split())
                code = " ".join(request.form.get("code", "").split())[:20]
                parent = request.form.get("parent_id", type=int)
                if parent and not con.execute("SELECT 1 FROM departments WHERE id=?", (parent,)).fetchone():
                    parent = None
                if not name:
                    flash("Укажите название подразделения.", "error")
                else:
                    try:
                        con.execute("INSERT INTO departments(name, code, parent_id) VALUES(?,?,?)", (name, code, parent))
                        flash(f"Подразделение «{name}» добавлено.", "ok")
                    except sqlite3.IntegrityError:
                        flash("Такое подразделение уже есть.", "error")
            elif action == "add_positions":
                dep_id = request.form.get("department_id", type=int)
                names = [" ".join(x.split()) for x in request.form.get("names", "").splitlines()]
                names = [n for n in names if n]
                if not names or con.execute("SELECT 1 FROM departments WHERE id=?", (dep_id,)).fetchone() is None:
                    flash("Выберите подразделение и впишите хотя бы одну должность.", "error")
                else:
                    added = 0
                    for n in names:
                        added += con.execute(
                            "INSERT OR IGNORE INTO positions(department_id, name) VALUES(?,?)",
                            (dep_id, n)).rowcount
                    flash(f"Добавлено должностей: {added}.", "ok")
            elif action == "update_position":
                pid = request.form.get("id", type=int)
                try:
                    rate = float(request.form.get("rate", "1").replace(",", "."))
                except ValueError:
                    rate = -1
                if not (0 < rate <= 1000):
                    flash("Количество ставок: число больше нуля, например 1 или 0,5.", "error")
                else:
                    con.execute("UPDATE positions SET rate=?, grade=?, category=? WHERE id=?",
                                (rate, request.form.get("grade", "").strip()[:40],
                                 request.form.get("category", "").strip()[:60], pid))
                    flash("Данные должности сохранены.", "ok")
            elif action == "delete_position":
                con.execute("DELETE FROM positions WHERE id=?", (request.form.get("id", type=int),))
                flash("Должность удалена из списка.", "ok")
            elif action == "update_department":
                did = request.form.get("id", type=int)
                code = " ".join(request.form.get("code", "").split())[:20]
                parent = request.form.get("parent_id", type=int) or None
                tree = {t["id"]: t for t in db.dept_tree(con)}
                if did not in tree:
                    flash("Подразделение не найдено.", "error")
                elif parent and (parent not in tree or parent in tree[did]["ids"]):
                    flash("Нельзя сделать подразделение вложенным в само себя или в своё же вложенное.", "error")
                else:
                    con.execute("UPDATE departments SET code=?, parent_id=? WHERE id=?", (code, parent, did))
                    flash("Код и место подразделения в структуре сохранены.", "ok")
            elif action == "delete_department":
                con.execute("UPDATE departments SET parent_id=NULL WHERE parent_id=?", (request.form.get("id", type=int),))
                con.execute("DELETE FROM departments WHERE id=?", (request.form.get("id", type=int),))
                flash("Подразделение удалено из списка вместе с должностями.", "ok")
            return redirect(url_for("staff"))
        fq = request.args.get("q", "").strip().lower()
        fdep = request.args.get("department", "")
        only_free = request.args.get("only_free") == "1"
        fcat = request.args.get("category", "")
        deps = []
        all_deps = []
        tree = db.dept_tree(con)
        totals = {}
        for t in tree:
            all_deps.append({"id": t["id"], "name": t["name"], "code": t["code"], "level": t["level"]})
            if fdep and t["name"] != fdep:
                continue
            plist = []
            for p in con.execute("SELECT * FROM positions WHERE department_id=? ORDER BY name", (t["id"],)):
                busy = con.execute("SELECT COUNT(*) FROM employees WHERE status=? AND department=? AND position=?",
                                   (db.ACTIVE, t["name"], p["name"])).fetchone()[0]
                item = {"p": p, "busy": busy, "free": round(p["rate"] - busy, 2)}
                if fq and fq not in p["name"].lower() and fq not in t["name"].lower() and fq not in (t["code"] or "").lower():
                    continue
                if only_free and item["free"] <= 0:
                    continue
                if fcat and p["category"] != fcat:
                    continue
                plist.append(item)
            filtered = bool(fq or only_free or fcat)
            if filtered and not plist:
                continue
            deps.append({"id": t["id"], "name": t["name"], "code": t["code"], "level": t["level"],
                         "parent_id": t["parent_id"], "ids": t["ids"], "positions": plist})
        # итоги по подразделению вместе со вложенными
        own = {}
        for t in tree:
            r = con.execute("SELECT COALESCE(SUM(rate),0) FROM positions WHERE department_id=?", (t["id"],)).fetchone()[0]
            b = con.execute("SELECT COUNT(*) FROM employees e JOIN positions p ON p.name=e.position JOIN departments d "
                            "ON d.id=p.department_id AND d.name=e.department WHERE e.status=? AND d.id=?",
                            (db.ACTIVE, t["id"])).fetchone()[0]
            own[t["id"]] = (r, b)
        for d in deps:
            r = sum(own[i][0] for i in d["ids"]); b = sum(own[i][1] for i in d["ids"])
            d["sum_rate"], d["sum_busy"], d["sum_free"] = round(r, 2), b, round(r - b, 2)
        cats = [r[0] for r in con.execute("SELECT DISTINCT category FROM positions WHERE category<>'' ORDER BY category")]
    finally:
        con.close()
    return render_template("staff.html", deps=deps, all_deps=all_deps, cats=cats, fq=request.args.get("q", ""),
                           fdep=fdep, only_free=only_free, fcat=fcat, filtered=bool(fq or fdep or only_free or fcat))


db.init_db()
ensure_admin()

import extras  # noqa: E402  (файлы приказов, группы, табель, график отпусков, выгрузки)
import appl  # noqa: E402  (заявления сотрудников)
import gdoc  # noqa: E402  (общие документы)
import dash  # noqa: E402  (главная и единый список документов)
import tpl  # noqa: E402  (шаблоны документов)
import refs  # noqa: E402  (справочники)
import emp  # noqa: E402  (расширенная карточка сотрудника)
import mail  # noqa: E402  (внутренняя почта)
import sheet  # noqa: E402  (листы согласования)
import cal  # noqa: E402  (рабочий календарь, справочники)
import reports  # noqa: E402  (отчёты, права ролей)
import corr  # noqa: E402  (входящие письма, контроль исполнения)
import pdfmaker  # noqa: E402  (PDF и пакеты документов)
import backup  # noqa: E402  (резервные копии)
import import_emp  # noqa: E402  (импорт сотрудников)
import deptren  # noqa: E402  (приказ о переименовании подразделения)
import tprint  # noqa: E402  (печатная форма табеля)
import restore  # noqa: E402  (вернуть на рассмотрение)
import notify  # noqa: E402  (отклонённые документы)
extras.setup(sys.modules[__name__])
appl.setup(sys.modules[__name__])
gdoc.setup(sys.modules[__name__])
dash.setup(sys.modules[__name__])
tpl.setup(sys.modules[__name__])
refs.setup(sys.modules[__name__])
emp.setup(sys.modules[__name__])
mail.setup(sys.modules[__name__])
sheet.setup(sys.modules[__name__])
cal.setup(sys.modules[__name__])
reports.setup(sys.modules[__name__])
corr.setup(sys.modules[__name__])
pdfmaker.setup(sys.modules[__name__])
backup.setup(sys.modules[__name__])
import_emp.setup(sys.modules[__name__])
deptren.setup(sys.modules[__name__])
tprint.setup(sys.modules[__name__])
restore.setup(sys.modules[__name__])
notify.setup(sys.modules[__name__])

if __name__ == "__main__":
    # 0.0.0.0 — чтобы коллеги могли подключаться с других компьютеров сети
    app.run(host="0.0.0.0", port=5000, debug=False)
