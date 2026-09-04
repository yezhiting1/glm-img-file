"""Excel 导出路由。

清单第 8.5 节:
  GET    /api/export/summary
  POST   /api/export/excel
  GET    /api/export/download/{filename}
"""
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import AuthContext, get_auth_context
from app.services import excel_service

router = APIRouter(prefix="/api/export", tags=["export"])


@router.get("/summary")
async def get_summary(
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """获取导出汇总信息。"""
    return excel_service.get_export_summary(db, ctx.owner_id)


@router.post("/excel")
async def export_excel(
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """生成导出 Excel 文件。"""
    return excel_service.export_excel(db, ctx.owner_id, ctx.user.id)


@router.get("/download/{filename}")
async def download_file(
    filename: str,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
):
    """下载导出文件。"""
    file_path = excel_service.get_export_file_path(db, filename, ctx.owner_id)
    return FileResponse(
        str(file_path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )
