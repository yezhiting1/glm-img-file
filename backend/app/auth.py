"""Opaque cookie sessions, CSRF validation, RBAC, and owner selection."""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, Header, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import AuthSession, ROLE_ADMIN, User

_hasher = PasswordHasher()
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("密码至少需要 12 个字符")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuthContext:
    user: User
    owner_id: int
    session_id: int

    @property
    def is_admin(self) -> bool:
        return self.user.role == ROLE_ADMIN


def create_session(db: Session, user: User) -> tuple[str, str, AuthSession]:
    token = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    session = AuthSession(
        user_id=user.id,
        token_hash=_token_hash(token),
        csrf_hash=_token_hash(csrf),
        expires_at=_utcnow() + timedelta(hours=settings.auth_session_hours),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return token, csrf, session


def set_session_cookies(response: Response, token: str, csrf: str) -> None:
    response.set_cookie(
        settings.auth_cookie_name,
        token,
        max_age=settings.auth_session_hours * 3600,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        settings.auth_csrf_cookie_name,
        csrf,
        max_age=settings.auth_session_hours * 3600,
        httponly=False,
        secure=settings.auth_cookie_secure,
        samesite="strict",
        path="/",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(settings.auth_cookie_name, path="/")
    response.delete_cookie(settings.auth_csrf_cookie_name, path="/")


def get_auth_context(
    request: Request,
    db: Session = Depends(get_db),
    x_csrf_token: str | None = Header(None, alias="X-CSRF-Token"),
    x_owner_id: int | None = Header(None, alias="X-Owner-ID"),
) -> AuthContext:
    token = request.cookies.get(settings.auth_cookie_name)
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    auth_session = (
        db.query(AuthSession)
        .filter(AuthSession.token_hash == _token_hash(token))
        .first()
    )
    if auth_session is None or auth_session.expires_at <= _utcnow():
        if auth_session is not None:
            db.delete(auth_session)
            db.commit()
        raise HTTPException(status_code=401, detail="会话已过期")
    user = db.get(User, auth_session.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="用户已停用")
    if request.method in UNSAFE_METHODS:
        if not x_csrf_token or not secrets.compare_digest(
            auth_session.csrf_hash, _token_hash(x_csrf_token)
        ):
            raise HTTPException(status_code=403, detail="CSRF 校验失败")
    owner_id = user.id
    if x_owner_id is not None:
        if user.role != ROLE_ADMIN:
            raise HTTPException(status_code=403, detail="普通用户不能选择其他数据所有者")
        owner = db.get(User, x_owner_id)
        if owner is None:
            raise HTTPException(status_code=422, detail="数据所有者不存在")
        owner_id = owner.id
    auth_session.last_seen_at = _utcnow()
    db.commit()
    return AuthContext(user=user, owner_id=owner_id, session_id=auth_session.id)


def require_admin(ctx: AuthContext = Depends(get_auth_context)) -> AuthContext:
    if not ctx.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return ctx
