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
# 未設定時用「每次啟動隨機產生」的金鑰，而不是固定的預設字串。
# 固定預設值印在公開 repo 裡，等於任何人都能偽造 session（含 admin）——
# 「忘記設定」的失敗模式必須是 session 重啟後失效（麻煩但安全），
# 不能是門戶大開。正式部署仍應設定 SECRET_KEY，讓 session 跨重啟存活。
SECRET_KEY = os.environ.get("SECRET_KEY") or ""
if not SECRET_KEY:
    import secrets as _secrets
    SECRET_KEY = _secrets.token_hex(32)
    print("[auth] 警告：未設定 SECRET_KEY，已改用本次啟動的隨機金鑰。"
          "重啟後所有登入將失效；正式環境請務必設定。")


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
