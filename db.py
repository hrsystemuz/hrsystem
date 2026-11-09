"""База данных кадровой программы и автоматическая нумерация."""
import os
import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = Path(os.environ.get("HR_DB", BASE_DIR / "data" / "hr.db"))

ACTIVE = "работает"
DISMISSED = "уволен"

EMPLOYEES_DDL = """
CREATE TABLE {name} (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    tabel_no         TEXT NOT NULL UNIQUE,
    contract_no      TEXT NOT NULL UNIQUE,
    folder_no        TEXT,
    workbook_no      TEXT,
    pinfl            TEXT NOT NULL,
    last_name        TEXT NOT NULL,
    first_name       TEXT NOT NULL,
    middle_name      TEXT NOT NULL DEFAULT '',
    birth_date       TEXT NOT NULL,
    passport         TEXT NOT NULL,
    address          TEXT NOT NULL,
    position         TEXT NOT NULL,
    department       TEXT NOT NULL,
    hire_date        TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'работает',
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    created_by       TEXT NOT NULL DEFAULT '',
    dismissal_date   TEXT,
    dismissal_reason TEXT NOT NULL DEFAULT '',
    old_folder_no    TEXT,
    hire_order_id    INTEGER
)
"""

COMMON_COLUMNS = (
    "id, tabel_no, contract_no, folder_no, workbook_no, pinfl, last_name, "
    "first_name, middle_name, birth_date, passport, address, position, "
    "department, hire_date, status, created_at, created_by"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    login         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name     TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL DEFAULT 'hr',
    title         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS counters (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS departments (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    department_id INTEGER NOT NULL REFERENCES departments(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    UNIQUE(department_id, name)
);
"""

INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_emp_folder
    ON employees(folder_no) WHERE folder_no IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_emp_pinfl_active
    ON employees(pinfl) WHERE status = 'работает';
"""


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, isolation_level=None)  # транзакциями управляем сами
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def _migrate_employees(con):
    """Старая версия таблицы (папка обязательна и уникальна навсегда) -> новая.

    Данные сохраняются. Если таблицы ещё нет, она просто создаётся.
    """
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='employees'"
    ).fetchone()
    if row is None:
        con.execute(EMPLOYEES_DDL.format(name="employees"))
        return
    if "dismissal_date" in row["sql"]:
        return  # уже новая версия
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("ALTER TABLE employees RENAME TO employees_old")
        con.execute(EMPLOYEES_DDL.format(name="employees"))
        con.execute(
            f"INSERT INTO employees({COMMON_COLUMNS}) "
            f"SELECT {COMMON_COLUMNS} FROM employees_old"
        )
        con.execute("DROP TABLE employees_old")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.execute("PRAGMA foreign_keys=ON")


# Создаётся после миграции сотрудников: при переименовании таблицы employees
# SQLite перенаправил бы на старое имя все внешние ссылки на неё.
TRANSFERS_DDL = """
CREATE TABLE IF NOT EXISTS transfers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id     INTEGER NOT NULL REFERENCES employees(id),
    transfer_date   TEXT NOT NULL,
    from_department TEXT NOT NULL,
    from_position   TEXT NOT NULL,
    to_department   TEXT NOT NULL,
    to_position     TEXT NOT NULL,
    order_no        TEXT NOT NULL,
    order_date      TEXT NOT NULL,
    article         TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    created_by      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_transfers_emp ON transfers(employee_id, transfer_date);
"""


ORDERS_DDL = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL DEFAULT 'hire',
    status           TEXT NOT NULL DEFAULT 'draft',
    number           TEXT,
    order_date       TEXT,
    data             TEXT NOT NULL,
    basis            TEXT NOT NULL DEFAULT '',
    employee_id      INTEGER,
    created_by       TEXT NOT NULL,
    created_by_name  TEXT NOT NULL DEFAULT '',
    created_by_title TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    signed_by        TEXT,
    signed_by_name   TEXT,
    signed_by_title  TEXT,
    signed_at        TEXT,
    token            TEXT UNIQUE,
    content_hash     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_number ON orders(number) WHERE number IS NOT NULL;

-- шаги согласования приказа: кто должен согласовать, к сведению или исполнению
CREATE TABLE IF NOT EXISTS order_steps (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id  INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    ord       INTEGER NOT NULL DEFAULT 0,
    login     TEXT NOT NULL,
    name      TEXT NOT NULL DEFAULT '',
    title     TEXT NOT NULL DEFAULT '',
    action    TEXT NOT NULL DEFAULT 'rozilik',
    status    TEXT NOT NULL DEFAULT 'pending',
    at        TEXT,
    comment   TEXT NOT NULL DEFAULT '',
    due       TEXT NOT NULL DEFAULT ''
);

-- группы подписантов: готовые цепочки согласования
CREATE TABLE IF NOT EXISTS signer_groups (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS signer_group_members (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES signer_groups(id) ON DELETE CASCADE,
    login    TEXT NOT NULL,
    action   TEXT NOT NULL DEFAULT 'rozilik',
    ord      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS order_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    stored      TEXT NOT NULL UNIQUE,
    size        INTEGER NOT NULL DEFAULT 0,
    uploaded_by TEXT NOT NULL DEFAULT '',
    uploaded_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS order_links (
    order_id        INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    linked_order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    PRIMARY KEY (order_id, linked_order_id)
);

-- ручные отметки табеля (то, что не следует из приказов)
CREATE TABLE IF NOT EXISTS timesheet (
    employee_id INTEGER NOT NULL,
    day         TEXT NOT NULL,
    code        TEXT NOT NULL,
    PRIMARY KEY (employee_id, day)
);

-- план (график) отпусков на год
CREATE TABLE IF NOT EXISTS vacation_plan (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    employee_id INTEGER NOT NULL,
    year        INTEGER NOT NULL,
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);
"""


APPS_DDL = """
CREATE TABLE IF NOT EXISTS app_types (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name   TEXT NOT NULL UNIQUE,
    kind   TEXT NOT NULL DEFAULT 'none',
    body   TEXT NOT NULL DEFAULT '',
    fields TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1,
    ord    INTEGER NOT NULL DEFAULT 0
);
-- цепочка согласования заявлений каждого вида: who = 'head' (начальник подразделения заявителя) или 'user'
CREATE TABLE IF NOT EXISTS app_route (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    type_id INTEGER NOT NULL REFERENCES app_types(id) ON DELETE CASCADE,
    ord     INTEGER NOT NULL DEFAULT 0,
    who     TEXT NOT NULL DEFAULT 'user',
    login   TEXT NOT NULL DEFAULT '',
    label   TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS dept_heads (
    department TEXT PRIMARY KEY,
    login      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    type_id        INTEGER,
    type_name      TEXT NOT NULL DEFAULT '',
    kind           TEXT NOT NULL DEFAULT 'none',
    employee_id    INTEGER NOT NULL,
    author         TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'draft',
    fields         TEXT NOT NULL DEFAULT '{}',
    body           TEXT NOT NULL DEFAULT '',
    number         TEXT,
    created_at     TEXT NOT NULL DEFAULT '',
    submitted_at   TEXT,
    finished_at    TEXT,
    return_comment TEXT NOT NULL DEFAULT '',
    result_note    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS app_steps (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id  INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    ord     INTEGER NOT NULL DEFAULT 0,
    login   TEXT NOT NULL,
    name    TEXT NOT NULL DEFAULT '',
    title   TEXT NOT NULL DEFAULT '',
    label   TEXT NOT NULL DEFAULT '',
    status  TEXT NOT NULL DEFAULT 'pending',
    at      TEXT,
    comment TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS app_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    at     TEXT NOT NULL,
    who    TEXT NOT NULL DEFAULT '',
    text   TEXT NOT NULL
);
"""


def _ensure_columns(con):
    """Добавить столбцы, которых нет в базе, созданной прежними версиями."""
    def cols(table):
        return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
    if "hire_order_id" not in cols("employees"):
        con.execute("ALTER TABLE employees ADD COLUMN hire_order_id INTEGER")
    if "title" not in cols("users"):
        con.execute("ALTER TABLE users ADD COLUMN title TEXT NOT NULL DEFAULT ''")
    if "employee_id" not in cols("users"):
        con.execute("ALTER TABLE users ADD COLUMN employee_id INTEGER")
    for name, ddl in (("return_comment", "TEXT NOT NULL DEFAULT ''"), ("returned_by", "TEXT NOT NULL DEFAULT ''"),
                      ("deleted_at", "TEXT"), ("cancelled_at", "TEXT"), ("cancelled_by", "TEXT"),
                      ("cancel_reason", "TEXT NOT NULL DEFAULT ''"), ("group_name", "TEXT NOT NULL DEFAULT ''"),
                      ("signer_login", "TEXT NOT NULL DEFAULT ''"), ("app_id", "INTEGER")):
        if name not in cols("orders"):
            con.execute(f"ALTER TABLE orders ADD COLUMN {name} {ddl}")
    if "due" not in cols("order_steps"):
        con.execute("ALTER TABLE order_steps ADD COLUMN due TEXT NOT NULL DEFAULT ''")
    for name, ddl in (("gender", "TEXT NOT NULL DEFAULT ''"), ("education", "TEXT NOT NULL DEFAULT ''")):
        if name not in cols("employees"):
            con.execute(f"ALTER TABLE employees ADD COLUMN {name} {ddl}")
    for name, ddl in (("code", "TEXT NOT NULL DEFAULT ''"), ("parent_id", "INTEGER")):
        if name not in cols("departments"):
            con.execute(f"ALTER TABLE departments ADD COLUMN {name} {ddl}")
    for name, ddl in (("rate", "REAL NOT NULL DEFAULT 1"), ("grade", "TEXT NOT NULL DEFAULT ''"),
                      ("category", "TEXT NOT NULL DEFAULT ''")):
        if name not in cols("positions"):
            con.execute(f"ALTER TABLE positions ADD COLUMN {name} {ddl}")


def init_db():
    con = connect()
    con.executescript(SCHEMA)
    _migrate_employees(con)
    con.executescript(INDEXES)
    con.executescript(TRANSFERS_DDL)
    con.executescript(ORDERS_DDL)
    con.executescript(APPS_DDL)
    _ensure_columns(con)
    con.close()


# ---------- настройки организации ----------

SETTING_DEFAULTS = {
    "org_name": "",                                   # название в шапке приказа
    "org_form": "",                                   # например: Mas'uliyati cheklangan jamiyati
    "public_url": "",                                 # адрес для QR-кода (необязательно)
    "order_number_format": "01/{seq:04d}-SHT",        # приказ о приёме; {seq} и {yy}
    "order_number_format_dismiss": "02/{seq:04d}-SHT",    # приказ об увольнении
    "order_number_format_transfer": "03/{seq:04d}-SHT",   # приказ о переводе
    "order_number_format_vacation": "04/{seq:04d}-SHT",   # приказ об отпуске
    "order_number_format_trip": "05/{seq:04d}-SHT",       # приказ о командировке
    "annual_leave_days": "21",                            # ежегодный отпуск, календарных дней
    "work_days_per_week": "5",                            # 5 или 6: влияет на табель
    "extra_holidays": "",                                 # нерабочие дни через запятую: ГГГГ-ММ-ДД
    "strict_vacancy": "1",                                # 1: приказ о приёме/переводе нельзя подписать без свободной ставки
}

# какой формат и какой счётчик у какого вида приказа (у каждого вида своя нумерация)
ORDER_SETTING = {"hire": "order_number_format", "dismiss": "order_number_format_dismiss",
                 "transfer": "order_number_format_transfer", "vacation": "order_number_format_vacation",
                 "trip": "order_number_format_trip", "vac_extend": "order_number_format_vacation",
                 "vac_recall": "order_number_format_vacation",
                 "discipline": "order_number_format", "discipline_lift": "order_number_format",
                 "dept_rename": "order_number_format"}
ORDER_COUNTER = {"hire": "order:{y}", "dismiss": "order:dismiss:{y}", "transfer": "order:transfer:{y}",
                 "vacation": "order:vacation:{y}", "trip": "order:trip:{y}", "vac_extend": "order:vacation:{y}",
                 "vac_recall": "order:vacation:{y}",
                 "discipline": "order:{y}", "discipline_lift": "order:{y}", "dept_rename": "order:{y}"}
ABSENCE_KINDS = ("vacation", "trip")
DISCIPLINE = ("discipline", "discipline_lift")   # дисциплинарное взыскание и его снятие
DISCIPLINE_MONTHS = 12                            # сколько действует взыскание, если его не сняли раньше
VAC_ADJ = ("vac_extend", "vac_recall")   # продление и отзыв из отпуска: меняют срок ранее подписанного приказа об отпуске


def get_settings(con):
    result = dict(SETTING_DEFAULTS)
    for r in con.execute("SELECT key, value FROM settings"):
        if r["key"] in result:
            result[r["key"]] = r["value"]
    return result


def set_setting(con, key, value):
    con.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def valid_number_format(fmt):
    """Формат номера: только {seq} (с форматом, например {seq:04d}) и {yy}."""
    import string
    try:
        for _, field, spec, conv in string.Formatter().parse(fmt):
            if field is None:
                continue
            if field not in ("seq", "yy") or conv:
                return False
        fmt.format(seq=1, yy="26")
    except (ValueError, KeyError, IndexError):
        return False
    return "{seq" in fmt


def now_local():
    """Время в Узбекистане (UTC+5, без перехода на летнее время)."""
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=5)))


# ---------- счётчики ----------

def next_counter(con, key):
    """Следующее число счётчика. Вызывать только внутри транзакции."""
    con.execute(
        "INSERT INTO counters(key, value) VALUES(?, 1) "
        "ON CONFLICT(key) DO UPDATE SET value = value + 1",
        (key,),
    )
    return con.execute("SELECT value FROM counters WHERE key=?", (key,)).fetchone()[0]


def set_counter(con, key, value):
    """Задать стартовое значение (если в организации уже есть сотрудники)."""
    con.execute(
        "INSERT INTO counters(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---------- папки ----------

def folder_letter(last_name):
    return last_name.strip()[0].upper()


def next_free_folder(con, letter):
    """Наименьший свободный номер папки на эту букву.

    Номера уволенных освобождаются, поэтому подбираем первый свободный:
    если заняты А-1, А-2, А-4, то следующая папка будет А-3.
    """
    used = set()
    for r in con.execute("SELECT folder_no FROM employees WHERE folder_no IS NOT NULL"):
        prefix, _, num = r[0].rpartition("-")
        if prefix == letter and num.isdigit():
            used.add(int(num))
    n = 1
    while n in used:
        n += 1
    return f"{letter}-{n}"


# ---------- приём и увольнение ----------

def hire_employee(con, d, created_by=""):
    """Принять сотрудника: присвоить все номера и сохранить карточку.

    Всё выполняется одной операцией: если что-то пошло не так, номера
    не «сгорают» и в нумерации не появляется пропусков.
    d — словарь с ключами: pinfl, last_name, first_name, middle_name,
    birth_date (ГГГГ-ММ-ДД), passport, address, position, department,
    hire_date (ГГГГ-ММ-ДД), has_workbook (bool).
    """
    con.execute("BEGIN IMMEDIATE")
    try:
        emp_id = insert_employee(con, d, created_by)
        con.execute("COMMIT")
        return emp_id
    except Exception:
        con.execute("ROLLBACK")
        raise


def insert_employee(con, d, created_by="", hire_order_id=None):
    """Присвоить номера и записать сотрудника. Вызывать внутри транзакции."""
    yy = d["hire_date"][2:4]
    tabel = f"{next_counter(con, 'tabel'):04d}"
    contract = f"{next_counter(con, f'contract:{yy}'):02d}/{yy}"
    folder = next_free_folder(con, folder_letter(d["last_name"]))
    # Трудовая книжка нумеруется так же, как папка сотрудника
    workbook = folder if d.get("has_workbook") else None
    cur = con.execute(
        """INSERT INTO employees
           (tabel_no, contract_no, folder_no, workbook_no, pinfl,
            last_name, first_name, middle_name, birth_date, passport,
            address, position, department, hire_date, created_by, hire_order_id,
            gender, education)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (tabel, contract, folder, workbook, d["pinfl"],
         d["last_name"], d["first_name"], d.get("middle_name", ""),
         d["birth_date"], d["passport"], d["address"], d["position"],
         d["department"], d["hire_date"], created_by, hire_order_id,
         d.get("gender", ""), d.get("education", "")),
    )
    return cur.lastrowid


# ---------- приказы ----------

def order_hash(order, tabel_no, steps=None):
    """Отпечаток содержимого приказа: по нему проверяется, что приказ не менялся."""
    import hashlib
    import json
    payload = {
        "kind": order["kind"], "number": order["number"], "date": order["order_date"],
        "data": json.loads(order["data"]), "basis": order["basis"], "tabel": tabel_no,
        "author": order["created_by_name"], "created_at": order["created_at"],
        "signer": order["signed_by_name"], "signed_at": order["signed_at"],
    }
    if steps:   # согласующие входят в отпечаток только если они были
        payload["steps"] = [[x["login"], x["name"], x["title"], x["action"], x["status"], x["at"]] for x in steps]
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _apply_dismissal(con, emp_id, dismissal_date, reason=""):
    """Уволить (внутри уже открытой транзакции). Возвращает (номер папки, ошибка)."""
    emp = con.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
    if emp is None or emp["status"] != ACTIVE:
        return None, "Сотрудник уже уволен или не найден."
    last = con.execute(
        "SELECT MAX(transfer_date) FROM transfers WHERE employee_id=?", (emp_id,)
    ).fetchone()[0]
    if dismissal_date < max(emp["hire_date"], last or ""):
        return None, "Дата увольнения не может быть раньше приёма или последнего перевода."
    con.execute(
        """UPDATE employees
           SET status=?, dismissal_date=?, dismissal_reason=?,
               old_folder_no=folder_no, folder_no=NULL, workbook_no=NULL
           WHERE id=?""",
        (DISMISSED, dismissal_date, reason, emp_id),
    )
    return emp["folder_no"], None


def _apply_transfer(con, emp_id, to_department, to_position, transfer_date,
                    order_no, order_date, article, created_by=""):
    """Перевести (внутри уже открытой транзакции). Возвращает None или текст ошибки."""
    emp = con.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
    if emp is None or emp["status"] != ACTIVE:
        return "Переводить можно только работающего сотрудника."
    if (emp["department"], emp["position"]) == (to_department, to_position):
        return "Сотрудник уже работает в этом подразделении на этой должности."
    last = con.execute(
        "SELECT MAX(transfer_date) FROM transfers WHERE employee_id=?", (emp_id,)
    ).fetchone()[0]
    if transfer_date < max(emp["hire_date"], last or ""):
        return "Дата перевода не может быть раньше приёма или предыдущего перевода."
    con.execute(
        """INSERT INTO transfers
           (employee_id, transfer_date, from_department, from_position,
            to_department, to_position, order_no, order_date, article, created_by)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (emp_id, transfer_date, emp["department"], emp["position"],
         to_department, to_position, order_no, order_date, article, created_by),
    )
    con.execute("UPDATE employees SET department=?, position=? WHERE id=?",
                (to_department, to_position, emp_id))
    return None


def dismiss_reason_text(article, reason):
    return f"{article} — {reason}" if reason else article


def sign_order(con, order_id, signer):
    """Подписать приказ (о приёме, переводе или увольнении).

    Присваивает номер приказа и выполняет действие приказа: принимает,
    переводит или увольняет. signer — словарь: login, full_name, title.
    Всё одной операцией: если что-то не удалось, ничего не меняется и
    номер приказа не «сгорает».
    Возвращает (employee_id, None) или (None, текст ошибки).
    """
    import json
    import secrets
    con.execute("BEGIN IMMEDIATE")
    try:
        order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if order is None:
            con.execute("ROLLBACK")
            return None, "Приказ не найден."
        if order["status"] != "draft" or order["deleted_at"]:
            con.execute("ROLLBACK")
            return None, "Приказ уже подписан или недоступен для подписи."
        if con.execute("SELECT 1 FROM order_steps WHERE order_id=? AND action='rozilik' AND status<>'approved'",
                       (order_id,)).fetchone():
            con.execute("ROLLBACK")
            return None, "Приказ ещё не согласован всеми участниками."
        kind = order["kind"]
        d = json.loads(order["data"])
        now = now_local()
        order_date = now.date().isoformat()
        if kind not in ORDER_SETTING:
            con.execute("ROLLBACK")
            return None, "Неизвестный вид приказа."
        fmt = get_settings(con)[ORDER_SETTING[kind]]
        seq = next_counter(con, ORDER_COUNTER[kind].format(y=now.year))
        number = fmt.format(seq=seq, yy=f"{now.year % 100:02d}")
        if kind in ("hire", "transfer") and get_settings(con).get("strict_vacancy", "1") == "1":
            problem = vacancy_problem(con, kind, d)
            if problem:
                con.execute("ROLLBACK")
                return None, problem + " Подписать приказ нельзя: измените штатное расписание или приказ."
        if kind == "hire":
            try:
                emp_id = insert_employee(con, d, created_by=order["created_by"], hire_order_id=order_id)
            except sqlite3.IntegrityError:
                con.execute("ROLLBACK")
                return None, "Сотрудник с таким ПИНФЛ уже работает."
        elif kind == "transfer":
            emp_id = d["employee_id"]
            err = _apply_transfer(con, emp_id, d["to_department"], d["to_position"],
                                  d["transfer_date"], number, order_date, d["article"],
                                  created_by=order["created_by"])
            if err:
                con.execute("ROLLBACK")
                return None, err
        elif kind == "dismiss":
            emp_id = d["employee_id"]
            _, err = _apply_dismissal(con, emp_id, d["dismissal_date"],
                                      dismiss_reason_text(d["article"], d.get("reason", "")))
            if err:
                con.execute("ROLLBACK")
                return None, err
        elif kind == "dept_rename":
            emp_id = None
            err = apply_dept_rename(con, d)
            if err:
                con.execute("ROLLBACK")
                return None, err
        elif kind in DISCIPLINE:
            emp_id = d["employee_id"]
            emp = con.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
            if emp is None or emp["status"] != ACTIVE:
                con.execute("ROLLBACK")
                return None, "Сотрудник уже уволен или не найден."
            if kind == "discipline_lift":
                err = check_discipline_lift(con, d, order_id)
                if err:
                    con.execute("ROLLBACK")
                    return None, err
        elif kind in VAC_ADJ:
            emp_id = d["employee_id"]
            emp = con.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
            if emp is None or emp["status"] != ACTIVE:
                con.execute("ROLLBACK")
                return None, "Сотрудник уже уволен или не найден."
            err = check_vac_adjust(con, kind, d, order_id)
            if err:
                con.execute("ROLLBACK")
                return None, err
        elif kind in ABSENCE_KINDS:
            emp_id = d["employee_id"]
            emp = con.execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
            if emp is None or emp["status"] != ACTIVE:
                con.execute("ROLLBACK")
                return None, "Сотрудник уже уволен или не найден."
            clash = absence_conflict(con, emp_id, d["start_date"], d["end_date"], order_id)
            if clash:
                con.execute("ROLLBACK")
                return None, f"Даты пересекаются с приказом № {clash}."
        else:
            con.execute("ROLLBACK")
            return None, "Неизвестный вид приказа."
        tabel = con.execute("SELECT tabel_no FROM employees WHERE id=?", (emp_id,)).fetchone()[0] if emp_id else None
        con.execute(
            """UPDATE orders SET status='signed', number=?, order_date=?, employee_id=?,
                      signed_by=?, signed_by_name=?, signed_by_title=?, signed_at=?, token=?
               WHERE id=?""",
            (number, order_date, emp_id, signer["login"], signer["full_name"],
             signer["title"], now.strftime("%Y-%m-%d %H:%M"), secrets.token_urlsafe(18), order_id),
        )
        signed = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        steps = con.execute("SELECT * FROM order_steps WHERE order_id=? ORDER BY CASE action WHEN 'rozilik' THEN 0 WHEN 'ijro' THEN 1 ELSE 2 END, ord, id", (order_id,)).fetchall()
        con.execute("UPDATE orders SET content_hash=? WHERE id=?",
                    (order_hash(signed, tabel, steps), order_id))
        con.execute("COMMIT")
        return emp_id, None
    except Exception:
        con.execute("ROLLBACK")
        raise


def apply_dept_rename(con, d):
    """Переименовать подразделение (внутри открытой транзакции). Возвращает None или текст ошибки."""
    old, new = d["department"], d["new_name"]
    if con.execute("SELECT 1 FROM departments WHERE name=?", (old,)).fetchone() is None:
        return f"Подразделения «{old}» нет в штатном расписании (возможно, оно уже переименовано)."
    if not new or new == old:
        return "Новое название пустое или совпадает со старым."
    if con.execute("SELECT 1 FROM departments WHERE lower(name)=lower(?) AND name<>?", (new, old)).fetchone():
        return f"Подразделение «{new}» уже есть в штатном расписании."
    con.execute("UPDATE departments SET name=? WHERE name=?", (new, old))
    con.execute("UPDATE employees SET department=? WHERE department=? AND status=?", (new, old, ACTIVE))
    con.execute("UPDATE dept_heads SET department=? WHERE department=?", (new, old))
    return None


sign_hire_order = sign_order


def _add_days(iso, n):
    import datetime
    return (datetime.date.fromisoformat(iso) + datetime.timedelta(days=n)).isoformat()


def _days_between(a, b):
    """Календарных дней от a до b включительно."""
    import datetime
    return (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days + 1


def adjustments(con):
    """Подписанные продления и отзывы по каждому приказу об отпуске: {id приказа об отпуске: [(id, kind, number, data)]}."""
    import json
    out = {}
    for r in con.execute("SELECT id, kind, number, data FROM orders WHERE kind IN ('vac_extend','vac_recall') "
                         "AND status='signed' ORDER BY id"):
        d = json.loads(r["data"])
        out.setdefault(d.get("base_order_id"), []).append((r["id"], r["kind"], r["number"], d))
    return out


def effective(d, adj_list):
    """Данные приказа об отпуске с учётом продлений и отзывов: новая дата окончания и число дней."""
    d = dict(d)
    d["orig_end_date"], d["orig_days"] = d.get("end_date"), d.get("days")
    d["extended"] = d["recalled"] = False
    for _id, kind, _num, a in adj_list or []:
        if kind == "vac_extend":
            d["end_date"] = a["new_end_date"]
            d["days"] = int(d.get("days", 0)) + int(a.get("days_added", 0))
            d["extended"] = True
        else:
            d["end_date"] = _add_days(a["recall_date"], -1)
            d["days"] = max(int(d.get("days", 0)) - int(a.get("days_unused", 0)), 0)
            d["recalled"] = True
    return d


def absence_rows(con, kinds=("vacation", "trip"), emp_id=None, exclude_order_id=0):
    """Подписанные приказы об отпуске/командировке: [(строка заказа, данные с учётом продлений и отзывов)]."""
    import json
    adj = adjustments(con)
    q = "SELECT * FROM orders WHERE status='signed' AND kind IN (%s) AND id<>?" % ",".join("?" * len(kinds))
    args = list(kinds) + [exclude_order_id]
    if emp_id is not None:
        q += " AND employee_id=?"
        args.append(emp_id)
    out = []
    for r in con.execute(q + " ORDER BY id", args):
        d = json.loads(r["data"])
        out.append((r, effective(d, adj.get(r["id"])) if r["kind"] == "vacation" else d))
    return out


def absence_conflict(con, emp_id, start, end, exclude_order_id=0):
    """Номер подписанного приказа об отпуске/командировке, с которым пересекаются даты, или None."""
    for r, d in absence_rows(con, emp_id=emp_id, exclude_order_id=exclude_order_id):
        if d.get("days", 1) > 0 and d["start_date"] <= end and start <= d["end_date"]:
            return r["number"]
    return None


def leave_used(con, emp_id, year, vacation_type="annual", exclude_order_id=0):
    """Сколько календарных дней отпуска этого вида использовано (подписано) в году, с учётом продлений и отзывов."""
    total = 0
    for r, d in absence_rows(con, ("vacation",), emp_id, exclude_order_id):
        if d.get("vacation_type") == vacation_type and d["start_date"][:4] == str(year):
            total += int(d.get("days", 0))
    return total


def _add_months(iso, n):
    import calendar
    y, m, dd = int(iso[:4]), int(iso[5:7]), int(iso[8:10])
    m0 = m - 1 + n
    y, m = y + m0 // 12, m0 % 12 + 1
    return f"{y:04d}-{m:02d}-{min(dd, calendar.monthrange(y, m)[1]):02d}"


def discipline_rows(con, emp_id, today=None):
    """Дисциплинарные взыскания сотрудника по подписанным приказам:
    [{id, number, date, d (данные), until, lift (id, номер, дата снятия) или None, state: active|lifted|expired}]."""
    import json
    from datetime import date as _date
    today = today or _date.today().isoformat()
    lifts = {}
    for r in con.execute("SELECT id, number, order_date, data FROM orders WHERE kind='discipline_lift' AND status='signed' ORDER BY id"):
        lifts.setdefault(json.loads(r["data"]).get("base_order_id"), r)
    out = []
    for r in con.execute("SELECT * FROM orders WHERE kind='discipline' AND status='signed' AND employee_id=? ORDER BY id", (emp_id,)):
        until = _add_months(r["order_date"], DISCIPLINE_MONTHS)
        lf = lifts.get(r["id"])
        state = "lifted" if lf else ("expired" if until < today else "active")
        out.append({"id": r["id"], "number": r["number"], "date": r["order_date"], "d": json.loads(r["data"]), "until": until,
                    "lift": ({"id": lf["id"], "number": lf["number"], "date": lf["order_date"]} if lf else None), "state": state})
    return out


def check_discipline_lift(con, d, order_id=0):
    """Проверка приказа о снятии взыскания: текст ошибки или None."""
    import json
    base = con.execute("SELECT * FROM orders WHERE id=? AND kind='discipline' AND status='signed' AND employee_id=?",
                       (d.get("base_order_id"), d.get("employee_id"))).fetchone()
    if base is None:
        return "Приказ о взыскании не найден, не подписан или отменён."
    for r in con.execute("SELECT id, number, data FROM orders WHERE kind='discipline_lift' AND status='signed' AND id<>?", (order_id,)):
        if json.loads(r["data"]).get("base_order_id") == base["id"]:
            return f"Это взыскание уже снято приказом № {r['number']}."
    return None


def check_vac_adjust(con, kind, d, order_id=0):
    """Проверка продления или отзыва: текст ошибки или None. Вызывается при составлении и при подписи."""
    import json
    base = con.execute("SELECT * FROM orders WHERE id=? AND kind='vacation' AND status='signed' AND employee_id=?",
                       (d.get("base_order_id"), d.get("employee_id"))).fetchone()
    if base is None:
        return "Исходный приказ об отпуске не найден, не подписан или отменён."
    bd = effective(json.loads(base["data"]), [a for a in adjustments(con).get(base["id"], []) if a[0] != order_id])
    if kind == "vac_extend":
        new_end = d.get("new_end_date", "")
        if not new_end or new_end <= bd["end_date"]:
            return f"Новая дата окончания должна быть позже текущей ({bd['end_date']})."
        clash = absence_conflict(con, d["employee_id"], _add_days(bd["end_date"], 1), new_end, base["id"])
        if clash:
            return f"Продление пересекается с приказом № {clash}."
    else:
        rd = d.get("recall_date", "")
        if not rd or not (bd["start_date"] < rd <= bd["end_date"]):
            return f"Дата отзыва должна быть внутри отпуска: после {bd['start_date']} и не позже {bd['end_date']}."
    return None


# ---------- согласование ----------

STEP_ACTIONS = {"rozilik": "Согласие", "ijro": "На исполнение", "info": "К сведению"}


def steps_for(con, order_id):
    return con.execute("SELECT * FROM order_steps WHERE order_id=? ORDER BY CASE action WHEN 'rozilik' THEN 0 WHEN 'ijro' THEN 1 ELSE 2 END, ord, id", (order_id,)).fetchall()


def set_steps_from_group(con, order_id, group_id):
    """Заменить шаги приказа участниками группы. Возвращает название группы или ''. """
    con.execute("DELETE FROM order_steps WHERE order_id=?", (order_id,))
    group = con.execute("SELECT * FROM signer_groups WHERE id=?", (group_id,)).fetchone() if group_id else None
    if group is None:
        con.execute("UPDATE orders SET group_name='' WHERE id=?", (order_id,))
        return ""
    for m in con.execute(
            """SELECT m.*, u.full_name, u.title FROM signer_group_members m
               JOIN users u ON u.login = m.login WHERE m.group_id=? ORDER BY m.ord, m.id""", (group["id"],)):
        con.execute("INSERT INTO order_steps(order_id, ord, login, name, title, action) VALUES(?,?,?,?,?,?)",
                    (order_id, m["ord"], m["login"], m["full_name"] or m["login"], m["title"], m["action"]))
    con.execute("UPDATE orders SET group_name=? WHERE id=?", (group["name"], order_id))
    return group["name"]


def replace_steps(con, order_id, rows):
    """Задать участников приказа списком [(login, action[, queue[, due]])]. Уже согласовавшие остаются согласовавшими.

    queue — номер очереди согласия (меньше — раньше; одинаковые номера — одновременно); due — срок «ГГГГ-ММ-ДД».
    Возвращает текст ошибки или None.
    """
    old = {(x["login"], x["action"]): x for x in steps_for(con, order_id)}
    seen = set()
    clean = []
    for i, item in enumerate(rows):
        login_, action = item[0], item[1]
        queue = item[2] if len(item) > 2 else None
        due = item[3] if len(item) > 3 else ""
        if action not in STEP_ACTIONS:
            return "Неизвестное действие участника."
        if (login_, action) in seen:
            continue
        u = con.execute("SELECT login, full_name, title FROM users WHERE login=?", (login_,)).fetchone()
        if u is None:
            return f"Пользователь «{login_}» не найден."
        seen.add((login_, action))
        if action == "rozilik":
            ordn = queue if queue is not None else i + 1
        else:
            ordn = 0
        clean.append((u, action, ordn, due or ""))
    con.execute("DELETE FROM order_steps WHERE order_id=?", (order_id,))
    for u, action, ordn, due in clean:
        prev = old.get((u["login"], action))
        if prev is not None and prev["status"] == "approved":
            con.execute("INSERT INTO order_steps(order_id, ord, login, name, title, action, status, at, comment, due) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)", (order_id, ordn, u["login"], prev["name"], prev["title"], action,
                                                         "approved", prev["at"], prev["comment"], due))
        else:
            con.execute("INSERT INTO order_steps(order_id, ord, login, name, title, action, due) VALUES(?,?,?,?,?,?,?)",
                        (order_id, ordn, u["login"], u["full_name"] or u["login"], u["title"], action, due))
    return None


def reset_steps(con, order_id):
    con.execute("UPDATE order_steps SET status='pending', at=NULL, comment='' WHERE order_id=?", (order_id,))


def approve_step(con, order_id, login):
    """Согласующий подтверждает приказ. Возвращает текст ошибки или None."""
    order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if order is None or order["status"] != "draft" or order["deleted_at"]:
        return "Приказ сейчас не на согласовании."
    step = con.execute("SELECT * FROM order_steps WHERE order_id=? AND login=? AND status='pending' "
                       "AND action<>'info' ORDER BY ord, id LIMIT 1", (order_id, login)).fetchone()
    if step is None:
        return "Для вас нет шага согласования в этом приказе."
    if step["action"] == "rozilik":
        before = con.execute("SELECT name, ord FROM order_steps WHERE order_id=? AND action='rozilik' AND status<>'approved' "
                             "AND ord<? ORDER BY ord, id LIMIT 1", (order_id, step["ord"])).fetchone()
        if before is not None:
            return f"Пока рано: сначала должен согласовать {before['name']} (очередь {before['ord']})."
    me = con.execute("SELECT full_name, title FROM users WHERE login=?", (login,)).fetchone()
    con.execute("UPDATE order_steps SET status='approved', at=?, name=?, title=? WHERE id=?",
                (now_local().strftime("%Y-%m-%d %H:%M"), (me["full_name"] or login), me["title"], step["id"]))
    return None


def return_order(con, order_id, by_login, comment):
    """Вернуть проект кадровику на доработку. Возвращает текст ошибки или None."""
    order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if order is None or order["status"] != "draft" or order["deleted_at"]:
        return "Приказ сейчас не на согласовании."
    con.execute("UPDATE orders SET status='returned', return_comment=?, returned_by=? WHERE id=?",
                (comment, by_login, order_id))
    return None


def dismiss_employee(con, emp_id, dismissal_date, reason=""):
    """Уволить напрямую (без приказа; используется в тестах).

    Возвращает освобождённый номер папки или None, если сотрудник уже уволен.
    """
    con.execute("BEGIN IMMEDIATE")
    try:
        freed, err = _apply_dismissal(con, emp_id, dismissal_date, reason)
        con.execute("ROLLBACK" if err else "COMMIT")
        return None if err else freed
    except Exception:
        con.execute("ROLLBACK")
        raise


def transfer_employee(con, emp_id, to_department, to_position, transfer_date,
                      order_no, order_date, article, created_by=""):
    """Перевести напрямую (без приказа; используется в тестах). None или текст ошибки."""
    con.execute("BEGIN IMMEDIATE")
    try:
        err = _apply_transfer(con, emp_id, to_department, to_position, transfer_date,
                              order_no, order_date, article, created_by)
        con.execute("ROLLBACK" if err else "COMMIT")
        return err
    except Exception:
        con.execute("ROLLBACK")
        raise


# ---------- штатное расписание ----------

def dept_tree(con):
    """Подразделения деревом: [{id, name, code, parent_id, level, ids (сам и все вложенные)}] в порядке обхода."""
    rows = [dict(r) for r in con.execute("SELECT id, name, code, parent_id FROM departments")]
    ids = {r["id"] for r in rows}
    kids = {}
    for r in rows:
        kids.setdefault(r["parent_id"] if r["parent_id"] in ids else None, []).append(r)
    for v in kids.values():
        v.sort(key=lambda r: ((r["code"] or "~"), r["name"].lower()))
    out = []

    def walk(pid, level, seen):
        for r in kids.get(pid, []):
            if r["id"] in seen:
                continue
            r["level"] = level
            out.append(r)
            walk(r["id"], level + 1, seen | {r["id"]})
    walk(None, 0, set())
    by = {r["id"]: r for r in out}
    for r in out:
        r["ids"] = [r["id"]]
    for r in out:   # каждый вносит себя во всех родителей
        pid = r["parent_id"]
        while pid in by:
            by[pid]["ids"].append(r["id"])
            pid = by[pid]["parent_id"]
    return out


def vacancy(con, department, position):
    """(ставок, занято, свободно) по должности в подразделении; None, если такой должности нет в штатном расписании."""
    row = con.execute("SELECT p.rate FROM positions p JOIN departments d ON d.id=p.department_id "
                      "WHERE d.name=? AND p.name=?", (department, position)).fetchone()
    if row is None:
        return None
    busy = con.execute("SELECT COUNT(*) FROM employees WHERE status=? AND department=? AND position=?",
                       (ACTIVE, department, position)).fetchone()[0]
    return row["rate"], busy, round(row["rate"] - busy, 2)


def vacancy_problem(con, kind, d):
    """Текст проблемы со штатным расписанием для приказа о приёме или переводе, иначе None."""
    if kind == "hire":
        dep, pos = d.get("department"), d.get("position")
    elif kind == "transfer":
        dep, pos = d.get("to_department"), d.get("to_position")
        cur = con.execute("SELECT department, position FROM employees WHERE id=?", (d.get("employee_id"),)).fetchone()
        if cur and cur["department"] == dep and cur["position"] == pos:
            return None    # сотрудник остаётся на той же должности: ставка не занимается заново
    else:
        return None
    if not dep or not pos:
        return None
    v = vacancy(con, dep, pos)
    if v is None:
        return f"Должности «{pos}» в подразделении «{dep}» нет в штатном расписании."
    if v[2] <= 0:
        return f"В штатном расписании нет свободной ставки: «{pos}», «{dep}» — ставок {v[0]:g}, занято {v[1]}."
    return None


def staff_map(con):
    """{подразделение: [должности]} — для формы приёма."""
    result = {}
    for d in con.execute("SELECT id, name FROM departments ORDER BY name"):
        result[d["name"]] = [
            p["name"] for p in con.execute(
                "SELECT name FROM positions WHERE department_id=? ORDER BY name", (d["id"],))
        ]
    return result


# ---------- тестовый штат (из файла Joylashuv) ----------

DEMO_FILE = BASE_DIR / "demo_staff.json"
DEMO_USER = "demo"


def demo_loaded(con):
    return con.execute("SELECT COUNT(*) FROM employees WHERE created_by=?", (DEMO_USER,)).fetchone()[0]


def load_demo(con):
    """Загрузить тестовое штатное расписание и придуманных сотрудников.

    Возвращает (принято, уволено, переведено) или None, если уже загружено.
    """
    import json
    if demo_loaded(con):
        return None
    data = json.loads(DEMO_FILE.read_text(encoding="utf-8"))
    con.execute("BEGIN IMMEDIATE")
    try:
        for p in data["positions"]:
            con.execute("INSERT OR IGNORE INTO departments(name) VALUES(?)", (p["department"],))
            did = con.execute("SELECT id FROM departments WHERE name=?", (p["department"],)).fetchone()[0]
            ex = con.execute("SELECT id FROM positions WHERE department_id=? AND name=?",
                             (did, p["position"])).fetchone()
            if ex is None:
                con.execute("INSERT INTO positions(department_id, name, rate, category) VALUES(?,?,?,?)",
                            (did, p["position"], p["rate"], p["category"]))
        # старые приёмы идут первыми, чтобы табельные номера росли по датам
        jobs = []
        for p in data["positions"]:
            for e in p["people"]:
                jobs.append((e["hire_date"], "active", p, e))
        for e in data["dismissed"]:
            jobs.append((e["hire_date"], "dismissed", e, e))
        jobs.sort(key=lambda j: (j[0], j[1]))
        hired = dismissed = moved = 0
        for _, kind, p, e in jobs:
            start_pos = e.get("transfer", {}).get("from_position") if kind == "active" else None
            d = dict(e, department=p["department"], position=start_pos or p["position"])
            emp_id = insert_employee(con, d, DEMO_USER)
            hired += 1
            if kind == "active" and e.get("transfer"):
                tr = e["transfer"]
                err = _apply_transfer(con, emp_id, p["department"], p["position"], tr["date"],
                                      f"T-{emp_id}", tr["date"], "MK 90-modda", DEMO_USER)
                moved += 0 if err else 1
            if kind == "dismissed":
                _apply_dismissal(con, emp_id, e["dismissal_date"], dismiss_reason_text(e["article"], e["reason"]))
                dismissed += 1
        con.execute("COMMIT")
        return hired, dismissed, moved
    except Exception:
        con.execute("ROLLBACK")
        raise


def remove_demo(con):
    """Удалить тестовых сотрудников и вернуть счётчики номеров. Возвращает число удалённых."""
    import json
    con.execute("BEGIN IMMEDIATE")
    try:
        ids = [r[0] for r in con.execute("SELECT id FROM employees WHERE created_by=?", (DEMO_USER,))]
        for i in ids:
            con.execute("DELETE FROM transfers WHERE employee_id=?", (i,))
            con.execute("DELETE FROM employees WHERE id=?", (i,))
        data = json.loads(DEMO_FILE.read_text(encoding="utf-8"))
        for p in data["positions"]:
            row = con.execute("SELECT p.id FROM positions p JOIN departments d ON d.id=p.department_id "
                              "WHERE d.name=? AND p.name=?", (p["department"], p["position"])).fetchone()
            if row and not con.execute("SELECT 1 FROM employees WHERE department=? AND position=?",
                                       (p["department"], p["position"])).fetchone():
                con.execute("DELETE FROM positions WHERE id=?", (row["id"],))
        names = {p["department"] for p in data["positions"]}
        for r in con.execute("SELECT id, name FROM departments").fetchall():
            if r["name"] in names \
               and not con.execute("SELECT 1 FROM positions WHERE department_id=?", (r["id"],)).fetchone() \
               and not con.execute("SELECT 1 FROM employees WHERE department=?", (r["name"],)).fetchone():
                con.execute("DELETE FROM departments WHERE id=?", (r["id"],))
        mx = 0
        for r in con.execute("SELECT tabel_no FROM employees"):
            if r[0].isdigit():
                mx = max(mx, int(r[0]))
        set_counter(con, "tabel", mx)
        years = {}
        for r in con.execute("SELECT contract_no FROM employees"):
            n, _, yy = r[0].partition("/")
            if n.isdigit():
                years[yy] = max(years.get(yy, 0), int(n))
        for r in con.execute("SELECT key FROM counters WHERE key LIKE 'contract:%'").fetchall():
            set_counter(con, r[0], years.get(r[0].split(":", 1)[1], 0))
        con.execute("COMMIT")
        return len(ids)
    except Exception:
        con.execute("ROLLBACK")
        raise
