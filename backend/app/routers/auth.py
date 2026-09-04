"""Login/logout/session endpoints."""
from collections import defaultdict, deque
from threading import Lock
from time import monotonic

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.auth import (
    AuthContext,
    clear_session_cookies,
    create_session,
    get_auth_context,
    hash_password,
    set_session_cookies,
    verify_password,
)
from app.database import get_db
from app.config import settings
from app.models import AuthSession, User
from app.schemas import LoginRequest, LoginResponse, UserInfo

router = APIRouter(prefix="/api/auth", tags=["auth"])
_failed_logins: dict[str, deque[float]] = defaultdict(deque)
_failed_logins_lock = Lock()
_dummy_password_hash = hash_password("invalid-password-placeholder")


def _login_keys(request: Request, username: str) -> tuple[str, str]:
    client = request.client.host if request.client else "unknown"
    return f"ip:{client}", f"user:{username.casefold()}"


def _prune_attempts(attempts: deque[float], now: float) -> None:
    cutoff = now - settings.auth_login_window_seconds
    while attempts and attempts[0] <= cutoff:
        attempts.popleft()


def _enforce_login_limit(keys: tuple[str, str]) -> None:
    now = monotonic()
    with _failed_logins_lock:
        for key in keys:
            attempts = _failed_logins[key]
            _prune_attempts(attempts, now)
            if len(attempts) >= settings.auth_login_max_failures:
                raise HTTPException(
                    status_code=429,
                    detail="登录失败次数过多，请稍后再试",
                    headers={"Retry-After": str(settings.auth_login_window_seconds)},
                )


def _record_login_failure(keys: tuple[str, str]) -> None:
    now = monotonic()
    with _failed_logins_lock:
        for key in keys:
            attempts = _failed_logins[key]
            _prune_attempts(attempts, now)
            attempts.append(now)


def _clear_login_failures(keys: tuple[str, str]) -> None:
    with _failed_logins_lock:
        for key in keys:
            _failed_logins.pop(key, None)


@router.post("/login", response_model=LoginResponse)
def login(
    req: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    keys = _login_keys(request, req.username)
    _enforce_login_limit(keys)
    user = db.query(User).filter(User.username == req.username.strip()).first()
    password_valid = verify_password(
        user.password_hash if user is not None else _dummy_password_hash,
        req.password,
    )
    if user is None or not password_valid:
        _record_login_failure(keys)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not user.is_active:
        _record_login_failure(keys)
        raise HTTPException(status_code=403, detail="账号已停用")
    _clear_login_failures(keys)
    token, csrf, _ = create_session(db, user)
    set_session_cookies(response, token, csrf)
    return LoginResponse(user=UserInfo.model_validate(user), csrf_token=csrf)


@router.get("/me", response_model=UserInfo)
def me(ctx: AuthContext = Depends(get_auth_context)):
    return ctx.user


@router.post("/logout")
def logout(
    response: Response,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    session = db.get(AuthSession, ctx.session_id)
    if session is not None:
        db.delete(session)
        db.commit()
    clear_session_cookies(response)
    return {"status": "logged_out"}
