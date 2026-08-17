# -*- coding: utf-8 -*-
"""身分驗證。

原型階段：帳號 + 密碼（PBKDF2-SHA256），session 存在簽章 cookie。
未來換公司 AD / SSO 時，只需改寫 authenticate() 與 current_user()，
其餘 API 不受影響。
"""
import hashlib
import hmac
import os
import secrets

from fastapi import Request

from .db import SessionLocal, User

ITERATIONS = 120_000
SESSION_COOKIE = "safety_session"
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me-in-production")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters))
    return hmac.compare_digest(dk.hex(), digest)


def authenticate(username: str, password: str):
    db = SessionLocal()
    try:
        # 注意：布林欄位請用 == True，不要用 .is_(True)。
        # .is_(True) 在 SQL Server 會編譯成 `IS 1`，T-SQL 不支援此語法。
        user = db.query(User).filter(
            User.username == username, User.active == True).first()  # noqa: E712
        if user and verify_password(password, user.password_hash):
            return user
        return None
    finally:
        db.close()


def current_user(request: Request):
    """回傳目前登入者（dict），未登入回 None。"""
    return request.session.get("user")


def require_role(user, *roles) -> bool:
    if not user:
        return False
    if not roles:
        return True
    return user.get("role") in roles or user.get("role") == "admin"
