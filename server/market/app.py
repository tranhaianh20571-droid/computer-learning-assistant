"""Standalone character-card market service.

Accounts, email verification, device binding, card packages, downloads and the
72-hour top list live here. Learning data and model credentials never reach it.
"""
from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

DATA_DIR = Path(os.environ.get("MARKET_DATA_DIR", "/opt/studypal-market/data"))
WEB_DIR = Path(os.environ.get("MARKET_WEB_DIR", str(Path(__file__).resolve().parent / "web")))
DB_PATH = DATA_DIR / "market.db"
STORAGE = DATA_DIR / "cards"
BASE_URL = os.environ.get("MARKET_BASE_URL", "https://studypal.tailf6f37a.ts.net").rstrip("/")
SESSION_COOKIE = "market_session"
MAX_PACKAGE = 128 * 1024 * 1024
MAX_ENTRY = 32 * 1024 * 1024
MAX_ENTRIES = 64
MAX_COVER = 8 * 1024 * 1024
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
PASSWORD_MIN = 8
PASSWORD_MAX = 128
VERIFY_TTL = timedelta(hours=24)
CODE_TTL = timedelta(minutes=15)
CODE_MAX_ATTEMPTS = 6
USERNAME_MAX = 24
BIND_TTL = timedelta(minutes=10)
SESSION_TTL = timedelta(days=30)
_ph = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2)
_attempts: dict[str, list[float]] = {}

app = FastAPI(title="学伴市集", docs_url=None, redoc_url=None, openapi_url=None)


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def rate(key: str, limit: int, window: float) -> None:
    current = time.time()
    bucket = [item for item in _attempts.get(key, []) if item > current - window]
    if len(bucket) >= limit:
        raise HTTPException(429, detail={"error_code": "RATE_LIMITED", "message": "操作过于频繁，请稍后再试。"})
    bucket.append(current)
    _attempts[key] = bucket


@contextmanager
def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    STORAGE.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
            display_name TEXT NOT NULL DEFAULT '', verified INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS email_tokens (
            token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at TEXT NOT NULL,
            used_at TEXT
        );
        CREATE TABLE IF NOT EXISTS email_codes (
            email TEXT PRIMARY KEY, user_id TEXT NOT NULL, code_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS devices (
            token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, last_seen TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS web_login_tickets (
            token_hash TEXT PRIMARY KEY, device_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bindings (
            code_hash TEXT PRIMARY KEY, user_id TEXT, confirmed INTEGER NOT NULL DEFAULT 0,
            used INTEGER NOT NULL DEFAULT 0, expires_at TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS cards (
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, title TEXT NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]', cover_name TEXT NOT NULL,
            package_name TEXT NOT NULL, package_sha TEXT NOT NULL, size INTEGER NOT NULL,
            published INTEGER NOT NULL DEFAULT 1, official INTEGER NOT NULL DEFAULT 0,
            summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(owner_id, package_sha)
        );
        CREATE TABLE IF NOT EXISTS import_jobs (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, card_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', message TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_import_jobs_user ON import_jobs(user_id, status);
        CREATE TABLE IF NOT EXISTS downloads (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, card_id TEXT NOT NULL,
            day TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(user_id, card_id, day)
        );
        CREATE INDEX IF NOT EXISTS idx_downloads_card_time ON downloads(card_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_cards_created ON cards(created_at);
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
            event TEXT NOT NULL, actor_id TEXT, object_id TEXT NOT NULL, result TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS audit_logs_no_update BEFORE UPDATE ON audit_logs
        BEGIN SELECT RAISE(ABORT, 'append only'); END;
        CREATE TRIGGER IF NOT EXISTS audit_logs_no_delete BEFORE DELETE ON audit_logs
        BEGIN SELECT RAISE(ABORT, 'append only'); END;
        """)
        # Usernames are public identifiers: enforce case-insensitive uniqueness.
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_display_name "
                         "ON users(lower(display_name)) WHERE display_name <> ''")
        except sqlite3.IntegrityError:
            print("username uniqueness skipped: existing duplicates in users")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(cards)")}
        if "official" not in columns:
            conn.execute("ALTER TABLE cards ADD COLUMN official INTEGER NOT NULL DEFAULT 0")
        if "summary" not in columns:
            conn.execute("ALTER TABLE cards ADD COLUMN summary TEXT NOT NULL DEFAULT ''")
        for table, column, definition in (
            ("devices", "purpose", "TEXT NOT NULL DEFAULT 'full'"),
            ("devices", "receiving", "INTEGER NOT NULL DEFAULT 1"),
            ("bindings", "purpose", "TEXT NOT NULL DEFAULT 'login'"),
            ("import_jobs", "receiver", "TEXT NOT NULL DEFAULT ''"),
            ("import_jobs", "lease_until", "TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


USERNAME_RE = re.compile(r"^[\w\u4e00-\u9fff.\- ]+$", re.UNICODE)


def normalize_username(value: str) -> str:
    return " ".join((value or "").split())


def validate_username(value: str) -> str:
    name = normalize_username(value)
    if not name:
        raise HTTPException(422, detail={"error_code": "USERNAME_INVALID", "message": "用户名不能为空。"})
    if len(name) > USERNAME_MAX:
        raise HTTPException(422, detail={"error_code": "USERNAME_INVALID",
                                         "message": f"用户名最多 {USERNAME_MAX} 个字符。"})
    if not USERNAME_RE.match(name):
        raise HTTPException(422, detail={"error_code": "USERNAME_INVALID",
                                         "message": "用户名只能包含中文、字母、数字、空格、点和短横线。"})
    return name


def username_taken(conn, name: str, *, exclude_user_id: str = "") -> bool:
    row = conn.execute("SELECT id FROM users WHERE lower(display_name)=lower(?) LIMIT 1",
                       (name,)).fetchone()
    return bool(row and row["id"] != exclude_user_id)


def public_user(row) -> dict:
    return {"user_id": row["id"], "email": row["email"], "display_name": row["display_name"]}


def user_from(request: Request, *, session_only: bool = False) -> dict | None:
    token = ""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
    if not token and not session_only:
        token = request.cookies.get(SESSION_COOKIE, "")
    if session_only:
        token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires_at>?", (digest(token), iso(now()))
        ).fetchone()
        if row:
            return dict(row)
        if session_only:
            return None
        row = conn.execute(
            "SELECT u.*, d.purpose AS _purpose, d.token_hash AS _device_hash FROM devices d JOIN users u ON u.id=d.user_id "
            "WHERE d.token_hash=? AND u.verified=1", (digest(token),)
        ).fetchone()
        if row:
            return dict(row)
    return None


def require_user(request: Request, *, session_only: bool = False, receive_allowed: bool = False) -> dict:
    user = user_from(request, session_only=session_only)
    if not user or not user["verified"]:
        raise HTTPException(401, detail={"error_code": "SESSION_INVALID", "message": "请先登录并验证邮箱。"})
    if user.get("_purpose") == "receive" and not receive_allowed:
        raise HTTPException(403, detail={"error_code": "RECEIVE_ONLY", "message": "接收授权不能执行此操作，请登录市集。"})
    return user


def require_receiver(request: Request) -> dict:
    user = require_user(request, receive_allowed=True)
    if not user.get("_device_hash"):
        raise HTTPException(403, detail={"error_code": "RECEIVER_REQUIRED", "message": "需要账号接收授权。"})
    return user


def smtp_settings() -> dict | None:
    user = os.environ.get("QQ_SMTP_USER", "").strip()
    password = os.environ.get("QQ_SMTP_PASSWORD", "").strip()
    if not user or not password:
        return None
    return {
        "host": os.environ.get("QQ_SMTP_HOST", "smtp.qq.com"),
        "port": int(os.environ.get("QQ_SMTP_PORT", "465")),
        "user": user,
        "password": password,
        "from": os.environ.get("QQ_FROM_EMAIL", user),
    }


def send_mail(to: str, subject: str, body: str) -> None:
    config = smtp_settings()
    if not config:
        raise HTTPException(503, detail={"error_code": "EMAIL_UNAVAILABLE", "message": "邮件服务尚未配置。"})
    from email.message import EmailMessage
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = config["from"], to, subject
    message.set_content(body)
    try:
        with smtplib.SMTP_SSL(config["host"], config["port"], timeout=20,
                              context=ssl.create_default_context()) as smtp:
            smtp.login(config["user"], config["password"])
            smtp.send_message(message)
    except Exception:
        raise HTTPException(503, detail={"error_code": "EMAIL_UNAVAILABLE",
                                         "message": "验证邮件暂未发送成功，请稍后重试。"}) from None


def send_verification(email: str, token: str) -> None:
    send_mail(email, "学伴市集 · 验证邮箱",
              "请打开下面的链接完成邮箱验证：\n\n" + BASE_URL + "/verify?token=" + token +
              "\n\n如果不是你本人操作，请忽略这封邮件。")


def send_code(email: str, code: str) -> None:
    send_mail(email, "学伴市集 · 邮箱验证码",
              "你的邮箱验证码是：" + code + "\n\n" +
              "验证码 " + str(int(CODE_TTL.total_seconds() // 60)) + " 分钟内有效，请回到市集页面输入。" +
              "\n如果不是你本人操作，请忽略这封邮件。")


def _consume_code(conn, email: str, code: str) -> str:
    """Validate and burn a 6-digit code; returns the bound user_id ("" before signup)."""
    row = conn.execute("SELECT * FROM email_codes WHERE email=?", (email,)).fetchone()
    if not row or row["expires_at"] <= iso(now()):
        raise HTTPException(410, detail={"error_code": "CODE_EXPIRED",
                                         "message": "验证码已过期，请重新发送。"})
    if row["attempts"] >= CODE_MAX_ATTEMPTS:
        raise HTTPException(429, detail={"error_code": "CODE_LOCKED",
                                         "message": "尝试次数过多，请重新发送验证码。"})
    if not secrets.compare_digest(row["code_hash"], digest(code)):
        conn.execute("UPDATE email_codes SET attempts=attempts+1 WHERE email=?", (email,))
        left = CODE_MAX_ATTEMPTS - row["attempts"] - 1
        raise HTTPException(422, detail={"error_code": "CODE_INVALID",
                                         "message": "验证码不正确。" + (f"还可尝试 {left} 次。" if left > 0 else "请重新发送。")})
    conn.execute("DELETE FROM email_codes WHERE email=?", (email,))
    return row["user_id"] or ""


def _issue_code(conn, email: str, user_id: str) -> str:
    code = f"{secrets.randbelow(1000000):06d}"
    conn.execute(
        "INSERT INTO email_codes(email,user_id,code_hash,expires_at,attempts,created_at) VALUES(?,?,?,?,0,?) "
        "ON CONFLICT(email) DO UPDATE SET user_id=excluded.user_id, code_hash=excluded.code_hash, "
        "expires_at=excluded.expires_at, attempts=0, created_at=excluded.created_at",
        (email, user_id, digest(code), iso(now() + CODE_TTL), iso(now())))
    return code


class RegisterIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)
    display_name: str = Field(default="", max_length=100)
    code: str = Field(default="", max_length=12)
    bind_code: str = Field(default="", max_length=64)


class SendCodeIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=PASSWORD_MAX)


class VerifyCodeIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    code: str = Field(min_length=4, max_length=12)


class ResendIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)


class UsernameIn(BaseModel):
    display_name: str = Field(default="", max_length=USERNAME_MAX)


class ConfirmIn(BaseModel):
    code: str = Field(min_length=4, max_length=64)


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/health")
def health() -> dict:
    return {"ok": True}


def _index_page() -> HTMLResponse:
    page = WEB_DIR / "index.html"
    if not page.exists():
        return HTMLResponse("<h1>市集页面缺失</h1>", status_code=500)
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return _index_page()


@app.get("/featured", response_class=HTMLResponse)
def featured_page() -> HTMLResponse:
    return _index_page()


@app.get("/categories", response_class=HTMLResponse)
def categories_page() -> HTMLResponse:
    return _index_page()


@app.get("/uploads", response_class=HTMLResponse)
def uploads_page() -> HTMLResponse:
    return _index_page()


@app.get("/assets/logo.png")
def logo() -> FileResponse:
    path = WEB_DIR / "logo.png"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.post("/api/send-code")
def send_register_code(body: SendCodeIn, request: Request) -> dict:
    """Pre-signup code request used by the inline "发送验证码" button."""
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, detail={"error_code": "EMAIL_INVALID", "message": "邮箱格式不正确。"})
    rate("sendcode:" + (request.client.host if request.client else "?"), 10, 600)
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user and user["verified"]:
            raise HTTPException(409, detail={"error_code": "EMAIL_EXISTS",
                                             "message": "该邮箱已注册，请直接登录。"})
        row = conn.execute("SELECT created_at FROM email_codes WHERE email=?", (email,)).fetchone()
        if row:
            last = datetime.fromisoformat(row["created_at"])
            if (now() - last).total_seconds() < 60:
                raise HTTPException(429, detail={"error_code": "CODE_COOLDOWN",
                                                 "message": "请稍后再发送验证码。"})
        code = _issue_code(conn, email, user["id"] if user else "")
    send_code(email, code)
    return {"ok": True, "cooldown": 60}


@app.post("/api/register")
def register(body: RegisterIn, request: Request) -> dict:
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, detail={"error_code": "EMAIL_INVALID", "message": "邮箱格式不正确。"})
    rate("register:" + (request.client.host if request.client else "?"), 10, 600)
    display_name = validate_username(body.display_name)
    code = (body.code or "").strip().replace(" ", "")
    bind_code = (body.bind_code or "").strip()
    with db() as conn:
        if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise HTTPException(409, detail={"error_code": "EMAIL_EXISTS",
                                             "message": "该邮箱已注册，请直接登录或找回密码。"})
        if username_taken(conn, display_name):
            raise HTTPException(409, detail={"error_code": "USERNAME_TAKEN",
                                             "message": f"用户名「{display_name}」已被使用，换一个吧。"})
        binding = conn.execute("SELECT * FROM bindings WHERE code_hash=? AND used=0 AND confirmed=0 AND expires_at>?",
                               (digest(bind_code), iso(now()))).fetchone() if bind_code else None
        if not binding:
            raise HTTPException(409, detail={"error_code": "LOCAL_REQUIRED",
                                             "message": "请先在学伴桌面端点击「绑定市集账号」，从弹出的页面注册。"})
        if len(code) != 6 or not code.isdigit():
            raise HTTPException(422, detail={"error_code": "CODE_REQUIRED",
                                             "message": "请先获取并填写邮箱验证码。"})
        _consume_code(conn, email, code)
        user_id = "user_" + uuid.uuid4().hex[:16]
        try:
            conn.execute("INSERT INTO users(id,email,password_hash,display_name,verified,created_at) VALUES(?,?,?,?,1,?)",
                         (user_id, email, _ph.hash(body.password), display_name, iso(now())))
        except sqlite3.IntegrityError:
            raise HTTPException(409, detail={"error_code": "EMAIL_EXISTS",
                                             "message": "该邮箱已注册，请直接登录或找回密码。"}) from None
        conn.execute("UPDATE bindings SET user_id=?, confirmed=1 WHERE code_hash=?",
                     (user_id, digest(bind_code)))
    return {"ok": True, "verified": True, "user_id": user_id, "bound": True}


@app.post("/api/verify-code")
def verify_code(body: VerifyCodeIn, request: Request) -> dict:
    email = body.email.strip().lower()
    code = body.code.strip().replace(" ", "")
    rate("verify:" + (request.client.host if request.client else "?"), 20, 600)
    if not EMAIL_RE.match(email) or not code.isdigit() or len(code) != 6:
        raise HTTPException(422, detail={"error_code": "CODE_INVALID", "message": "验证码格式不正确。"})
    with db() as conn:
        row = conn.execute("SELECT * FROM email_codes WHERE email=?", (email,)).fetchone()
        if not row or row["expires_at"] <= iso(now()):
            raise HTTPException(410, detail={"error_code": "CODE_EXPIRED",
                                             "message": "验证码已过期，请重新发送。"})
        if row["attempts"] >= CODE_MAX_ATTEMPTS:
            raise HTTPException(429, detail={"error_code": "CODE_LOCKED",
                                             "message": "尝试次数过多，请重新发送验证码。"})
        if not secrets.compare_digest(row["code_hash"], digest(code)):
            conn.execute("UPDATE email_codes SET attempts=attempts+1 WHERE email=?", (email,))
            left = CODE_MAX_ATTEMPTS - row["attempts"] - 1
            raise HTTPException(422, detail={"error_code": "CODE_INVALID",
                                             "message": "验证码不正确。" + (f"还可尝试 {left} 次。" if left > 0 else "请重新发送。")})
        conn.execute("UPDATE users SET verified=1 WHERE id=?", (row["user_id"],))
        conn.execute("DELETE FROM email_codes WHERE email=?", (email,))
    return {"ok": True, "verified": True}


@app.post("/api/verify-code/resend")
def resend_code(body: ResendIn, request: Request) -> dict:
    email = body.email.strip().lower()
    rate("resend:" + (request.client.host if request.client else "?"), 5, 600)
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not user:
            raise HTTPException(404, detail={"error_code": "EMAIL_UNKNOWN", "message": "该邮箱尚未注册。"})
        if user["verified"]:
            return {"ok": True, "verified": True}
        code = _issue_code(conn, email, user["id"])
    send_code(email, code)
    return {"ok": True, "verification": "code"}


@app.get("/verify", response_class=HTMLResponse)
def verify(token: str) -> HTMLResponse:
    row = None
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM email_tokens WHERE token_hash=? AND used_at IS NULL AND expires_at>?",
            (digest(token), iso(now()))).fetchone()
        if row:
            conn.execute("UPDATE email_tokens SET used_at=? WHERE token_hash=?", (iso(now()), row["token_hash"]))
            conn.execute("UPDATE users SET verified=1 WHERE id=?", (row["user_id"],))
    if not row:
        return HTMLResponse(_page("验证失败", "<h1>链接无效或已过期</h1><p>请回到市集重新注册或登录。</p>"), status_code=410)
    return HTMLResponse(_page("验证成功", "<h1>邮箱验证成功</h1><p>现在可以关闭此页，回到绑定页面登录。</p>"))


@app.post("/api/login")
def login(body: LoginIn, response: Response) -> dict:
    email = body.email.strip().lower()
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not user:
            raise HTTPException(401, detail={"error_code": "LOGIN_REJECTED", "message": "邮箱或密码错误。"})
        try:
            _ph.verify(user["password_hash"], body.password)
        except (VerifyMismatchError, Exception):
            raise HTTPException(401, detail={"error_code": "LOGIN_REJECTED", "message": "邮箱或密码错误。"}) from None
        if not user["verified"]:
            raise HTTPException(403, detail={"error_code": "EMAIL_UNVERIFIED", "message": "请先完成邮箱验证。"})
        raw = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                     (digest(raw), user["id"], iso(now() + SESSION_TTL), iso(now())))
    response.set_cookie(SESSION_COOKIE, raw, httponly=True, secure=BASE_URL.startswith("https://"),
                        samesite="lax", max_age=int(SESSION_TTL.total_seconds()), path="/")
    return public_user(user)


@app.post("/api/logout")
def logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (digest(token),))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.post("/api/username")
def set_username(body: UsernameIn, request: Request) -> dict:
    user = require_user(request, session_only=True)
    display_name = validate_username(body.display_name)
    with db() as conn:
        if username_taken(conn, display_name, exclude_user_id=user["id"]):
            raise HTTPException(409, detail={"error_code": "USERNAME_TAKEN",
                                             "message": f"用户名「{display_name}」已被使用，换一个吧。"})
        try:
            conn.execute("UPDATE users SET display_name=? WHERE id=?", (display_name, user["id"]))
        except sqlite3.IntegrityError:
            raise HTTPException(409, detail={"error_code": "USERNAME_TAKEN",
                                             "message": "这个用户名刚被别人用了，换一个吧。"}) from None
    return {"ok": True, "display_name": display_name}


@app.get("/api/username/available")
def username_available(request: Request, response: Response, name: str = "") -> dict:
    response.headers["Cache-Control"] = "private, no-store"
    candidate = normalize_username(name)
    if not candidate:
        return {"available": False, "reason": "empty"}
    if len(candidate) > USERNAME_MAX:
        return {"available": False, "reason": "too_long"}
    if not USERNAME_RE.match(candidate):
        return {"available": False, "reason": "invalid_chars"}
    user = user_from(request, session_only=True)
    with db() as conn:
        taken = username_taken(conn, candidate, exclude_user_id=(user or {}).get("id", ""))
    return {"available": not taken, "reason": "taken" if taken else "ok"}


@app.get("/api/me")
def me(request: Request) -> dict:
    # Anonymous visitors are a normal state: return 200 so the page does not log an error.
    user = user_from(request, session_only=True)
    if not user or not user["verified"]:
        return {"authenticated": False}
    return {"authenticated": True, "user": public_user(user)}


@app.get("/favicon.ico")
def favicon() -> FileResponse:
    path = WEB_DIR / "logo.png"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.post("/api/devices")
def issue_device(request: Request) -> dict:
    """Desktop login: exchange a market session for a long-lived device token."""
    user = require_user(request, session_only=True)
    raw = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute("INSERT INTO devices(token_hash,user_id,name,created_at,last_seen,purpose) VALUES(?,?,?,?,?,?)",
                     (digest(raw), user["id"], "学伴账号接收", iso(now()), "", "full"))
    return {"token": raw, "user": public_user(user)}


@app.get("/api/local/profile")
def local_profile(request: Request, response: Response) -> dict:
    response.headers["Cache-Control"] = "private, no-store"
    return public_user(require_user(request, receive_allowed=True))


@app.post("/api/web-login/tickets")
def web_login_ticket(request: Request, response: Response) -> dict:
    user = require_user(request)
    if not user.get("_device_hash"):
        raise HTTPException(403, detail={"error_code": "DEVICE_REQUIRED", "message": "需要本机登录授权。"})
    raw = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute("DELETE FROM web_login_tickets WHERE expires_at<=?", (iso(now()),))
        conn.execute("INSERT INTO web_login_tickets VALUES(?,?,?)",
                     (digest(raw), user["_device_hash"], iso(now() + timedelta(seconds=60))))
        conn.execute("INSERT INTO audit_logs(timestamp,event,actor_id,object_id,result) VALUES(?,?,?,?,?)",
                     (iso(now()), "web_login.issued", user["id"], "", "success"))
    response.headers["Cache-Control"] = "private, no-store"
    return {"ticket": raw, "user": public_user(user)}


class WebLoginIn(BaseModel):
    ticket: str = Field(min_length=20, max_length=128)


@app.post("/api/web-login/consume")
def consume_web_login(body: WebLoginIn, request: Request, response: Response) -> dict:
    if request.headers.get("origin") != BASE_URL:
        raise HTTPException(403, detail={"error_code": "ACCESS_DENIED", "message": "请从市集页面登录。"})
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("DELETE FROM web_login_tickets WHERE token_hash=? AND expires_at>? "
                           "RETURNING device_hash", (digest(body.ticket), iso(now()))).fetchone()
        user = None if row is None else conn.execute(
            "SELECT u.* FROM devices d JOIN users u ON u.id=d.user_id "
            "WHERE d.token_hash=? AND d.purpose='full' AND u.verified=1", (row["device_hash"],)
        ).fetchone()
        if user is None:
            raise HTTPException(410, detail={"error_code": "TOKEN_INVALID", "message": "登录链接已失效，请从学伴重新打开市集。"})
        raw = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO sessions VALUES(?,?,?,?)",
                     (digest(raw), user["id"], iso(now() + SESSION_TTL), iso(now())))
        conn.execute("INSERT INTO audit_logs(timestamp,event,actor_id,object_id,result) VALUES(?,?,?,?,?)",
                     (iso(now()), "web_login.completed", user["id"], "", "success"))
    response.headers["Cache-Control"] = "private, no-store"
    response.set_cookie(SESSION_COOKIE, raw, httponly=True, secure=BASE_URL.startswith("https://"),
                        samesite="lax", max_age=int(SESSION_TTL.total_seconds()), path="/")
    return {"user": public_user(user)}


@app.post("/api/bind/confirm")
def bind_confirm(body: ConfirmIn, request: Request) -> dict:
    user = require_user(request, session_only=True)
    with db() as conn:
        row = conn.execute("SELECT * FROM bindings WHERE code_hash=? AND used=0 AND expires_at>?",
                           (digest(body.code), iso(now()))).fetchone()
        if row:
            conn.execute("UPDATE bindings SET user_id=?, confirmed=1 WHERE code_hash=?",
                         (user["id"], digest(body.code)))
        conn.execute("INSERT INTO audit_logs(timestamp,event,actor_id,object_id,result) VALUES(?,?,?,?,?)",
                     (iso(now()), "market.reception.confirmed", user["id"], "", "success" if row else "denied"))
    if not row:
        raise HTTPException(404, detail={"error_code": "BIND_INVALID", "message": "绑定码无效或已过期，请重新生成。"})
    return {"ok": True, "user_id": user["id"]}


@app.get("/api/bind/status")
def bind_status(code: str, request: Request) -> dict:
    rate("bind:" + (request.client.host if request.client else "?"), 30, 60)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM bindings WHERE code_hash=?", (digest(code),)).fetchone()
        if not row:
            raise HTTPException(404, detail={"error_code": "BIND_INVALID", "message": "绑定码无效。"})
        if row["used"]:
            raise HTTPException(410, detail={"error_code": "BIND_USED", "message": "绑定码已使用。"})
        if row["expires_at"] <= iso(now()):
            raise HTTPException(410, detail={"error_code": "BIND_EXPIRED", "message": "绑定码已过期。"})
        if not row["confirmed"] or not row["user_id"]:
            return {"status": "pending"}
        user = conn.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
        raw = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO devices(token_hash,user_id,name,created_at,last_seen,purpose) VALUES(?,?,?,?,?,?)",
                     (digest(raw), user["id"], "学伴账号接收", iso(now()), "",
                      "receive" if row["purpose"] == "receive" else "full"))
        conn.execute("UPDATE bindings SET used=1 WHERE code_hash=?", (digest(code),))
        conn.execute("INSERT INTO audit_logs(timestamp,event,actor_id,object_id,result) VALUES(?,?,?,?,?)",
                     (iso(now()), "market.reception.authorized", user["id"], "", "success"))
    return {"status": "confirmed", "token": raw, "purpose": row["purpose"], **public_user(user)}


def _record_binding(code: str, purpose: str = "login") -> None:
    # Reloading the bind page must never clear an already confirmed binding.
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO bindings(code_hash,confirmed,used,expires_at,created_at,purpose) VALUES(?,0,0,?,?,?)",
                     (digest(code), iso(now() + BIND_TTL), iso(now()), purpose))


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{margin:0;background:#0b0b0a;color:#f1eee6;font:15px/1.7 "PingFang SC","Microsoft YaHei",sans-serif}}
main{{max-width:520px;margin:8vh auto;padding:28px;border:1px solid rgba(241,238,230,.16);border-radius:24px;background:#111}}
h1{{font:400 26px/1.3 "Noto Serif SC","Songti SC",serif;margin:0 0 14px}}p{{color:#b8b3a7;font-size:13px}}
label{{display:block;font-size:12px;margin:14px 0 6px}}input{{width:100%;box-sizing:border-box;padding:10px;border-radius:12px;background:#0b0b0a;border:1px solid rgba(241,238,230,.24);color:#f1eee6;font:inherit;min-height:44px}}
button{{margin-top:16px;min-height:44px;padding:0 18px;border-radius:999px;background:#f1eee6;color:#0b0b0a;border:0;font-weight:600;cursor:pointer}}
:focus-visible{{outline:2px solid #f1eee6;outline-offset:3px}}
@media(max-width:600px){{main{{margin:24px 16px;padding:24px 20px}}}}
.err{{color:#e0a19a;font-size:12px;min-height:20px}}a{{color:#f1eee6}}</style></head><body><main>{body}</main></body></html>"""


@app.get("/bind", response_class=HTMLResponse)
def bind_page(code: str, request: Request, purpose: str = "login") -> HTMLResponse:
    if not re.fullmatch(r"[A-Za-z0-9_-]{12}", code):
        return HTMLResponse(_page("绑定失败", "<h1>缺少绑定码</h1><p>请回到学伴桌面端重新生成。</p>"), status_code=400)
    purpose = "receive" if purpose == "receive" else "login"
    _record_binding(code, purpose)
    user = user_from(request, session_only=True)
    if user and user["verified"]:
        title = "开启账号接收" if purpose == "receive" else "绑定学伴桌面端"
        description = ("允许后台接收当前市集账号的卡片，保存到该账号的本地空间；不会切换本机登录账号。"
                       if purpose == "receive" else "确认后，可上传和导入当前市集账号的卡片。")
        body = f"""<h1>{title}</h1><p>当前账号：{html.escape(user['email'])}</p>
        <p>{description}</p>
        <p class="err" id="msg"></p><button onclick="confirmBind()">{title}</button>
        <script>const code={json.dumps(code)};async function confirmBind(){{const r=await fetch('/api/bind/confirm',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{code}})}});const j=await r.json().catch(()=>({{}}));if(r.ok){{try{{sessionStorage.removeItem('market.pendingBinding')}}catch(e){{}}location.replace('/')}}else{{document.getElementById('msg').textContent=j.detail?.message||'绑定失败'}}}}</script>"""
        return HTMLResponse(_page(title, body))
    body = f"""<style>
    .row{{display:flex;gap:10px;align-items:stretch;margin-top:7px}}
    .row input{{margin-top:0;flex:1;min-width:0}}
    .row button{{flex:none;min-width:118px;margin-top:0;border-radius:12px;border:1px solid rgba(241,238,230,.24);
      background:rgba(241,238,230,.09);color:#f1eee6;font-size:13px;font-weight:500;cursor:pointer}}
    .row button:hover:not([disabled]){{background:rgba(241,238,230,.16)}}
    .row button[disabled]{{opacity:.5;cursor:not-allowed}}
    .hint{{font-size:12px;color:#8C887E;margin-top:6px}}
    .ok{{color:#CFCABE}}
    .alt{{margin-top:18px;padding-top:14px;border-top:1px solid rgba(241,238,230,.1);font-size:11.5px;color:#8C887E}}
    .alt button{{background:none;border:0;color:#CFCABE;font-size:11.5px;text-decoration:underline;cursor:pointer;padding:0}}
    </style>
    <h1>{"注册并开启账号接收" if purpose == "receive" else "注册并绑定这台电脑"}</h1>
    <p class="hint">绑定码：<b>{html.escape(code)}</b>（10 分钟内有效）</p>
    <p class="hint">{"只授权该账号的本地接收，不会切换本机登录账号；已有账号可在下方登录授权。" if purpose == "receive" else "这次注册会同时创建市集账号并绑定当前电脑。以后在网页点「导入」，卡片会直接送到这台电脑的「我的人物」。"}</p>
    <div id="msg" class="err"></div>
    <label for="email">邮箱 <span style="color:#e0a19a">*</span></label><input id="email" type="email" autocomplete="email" spellcheck="false">
    <label for="code">邮箱验证码 <span style="color:#e0a19a">*</span></label>
    <div class="row"><input id="code" type="text" inputmode="numeric" maxlength="6" placeholder="请输入验证码" autocomplete="one-time-code">
      <button id="send" onclick="sendCode()">发送验证码</button></div>
    <label for="username">用户名 <span style="color:#e0a19a">*</span></label><input id="username" type="text" maxlength="24" placeholder="中文、字母、数字，最多 24 个字符" autocomplete="nickname">
    <div id="nameHint" class="hint" style="min-height:16px"></div>
    <label for="password">密码 <span style="color:#e0a19a">*</span></label><input id="password" type="password" autocomplete="new-password">
    <button onclick="submitAuth('register')">{"注册并开启账号接收" if purpose == "receive" else "注册并绑定这台电脑"}</button>
    <div class="alt">已经有账号？<button onclick="submitAuth('login')">{"登录并开启账号接收" if purpose == "receive" else "登录并绑定这台电脑"}</button></div>
    <script>const bindCode={json.dumps(code)};const msg=document.getElementById('msg');
    const email=document.getElementById('email'),pwd=document.getElementById('password'),
      codeInput=document.getElementById('code'),user=document.getElementById('username'),
      sendBtn=document.getElementById('send'),nameHint=document.getElementById('nameHint');
    let timer=null,nameTimer=null;const nameCache={{}};
    function applyName(j){{if(j.available){{nameHint.className='ok';nameHint.textContent='可以使用';}}
      else{{nameHint.className='err';nameHint.textContent=j.reason==='taken'?'这个用户名已被使用'
        :j.reason==='too_long'?'用户名过长':'包含不支持的字符';}}}}
    user.addEventListener('input',()=>{{const value=user.value.trim();
      if(nameTimer)clearTimeout(nameTimer);nameHint.className='hint';nameHint.textContent='';
      if(!value)return;nameHint.textContent='检查中…';
      nameTimer=setTimeout(async()=>{{
        const cached=nameCache[value.toLowerCase()];
        if(cached){{applyName(cached);return;}}
        try{{const r=await fetch('/api/username/available?name='+encodeURIComponent(value));
          const j=await r.json();nameCache[value.toLowerCase()]=j;applyName(j);
        }}catch(e){{nameHint.textContent='';}}
      }},300);
    }});
    function say(text,ok){{msg.className=ok?'ok':'err';msg.textContent=text;}}
    function countdown(seconds){{let left=seconds;sendBtn.disabled=true;sendBtn.textContent=left+'s 后重发';
      if(timer)clearInterval(timer);timer=setInterval(()=>{{left-=1;if(left<=0){{clearInterval(timer);timer=null;
        sendBtn.disabled=false;sendBtn.textContent='重新发送';return}}sendBtn.textContent=left+'s 后重发';}},1000);}}
    async function sendCode(){{const value=email.value.trim();
      if(!value){{say('请先填写邮箱。');email.focus();return}}
      sendBtn.disabled=true;sendBtn.textContent='发送中…';say('',true);
      try{{const r=await fetch('/api/send-code',{{method:'POST',headers:{{'Content-Type':'application/json'}},
          body:JSON.stringify({{email:value}})}});const j=await r.json().catch(()=>({{}}));
        if(!r.ok){{sendBtn.disabled=false;sendBtn.textContent='发送验证码';say(j.detail?.message||'发送失败');return}}
        say('验证码已发送到 '+value+'，15 分钟内有效。',true);countdown(60);codeInput.focus();
      }}catch(e){{sendBtn.disabled=false;sendBtn.textContent='发送验证码';say('网络错误，请重试。')}}}}
    async function submitAuth(mode){{const value=email.value.trim(),password=pwd.value,c=codeInput.value.trim();
      if(!value||!password){{say('请填写邮箱和密码。');return}}
      if(mode==='register'&&!new RegExp('^[0-9]{{6}}$').test(c)){{say('请填写 6 位邮箱验证码。');return}}
      if(mode==='register'&&!user.value.trim()){{say('请填写用户名。');user.focus();return}}
      say('',true);
      try{{
        if(mode==='register'){{
          const r=await fetch('/api/register',{{method:'POST',headers:{{'Content-Type':'application/json'}},
            body:JSON.stringify({{email:value,password:password,display_name:user.value.trim(),code:c,bind_code:bindCode}})}});
          const j=await r.json().catch(()=>({{}}));
          if(!r.ok){{say(j.detail?.message||'注册失败');return}}
        }}
        const login=await fetch('/api/login',{{method:'POST',headers:{{'Content-Type':'application/json'}},
          body:JSON.stringify({{email:value,password:password}})}});
        const lj=await login.json().catch(()=>({{}}));
        if(!login.ok){{say(lj.detail?.message||'登录失败');return}}
        if(mode==='login'){{
        const confirm=await fetch('/api/bind/confirm',{{method:'POST',headers:{{'Content-Type':'application/json'}},
          body:JSON.stringify({{code:bindCode}})}});
        if(!confirm.ok){{const cj=await confirm.json().catch(()=>({{}}));say(cj.detail?.message||'绑定失败');return}}
        }}
        try{{sessionStorage.removeItem('market.pendingBinding')}}catch(e){{}}
        location.replace('/');
      }}catch(e){{say('网络错误，请重试。')}}}}
    </script>"""
    return HTMLResponse(_page("绑定学伴桌面端", body))


def card_view(row, downloads: int = 0) -> dict:
    official = bool(row["official"]) if "official" in row.keys() else False
    summary = row["summary"] if "summary" in row.keys() else ""
    return {
        "card_id": row["id"], "title": row["title"],
        "tags": json.loads(row["tags_json"] or "[]"),
        "summary": summary or "",
        "cover_url": f"/api/cards/{row['id']}/cover",
        "official": official,
        "downloads_72h": downloads, "created_at": row["created_at"],
    }


def official_owner_ids(conn) -> set[str]:
    emails = [value.strip().lower() for value in os.environ.get("MARKET_OFFICIAL_EMAILS", "").split(",") if value.strip()]
    if not emails:
        return set()
    marks = ",".join("?" for _ in emails)
    return {row["id"] for row in conn.execute(f"SELECT id FROM users WHERE lower(email) IN ({marks})", emails)}


def _validate_package(raw: bytes) -> dict:
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise HTTPException(422, detail={"error_code": "INVALID_PACKAGE", "message": "不是有效的卡片包。"}) from None
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        raise HTTPException(422, detail={"error_code": "INVALID_PACKAGE", "message": "卡片包条目过多。"})
    total = 0
    for info in infos:
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if info.flag_bits & 0x1 or name.startswith("/") or ".." in name.split("/"):
            raise HTTPException(422, detail={"error_code": "INVALID_PACKAGE", "message": "卡片包路径无效。"})
        total += info.file_size
        if info.file_size > MAX_ENTRY or total > MAX_PACKAGE:
            raise HTTPException(413, detail={"error_code": "INVALID_PACKAGE", "message": "卡片包过大。"})
    try:
        manifest = json.loads(archive.read("manifest.json"))
    except Exception:
        raise HTTPException(422, detail={"error_code": "INVALID_PACKAGE", "message": "卡片包缺少 manifest.json。"}) from None
    if manifest.get("format") != "studypal.card/1":
        raise HTTPException(422, detail={"error_code": "INVALID_PACKAGE", "message": "不支持的卡片包版本。"})
    profile = manifest.get("profile")
    if not isinstance(profile, dict) or not str(profile.get("name") or "").strip():
        raise HTTPException(422, detail={"error_code": "INVALID_PACKAGE", "message": "卡片包缺少人物设定。"})
    return manifest


def _summary_from(profile: dict) -> str:
    """Card-specific intro taken from the character itself, never a generic blurb."""
    identity = str(profile.get("identity") or "").strip()
    background = str(profile.get("background") or "").strip()
    personality = str(profile.get("personality") or "").strip()
    parts = []
    if identity:
        parts.append(identity.rstrip("。") + "。")
    if personality:
        parts.append("性格上，" + personality.rstrip("。") + "。")
    if not parts and background:
        parts.append(background[:160].rstrip("。") + "。")
    summary = "".join(parts).strip()
    return summary[:400]


def _cover_bytes(title: str, raw: bytes | None) -> tuple[str, bytes]:
    if raw:
        if raw.startswith(b"\x89PNG"):
            return "cover.png", raw
        if raw.startswith(b"\xff\xd8"):
            return "cover.jpg", raw
        if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            return "cover.webp", raw
        raise HTTPException(422, detail={"error_code": "INVALID_COVER", "message": "封面必须是 PNG/JPEG/WebP。"})
    safe = html.escape(title[:40])
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="800" height="1000">'
           f'<rect width="800" height="1000" fill="#111110"/>'
           f'<text x="60" y="520" fill="#F1EEE6" font-size="54" font-family="serif">{safe}</text></svg>')
    return "cover.svg", svg.encode("utf-8")


@app.post("/api/cards", status_code=201)
async def upload_card(request: Request, package: UploadFile = File(...),
                      cover: UploadFile | None = File(None), title: str = Form(""),
                      tags: str = Form("[]")) -> dict:
    user = require_user(request)
    raw = await package.read()
    if not raw or len(raw) > MAX_PACKAGE:
        raise HTTPException(413, detail={"error_code": "INVALID_PACKAGE", "message": "卡片包大小超出限制。"})
    manifest = _validate_package(raw)
    profile = manifest.get("profile") or {}
    title = (title or str(profile.get("name") or "")).strip()[:120]
    if not title:
        raise HTTPException(422, detail={"error_code": "INVALID_PACKAGE", "message": "卡片缺少名字。"})
    try:
        parsed_tags = json.loads(tags) if tags else profile.get("tags", [])
    except json.JSONDecodeError:
        raise HTTPException(422, detail={"error_code": "INVALID_TAGS", "message": "标签格式无效。"}) from None
    if not isinstance(parsed_tags, list):
        raise HTTPException(422, detail={"error_code": "INVALID_TAGS", "message": "标签格式无效。"})
    parsed_tags = [str(tag).strip()[:40] for tag in parsed_tags[:12] if str(tag).strip()]
    cover_raw = await cover.read() if cover else None
    if cover_raw and len(cover_raw) > MAX_COVER:
        raise HTTPException(413, detail={"error_code": "INVALID_COVER", "message": "封面过大。"})
    sha = str(manifest.get("content_sha256") or "") or hashlib.sha256(raw).hexdigest()
    with db() as conn:
        existing = conn.execute("SELECT * FROM cards WHERE owner_id=? AND package_sha=?", (user["id"], sha)).fetchone()
        if existing:
            raise HTTPException(409, detail={"error_code": "DUPLICATE_CARD", "message": "这张卡片已经上传过。",
                                             "card_id": existing["id"]})
        card_id = "card_" + uuid.uuid4().hex[:16]
        folder = STORAGE / card_id
        folder.mkdir(parents=True, exist_ok=True)
        cover_name, cover_payload = _cover_bytes(title, cover_raw)
        (folder / "package.zip").write_bytes(raw)
        (folder / cover_name).write_bytes(cover_payload)
        is_official = 1 if user["id"] in official_owner_ids(conn) else 0
        conn.execute("INSERT INTO cards(id,owner_id,title,tags_json,cover_name,package_name,package_sha,size,official,summary,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (card_id, user["id"], title, json.dumps(parsed_tags, ensure_ascii=False),
                      cover_name, "package.zip", sha, len(raw), is_official,
                      _summary_from(profile), iso(now())))
        row = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    return card_view(row)


@app.get("/api/cards")
def list_cards(request: Request, response: Response, scope: str = "all") -> dict:
    cutoff = iso(now() - timedelta(hours=72))
    owner = require_user(request)["id"] if scope == "mine" else None
    if owner:
        response.headers["Cache-Control"] = "private, no-store"
    with db() as conn:
        rows = conn.execute(
            "SELECT c.*, (SELECT COUNT(DISTINCT d.user_id) FROM downloads d "
            "WHERE d.card_id=c.id AND d.created_at>=?) AS downloads FROM cards c "
            "WHERE c.published=1" + (" AND c.owner_id=?" if owner else "") +
            (" ORDER BY c.created_at DESC" if owner else " ORDER BY downloads DESC, c.created_at DESC") +
            " LIMIT 200", (cutoff, owner) if owner else (cutoff,)).fetchall()
        owners = official_owner_ids(conn)
    items = []
    for row in rows:
        view = card_view(row, row["downloads"] or 0)
        view["official"] = view["official"] or row["owner_id"] in owners
        items.append(view)
    if scope == "official":
        items = [item for item in items if item["official"]]
    return {"data": items}


class ImportIn(BaseModel):
    card_id: str = Field(min_length=1, max_length=64)


@app.post("/api/imports", status_code=202)
def request_import(body: ImportIn, request: Request) -> dict:
    user = require_user(request, session_only=True)
    with db() as conn:
        card = conn.execute("SELECT * FROM cards WHERE id=? AND published=1", (body.card_id,)).fetchone()
        if not card:
            raise HTTPException(404, detail={"error_code": "CARD_NOT_FOUND", "message": "卡片不存在。"})
        device = conn.execute("SELECT * FROM devices WHERE user_id=? AND receiving=1 ORDER BY last_seen DESC LIMIT 1",
                              (user["id"],)).fetchone()
        online = bool(device and device["last_seen"] >= iso(now() - timedelta(seconds=90)))
        if not online:
            raise HTTPException(409, detail={"error_code": "LOCAL_OFFLINE",
                                             "message": "本地学伴没有连接。请先打开学伴，或下载 ZIP 手动导入。"})
        job_id = "imp_" + uuid.uuid4().hex[:16]
        conn.execute("INSERT INTO import_jobs(id,user_id,card_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (job_id, user["id"], body.card_id, "pending", iso(now()), iso(now())))
    return {"job_id": job_id, "status": "pending"}


@app.get("/api/imports/{job_id}")
def import_status(job_id: str, request: Request) -> dict:
    user = require_user(request, session_only=True)
    with db() as conn:
        job = conn.execute("SELECT * FROM import_jobs WHERE id=? AND user_id=?", (job_id, user["id"])).fetchone()
    if not job:
        raise HTTPException(404, detail={"error_code": "JOB_NOT_FOUND", "message": "导入任务不存在。"})
    return {"job_id": job["id"], "status": job["status"], "message": job["message"]}


@app.get("/api/local/imports")
def local_imports(request: Request) -> dict:
    """Polled by the desktop app with its device token."""
    user = require_receiver(request)
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        enabled = conn.execute("SELECT receiving FROM devices WHERE token_hash=?", (user["_device_hash"],)).fetchone()
        if not enabled or not enabled["receiving"]:
            return {"items": []}
        conn.execute("UPDATE devices SET last_seen=? WHERE token_hash=?",
                     (iso(now()), user["_device_hash"]))
        rows = conn.execute("SELECT * FROM import_jobs WHERE user_id=? AND "
                            "(status='pending' OR (status='taken' AND (receiver=? OR lease_until<?))) "
                            "ORDER BY created_at LIMIT 5",
                            (user["id"], user["_device_hash"], iso(now()))).fetchall()
        for row in rows:
            conn.execute("UPDATE import_jobs SET status='taken', receiver=?, lease_until=?, updated_at=? WHERE id=?",
                         (user["_device_hash"], iso(now() + timedelta(minutes=10)), iso(now()), row["id"]))
    return {"items": [{"job_id": row["id"], "card_id": row["card_id"]} for row in rows]}


@app.post("/api/local/imports/{job_id}")
async def local_import_result(job_id: str, request: Request) -> dict:
    user = require_receiver(request)
    payload = await request.json()
    status = str(payload.get("status") or "done")
    if status not in ("done", "failed"):
        raise HTTPException(422, detail={"error_code": "INVALID_STATUS", "message": "状态无效。"})
    message = str(payload.get("message") or "")[:200]
    with db() as conn:
        job = conn.execute("SELECT * FROM import_jobs WHERE id=? AND user_id=?", (job_id, user["id"])).fetchone()
        if not job:
            raise HTTPException(404, detail={"error_code": "JOB_NOT_FOUND", "message": "导入任务不存在。"})
        if job["receiver"] != user["_device_hash"]:
            raise HTTPException(409, detail={"error_code": "IMPORT_LEASE_LOST", "message": "任务已由其他接收端处理。"})
        if job["status"] in ("done", "failed"):
            return {"ok": True}
        conn.execute("UPDATE import_jobs SET status=?, message=?, updated_at=? WHERE id=?",
                     (status, message, iso(now()), job_id))
    return {"ok": True}


@app.delete("/api/local/authorization")
def revoke_reception(request: Request) -> dict:
    user = require_receiver(request)
    with db() as conn:
        conn.execute("INSERT INTO audit_logs(timestamp,event,actor_id,object_id,result) VALUES(?,?,?,?,?)",
                     (iso(now()), "market.reception.revoked", user["id"], "", "success"))
        conn.execute("DELETE FROM devices WHERE token_hash=?", (user["_device_hash"],))
        conn.execute("UPDATE import_jobs SET status='pending',receiver='',lease_until='' "
                     "WHERE user_id=? AND status='taken' AND receiver=?", (user["id"], user["_device_hash"]))
    return {"ok": True}


class ReceptionIn(BaseModel):
    enabled: bool


@app.patch("/api/local/authorization")
def pause_reception(body: ReceptionIn, request: Request) -> dict:
    user = require_receiver(request)
    with db() as conn:
        conn.execute("INSERT INTO audit_logs(timestamp,event,actor_id,object_id,result) VALUES(?,?,?,?,?)",
                     (iso(now()), "market.reception.changed", user["id"], "",
                      "enabled" if body.enabled else "paused"))
        conn.execute("UPDATE devices SET receiving=?, last_seen=? WHERE token_hash=?",
                     (int(body.enabled), "", user["_device_hash"]))
    return {"ok": True}


@app.get("/help/import", response_class=HTMLResponse)
def help_import() -> HTMLResponse:
    page = WEB_DIR / "help-import.html"
    if not page.exists():
        raise HTTPException(404)
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/api/cards/{card_id}/cover")
def card_cover(card_id: str) -> FileResponse:
    with db() as conn:
        row = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    path = STORAGE / card_id / row["cover_name"]
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=300",
                                       "X-Content-Type-Options": "nosniff"})


@app.get("/api/cards/{card_id}/package")
def card_package(card_id: str, request: Request) -> FileResponse:
    user = require_user(request, receive_allowed=True)
    with db() as conn:
        row = conn.execute("SELECT * FROM cards WHERE id=? AND published=1", (card_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        path = STORAGE / card_id / row["package_name"]
        if not path.exists():
            raise HTTPException(404)
        day = now().date().isoformat()
        conn.execute("INSERT OR IGNORE INTO downloads(id,user_id,card_id,day,created_at) VALUES(?,?,?,?,?)",
                     ("dl_" + uuid.uuid4().hex[:16], user["id"], card_id, day, iso(now())))
    safe = re.sub(r"[^\w\-.]+", "_", row["title"])[:60] or "card"
    return FileResponse(path, media_type="application/zip",
                        filename=f"{safe}.studycard.zip",
                        headers={"Cache-Control": "private, no-store"})


@app.delete("/api/cards/{card_id}")
def delete_card(card_id: str, request: Request) -> dict:
    actor = None
    object_id = card_id if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", card_id) else ""

    def audit(conn, result):
        conn.execute("INSERT INTO audit_logs(timestamp,event,actor_id,object_id,result) VALUES(?,?,?,?,?)",
                     (iso(now()), "market.card.deleted", actor, object_id, result))

    try:
        user = require_user(request)
        actor = user["id"]
        if request.cookies.get(SESSION_COOKIE) and not request.headers.get("Authorization"):
            origin = request.headers.get("origin")
            if (origin and origin != BASE_URL) or request.headers.get("sec-fetch-site") == "cross-site":
                raise HTTPException(403, detail={"error_code": "ORIGIN_DENIED", "message": "请从市集页面删除。"})
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM cards WHERE id=? AND owner_id=?", (card_id, actor)).fetchone()
            if not row:
                raise HTTPException(404, detail={"error_code": "CARD_NOT_FOUND", "message": "卡片不存在或无权删除。"})
            audit(conn, "success")
            folder = STORAGE / card_id
            if folder.is_symlink() or folder.resolve().parent != STORAGE.resolve():
                raise OSError("invalid card directory")
            if folder.exists():
                for item in folder.iterdir():
                    item.unlink(missing_ok=True)
                folder.rmdir()
            conn.execute("DELETE FROM cards WHERE id=? AND owner_id=?", (card_id, actor))
    except (HTTPException, OSError, sqlite3.Error) as exc:
        try:
            with db() as conn:
                audit(conn, "denied" if isinstance(exc, HTTPException) else "failed")
        except sqlite3.Error:
            raise HTTPException(503, detail={"error_code": "AUDIT_UNAVAILABLE", "message": "删除记录暂时不可用，请稍后重试。"}) from None
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(503, detail={"error_code": "DELETE_FAILED", "message": "删除未完成，请稍后重试。"}) from None
    return {"ok": True}
