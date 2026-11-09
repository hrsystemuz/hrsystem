"""Скачивание документов в PDF и «пакетом» (приказ + основания) одним файлом.
PDF делает браузер Edge/Chrome, который уже есть на компьютере (в скрытом режиме), поэтому вид документа тот же, что на экране."""
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from flask import abort, flash, redirect, request, send_file, session, url_for

import db
import extras
from extras import login_required, route

M = None
IMG_EXT = {"jpg", "jpeg", "png"}


def setup(app_module):
    global M
    M = app_module


def find_browser():
    env = os.environ.get("HR_BROWSER")
    if env and Path(env).exists():
        return env
    cands = []
    if sys.platform.startswith("win"):
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if base:
                cands += [Path(base) / "Microsoft/Edge/Application/msedge.exe", Path(base) / "Google/Chrome/Application/chrome.exe"]
    for n in ("msedge", "google-chrome", "chromium", "chromium-browser", "chrome"):
        p = shutil.which(n)
        if p:
            cands.append(Path(p))
    for c in cands:
        if c.exists():
            return str(c)
    return None


class PdfError(Exception):
    pass


def html_to_pdf(html):
    exe = find_browser()
    if not exe:
        raise PdfError("На компьютере не найден браузер Edge или Chrome, который делает PDF. Используйте кнопку «Печать» и выберите «Сохранить как PDF».")
    tmp = Path(tempfile.mkdtemp(prefix="hrpdf_"))
    try:
        src, out = tmp / "page.html", tmp / "out.pdf"
        src.write_text(html, encoding="utf-8")
        cmd = [exe, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--print-to-pdf-no-header",
               "--run-all-compositor-stages-before-draw", "--no-first-run", "--disable-background-networking", "--disable-extensions", f"--user-data-dir={tmp / 'prof'}", f"--print-to-pdf={out}"]
        if not sys.platform.startswith("win") and hasattr(os, "geteuid") and os.geteuid() == 0:
            cmd.append("--no-sandbox")
        cmd.append(src.as_uri())
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        last, stable, t0 = -1, 0, time.time()
        while time.time() - t0 < 60:                       # ждём файл; браузер может не закрыться сам — тогда закрываем
            time.sleep(0.3)
            size = out.stat().st_size if out.exists() else -1
            if proc.poll() is not None:
                break
            stable = stable + 1 if (size > 0 and size == last) else 0
            last = size
            if stable >= 4:
                break
        if proc.poll() is None:
            proc.kill()
        if not out.exists() or out.stat().st_size < 200:
            raise PdfError("Не удалось создать PDF. Закройте лишние окна браузера и повторите или используйте «Печать → Сохранить как PDF».")
        return out.read_bytes()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _page(path):
    """HTML страницы с правами текущего пользователя (внутренний запрос)."""
    if not path.startswith("/") or path.startswith("//") or path.startswith("/pdf"):
        abort(400)
    c = M.app.test_client()
    with c.session_transaction() as s:
        s.update(dict(session))
    r = c.get(path)
    if r.status_code != 200:
        abort(r.status_code if r.status_code in (403, 404) else 400)
    return r.get_data(as_text=True)


def _title(html, default):
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    t = re.sub(r"\s+", " ", m.group(1)).strip() if m else default
    t = re.sub(r"[\\/:*?\"<>|]", "_", t).strip(" .") or default
    return t[:80]


def _back(path):
    return redirect(request.referrer or path)


def _file_to_pdf(path, ext):
    """Вложение -> PDF-байты, либо None, если конвертировать нельзя."""
    if ext == "pdf":
        return path.read_bytes()
    if ext in IMG_EXT:
        try:
            from PIL import Image
            im = Image.open(path)
            im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "PDF", resolution=150.0)
            return buf.getvalue()
        except Exception:                                                      # noqa: BLE001
            return None
    return None


def _merge(parts):
    from pypdf import PdfWriter
    w = PdfWriter()
    for b in parts:
        w.append(io.BytesIO(b))
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def _package(main_pdfs, files, name):
    """Склеить основной PDF и вложения. Вложения, которые нельзя склеить (Word, Excel), кладутся в ZIP рядом."""
    parts = list(main_pdfs)
    extra = []
    for f in files:
        p = extras.files_dir() / f["stored"]
        ext = f["filename"].rsplit(".", 1)[-1].lower() if "." in f["filename"] else ""
        b = _file_to_pdf(p, ext) if p.exists() else None
        if b is not None:
            try:
                _merge([b])
                parts.append(b)
                continue
            except Exception:                                                  # noqa: BLE001
                pass
        if p.exists():
            extra.append((f["filename"], p))
    try:
        merged = _merge(parts)
    except Exception:                                                          # noqa: BLE001
        raise PdfError("Не удалось склеить файлы в один PDF.")
    if not extra:
        return io.BytesIO(merged), name + ".pdf", "application/pdf"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name + ".pdf", merged)
        for i, (fn, p) in enumerate(extra, 1):
            z.write(p, f"{i:02d}_{extras._clean_name(fn)}")
    buf.seek(0)
    return buf, name + ".zip", "application/zip"


@route("/pdf")
@login_required
def pdf_page():
    path = request.args.get("u", "")
    html = _page(path)
    try:
        data = html_to_pdf(html)
    except PdfError as e:
        flash(str(e), "error")
        return _back(path)
    return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=_title(html, "document") + ".pdf")


@route("/pdf/order/<int:order_id>/package")
@login_required
def pdf_order_package(order_id):
    path = url_for("order_view", order_id=order_id)
    html = _page(path)
    con = db.connect()
    try:
        o = con.execute("SELECT number, app_id FROM orders WHERE id=?", (order_id,)).fetchone()
        files = con.execute("SELECT * FROM order_files WHERE order_id=? ORDER BY id", (order_id,)).fetchall()
    finally:
        con.close()
    try:
        parts = [html_to_pdf(html)]
        if o["app_id"]:
            try:
                parts.append(html_to_pdf(_page(url_for("ap_view", app_id=o["app_id"]))))
            except Exception:                                                  # нет доступа к заявлению — пакет без него
                pass
        buf, fn, mt = _package(parts, files, "Paket_" + extras._clean_name(o["number"] or f"order{order_id}"))
    except PdfError as e:
        flash(str(e), "error")
        return _back(path)
    return send_file(buf, mimetype=mt, as_attachment=True, download_name=fn)


@route("/pdf/gd/<int:doc_id>/package")
@login_required
def pdf_gd_package(doc_id):
    path = url_for("gd_view", doc_id=doc_id)
    html = _page(path)
    con = db.connect()
    try:
        files = con.execute("SELECT * FROM gdoc_files WHERE doc_id=? ORDER BY id", (doc_id,)).fetchall()
    finally:
        con.close()
    try:
        buf, fn, mt = _package([html_to_pdf(html)], files, "Paket_" + extras._clean_name(_title(html, f"doc{doc_id}")))
    except PdfError as e:
        flash(str(e), "error")
        return _back(path)
    return send_file(buf, mimetype=mt, as_attachment=True, download_name=fn)
