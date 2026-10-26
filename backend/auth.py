"""
auth.py — JWT 認証ユーティリティ
==================================
- パスワードハッシュ: bcrypt (rounds=12)
- トークン形式: JWT (HS256)
- トークン有効期限: 7日
- トークン保存: HttpOnly / Secure / SameSite=Strict Cookie
"""

import os
import bcrypt
from datetime import datetime, timedelta, timezone
import jwt
from jwt import InvalidTokenError as JWTError
from fastapi import Cookie, Depends, HTTPException, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.models_db import User

# ── 設定 ──────────────────────────────────────────────────────────────
_raw_secret = os.environ.get("JWT_SECRET", "")
_IS_PROD    = not os.environ.get("DEBUG")

# 本番環境でデフォルト秘密鍵のまま起動することを防ぐ。
_KNOWN_WEAK_SECRETS = {"", "dev-secret-change-in-production", "secret", "changeme"}
if _raw_secret in _KNOWN_WEAK_SECRETS:
    import warnings
    warnings.warn(
        "警告: JWT_SECRET が未設定または既知の弱い値です。"
        " .env に強いランダム文字列を設定してください（openssl rand -hex 32）",
        stacklevel=2,
    )
    if _IS_PROD:
        raise RuntimeError(
            "JWT_SECRET が未設定です。本番環境では必ず強いランダム文字列を設定してください。"
            " 例: JWT_SECRET=$(openssl rand -hex 32)"
        )
    _raw_secret = "dev-only-insecure-secret-do-not-use-in-production"

SECRET_KEY    = _raw_secret
ALGORITHM     = "HS256"
EXPIRE_DAYS   = 7
BCRYPT_ROUNDS = 12

# Cookie 設定
COOKIE_NAME    = "et_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * EXPIRE_DAYS   # 秒

# Authorization ヘッダー（APIクライアント等からの利用用・任意）
bearer_opt = HTTPBearer(auto_error=False)


# ── パスワード ────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()

def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ── JWT ───────────────────────────────────────────────────────────────

def create_token(user_id: int, email: str) -> str:
    """アクセストークンを生成して返す。"""
    expire = datetime.now(timezone.utc) + timedelta(days=EXPIRE_DAYS)
    return jwt.encode(
        {"sub": str(user_id), "email": email, "exp": expire},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )

def _decode_token(token: str) -> dict:
    try:
        return jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
            options={"verify_exp": True, "require": ["exp", "sub"]},
        )
    except JWTError:
        raise HTTPException(status_code=401, detail="認証トークンが無効または期限切れです")


# ── Cookie ヘルパー ───────────────────────────────────────────────────

def set_auth_cookie(response: Response, token: str) -> None:
    """HttpOnly Cookie にJWTをセットする。"""
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,           # JS から読み取り不可（XSS対策）
        secure=_IS_PROD,         # 本番は HTTPS のみ（開発は HTTP も許可）
        samesite="strict",       # CSRF対策: 同一サイトリクエストのみ送信
        path="/",
    )

def clear_auth_cookie(response: Response) -> None:
    """Cookie を削除してログアウトする。"""
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="strict",
    )


# ── FastAPI Depends ───────────────────────────────────────────────────

def get_current_user(
    et_session: str | None = Cookie(default=None),
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_opt),
    db: Session = Depends(get_db),
) -> User:
    """
    必須認証。
    Cookie（et_session）を優先し、なければ Authorization ヘッダーを確認する。
    どちらもなければ 401 を返す。
    """
    token = et_session or (creds.credentials if creds else None)
    if not token:
        raise HTTPException(status_code=401, detail="認証が必要です")
    payload = _decode_token(token)
    user    = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user:
        raise HTTPException(status_code=401, detail="ユーザーが見つかりません")
    return user


def get_current_user_optional(
    et_session: str | None = Cookie(default=None),
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_opt),
    db: Session = Depends(get_db),
) -> User | None:
    """任意認証: トークンがなければ None を返す（匿名でも利用可能なエンドポイント用）。"""
    token = et_session or (creds.credentials if creds else None)
    if not token:
        return None
    try:
        payload = _decode_token(token)
        return db.query(User).filter(User.id == int(payload["sub"])).first()
    except HTTPException:
        return None
