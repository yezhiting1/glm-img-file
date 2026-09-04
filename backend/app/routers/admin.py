"""Administrator user and quota management."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import AuthContext, hash_password, require_admin
from app.config import settings
from app.database import get_db
from app.models import (
    AuthSession,
    QuotaAdjustment,
    ROLE_ADMIN,
    ROLE_USER,
    User,
    WorkflowUsage,
)
from app.schemas import QuotaUpdate, UserCreate, UserInfo, UserUpdate

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/users", response_model=list[UserInfo])
def list_users(
    _ctx: AuthContext = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return db.query(User).order_by(User.id.asc()).all()


@router.post("/users", response_model=UserInfo, status_code=201)
def create_user(
    req: UserCreate,
    _ctx: AuthContext = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if req.role not in (ROLE_ADMIN, ROLE_USER):
        raise HTTPException(status_code=422, detail="无效的用户角色")
    quota = None if req.role == ROLE_ADMIN else (
        req.workflow_quota
        if req.workflow_quota is not None
        else settings.default_user_quota
    )
    user = User(
        username=req.username.strip(),
        password_hash=hash_password(req.password),
        role=req.role,
        workflow_quota=quota,
        is_active=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="用户名已存在") from exc
    db.refresh(user)
    return user


@router.patch("/users/{user_id}", response_model=UserInfo)
def update_user(
    user_id: int,
    req: UserUpdate,
    ctx: AuthContext = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if req.is_active is False and user.id == ctx.user.id:
        raise HTTPException(status_code=409, detail="不能停用当前管理员")
    if req.is_active is not None:
        user.is_active = req.is_active
        if not user.is_active:
            db.query(AuthSession).filter(AuthSession.user_id == user.id).delete()
    if req.password is not None:
        user.password_hash = hash_password(req.password)
        db.query(AuthSession).filter(AuthSession.user_id == user.id).delete()
    db.commit()
    db.refresh(user)
    return user


@router.put("/users/{user_id}/quota", response_model=UserInfo)
def update_quota(
    user_id: int,
    req: QuotaUpdate,
    ctx: AuthContext = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.role == ROLE_ADMIN:
        if req.workflow_quota is not None:
            raise HTTPException(status_code=422, detail="管理员配额必须为无限")
        return user
    if req.workflow_quota is None:
        raise HTTPException(status_code=422, detail="普通用户必须设置有限配额")
    old = user.workflow_quota
    user.workflow_quota = req.workflow_quota
    db.add(
        QuotaAdjustment(
            user_id=user.id,
            actor_user_id=ctx.user.id,
            old_quota=old,
            new_quota=req.workflow_quota,
            reason=req.reason.strip(),
        )
    )
    db.commit()
    db.refresh(user)
    return user


@router.get("/quota-adjustments")
def quota_adjustments(
    _ctx: AuthContext = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = db.query(QuotaAdjustment).order_by(QuotaAdjustment.id.desc()).all()
    return [
        {
            "id": row.id,
            "user_id": row.user_id,
            "actor_user_id": row.actor_user_id,
            "old_quota": row.old_quota,
            "new_quota": row.new_quota,
            "reason": row.reason,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.get("/users/{user_id}/usage-history")
def usage_history(
    user_id: int,
    _ctx: AuthContext = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if db.get(User, user_id) is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    rows = (
        db.query(WorkflowUsage)
        .filter(WorkflowUsage.owner_id == user_id)
        .order_by(WorkflowUsage.id.desc())
        .all()
    )
    return [
        {
            "id": row.id,
            "user_id": row.owner_id,
            "record_id": row.record_id,
            "status": row.status,
            "reserved_at": row.reserved_at,
            "charged_at": row.charged_at,
            "released_at": row.released_at,
        }
        for row in rows
    ]
