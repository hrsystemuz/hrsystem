"""Шаблоны документов: администратор с нуля создаёт шаблон (название, вид, заголовок, текст с полями,
подписанты). Любой сотрудник кадров/руководитель выбирает шаблон, заполняет поля и получает готовый черновик."""
import html
import json
import re

from flask import abort, flash, redirect, render_template, request, session, url_for

import db
import extras
from extras import login_required, roles_required, route

M = None

DDL = """
CREATE TABLE IF NOT EXISTS doc_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'Приказ',
    title       TEXT NOT NULL DEFAULT '',
    items       TEXT NOT NULL DEFAULT '[]',
    fields      TEXT NOT NULL DEFAULT '[]',
    steps       TEXT NOT NULL DEFAULT '[]',
    active      INTEGER NOT NULL DEFAULT 1,
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT '',
    used        INTEGER NOT NULL DEFAULT 0
);
"""

FIELD_TYPES = {"text": "Короткий текст", "area": "Длинный текст", "date": "Дата", "number": "Число",
               "employee": "Сотрудник (из списка)"}
EMP_SUFFIX = {"должность": "position", "подразделение": "department", "табельный номер": "tabel_no"}
BASE_TOKENS = ["Дата", "Автор", "Организация"]


def setup(app_module):
    global M
    M = app_module
    M.app.add_template_filter(lambda v: json.loads(v or "[]"), "fromjson")
    con = db.connect()
    try:
        con.executescript(DDL)
    finally:
        con.close()


def _now():
    return db.now_local().strftime("%Y-%m-%d %H:%M")


def _fields(t):
    try:
        return json.loads(t["fields"])
    except ValueError:
        return []


def _forbid_employee():
    if session.get("role") == "employee":
        abort(403)


def _parse_fields(form):
    """Поля из формы: [{label, type, required}] и ошибки."""
    fields, errors, seen = [], [], set()
    for lbl, typ, req in zip(form.getlist("f_label"), form.getlist("f_type"), form.getlist("f_req")):
        lbl = " ".join(lbl.split())[:60]
        if not lbl:
            continue
        if re.search(r"[{}/]", lbl):
            errors.append("В названии поля «" + lbl + "» нельзя использовать скобки { } и косую черту.")
            continue
        if lbl in seen or lbl in BASE_TOKENS:
            errors.append(f"Название поля «{lbl}» повторяется или совпадает со стандартным.")
            continue
        seen.add(lbl)
        fields.append({"label": lbl, "type": typ if typ in FIELD_TYPES else "text", "required": req != "no"})
    return fields, errors


def tokens_of(fields):
    """Все метки, которые можно вставить в текст: [(метка, подсказка)]."""
    out = [("{" + b + "}", b) for b in BASE_TOKENS]
    for f in fields:
        out.append(("{" + f["label"] + "}", f["label"]))
        if f["type"] == "employee":
            for suf in EMP_SUFFIX:
                out.append(("{" + f["label"] + " / " + suf + "}", f["label"] + ": " + suf))
    return out


def _fio(e):
    return " ".join(x for x in (e["last_name"], e["first_name"], e["middle_name"]) if x)


def _emp_options(con):
    """{строка в списке: сотрудник} для выбора работающих."""
    opts = {}
    for e in con.execute("SELECT * FROM employees WHERE status=? ORDER BY last_name, first_name", (db.ACTIVE,)):
        opts[f"{_fio(e)} · таб. № {e['tabel_no'] or '—'} · {e['department'] or ''}"] = e
    return opts


def fill(text, fields, values, emps, author, org):
    """Подставить значения в текст. Неизвестные метки оставляет как есть, пустое значение — прочерк."""
    base = {"Дата": db.now_local().strftime("%d.%m.%Y"), "Автор": author, "Организация": org}
    ftype = {f["label"]: f["type"] for f in fields}

    def sub(m):
        key = m.group(1).strip()
        if key in base:
            v = base[key]
        elif key in ftype:
            v = values.get(key, "")
            if ftype[key] == "date" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v or ""):
                v = f"{v[8:10]}.{v[5:7]}.{v[:4]}"
            if ftype[key] == "employee":
                e = emps.get(key)
                v = _fio(e) if e else ""
        elif " / " in key:
            name, suf = [x.strip() for x in key.split(" / ", 1)]
            if ftype.get(name) != "employee" or suf not in EMP_SUFFIX:
                return m.group(0)
            e = emps.get(name)
            v = (e[EMP_SUFFIX[suf]] if e else "") or ""
        else:
            return m.group(0)
        v = (v or "").strip()
        return html.escape(v) if v else "________"
    return re.sub(r"\{([^{}]{1,80})\}", sub, text)


# ---------- администратор: список и правка ----------

@route("/tpl")
@roles_required("admin")
def tpl_list():
    con = db.connect()
    try:
        rows = con.execute("SELECT * FROM doc_templates ORDER BY name").fetchall()
    finally:
        con.close()
    return render_template("tpl_list.html", rows=rows, FIELD_TYPES=FIELD_TYPES)


@route("/tpl/new", ("POST",))
@roles_required("admin")
def tpl_new():
    name = " ".join(request.form.get("name", "").split())[:120]
    if not name:
        flash("Введите название шаблона.", "error")
        return redirect(url_for("tpl_list"))
    con = db.connect()
    try:
        try:
            cur = con.execute("INSERT INTO doc_templates(name, items, created_by, created_at) VALUES(?,?,?,?)",
                              (name, json.dumps([""]), session["user"], _now()))
        except Exception:
            flash("Шаблон с таким названием уже есть.", "error")
            return redirect(url_for("tpl_list"))
        return redirect(url_for("tpl_edit", tpl_id=cur.lastrowid))
    finally:
        con.close()


def _edit_page(con, t, data, fields, errors=None):
    import gdoc
    rows = [{"login": s[0], "action": s[1], "queue": s[2], "due": s[3]} for s in data["steps"]]
    return render_template("gd_form.html", d=None, data=data, users=extras.people(con), groups=gdoc._groups(con),
                           choices=[], ACTIONS=gdoc.ACTIONS, KINDS=gdoc.KINDS, step_rows=rows, files=[],
                           errors=errors or [], tpl=t, tpl_fields=fields, COMMISSION=gdoc.COMMISSION, is_hr=False, cm_name="", cm_rows=[], CM_ROLES=gdoc.CM_ROLES, FIELD_TYPES=FIELD_TYPES,
                           BASE_TOKENS=BASE_TOKENS, EMP_SUFFIX=list(EMP_SUFFIX))


@route("/tpl/<int:tpl_id>", ("GET", "POST"))
@roles_required("admin")
def tpl_edit(tpl_id):
    import gdoc
    con = db.connect()
    try:
        t = con.execute("SELECT * FROM doc_templates WHERE id=?", (tpl_id,)).fetchone()
        if t is None:
            abort(404)
        if request.method == "POST":
            act = request.form.get("act", "save")
            if act == "delete":
                con.execute("DELETE FROM doc_templates WHERE id=?", (tpl_id,))
                flash("Шаблон удалён. Созданные по нему документы остались.", "ok")
                return redirect(url_for("tpl_list"))
            if act == "copy":
                n = 1
                while True:
                    nm = f"{t['name']} (копия{'' if n == 1 else ' ' + str(n)})"
                    if not con.execute("SELECT 1 FROM doc_templates WHERE name=?", (nm,)).fetchone():
                        break
                    n += 1
                cur = con.execute("INSERT INTO doc_templates(name, description, kind, title, items, fields, steps, active, "
                                  "created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                  (nm, t["description"], t["kind"], t["title"], t["items"], t["fields"], t["steps"], 0,
                                   session["user"], _now()))
                flash("Создана копия шаблона. Она скрыта, пока вы не включите её.", "ok")
                return redirect(url_for("tpl_edit", tpl_id=cur.lastrowid))
            data, errors = gdoc._parse_form(con)
            fields, ferr = _parse_fields(request.form)
            errors += ferr
            name = " ".join(request.form.get("name", "").split())[:120]
            if not name:
                errors.append("Введите название шаблона.")
            if sum(1 for s in data["steps"] if s[1] == "sign") > 1:
                errors.append("В шаблоне может быть не больше одного подписанта с действием «Подпись (утверждает)».")
            if errors:
                t2 = dict(t); t2["name"] = name; t2["description"] = request.form.get("description", ""); t2["active"] = 1 if request.form.get("active") else 0
                return _edit_page(con, t2, data, fields, errors)
            try:
                con.execute("UPDATE doc_templates SET name=?, description=?, kind=?, title=?, items=?, fields=?, steps=?, active=? WHERE id=?",
                            (name, request.form.get("description", "").strip()[:500], data["kind"], data["title"],
                             json.dumps(data["items"] or [""], ensure_ascii=False), json.dumps(fields, ensure_ascii=False),
                             json.dumps([list(s) for s in data["steps"]], ensure_ascii=False),
                             1 if request.form.get("active") else 0, tpl_id))
            except Exception:
                t2 = dict(t); t2["name"] = name
                return _edit_page(con, t2, data, fields, ["Шаблон с таким названием уже есть."])
            flash("Шаблон сохранён.", "ok")
            return redirect(url_for("tpl_edit", tpl_id=tpl_id))
        steps = [tuple(s) for s in json.loads(t["steps"])]
        data = {"title": t["title"], "kind": t["kind"], "items": json.loads(t["items"]) or [""], "related": [], "steps": steps}
        return _edit_page(con, t, data, _fields(t))
    finally:
        con.close()


# ---------- использование ----------

@route("/tpl/choose")
@login_required
def tpl_choose():
    _forbid_employee()
    con = db.connect()
    try:
        rows = con.execute("SELECT * FROM doc_templates WHERE active=1 ORDER BY name").fetchall()
    finally:
        con.close()
    return render_template("tpl_choose.html", rows=rows)


@route("/tpl/<int:tpl_id>/use", ("GET", "POST"))
@login_required
def tpl_use(tpl_id):
    import gdoc
    _forbid_employee()
    con = db.connect()
    try:
        t = con.execute("SELECT * FROM doc_templates WHERE id=? AND active=1", (tpl_id,)).fetchone()
        if t is None:
            abort(404)
        fields = _fields(t)
        emp_opts = _emp_options(con) if any(f["type"] == "employee" for f in fields) else {}
        values = {f["label"]: "" for f in fields}
        if request.method == "POST":
            errors, emps = [], {}
            for f in fields:
                v = (request.form.get("f_" + f["label"], "") or "").strip()[:2000]
                values[f["label"]] = v
                if not v:
                    if f["required"]:
                        errors.append(f"Заполните поле «{f['label']}».")
                    continue
                if f["type"] == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                    errors.append(f"Неверная дата в поле «{f['label']}».")
                elif f["type"] == "number" and not re.fullmatch(r"\d+([.,]\d+)?", v):
                    errors.append(f"В поле «{f['label']}» нужно число.")
                elif f["type"] == "employee":
                    if v in emp_opts:
                        emps[f["label"]] = emp_opts[v]
                    else:
                        errors.append(f"Выберите сотрудника из списка в поле «{f['label']}».")
            if not errors:
                author = extras.user_name(con, session["user"])
                org = (db.get_settings(con)["org_name"] or "").strip()
                items = [fill(x, fields, values, emps, author, org) for x in json.loads(t["items"])] or [""]
                title = " ".join(re.sub(r"<[^>]+>", " ", fill(t["title"], fields, values, emps, author, org)).split())[:300]
                steps = []
                for lg, act, queue, due in (tuple(s) for s in json.loads(t["steps"])):
                    if con.execute("SELECT 1 FROM users WHERE login=? AND role<>'employee'", (lg,)).fetchone():
                        steps.append((lg, act, queue, ""))
                con.execute("BEGIN IMMEDIATE")
                try:
                    cur = con.execute(
                        "INSERT INTO gdocs(kind, title, items, related, author, status, created_at) VALUES(?,?,?,?,?,?,?)",
                        (t["kind"], title, json.dumps(items), "[]", session["user"], "draft", _now()))
                    doc_id = cur.lastrowid
                    gdoc._save_steps(con, doc_id, steps)
                    gdoc.log(con, doc_id, f"Создан по шаблону «{t['name']}»")
                    con.execute("UPDATE doc_templates SET used=used+1 WHERE id=?", (tpl_id,))
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    raise
                flash("Черновик создан по шаблону. Проверьте текст и подписантов, затем отправьте на согласование.", "ok")
                return redirect(url_for("gd_edit", doc_id=doc_id))
            for e in errors:
                flash(e, "error")
        return render_template("tpl_use.html", t=t, fields=fields, values=values, emp_opts=list(emp_opts),
                               FIELD_TYPES=FIELD_TYPES)
    finally:
        con.close()
