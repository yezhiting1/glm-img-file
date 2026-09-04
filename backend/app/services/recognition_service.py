"""识别服务:图片保存、模型提取、确认入表、分类补全。

清单第 8.3 节流程:
  extract -> 保存图片+创建记录+预处理+调用视觉模型提取5项 -> awaiting_confirmation
  re-extract -> 同一原图重新提取5项
  confirm-extraction -> 保存确认值 -> 查缓存/调分类 -> 校验 -> completed
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import IMAGES_DIR, PROCESSED_IMAGES_DIR
from app.field_mapping import (
    COLUMN_TO_FIELD,
    FIELD_TO_COLUMN,
    IMAGE_EXTRACTED_FIELDS,
    TAXONOMY_FIELDS,
)
from app.models import (
    AppSettings,
    MaterialItem,
    SpecimenRecord,
    TaxonomyCache,
    MATERIAL_STATUS_COMPLETED,
    MATERIAL_STATUS_FAILED,
    MATERIAL_STATUS_PROCESSING,
    STATUS_AWAITING_CONFIRMATION,
    STATUS_CLASSIFICATION_FAILED,
    STATUS_COMPLETED,
    STATUS_EXTRACTING,
    STATUS_EXTRACTION_FAILED,
)
from app.routers.settings import _get_or_create_settings
from app.services.model_provider import ModelError, VisionModelClient
from app.services import quota_service


def _load_prompt(db: Session, attr: str, default_filename: str) -> str:
    """从数据库读提示词,空则用默认文件。"""
    s = _get_or_create_settings(db)
    val = getattr(s, attr)
    if val:
        return val
    from app.routers.settings import _load_default_prompt
    return _load_default_prompt(default_filename)


def _get_model_client(db: Session) -> VisionModelClient:
    """从已保存配置创建模型客户端。"""
    s = _get_or_create_settings(db)
    if not s.base_url or not s.api_key or not s.model_name:
        raise HTTPException(
            status_code=400,
            detail="尚未配置模型 API,请先在设置页面配置 Base URL、API Key 和模型名称",
        )
    return VisionModelClient(s.base_url, s.api_key, s.model_name)


def save_uploaded_image(file_content: bytes, original_filename: str) -> tuple[str, str]:
    """保存上传图片,返回 (文件名, 绝对路径)。"""
    suffix = uuid.uuid4().hex[:8]
    ext = Path(original_filename).suffix.lower() or ".jpg"
    stored_name = f"img_{suffix}{ext}"
    stored_path = IMAGES_DIR / stored_name
    with stored_path.open("wb") as f:
        f.write(file_content)
    return stored_name, str(stored_path)


def get_active_draft(db: Session, owner_id: int) -> SpecimenRecord | None:
    """获取当前活跃草稿(非 completed/discarded 的最新记录)。"""
    from app.models import ACTIVE_DRAFT_STATUSES

    return (
        db.query(SpecimenRecord)
        .filter(
            SpecimenRecord.owner_id == owner_id,
            SpecimenRecord.status.in_(ACTIVE_DRAFT_STATUSES),
        )
        .order_by(SpecimenRecord.id.desc())
        .first()
    )


def discard_draft(db: Session, record: SpecimenRecord) -> None:
    """放弃草稿(状态改为 discarded)。"""
    record.status = "discarded"
    quota_service.release(db, record.id)
    db.commit()


async def extract_image_info(
    db: Session,
    image_path: str,
    image_filename: str,
    rotation_degrees: int = 0,
    record: SpecimenRecord | None = None,
    precomputed_result: dict[str, Any] | None = None,
    owner_id: int | None = None,
) -> SpecimenRecord:
    """上传图片并提取5项图片原始信息。

    清单 8.3 extract:
    - 保存图片
    - 创建记录
    - 接收旋转角度并生成预处理图片
    - 调用视觉模型提取5项
    - 状态设为 awaiting_confirmation

    当 precomputed_result 不为 None 时，跳过模型调用直接使用预加载结果。
    """
    if record is None:
        if owner_id is None:
            raise ValueError("owner_id is required for a new record")
        record = SpecimenRecord(owner_id=owner_id)
        db.add(record)
    record.image_filename = image_filename
    record.image_path = image_path
    record.rotation_degrees = rotation_degrees % 360
    record.status = STATUS_EXTRACTING
    db.flush()
    quota_service.reserve(db, record.owner_id, record.id)
    db.refresh(record)

    if precomputed_result is not None:
        result = precomputed_result
    else:
        client = _get_model_client(db)
        prompt = _load_prompt(db, "recognition_prompt", "recognition_prompt.txt")

        try:
            result = await client.recognize_image(image_path, prompt, rotation_degrees)
        except ModelError as e:
            record.status = STATUS_EXTRACTION_FAILED
            record.warnings_json = json.dumps([str(e)], ensure_ascii=False)
            quota_service.release(db, record.id)
            db.commit()
            raise HTTPException(status_code=502, detail=f"图片识别失败: {e}")

    # 保存模型原始响应
    record.raw_model_response = json.dumps(result, ensure_ascii=False)

    # 提取5个图片原始信息字段
    extracted = {}
    for field in IMAGE_EXTRACTED_FIELDS:
        val = result.get(field, "")
        extracted[field] = str(val).strip() if val else ""

    # 置信度和证据(用于前端显示)
    confidence = result.get("confidence", {})
    evidence = result.get("evidence", {})
    warnings = result.get("warnings", [])
    if not isinstance(warnings, list):
        warnings = [str(warnings)]

    record.extracted_draft_json = json.dumps(
        {
            "extracted": extracted,
            "confidence": confidence,
            "evidence": evidence,
            "warnings": warnings,
        },
        ensure_ascii=False,
    )
    record.warnings_json = json.dumps(warnings, ensure_ascii=False)

    # 同时写入扁平字段(便于查询)
    for field, col in FIELD_TO_COLUMN.items():
        if field in extracted:
            setattr(record, col, extracted[field])

    record.status = STATUS_AWAITING_CONFIRMATION
    db.commit()
    db.refresh(record)
    return record


async def re_extract_image_info(
    db: Session,
    record: SpecimenRecord,
) -> SpecimenRecord:
    """重新识别:使用同一张原图重新调用视觉模型。"""
    client = _get_model_client(db)
    prompt = _load_prompt(db, "recognition_prompt", "recognition_prompt.txt")

    record.status = STATUS_EXTRACTING
    db.commit()
    quota_service.reserve(db, record.owner_id, record.id)

    try:
        result = await client.recognize_image(
            record.image_path, prompt, record.rotation_degrees
        )
    except ModelError as e:
        record.status = STATUS_EXTRACTION_FAILED
        record.warnings_json = json.dumps([str(e)], ensure_ascii=False)
        quota_service.release(db, record.id)
        db.commit()
        raise HTTPException(status_code=502, detail=f"重新识别失败: {e}")

    record.raw_model_response = json.dumps(result, ensure_ascii=False)

    extracted = {}
    for field in IMAGE_EXTRACTED_FIELDS:
        val = result.get(field, "")
        extracted[field] = str(val).strip() if val else ""

    confidence = result.get("confidence", {})
    evidence = result.get("evidence", {})
    warnings = result.get("warnings", [])
    if not isinstance(warnings, list):
        warnings = [str(warnings)]

    record.extracted_draft_json = json.dumps(
        {
            "extracted": extracted,
            "confidence": confidence,
            "evidence": evidence,
            "warnings": warnings,
        },
        ensure_ascii=False,
    )
    record.warnings_json = json.dumps(warnings, ensure_ascii=False)

    for field, col in FIELD_TO_COLUMN.items():
        if field in extracted:
            setattr(record, col, extracted[field])

    record.status = STATUS_AWAITING_CONFIRMATION
    record.confirmed_extraction_json = ""  # 清除旧确认
    db.commit()
    db.refresh(record)
    return record


def check_duplicate_tuxiang(
    db: Session, tuxiang: str, owner_id: int, exclude_id: int | None = None
) -> SpecimenRecord | None:
    """检查图像编号是否在已完成记录中已存在。"""
    if not tuxiang:
        return None
    q = db.query(SpecimenRecord).filter(
        SpecimenRecord.owner_id == owner_id,
        SpecimenRecord.tuxiang == tuxiang,
        SpecimenRecord.status == STATUS_COMPLETED,
    )
    if exclude_id is not None:
        q = q.filter(SpecimenRecord.id != exclude_id)
    return q.first()


def validate_confirmed_fields(confirmed: dict[str, str]) -> list[str]:
    """校验确认字段,返回警告列表。

    清单要求:
    - 中名和图像必填
    - 产地3或采集日期为空时警告但允许继续
    - 采集人允许为空
    """
    warnings = []
    if not confirmed.get("中名", "").strip():
        raise HTTPException(
            status_code=422,
            detail="中名不能为空,请填写后再确认入表",
        )
    if not confirmed.get("图像", "").strip():
        raise HTTPException(
            status_code=422,
            detail="图像编号不能为空,请填写后再确认入表",
        )
    if not confirmed.get("产地3", "").strip():
        warnings.append("产地3 为空")
    if not confirmed.get("采集日期", "").strip():
        warnings.append("采集日期 为空")
    return warnings


async def confirm_and_classify(
    db: Session,
    record: SpecimenRecord,
    confirmed: dict[str, str],
    duplicate_action: str | None = None,
    existing_record: SpecimenRecord | None = None,
    material_item: MaterialItem | None = None,
) -> SpecimenRecord:
    """确认图片信息并自动入表(分类补全+校验+保存)。

    清单 8.3 confirm-extraction 完整流程。
    """
    # 1. 校验必填字段
    field_warnings = validate_confirmed_fields(confirmed)

    # 2. 处理重复编号
    billing_record_id = record.id
    if existing_record is not None and duplicate_action != "replace":
        raise HTTPException(
            status_code=409,
            detail="图像编号已存在",
        )
    target = record
    if material_item is not None:
        material_item.record_id = target.id
        material_item.status = MATERIAL_STATUS_PROCESSING
        material_item.error_message = ""

    # 3. 保存确认值到 confirmed_extraction_json
    target.confirmed_extraction_json = json.dumps(
        {"confirmed": confirmed}, ensure_ascii=False
    )

    # 4. 更新扁平字段(5项确认值)
    for field in IMAGE_EXTRACTED_FIELDS:
        if field in confirmed:
            setattr(target, FIELD_TO_COLUMN[field], confirmed[field])

    target.status = STATUS_EXTRACTING if False else "classifying"
    db.commit()

    # 5. 查分类缓存
    confirmed_zhongming = confirmed["中名"].strip()
    taxonomy = _query_taxonomy_cache(db, record.owner_id, confirmed_zhongming)

    # 6. 缓存未命中则调用模型
    try:
        if taxonomy is None:
            taxonomy = await _call_taxonomy_model(db, confirmed_zhongming)

        # 7. 校验分类字段
        validation_errors = validate_taxonomy(taxonomy)

        # 8. 首次校验失败时自动纠正重试1次
        if validation_errors:
            taxonomy2 = await _call_taxonomy_model_with_errors(
                db, confirmed_zhongming, validation_errors
            )
            taxonomy = taxonomy2
            validation_errors = validate_taxonomy(taxonomy)
    except HTTPException:
        target.status = STATUS_CLASSIFICATION_FAILED
        if material_item is not None:
            material_item.status = MATERIAL_STATUS_FAILED
            material_item.error_message = "分类补全失败"
        db.commit()
        raise

    if validation_errors:
        # 分类失败:保留5项确认信息,不写缓存
        target.status = STATUS_CLASSIFICATION_FAILED
        target.taxonomy_result_json = json.dumps(taxonomy, ensure_ascii=False)
        all_warnings = field_warnings + validation_errors
        target.warnings_json = json.dumps(all_warnings, ensure_ascii=False)
        if material_item is not None:
            material_item.status = MATERIAL_STATUS_FAILED
            material_item.error_message = "；".join(validation_errors)
        db.commit()
        db.refresh(target)
        return target

    # 9. 校验通过:写入8个分类字段
    for field in TAXONOMY_FIELDS:
        if field in taxonomy:
            setattr(target, FIELD_TO_COLUMN[field], str(taxonomy[field]).strip())

    target.taxonomy_result_json = json.dumps(taxonomy, ensure_ascii=False)

    # 10. 更新缓存(只有校验通过才写)
    _update_taxonomy_cache(db, record.owner_id, confirmed_zhongming, taxonomy)

    # 11. 合并13字段完成,状态设为 completed
    target.warnings_json = json.dumps(field_warnings, ensure_ascii=False)
    result = target
    if existing_record is not None:
        for field in IMAGE_EXTRACTED_FIELDS + TAXONOMY_FIELDS:
            column = FIELD_TO_COLUMN[field]
            setattr(existing_record, column, getattr(target, column))
        existing_record.confirmed_extraction_json = target.confirmed_extraction_json
        existing_record.taxonomy_result_json = target.taxonomy_result_json
        existing_record.warnings_json = target.warnings_json
        existing_record.status = STATUS_COMPLETED
        target.status = "discarded"
        result = existing_record
    else:
        target.status = STATUS_COMPLETED
    if material_item is not None:
        material_item.record_id = result.id
        material_item.status = MATERIAL_STATUS_COMPLETED
        material_item.error_message = ""
    quota_service.charge(db, billing_record_id)
    db.commit()
    db.refresh(result)
    return result


def _query_taxonomy_cache(
    db: Session, owner_id: int, zhongming: str
) -> dict[str, Any] | None:
    """查询分类缓存。"""
    cache = db.query(TaxonomyCache).filter(
        TaxonomyCache.owner_id == owner_id,
        TaxonomyCache.zhongming == zhongming,
    ).first()
    if cache is None:
        return None
    result = {}
    for field in TAXONOMY_FIELDS:
        col = FIELD_TO_COLUMN[field]
        result[field] = getattr(cache, col, "")
    # 缓存也要校验
    errors = validate_taxonomy(result)
    if errors:
        # 无效缓存,删除并重新调模型
        db.delete(cache)
        db.commit()
        return None
    return result


async def _call_taxonomy_model(
    db: Session, zhongming: str
) -> dict[str, Any]:
    """调用分类模型。"""
    client = _get_model_client(db)
    prompt = _load_prompt(db, "taxonomy_prompt", "taxonomy_prompt.txt")
    try:
        return await client.complete_taxonomy(zhongming, prompt)
    except ModelError as e:
        raise HTTPException(status_code=502, detail=f"分类补全失败: {e}")


async def _call_taxonomy_model_with_errors(
    db: Session, zhongming: str, errors: list[str]
) -> dict[str, Any]:
    """带错误提示的纠正重试。"""
    client = _get_model_client(db)
    base_prompt = _load_prompt(db, "taxonomy_prompt", "taxonomy_prompt.txt")
    error_hint = (
        f"\n\n上次返回的结果有以下问题,请修正后重新返回:\n"
        + "\n".join(f"- {e}" for e in errors)
    )
    prompt = base_prompt + error_hint
    try:
        return await client.complete_taxonomy(zhongming, prompt)
    except ModelError as e:
        raise HTTPException(status_code=502, detail=f"分类纠正重试失败: {e}")


def validate_taxonomy(taxonomy: dict[str, Any]) -> list[str]:
    """校验8个分类字段,返回错误列表(空列表表示通过)。

    清单第 11 节校验规则。
    """
    import re

    errors = []

    # 1. 8个字段全部存在且非空
    for field in TAXONOMY_FIELDS:
        val = str(taxonomy.get(field, "")).strip()
        if not val:
            errors.append(f"{field} 为空")
            taxonomy[field] = ""

    if errors:
        return errors

    # 2. 拉丁文字段格式校验
    latin_pattern = re.compile(r"^[A-Za-z][A-Za-z\s\-]+$")
    latin_fields = ["Phylum", "Class", "Order", "科名", "属名", "种名"]
    for field in latin_fields:
        val = str(taxonomy.get(field, "")).strip()
        if val and not latin_pattern.match(val):
            errors.append(f"{field} '{val}' 不是有效的拉丁文格式")

    # 3. 中文格式校验
    for field in ["纲", "中文科名"]:
        val = str(taxonomy.get(field, "")).strip()
        if val and not re.search(r"[\u4e00-\u9fff]", val):
            errors.append(f"{field} '{val}' 必须包含中文字符")

    # 4. 属名首字母大写
    shu = str(taxonomy.get("属名", "")).strip()
    if shu and not shu[0].isupper():
        errors.append(f"属名 '{shu}' 首字母必须大写")

    # 5. 种名首字母小写,只能是种加词
    zhong = str(taxonomy.get("种名", "")).strip()
    if zhong:
        if zhong[0].isupper():
            errors.append(f"种名 '{zhong}' 首字母必须小写")
        # 不能包含空格(完整双名会有空格)
        if " " in zhong:
            errors.append(f"种名 '{zhong}' 只能是种加词,不能包含空格(完整双名)")

    return errors


def _update_taxonomy_cache(
    db: Session,
    owner_id: int,
    zhongming: str,
    taxonomy: dict[str, Any],
) -> None:
    """更新分类缓存(只有校验通过才调用)。"""
    cache = db.query(TaxonomyCache).filter(
        TaxonomyCache.owner_id == owner_id,
        TaxonomyCache.zhongming == zhongming,
    ).first()
    if cache is None:
        cache = TaxonomyCache(owner_id=owner_id, zhongming=zhongming)
        db.add(cache)
    for field in TAXONOMY_FIELDS:
        col = FIELD_TO_COLUMN[field]
        setattr(cache, col, str(taxonomy.get(field, "")).strip())
    db.flush()


def record_to_fields(record: SpecimenRecord) -> dict[str, str]:
    """将记录的13个扁平字段转为中文字段名->值的字典。"""
    result = {}
    for field, col in FIELD_TO_COLUMN.items():
        result[field] = str(getattr(record, col, "") or "")
    return result


def record_to_detail(record: SpecimenRecord) -> dict[str, Any]:
    """将记录转为详情字典(含JSON草稿)。"""
    return {
        "id": record.id,
        "image_filename": record.image_filename,
        "image_path": record.image_path,
        "processed_image_path": record.processed_image_path,
        "rotation_degrees": record.rotation_degrees,
        "status": record.status,
        "extracted_draft": json.loads(record.extracted_draft_json) if record.extracted_draft_json else {},
        "confirmed_extraction": json.loads(record.confirmed_extraction_json) if record.confirmed_extraction_json else {},
        "taxonomy_result": json.loads(record.taxonomy_result_json) if record.taxonomy_result_json else {},
        "warnings": json.loads(record.warnings_json) if record.warnings_json else [],
        "fields": record_to_fields(record),
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


def parse_extracted_draft(record: SpecimenRecord) -> dict[str, Any]:
    """解析草稿JSON,返回 extracted/confidence/evidence/warnings。"""
    if not record.extracted_draft_json:
        return {"extracted": {}, "confidence": {}, "evidence": {}, "warnings": []}
    return json.loads(record.extracted_draft_json)


def parse_confirmed(record: SpecimenRecord) -> dict[str, Any]:
    """解析已确认的5项信息。"""
    if not record.confirmed_extraction_json:
        return {}
    return json.loads(record.confirmed_extraction_json)
