"""Главная со статистикой и единый список документов (приказы + заявления)."""
import json
import re

from flask import redirect, render_template, request, session, url_for

import db
import extras
from extras import login_required, route

M = None   # модуль app (его задаёт setup)


def setup(app_module):
    global M
    M = app_module

    @M.app.context_processor
    def _org():
        if "user" not in session:
            return {}
        con = db.connect()
        try:
            s = db.get_settings(con)
            return {"org_title": (s["org_name"] or "").strip()}
        finally:
            con.close()


# статус документа -> (подпись, цвет)
CHIPS = {
    "new": ("Черновик", "grey"),
    "work": ("В процессе", "purple"),
    "back": ("На доработке", "blue"),
    "done": ("Подписан / исполнен", "green"),
    "no": ("Отказано / отменено", "red"),
}


def _order_state(o):
    return {"signed": "done", "cancelled": "no", "returned": "back"}.get(o["status"], "work")


def _app_state(a):
    return {"draft": "new", "review": "work", "returned": "back", "approved": "work",
            "rejected": "no", "done": "done"}.get(a["status"], "work")


def _short(text, n=700):
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "…"


def _plain(text, n=20000):
    """HTML или текст -> простой текст с абзацами (для просмотра и поиска по тексту)."""
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h\d>|</tr>", "\n", text or "")
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"&nbsp;", " ", t)
    t = "\n".join(re.sub(r"[ \t]+", " ", ln).strip() for ln in t.splitlines())
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t[:n]


def _tabel(con, o):
    if not o["employee_id"]:
        return None
    r = con.execute("SELECT tabel_no FROM employees WHERE id=?", (o["employee_id"],)).fetchone()
    return r["tabel_no"] if r else None


def collect(con):
    """Все документы, доступные пользователю, в одном списке (новые сверху)."""
    import appl
    role, me = session.get("role"), session["user"]
    mine_orders = M._my_queue(con) if role != "employee" else set()
    items = []
    if role != "employee":
        rows = con.execute("SELECT * FROM orders WHERE deleted_at IS NULL").fetchall()
        for it in M._order_items(rows):
            o, d = it["o"], it["d"]
            if not M._order_visible(con, o):
                continue
            st = _order_state(o)
            try:
                preview = d.get("custom_text") or M.default_text(o["kind"], d, it["tabel"] or _tabel(con, o))
            except Exception:                                                  # noqa: BLE001
                preview = (f"{d.get('position', '')} {d.get('department', '')}".strip()
                           + (f" → {d.get('to_position', '')} {d.get('to_department', '')}".rstrip()
                              if d.get("to_position") or d.get("to_department") else ""))
            items.append({
                "src": "order", "id": o["id"], "number": o["number"] or f"проект №{o['id']}",
                "date": (o["order_date"] or o["created_at"][:10]), "sort": o["created_at"],
                "title": "Приказ " + M.KIND_RU[o["kind"]].lower(), "subject": it["fio"],
                "author": o["created_by_name"], "dept": d.get("department", ""),
                "state": st, "mine": o["id"] in mine_orders,
                "url": url_for("order_view", order_id=o["id"]),
                "preview": _plain(preview), "kind": o["kind"]})
    ap_rows = con.execute("SELECT * FROM applications WHERE status<>'draft' OR author=?", (me,)).fetchall()
    for a in ap_rows:
        if not appl.can_see(con, a):
            continue
        e = con.execute("SELECT * FROM employees WHERE id=?", (a["employee_id"],)).fetchone()
        cur = appl.current_step(con, a["id"]) if a["status"] == "review" else None
        mine = bool(cur and cur["login"] == me) or (a["status"] == "returned" and a["author"] == me) \
            or (a["status"] == "approved" and role in ("hr", "admin"))
        items.append({
            "src": "app", "id": a["id"], "number": a["number"] or f"черновик №{a['id']}",
            "date": (a["submitted_at"] or a["created_at"])[:10], "sort": a["submitted_at"] or a["created_at"],
            "title": "Заявление: " + a["type_name"], "subject": appl.fio_of(e) if e else "?",
            "author": a["author"], "dept": e["department"] if e else "",
            "state": _app_state(a), "mine": mine, "url": url_for("ap_view", app_id=a["id"]),
            "preview": _plain(a["body"]), "kind": "app"})
    if role != "employee":
        import gdoc
        for g in con.execute("SELECT * FROM gdocs WHERE status<>'draft' OR author=?", (me,)).fetchall():
            if not gdoc.can_see(con, g):
                continue
            au = con.execute("SELECT full_name FROM users WHERE login=?", (g["author"],)).fetchone()
            items.append({
                "src": "gdoc", "id": g["id"], "number": g["number"] or f"черновик №{g['id']}",
                "date": (g["finished_at"] or g["submitted_at"] or g["created_at"])[:10],
                "sort": g["submitted_at"] or g["created_at"], "title": f"{g['kind']}: {g['title']}",
                "subject": g["title"], "author": (au["full_name"] if au and au["full_name"] else g["author"]),
                "dept": "", "state": {"draft": "new", "review": "work", "returned": "back", "signed": "done",
                                      "done": "done", "rejected": "no"}.get(g["status"], "work"),
                "mine": gdoc.waiting_for_me(con, g), "url": url_for("gd_view", doc_id=g["id"]),
                "preview": _plain("\n\n".join(json.loads(g["items"]))), "kind": "gdoc"})
    items.sort(key=lambda i: (i["sort"], i["id"]), reverse=True)
    return items


@route("/")
@login_required
def home():
    if session.get("role") == "employee":
        return redirect(url_for("ap_my"))
    con = db.connect()
    try:
        items = collect(con)
        emp_active = con.execute("SELECT COUNT(*) FROM employees WHERE status=?", (db.ACTIVE,)).fetchone()[0]
        emp_gone = con.execute("SELECT COUNT(*) FROM employees WHERE status=?", (db.DISMISSED,)).fetchone()[0]
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        con.close()
    orders = [i for i in items if i["src"] == "order"]
    apps = [i for i in items if i["src"] == "app"]

    def cnt(lst, *states):
        return sum(1 for i in lst if i["state"] in states)

    cards = [
        {"title": "Ждут вашего действия", "big": sum(1 for i in items if i["mine"]),
         "sub": len(items), "subtxt": "всего документов", "url": url_for("docs", scope="mine")},
        {"title": "Приказы в работе", "big": cnt(orders, "work", "new"),
         "sub": len(orders), "subtxt": "всего приказов", "url": url_for("orders")},
        {"title": "Заявления в работе", "big": cnt(apps, "work", "new"),
         "sub": len(apps), "subtxt": "всего заявлений", "url": url_for("docs", src="app")},
        {"title": "Возвращены на доработку", "big": cnt(items, "back"),
         "sub": cnt(items, "no"), "subtxt": "отказано / отменено", "url": url_for("docs", state="back")},
    ]
    # данные для круговых диаграмм
    def donut(lst):
        total = max(len(lst), 1)
        parts, acc = [], 0
        for key, (label, color) in CHIPS.items():
            n = cnt(lst, key)
            if n:
                parts.append({"label": label, "color": color, "n": n, "from": acc / total * 100,
                              "len": n / total * 100})
                acc += n
        return parts
    return render_template("home.html", cards=cards, donut_all=donut(items), donut_orders=donut(orders),
                           recent=items[:8], emp_active=emp_active, emp_gone=emp_gone, users=users,
                           n_orders=len(orders), n_apps=len(apps), CHIPS=CHIPS)


@route("/documents")
@login_required
def docs():
    if session.get("role") == "employee":
        return redirect(url_for("ap_my"))
    q = " ".join(request.args.get("q", "").lower().split())
    scope = request.args.get("scope", "")
    src = request.args.get("src", "")
    state = request.args.get("state", "")
    department = request.args.get("department", "")
    num = request.args.get("num", "").strip().lower()
    subj = request.args.get("subject", "").strip().lower()
    by = request.args.get("by", "").strip().lower()
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    if request.args.get("date"):
        date_from = date_to = request.args["date"]
    if state == "mine":
        scope, state = "mine", ""
    con = db.connect()
    try:
        items = collect(con)
        deps = [r[0] for r in con.execute("SELECT name FROM departments ORDER BY name")]
    finally:
        con.close()
    total = len(items)
    mine_count = sum(1 for i in items if i["mine"])
    if scope == "mine":
        items = [i for i in items if i["mine"]]
    if src:
        items = [i for i in items if i["src"] == src]
    if state:
        items = [i for i in items if i["state"] == state]
    if department:
        items = [i for i in items if i["dept"] == department]
    if num:
        items = [i for i in items if num in i["number"].lower()]
    if subj:
        items = [i for i in items if subj in i["subject"].lower()]
    if by:
        items = [i for i in items if by in i["author"].lower()]
    if date_from:
        items = [i for i in items if i["date"] >= date_from]
    if date_to:
        items = [i for i in items if i["date"] <= date_to]
    if q:                                    # поиск по всему тексту: номер, заголовок, сотрудник, автор и сам текст документа
        items = [i for i in items if all(w in " ".join([i["number"], i["title"], i["subject"], i["author"], i["dept"], i["preview"]]).lower()
                                         for w in q.split())]
    shown = len(items)
    page = max(request.args.get("page", 1, type=int), 1)
    per = 20
    pages = max((shown + per - 1) // per, 1)
    page = min(page, pages)
    items = items[(page - 1) * per: page * per]
    args = {k: v for k, v in request.args.items() if k != "page"}
    return render_template("docs.html", items=items, CHIPS=CHIPS, deps=deps, q=request.args.get("q", ""),
                           scope=scope, src=src, state=state, department=department, total=total, num=num, subj=subj, by=by,
                           date_from=date_from, date_to=date_to, qwords=q.split(),
                           shown=shown, mine_count=mine_count, page=page, pages=pages, args=args,
                           filtered=any([q, scope, src, state, department, num, subj, by, date_from, date_to]))
