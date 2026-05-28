import os
import sqlite3
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, abort, flash, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "app.db"
STORAGE_DIR = BASE_DIR / "storage"
UPLOAD_DIR = STORAGE_DIR / "uploads"
TRASH_DIR = STORAGE_DIR / "trash"

DEFAULT_QUOTA_MB = 1024

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret")


def get_fernet() -> Optional[Fernet]:
    key = os.environ.get("ENCRYPTION_KEY", "").strip()
    if not key:
        return None
    try:
        return Fernet(key.encode("utf-8"))
    except (ValueError, TypeError):
        return None


def ensure_dirs() -> None:
    for directory in (DATA_DIR, STORAGE_DIR, UPLOAD_DIR, TRASH_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    ensure_dirs()
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                size INTEGER NOT NULL,
                uploaded_at TEXT NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        cur = conn.execute("SELECT value FROM settings WHERE key = 'quota_bytes'")
        if cur.fetchone() is None:
            quota_bytes = DEFAULT_QUOTA_MB * 1024 * 1024
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                ("quota_bytes", str(quota_bytes)),
            )
        ensure_files_table_columns(conn)


def ensure_files_table_columns(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(files)").fetchall()
    }
    if "is_encrypted" not in columns:
        conn.execute(
            "ALTER TABLE files ADD COLUMN is_encrypted INTEGER NOT NULL DEFAULT 0"
        )


def get_quota_bytes(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT value FROM settings WHERE key = 'quota_bytes'")
    row = cur.fetchone()
    if row is None:
        return DEFAULT_QUOTA_MB * 1024 * 1024
    return int(row["value"])


def set_quota_bytes(conn: sqlite3.Connection, quota_bytes: int) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('quota_bytes', ?)"
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(quota_bytes),),
    )


def get_used_bytes(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "SELECT COALESCE(SUM(size), 0) AS used FROM files WHERE status = 'active'"
    )
    return int(cur.fetchone()["used"])


def format_bytes(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KB"
    if num < 1024 * 1024 * 1024:
        return f"{num / (1024 * 1024):.1f} MB"
    return f"{num / (1024 * 1024 * 1024):.2f} GB"


app.jinja_env.filters["bytes"] = format_bytes


@app.route("/")
def index():
    with get_db() as conn:
        files = conn.execute(
            "SELECT id, original_name, size, uploaded_at, is_encrypted FROM files "
            "WHERE status = 'active' ORDER BY uploaded_at DESC"
        ).fetchall()
        used_bytes = get_used_bytes(conn)
        quota_bytes = get_quota_bytes(conn)
    encryption_available = get_fernet() is not None
    remaining_bytes = max(quota_bytes - used_bytes, 0)
    return render_template(
        "index.html",
        files=files,
        used_bytes=used_bytes,
        quota_bytes=quota_bytes,
        remaining_bytes=remaining_bytes,
        encryption_available=encryption_available,
    )


@app.route("/set-quota", methods=["POST"])
def set_quota():
    quota_mb = request.form.get("quota_mb", type=int)
    if quota_mb is None or quota_mb <= 0:
        flash("Dung luong khong hop le.", "error")
        return redirect(url_for("index"))
    quota_bytes = quota_mb * 1024 * 1024
    with get_db() as conn:
        set_quota_bytes(conn, quota_bytes)
    flash("Da cap nhat dung luong.", "success")
    return redirect(url_for("index"))


@app.route("/upload", methods=["POST"])
def upload():
    upload_file = request.files.get("file")
    if upload_file is None or upload_file.filename == "":
        flash("Vui long chon file.", "error")
        return redirect(url_for("index"))

    original_name = secure_filename(upload_file.filename)
    if not original_name:
        flash("Ten file khong hop le.", "error")
        return redirect(url_for("index"))

    encrypt_requested = request.form.get("encrypt") == "on"
    fernet = None
    if encrypt_requested:
        fernet = get_fernet()
        if fernet is None:
            flash("Chua co khoa ma hoa hop le.", "error")
            return redirect(url_for("index"))

    stored_name = uuid.uuid4().hex
    target_path = UPLOAD_DIR / stored_name
    if fernet is None:
        upload_file.save(target_path)
    else:
        plaintext = upload_file.read()
        token = fernet.encrypt(plaintext)
        target_path.write_bytes(token)
    size = target_path.stat().st_size

    with get_db() as conn:
        used_bytes = get_used_bytes(conn)
        quota_bytes = get_quota_bytes(conn)
        if used_bytes + size > quota_bytes:
            target_path.unlink(missing_ok=True)
            flash("Vuot qua dung luong cho phep.", "error")
            return redirect(url_for("index"))

        conn.execute(
            "INSERT INTO files (id, original_name, stored_name, size, uploaded_at, status) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?)",
            (
                uuid.uuid4().hex,
                original_name,
                stored_name,
                size,
                datetime.utcnow().isoformat(timespec="seconds") + "Z",
                1 if fernet is not None else 0,
            ),
        )

    flash("Upload thanh cong.", "success")
    return redirect(url_for("index"))


@app.route("/download/<file_id>")
def download(file_id: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT stored_name, original_name, status, is_encrypted FROM files WHERE id = ?",
            (file_id,),
        ).fetchone()
    if row is None or row["status"] != "active":
        abort(404)
    file_path = UPLOAD_DIR / row["stored_name"]
    if not file_path.exists():
        abort(404)
    if row["is_encrypted"] == 1:
        fernet = get_fernet()
        if fernet is None:
            abort(500)
        try:
            plaintext = fernet.decrypt(file_path.read_bytes())
        except InvalidToken:
            abort(500)
        return send_file(
            BytesIO(plaintext),
            as_attachment=True,
            download_name=row["original_name"],
            mimetype="application/octet-stream",
        )
    return send_file(file_path, as_attachment=True, download_name=row["original_name"])


@app.route("/delete/<file_id>", methods=["POST"])
def move_to_trash(file_id: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT stored_name, status FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        if row is None or row["status"] != "active":
            abort(404)
        source_path = UPLOAD_DIR / row["stored_name"]
        target_path = TRASH_DIR / row["stored_name"]
        if source_path.exists():
            source_path.replace(target_path)
        conn.execute("UPDATE files SET status = 'trash' WHERE id = ?", (file_id,))
    flash("Da chuyen vao thung rac.", "success")
    return redirect(url_for("index"))


@app.route("/trash")
def trash():
    with get_db() as conn:
        files = conn.execute(
            "SELECT id, original_name, size, uploaded_at, is_encrypted FROM files "
            "WHERE status = 'trash' ORDER BY uploaded_at DESC"
        ).fetchall()
    return render_template("trash.html", files=files)


@app.route("/restore/<file_id>", methods=["POST"])
def restore(file_id: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT stored_name, size, status FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        if row is None or row["status"] != "trash":
            abort(404)
        used_bytes = get_used_bytes(conn)
        quota_bytes = get_quota_bytes(conn)
        if used_bytes + row["size"] > quota_bytes:
            flash("Khong du dung luong de khoi phuc.", "error")
            return redirect(url_for("trash"))
        source_path = TRASH_DIR / row["stored_name"]
        target_path = UPLOAD_DIR / row["stored_name"]
        if source_path.exists():
            source_path.replace(target_path)
        conn.execute("UPDATE files SET status = 'active' WHERE id = ?", (file_id,))
    flash("Da khoi phuc file.", "success")
    return redirect(url_for("trash"))


@app.route("/purge/<file_id>", methods=["POST"])
def purge(file_id: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT stored_name, status FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        if row is None or row["status"] != "trash":
            abort(404)
        file_path = TRASH_DIR / row["stored_name"]
        if file_path.exists():
            file_path.unlink()
        conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
    flash("Da xoa vinh vien.", "success")
    return redirect(url_for("trash"))


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000)
