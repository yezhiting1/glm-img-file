"""Concurrency-safe, idempotent workflow quota accounting."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import and_, or_, update
from sqlalchemy.orm import Session

from app.models import (
    ROLE_ADMIN,
    USAGE_CHARGED,
    USAGE_RELEASED,
    USAGE_RESERVED,
    User,
    WorkflowUsage,
)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def reserve(db: Session, owner_id: int, record_id: int) -> WorkflowUsage:
    usage = (
        db.query(WorkflowUsage)
        .filter(WorkflowUsage.record_id == record_id)
        .first()
    )
    if usage is not None and usage.status in (USAGE_RESERVED, USAGE_CHARGED):
        return usage

    user = db.get(User, owner_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=403, detail="用户不可用")
    unlimited = user.role == ROLE_ADMIN or user.workflow_quota is None
    if not unlimited:
        result = db.execute(
            update(User)
            .where(
                User.id == owner_id,
                User.is_active.is_(True),
                User.workflow_reserved + User.workflow_charged
                < User.workflow_quota,
            )
            .values(workflow_reserved=User.workflow_reserved + 1)
        )
        if result.rowcount != 1:
            db.rollback()
            raise HTTPException(status_code=429, detail="工作流配额已用尽")

    if usage is None:
        usage = WorkflowUsage(
            owner_id=owner_id,
            record_id=record_id,
            status=USAGE_RESERVED,
        )
        db.add(usage)
    else:
        usage.status = USAGE_RESERVED
        usage.reserved_at = _utcnow()
        usage.released_at = None
    db.commit()
    db.refresh(usage)
    return usage


def charge(db: Session, record_id: int) -> bool:
    usage = (
        db.query(WorkflowUsage)
        .filter(WorkflowUsage.record_id == record_id)
        .first()
    )
    if usage is None:
        raise RuntimeError("完成工作流前没有配额预留")
    if usage.status == USAGE_CHARGED:
        return False
    if usage.status != USAGE_RESERVED:
        raise RuntimeError("已释放的工作流不能直接计费")
    user = db.get(User, usage.owner_id)
    if user is None:
        raise RuntimeError("配额用户不存在")
    if user.role != ROLE_ADMIN and user.workflow_quota is not None:
        if user.workflow_reserved <= 0:
            raise RuntimeError("配额预留计数不一致")
        user.workflow_reserved -= 1
        user.workflow_charged += 1
    usage.status = USAGE_CHARGED
    usage.charged_at = _utcnow()
    return True


def release(db: Session, record_id: int) -> bool:
    usage = (
        db.query(WorkflowUsage)
        .filter(WorkflowUsage.record_id == record_id)
        .first()
    )
    if usage is None or usage.status in (USAGE_RELEASED, USAGE_CHARGED):
        return False
    user = db.get(User, usage.owner_id)
    if (
        user is not None
        and user.role != ROLE_ADMIN
        and user.workflow_quota is not None
    ):
        if user.workflow_reserved <= 0:
            raise RuntimeError("配额预留计数不一致")
        user.workflow_reserved -= 1
    usage.status = USAGE_RELEASED
    usage.released_at = _utcnow()
    return True
