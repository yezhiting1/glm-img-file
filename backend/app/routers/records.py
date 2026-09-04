"""记录管理路由。

清单第 8.4 节:
  GET    /api/records
  GET    /api/records/{id}
  PATCH  /api/records/{id}
  DELETE /api/records/{id}
  POST   /api/records/{id}/reclassify
"""
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import AuthContext, get_auth_context
from app.field_mapping import FIELD_TO_COLUMN, TAXONOMY_FIELDS, IMAGE_EXTRACTED_FIELDS
from app.models import SpecimenRecord, STATUS_COMPLETED
from app.schemas import RecordDetail, RecordSummary, RecordUpdate
from app.services import recognition_service as svc
from app.services import materials_service

router = APIRouter(prefix="/api/records", tags=["records"])


@router.get("")
async def list_records(
    search: str | None = Query(None, description="按中名或图像编号搜索"),
    status: str | None = Query(None, description="按状态筛选"),
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """获取记录列表,支持搜索和状态筛选。"""
    q = db.query(SpecimenRecord).filter(
        SpecimenRecord.owner_id == ctx.owner_id,
        SpecimenRecord.status != "discarded",
    )

    if search:
        keyword = f"%{search.strip()}%"
        q = q.filter(
            (SpecimenRecord.zhongming.like(keyword))
            | (SpecimenRecord.tuxiang.like(keyword))
        )

    if status:
        q = q.filter(SpecimenRecord.status == status)

    records = q.order_by(SpecimenRecord.id.desc()).all()
    return [svc.record_to_detail(r) for r in records]


@router.get("/{record_id}")
async def get_record(
    record_id: int,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """获取记录详情。"""
    record = db.query(SpecimenRecord).filter(
        SpecimenRecord.id == record_id,
        SpecimenRecord.owner_id == ctx.owner_id,
    ).first()
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    return svc.record_to_detail(record)


@router.patch("/{record_id}")
async def update_record(
    record_id: int,
    req: RecordUpdate,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """编辑记录。

    如果修改了分类字段且通过校验,同步更新 taxonomy_cache。
    如果修改了中名,清除旧的 confirmed_extraction 并允许重新分类。
    """
    record = db.query(SpecimenRecord).filter(
        SpecimenRecord.id == record_id,
        SpecimenRecord.owner_id == ctx.owner_id,
    ).first()
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")

    if req.fields:
        old_zhongming = record.zhongming
        for field, value in req.fields.items():
            col = FIELD_TO_COLUMN.get(field)
            if col:
                setattr(record, col, str(value).strip())

        # 如果修改了分类字段且记录是 completed,校验后同步缓存
        new_zhongming = req.fields.get("中名", old_zhongming).strip()
        if record.status == STATUS_COMPLETED:
            taxonomy = {}
            for field in TAXONOMY_FIELDS:
                col = FIELD_TO_COLUMN[field]
                taxonomy[field] = str(getattr(record, col, "")).strip()

            errors = svc.validate_taxonomy(taxonomy)
            if not errors:
                # 校验通过,更新缓存
                svc._update_taxonomy_cache(
                    db,
                    record.owner_id,
                    new_zhongming,
                    taxonomy,
                )
            # 校验不通过时不报错,只不更新缓存(用户可以在记录管理继续编辑)

        db.commit()
        db.refresh(record)

    return svc.record_to_detail(record)


@router.delete("/{record_id}")
async def delete_record(
    record_id: int,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """删除记录。"""
    record = db.query(SpecimenRecord).filter(
        SpecimenRecord.id == record_id,
        SpecimenRecord.owner_id == ctx.owner_id,
    ).first()
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")

    # 删除关联图片文件(原图和处理图)
    from app.config import IMAGES_DIR, PROCESSED_IMAGES_DIR
    from pathlib import Path

    if record.image_path:
        img_file = Path(record.image_path)
        if img_file.exists():
            img_file.unlink(missing_ok=True)
    if record.processed_image_path:
        proc_file = Path(record.processed_image_path)
        if proc_file.exists():
            proc_file.unlink(missing_ok=True)

    materials_service.reset_item_for_deleted_record(db, record_id)
    from app.services import quota_service
    quota_service.release(db, record_id)
    record.status = "discarded"
    db.commit()
    return {"status": "deleted"}


@router.post("/{record_id}/reclassify")
async def reclassify_record(
    record_id: int,
    ctx: AuthContext = Depends(get_auth_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """对记录重新执行分类(不重新识别图片)。

    清单 8.4: 只允许对已经保存 confirmed_extraction_json 的记录重新分类。
    """
    record = db.query(SpecimenRecord).filter(
        SpecimenRecord.id == record_id,
        SpecimenRecord.owner_id == ctx.owner_id,
    ).first()
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")

    # 必须有已确认的图片信息
    confirmed_data = svc.parse_confirmed(record)
    if not confirmed_data or "confirmed" not in confirmed_data:
        raise HTTPException(
            status_code=400,
            detail="该记录尚未确认图片信息,无法重新分类",
        )

    confirmed = confirmed_data["confirmed"]
    material_item = materials_service.get_linked_item(db, record_id)
    result = await svc.confirm_and_classify(
        db,
        record,
        confirmed,
        duplicate_action="replace",
        existing_record=None,
        material_item=material_item,
    )

    # 计算 Excel 行号
    from app.models import ExcelTemplate

    template = (
        db.query(ExcelTemplate)
        .filter(
            ExcelTemplate.owner_id == ctx.owner_id,
            ExcelTemplate.is_active == True,  # noqa: E712
        )
        .first()
    )
    excel_row = None
    if template and template.base_write_row and result.status == STATUS_COMPLETED:
        completed_before = (
            db.query(SpecimenRecord)
            .filter(
                SpecimenRecord.status == STATUS_COMPLETED,
                SpecimenRecord.owner_id == ctx.owner_id,
                SpecimenRecord.id <= result.id,
            )
            .count()
        )
        excel_row = template.base_write_row + completed_before - 1

    return {
        "record_id": result.id,
        "status": result.status,
        "fields": svc.record_to_fields(result),
        "excel_row": excel_row or 0,
        "warnings": json.loads(result.warnings_json) if result.warnings_json else [],
    }
