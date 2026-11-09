"""Импорт сотрудников из Excel/CSV: проверка перед загрузкой, сохранение своих табельных номеров, пополнение штатного расписания."""
import csv
import io
import json
import re
import secrets
from datetime import date, datetime, timedelta

from flask import abort, flash, redirect, render_template, request, send_file, session, url_for

import db
from extras import csv_response, roles_required, route

M = None
MAX_ROWS = 5000

# поле -> варианты названий колонок (рус., узб., англ.)
HEADERS = {
    "fio": ["фио", "ф и о", "ф.и.о", "ф.и.о.", "f.i.sh", "fish", "fio", "сотрудник", "xodim", "фамилия имя отчество", "familiya ism sharif"],
    "last_name": ["фамилия", "familiya", "familiyasi", "last name", "surname"],
    "first_name": ["имя", "ism", "ismi", "first name", "name"],
    "middle_name": ["отчество", "sharif", "sharifi", "otasining ismi", "middle name", "otchestvo"],
    "birth_date": ["дата рождения", "дата р", "tug'ilgan sana", "tugilgan sana", "tug‘ilgan sana", "birth date", "date of birth", "туғилган сана", "tug'ilgan kun"],
    "pinfl": ["пинфл", "pinfl", "jshshir", "жшшир", "пинфл (jshshir)"],
    "passport": ["паспорт", "серия и номер паспорта", "pasport", "passport", "паспорт серия номер"],
    "address": ["адрес", "адрес прописки", "прописка", "manzil", "address", "yashash manzili"],
    "department": ["подразделение", "отдел", "цех", "департамент", "управление", "bo'lim", "bolim", "bo‘lim", "department", "структурное подразделение"],
    "position": ["должность", "lavozim", "position", "профессия", "lavozimi"],
    "hire_date": ["дата приема", "дата приёма", "дата приема на работу", "дата начала работы", "ishga qabul sanasi", "qabul qilingan sana", "hire date", "ishga kirgan sana"],
    "tabel_no": ["табельный номер", "табельный", "таб номер", "таб №", "таб.№", "tabel raqami", "tabel", "tabel no", "табель"],
    "contract_no": ["номер договора", "договор", "номер трудового договора", "shartnoma raqami", "shartnoma", "contract no"],
    "folder_no": ["номер папки", "папка", "papka", "folder"],
    "workbook": ["трудовая книжка", "трудовая", "mehnat daftarchasi", "mehnat daftarcha", "workbook"],
    "gender": ["пол", "jinsi", "gender", "jins"],
    "education": ["образование", "ma'lumoti", "malumoti", "education", "ma‘lumoti"],
    "phone": ["телефон", "тел", "telefon", "phone", "тел."],
}
LABELS = {"last_name": "Фамилия", "first_name": "Имя", "middle_name": "Отчество", "birth_date": "Дата рождения", "pinfl": "ПИНФЛ",
          "passport": "Паспорт", "address": "Адрес", "department": "Подразделение", "position": "Должность", "hire_date": "Дата приёма",
          "tabel_no": "Табельный номер", "contract_no": "Номер договора", "folder_no": "Номер папки", "workbook": "Трудовая книжка",
          "gender": "Пол", "education": "Образование", "phone": "Телефон"}
REQUIRED = ["last_name", "first_name", "birth_date", "pinfl", "department", "position", "hire_date"]


def setup(app_module):
    global M
    M = app_module


def _norm(s):
    s = str(s or "").lower().replace("ё", "е").replace("’", "'").replace("ʻ", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", re.sub(r"[_\n\r]+", " ", s)).strip(" :.")


_LOOKUP = {}
for _f, _names in HEADERS.items():
    for _n in _names:
        _LOOKUP[_norm(_n)] = _f


def _read(upload):
    """Прочитать файл -> список строк-списков. Выбрасывает ValueError с понятным текстом."""
    name = (upload.filename or "").lower()
    blob = upload.read(10 * 1024 * 1024 + 1)
    if not blob:
        raise ValueError("Файл пустой.")
    if len(blob) > 10 * 1024 * 1024:
        raise ValueError("Файл больше 10 МБ.")
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        try:
            wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
        except Exception:                                                      # noqa: BLE001
            raise ValueError("Не удалось открыть Excel-файл. Сохраните его как .xlsx и повторите.")
        ws = wb.worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    if name.endswith((".csv", ".txt")):
        text = None
        for enc in ("utf-8-sig", "cp1251"):
            try:
                text = blob.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError("Не удалось прочитать кодировку файла. Сохраните его как .xlsx.")
        dialect = ";" if text.count(";") >= text.count(",") else ","
        return [r for r in csv.reader(io.StringIO(text), delimiter=dialect)]
    raise ValueError("Нужен файл Excel (.xlsx) или CSV. Старый формат .xls сохраните как .xlsx.")


def _find_header(rows):
    for i, r in enumerate(rows[:15]):
        m = {}
        for j, c in enumerate(r):
            f = _LOOKUP.get(_norm(c))
            if f and f not in m.values():
                m[j] = f
        fields = set(m.values())
        if len(fields) >= 3 and ({"last_name", "fio"} & fields):
            return i, m
    return None, {}


def _date(v):
    if v is None or v == "":
        return ""
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (int, float)) and 20000 < v < 80000:           # число-дата Excel
        return (date(1899, 12, 30) + timedelta(days=int(v))).isoformat()
    s = str(v).strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%y", "%Y.%m.%d"):
        try:
            return datetime.strptime(s[:10] if fmt == "%Y-%m-%d" else s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _text(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return " ".join(str(v).split())


def _parse(rows):
    hi, colmap = _find_header(rows)
    if hi is None:
        raise ValueError("Не нашёл строку с названиями колонок. Нужны хотя бы: Фамилия, Имя, Дата рождения, ПИНФЛ, Подразделение, Должность, Дата приёма. Скачайте шаблон.")
    out = []
    for n, r in enumerate(rows[hi + 1:], start=hi + 2):
        if not any(_text(c) for c in r):
            continue
        d = {"_row": n}
        for j, f in colmap.items():
            d[f] = r[j] if j < len(r) else None
        out.append(d)
    if len(out) > MAX_ROWS:
        raise ValueError(f"В файле больше {MAX_ROWS} строк. Разделите его на части.")
    if not out:
        raise ValueError("В файле нет строк с данными под заголовком.")
    return out, sorted(set(colmap.values()))


def _clean(raw):
    """Строка файла -> словарь значений и список ошибок формата."""
    errs = []
    v = {"row": raw["_row"]}
    fio = _text(raw.get("fio"))
    if fio and not _text(raw.get("last_name")):
        p = fio.split()
        v["last_name"], v["first_name"], v["middle_name"] = p[0], (p[1] if len(p) > 1 else ""), " ".join(p[2:])
    else:
        for k in ("last_name", "first_name", "middle_name"):
            v[k] = _text(raw.get(k))
    for k in ("pinfl", "passport", "address", "department", "position", "tabel_no", "contract_no", "folder_no", "education", "phone"):
        v[k] = _text(raw.get(k))
    v["pinfl"] = re.sub(r"\D", "", v["pinfl"]) if v["pinfl"] else ""
    v["passport"] = v["passport"].replace(" ", "").upper()
    for k in ("birth_date", "hire_date"):
        d = _date(raw.get(k))
        if d is None:
            errs.append(f"{LABELS[k]}: не понял дату «{_text(raw.get(k))}».")
            d = ""
        v[k] = d
    wb = _norm(raw.get("workbook"))
    v["workbook"] = wb in ("да", "yes", "1", "bor", "есть", "+", "true", "y")
    g = _norm(raw.get("gender"))
    v["gender"] = "М" if g in ("м", "муж", "мужской", "m", "male", "erkak", "э") else ("Ж" if g in ("ж", "жен", "женский", "f", "female", "ayol", "а") else "")
    return v, errs


def _validate(con, rows, create_staff):
    """Проверить строки. Возвращает (готовые, итоговый список строк со статусами)."""
    emp = {"pinfl": {r[0] for r in con.execute("SELECT pinfl FROM employees WHERE status=?", (db.ACTIVE,))},
           "tabel": {r[0] for r in con.execute("SELECT tabel_no FROM employees")},
           "contract": {r[0] for r in con.execute("SELECT contract_no FROM employees")},
           "folder": {r[0] for r in con.execute("SELECT folder_no FROM employees WHERE folder_no IS NOT NULL")}}
    staff = {}
    for d in con.execute("SELECT id, name FROM departments"):
        staff[d["name"]] = {p["name"] for p in con.execute("SELECT name FROM positions WHERE department_id=?", (d["id"],))}
    seen = {"pinfl": set(), "tabel": set(), "contract": set(), "folder": set()}
    result = []
    for raw in rows:
        v, errs = _clean(raw)
        warns = []
        for k in REQUIRED:
            if k != "pinfl" and not v.get(k) and not (k in ("last_name", "first_name") and False):
                if k in ("birth_date", "hire_date") and any(k in e.lower() for e in errs):
                    continue
                errs.append(f"Не заполнено: {LABELS[k]}.")
        if v["pinfl"] and not re.fullmatch(r"\d{14}", v["pinfl"]):
            errs.append("ПИНФЛ должен состоять ровно из 14 цифр.")
        elif not v["pinfl"]:
            errs.append("Не заполнено: ПИНФЛ.")
        for k, label in (("last_name", "Фамилия"), ("first_name", "Имя"), ("middle_name", "Отчество")):
            if v[k] and not M.NAME_RE.fullmatch(v[k]):
                errs.append(f"{label}: нужна латиница (A–Z). Сейчас: «{v[k]}».")
        if v["birth_date"] and v["hire_date"]:
            if v["birth_date"] >= v["hire_date"]:
                errs.append("Дата рождения позже даты приёма.")
            elif int(v["hire_date"][:4]) - int(v["birth_date"][:4]) < 16:
                errs.append("Меньше 16 лет на дату приёма.")
        if v["pinfl"]:
            if v["pinfl"] in emp["pinfl"]:
                errs.append("Сотрудник с таким ПИНФЛ уже работает в программе.")
            elif v["pinfl"] in seen["pinfl"]:
                errs.append("Этот ПИНФЛ повторяется в файле выше.")
        for k, key, label in (("tabel_no", "tabel", "Табельный номер"), ("contract_no", "contract", "Номер договора"), ("folder_no", "folder", "Номер папки")):
            if v[k]:
                if v[k] in emp[key]:
                    errs.append(f"{label} {v[k]} уже есть в программе.")
                elif v[k] in seen[key]:
                    errs.append(f"{label} {v[k]} повторяется в файле выше.")
        if v["department"] and v["position"]:
            if v["department"] not in staff or v["position"] not in staff.get(v["department"], ()):
                if create_staff:
                    warns.append("Подразделение/должность будут добавлены в штатное расписание.")
                else:
                    errs.append("Такого подразделения или должности нет в штатном расписании.")
        if not v["passport"]:
            warns.append("Нет паспорта.")
        if not v["address"]:
            warns.append("Нет адреса.")
        v["errors"], v["warnings"] = errs, warns
        if not errs:
            for k, key in (("pinfl", "pinfl"), ("tabel_no", "tabel"), ("contract_no", "contract"), ("folder_no", "folder")):
                if v[k]:
                    seen[key].add(v[k])
        result.append(v)
    return result


def _raw_dir():
    p = db.DB_PATH.parent / "imports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def template_bytes():
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Сотрудники"
    ws.append(["Фамилия", "Имя", "Отчество", "Дата рождения", "ПИНФЛ", "Паспорт", "Адрес", "Подразделение", "Должность",
               "Дата приёма", "Табельный номер", "Номер договора", "Номер папки", "Трудовая книжка", "Пол", "Образование", "Телефон"])
    ws2 = wb.create_sheet("Пример")
    ws2.append(ws[1][i].value for i in range(17))
    ws2.append(["Aliyev", "Azamat", "Karimovich", "15.03.1990", "30103900000011", "AA1234567", "Toshkent sh., Chilonzor t.", "Kadrlar bo'limi",
                "Katta mutaxassis", "12.01.2024", "0101", "05/24", "A-1", "да", "М", "Oliy", "+998901234567"])
    ws3 = wb.create_sheet("Памятка")
    for line in ["Заполняйте лист «Сотрудники» (первая строка — названия колонок, не меняйте).",
                 "Обязательно: Фамилия, Имя, Дата рождения, ПИНФЛ (14 цифр), Подразделение, Должность, Дата приёма.",
                 "Фамилия, имя и отчество — латиницей, как в программе.",
                 "Табельный номер, номер договора, номер папки — необязательно. Если указаны, сохранятся как есть; если пусто — программа присвоит сама.",
                 "Даты: 15.03.1990 или 1990-03-15.", "Трудовая книжка: да / нет. Пол: М / Ж.",
                 "Можно вместо трёх колонок ФИО сделать одну колонку «ФИО»."]:
        ws3.append([line])
    for w in (ws, ws2):
        for col, width in zip("ABCDEFGHIJKLMNOPQ", (16, 14, 16, 14, 16, 12, 30, 24, 24, 12, 12, 12, 12, 12, 6, 14, 16)):
            w.column_dimensions[col].width = width
    ws3.column_dimensions["A"].width = 120
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


@route("/import")
@roles_required("hr", "admin")
def import_page():
    return render_template("import.html", preview=None)


@route("/import/template.xlsx")
@roles_required("hr", "admin")
def import_template():
    return send_file(template_bytes(), as_attachment=True, download_name="kadry_import_shablon.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@route("/import/check", ("POST",))
@roles_required("hr", "admin")
def import_check():
    f = request.files.get("file")
    if f is None or not f.filename:
        flash("Выберите файл.", "error")
        return redirect(url_for("import_page"))
    try:
        rows, found = _read(f), None
        raws, found = _parse(rows)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("import_page"))
    create_staff = request.form.get("create_staff") == "1"
    con = db.connect()
    try:
        res = _validate(con, raws, create_staff)
    finally:
        con.close()
    token = secrets.token_hex(12)
    (_raw_dir() / f"{token}.json").write_text(json.dumps({"user": session["user"], "create_staff": create_staff,
                                                          "rows": [{k: (v.isoformat() if isinstance(v, (date, datetime)) else v) for k, v in r.items()} for r in raws]},
                                                         ensure_ascii=False), encoding="utf-8")
    ok = [r for r in res if not r["errors"]]
    missing = [LABELS[k] for k in REQUIRED if k not in found and not (k in ("last_name", "first_name") and "fio" in found)]
    return render_template("import.html", preview={"rows": res, "ok": len(ok), "bad": len(res) - len(ok), "token": token,
                                                   "warn": sum(1 for r in ok if r["warnings"]), "found": [LABELS.get(k, "ФИО") for k in found],
                                                   "missing": missing, "create_staff": create_staff})


@route("/import/errors.csv")
@roles_required("hr", "admin")
def import_errors():
    token = request.args.get("t", "")
    p = _raw_dir() / f"{re.sub(r'[^0-9a-f]', '', token)}.json"
    if not p.exists():
        abort(404)
    data = json.loads(p.read_text(encoding="utf-8"))
    if data["user"] != session["user"]:
        abort(403)
    con = db.connect()
    try:
        res = _validate(con, data["rows"], data["create_staff"])
    finally:
        con.close()
    return csv_response("import_errors.csv", ["Строка файла", "Фамилия", "Имя", "ПИНФЛ", "Проблемы"],
                        [[r["row"], r["last_name"], r["first_name"], r["pinfl"], " ".join(r["errors"])] for r in res if r["errors"]])


def _next_free(con, key_counter, fmt, used):
    while True:
        n = db.next_counter(con, key_counter)
        val = fmt(n)
        if val not in used:
            return val


@route("/import/run", ("POST",))
@roles_required("hr", "admin")
def import_run():
    token = re.sub(r"[^0-9a-f]", "", request.form.get("token", ""))
    p = _raw_dir() / f"{token}.json"
    if not token or not p.exists():
        flash("Проверка устарела. Загрузите файл заново.", "error")
        return redirect(url_for("import_page"))
    data = json.loads(p.read_text(encoding="utf-8"))
    if data["user"] != session["user"]:
        abort(403)
    con = db.connect()
    try:
        res = _validate(con, data["rows"], data["create_staff"])
        good = [r for r in res if not r["errors"]]
        if not good:
            flash("Нет строк без ошибок: загружать нечего.", "error")
            return redirect(url_for("import_page"))
        con.execute("BEGIN IMMEDIATE")
        try:
            # штатное расписание: добавляем недостающее, ставок столько, сколько людей из файла
            need = {}
            for r in good:
                need[(r["department"], r["position"])] = need.get((r["department"], r["position"]), 0) + 1
            for (dep, pos), n in need.items():
                row = con.execute("SELECT id FROM departments WHERE name=?", (dep,)).fetchone()
                if row is None:
                    con.execute("INSERT INTO departments(name) VALUES(?)", (dep,))
                    row = con.execute("SELECT id FROM departments WHERE name=?", (dep,)).fetchone()
                ps = con.execute("SELECT rate FROM positions WHERE department_id=? AND name=?", (row["id"], pos)).fetchone()
                if ps is None:
                    con.execute("INSERT INTO positions(department_id, name, rate) VALUES(?,?,?)", (row["id"], pos, n))
            # счётчики не ниже самых больших имеющихся номеров
            used_t = {r[0] for r in con.execute("SELECT tabel_no FROM employees")} | {r["tabel_no"] for r in good if r["tabel_no"]}
            used_c = {r[0] for r in con.execute("SELECT contract_no FROM employees")} | {r["contract_no"] for r in good if r["contract_no"]}
            used_f = {r[0] for r in con.execute("SELECT folder_no FROM employees WHERE folder_no IS NOT NULL")} | {r["folder_no"] for r in good if r["folder_no"]}
            mx = max([int(x) for x in used_t if x.isdigit()] + [0])
            cur = con.execute("SELECT value FROM counters WHERE key='tabel'").fetchone()
            db.set_counter(con, "tabel", max(mx, cur[0] if cur else 0))
            for x in used_c:
                n, _, yy = x.partition("/")
                if n.isdigit() and yy.isdigit():
                    cur = con.execute("SELECT value FROM counters WHERE key=?", (f"contract:{yy}",)).fetchone()
                    if cur is None or cur[0] < int(n):
                        db.set_counter(con, f"contract:{yy}", int(n))
            cols = {r["name"] for r in con.execute("PRAGMA table_info(employees)")}
            added = 0
            for r in sorted(good, key=lambda x: x["row"]):
                yy = r["hire_date"][2:4]
                tabel = r["tabel_no"] or _next_free(con, "tabel", lambda n: f"{n:04d}", used_t)
                contract = r["contract_no"] or _next_free(con, f"contract:{yy}", lambda n: f"{n:02d}/{yy}", used_c)
                folder = r["folder_no"] or db.next_free_folder(con, db.folder_letter(r["last_name"]))
                used_t.add(tabel); used_c.add(contract); used_f.add(folder)
                workbook = folder if r["workbook"] else None
                cur = con.execute(
                    """INSERT INTO employees(tabel_no, contract_no, folder_no, workbook_no, pinfl, last_name, first_name, middle_name,
                       birth_date, passport, address, position, department, hire_date, created_by, gender, education)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (tabel, contract, folder, workbook, r["pinfl"], r["last_name"], r["first_name"], r["middle_name"], r["birth_date"],
                     r["passport"], r["address"], r["position"], r["department"], r["hire_date"], session["user"], r["gender"], r["education"]))
                if r["phone"] and "phone" in cols:
                    con.execute("UPDATE employees SET phone=? WHERE id=?", (r["phone"][:40], cur.lastrowid))
                added += 1
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()
    p.unlink(missing_ok=True)
    skipped = len(res) - len(good)
    flash(f"Загружено сотрудников: {added}." + (f" Пропущено строк с ошибками: {skipped}." if skipped else ""), "ok")
    return redirect(url_for("employees"))
