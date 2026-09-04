"""Excel 模板路由。

清单第 8.2 节:
  POST   /api/templates/upload
  GET    /api/templates/current
  GET    /api/templates/{id}/sheets
  POST   /api/templates/{id}/inspect
  PUT    /api/templates/{id}/mapping
  POST   /api/templates/{id}/test
"""
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import AuthContext, get_auth_context
from app.schemas import FieldMappingUpdate, SheetInfo
from app.services import template_service
from app.services.template_service import TemplateError

router = APIRouter(prefix="/api/templates", tags=["templates"])


@router.post("/upload")
async def upload_template(
    file: UploadFile = File(...),
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """上传 Excel 模板。"""
    template = template_service.upload_template(db, file, ctx.owner_id)
    return template_service.template_to_info(template)


@router.get("/current")
async def get_current_template(
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any] | None:
    """获取当前活跃模板配置。"""
    template = template_service.get_active_template(db, ctx.owner_id)
    if template is None:
        return None
    return template_service.template_to_info(template)


@router.get("/{template_id}/sheets", response_model=list[SheetInfo])
async def get_sheets(
    template_id: int,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """读取模板所有工作表。"""
    template = template_service.get_template_or_404(db, template_id, ctx.owner_id)
    return template_service.list_sheets(template)


@router.post("/{template_id}/inspect")
async def inspect_template(
    template_id: int,
    sheet_name: str | None = Query(None),
    header_row: int | None = Query(None),
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """检查模板,自动检测表头行和字段映射。"""
    template = template_service.get_template_or_404(db, template_id, ctx.owner_id)
    return template_service.inspect_template(template, sheet_name, header_row)


@router.put("/{template_id}/mapping")
async def update_mapping(
    template_id: int,
    config: FieldMappingUpdate,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """保存字段映射配置(含 base_write_row 计算)。"""
    template = template_service.get_template_or_404(db, template_id, ctx.owner_id)
    template = template_service.save_mapping(
        db,
        template,
        config.target_sheet,
        config.header_row,
        config.start_row,
        config.style_source_row,
        config.field_mapping,
    )
    return template_service.template_to_info(template)


@router.post("/{template_id}/test")
async def test_template(
    template_id: int,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """测试模板配置。"""
    template = template_service.get_template_or_404(db, template_id, ctx.owner_id)
    return template_service.test_mapping(template)
