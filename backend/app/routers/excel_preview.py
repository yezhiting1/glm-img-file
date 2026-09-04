"""Excel 实时预览路由。

清单第 8.6 节:
  GET /api/excel/preview
  GET /api/excel/preview?mode=target&limit=100
  GET /api/excel/preview?mode=all&limit=100
"""
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import AuthContext, get_auth_context
from app.services import preview_service

router = APIRouter(prefix="/api/excel", tags=["excel-preview"])


@router.get("/preview")
async def get_preview(
    mode: str = Query("target", pattern="^(target|all)$"),
    limit: int = Query(100, ge=1, le=1000),
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """获取 Excel 实时预览数据。"""
    return preview_service.get_preview(db, mode, limit, ctx.owner_id)
