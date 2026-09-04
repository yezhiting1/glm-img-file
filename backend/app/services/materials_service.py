"""数据素材 ZIP、处理队列、跳过记录与导出服务。"""
from __future__ import annotations

import asyncio
import json
import shutil
import struct
import uuid
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import (
    IMAGES_DIR,
    MATERIAL_EXPORTS_DIR,
    MATERIAL_IMAGES_DIR,
    MATERIAL_ZIPS_DIR,
    settings,
)
from app.models import (
    ACTIVE_DRAFT_STATUSES,
    MATERIAL_STATUS_COMPLETED,
    MATERIAL_STATUS_FAILED,
    MATERIAL_STATUS_PENDING,
    MATERIAL_STATUS_PROCESSING,
    MATERIAL_STATUS_SKIPPED,
    PREFETCH_STATUS_FAILED,
    PREFETCH_STATUS_QUEUED,
    PREFETCH_STATUS_READY,
    PREFETCH_STATUS_RUNNING,
    STATUS_DISCARDED,
    STATUS_EXTRACTION_FAILED,
    MaterialBatch,
    MaterialItem,
    MaterialPrefetchResult,
    SpecimenRecord,
)
from app.services import recognition_service


def resolve_material_image_path(stored_path: str) -> Path:
    stored = Path(stored_path)
    if stored.is_file():
        return stored
    normalized = stored_path.replace("\\", "/")
    marker = "/materials/images/"
    if marker in normalized:
        portable = MATERIAL_IMAGES_DIR / normalized.split(marker, 1)[1]
        if portable.is_file():
            return portable
    matches = list(MATERIAL_IMAGES_DIR.rglob(stored.name))
    return matches[0] if len(matches) == 1 else stored


ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
ZIP_EOCD_SIGNATURE = b"PK\x05\x06"
ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"


def get_active_batch(db: Session, owner_id: int) -> MaterialBatch | None:
    return (
        db.query(MaterialBatch)
        .filter(
            MaterialBatch.owner_id == owner_id,
            MaterialBatch.is_active.is_(True),
        )
        .order_by(MaterialBatch.id.desc())
        .first()
    )


def get_summary(db: Session, owner_id: int) -> dict[str, Any]:
    batch = get_active_batch(db, owner_id)
    summary: dict[str, Any] = {
        "batch": batch,
        "total_count": 0,
        "pending_count": 0,
        "processing_count": 0,
        "completed_count": 0,
        "skipped_count": 0,
        "failed_count": 0,
    }
    if batch is None:
        return summary

    summary["total_count"] = batch.total_count
    rows = (
        db.query(MaterialItem.status, func.count(MaterialItem.id))
        .filter(MaterialItem.batch_id == batch.id)
        .group_by(MaterialItem.status)
        .all()
    )
    counts = {status: count for status, count in rows}
    summary["pending_count"] = counts.get(MATERIAL_STATUS_PENDING, 0)
    summary["processing_count"] = counts.get(MATERIAL_STATUS_PROCESSING, 0)
    summary["completed_count"] = counts.get(MATERIAL_STATUS_COMPLETED, 0)
    summary["skipped_count"] = counts.get(MATERIAL_STATUS_SKIPPED, 0)
    summary["failed_count"] = counts.get(MATERIAL_STATUS_FAILED, 0)
    return summary


def _normalized_archive_path(filename: str) -> str | None:
    normalized = filename.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or not path.name
        or ".." in path.parts
        or (path.parts and path.parts[0].endswith(":"))
    ):
        return None
    if "__MACOSX" in path.parts or path.name.startswith("."):
        return None
    return path.as_posix()


def _zip_directory_metrics(source_path: Path) -> tuple[int, int] | None:
    file_size = source_path.stat().st_size
    tail_size = min(file_size, 65557)
    with source_path.open("rb") as source:
        source.seek(file_size - tail_size)
        tail = source.read(tail_size)

        eocd_index = tail.rfind(ZIP_EOCD_SIGNATURE)
        while eocd_index >= 0:
            if eocd_index + 22 <= len(tail):
                comment_size = struct.unpack_from("<H", tail, eocd_index + 20)[0]
                if eocd_index + 22 + comment_size == len(tail):
                    break
            eocd_index = tail.rfind(ZIP_EOCD_SIGNATURE, 0, eocd_index)
        if eocd_index < 0:
            return None

        fields = struct.unpack_from("<4s4H2LH", tail, eocd_index)
        entry_count = fields[4]
        central_directory_size = fields[5]
        if entry_count != 0xFFFF and central_directory_size != 0xFFFFFFFF:
            return entry_count, central_directory_size

        eocd_offset = file_size - tail_size + eocd_index
        locator_offset = eocd_offset - 20
        if locator_offset < 0:
            return None
        source.seek(locator_offset)
        locator = source.read(20)
        if len(locator) != 20 or locator[:4] != ZIP64_LOCATOR_SIGNATURE:
            return None
        zip64_offset = struct.unpack_from("<Q", locator, 8)[0]
        if zip64_offset > file_size - 56:
            return None
        source.seek(zip64_offset)
        zip64_eocd = source.read(56)
        if len(zip64_eocd) != 56 or zip64_eocd[:4] != ZIP64_EOCD_SIGNATURE:
            return None
        zip64_fields = struct.unpack("<4sQ2H2L4Q", zip64_eocd)
        return zip64_fields[7], zip64_fields[8]


def _validate_zip_directory(source_path: Path) -> None:
    metrics = _zip_directory_metrics(source_path)
    if metrics is None:
        raise HTTPException(status_code=400, detail="压缩包目录信息无效")
    entry_count, central_directory_size = metrics
    if entry_count > settings.material_zip_max_entries:
        raise HTTPException(
            status_code=413,
            detail=f"压缩包最多包含 {settings.material_zip_max_entries} 个文件条目",
        )
    max_directory_bytes = settings.material_zip_max_entries * 1024
    if central_directory_size > max_directory_bytes:
        raise HTTPException(status_code=413, detail="压缩包目录信息过大")


def _valid_image_members(zf: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str]]:
    members: list[tuple[zipfile.ZipInfo, str]] = []
    total_size = 0
    max_bytes = settings.material_zip_max_uncompressed_mb * 1024 * 1024

    for info in zf.infolist():
        if info.is_dir():
            continue
        archive_path = _normalized_archive_path(info.filename)
        if archive_path is None:
            unsafe_path = PurePosixPath(info.filename.replace("\\", "/"))
            if (
                unsafe_path.is_absolute()
                or ".." in unsafe_path.parts
                or (unsafe_path.parts and unsafe_path.parts[0].endswith(":"))
            ):
                raise HTTPException(status_code=400, detail="压缩包包含不安全的文件路径")
            continue
        if PurePosixPath(archive_path).suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
            continue
        if info.flag_bits & 0x1:
            raise HTTPException(status_code=400, detail="不支持加密压缩包")
        total_size += info.file_size
        if total_size > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"解压后图片总大小不能超过 {settings.material_zip_max_uncompressed_mb} MB",
            )
        members.append((info, archive_path))
        if len(members) > settings.material_zip_max_images:
            raise HTTPException(
                status_code=413,
                detail=f"单个压缩包最多包含 {settings.material_zip_max_images} 张图片",
            )
    return members


def create_batch_from_zip_path(
    db: Session,
    source_path: Path,
    original_filename: str,
    owner_id: int,
) -> dict[str, Any]:
    if Path(original_filename).suffix.lower() != ".zip":
        raise HTTPException(status_code=400, detail="请上传 ZIP 格式的素材压缩包")
    max_zip_bytes = settings.material_zip_max_size_mb * 1024 * 1024
    if source_path.stat().st_size > max_zip_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"压缩包不能超过 {settings.material_zip_max_size_mb} MB",
        )
    _validate_zip_directory(source_path)

    try:
        zf = zipfile.ZipFile(source_path)
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="压缩包损坏或不是有效的 ZIP 文件") from exc

    batch_token = uuid.uuid4().hex
    zip_path = MATERIAL_ZIPS_DIR / f"materials_{batch_token}.zip"
    extract_dir = MATERIAL_IMAGES_DIR / f"batch_{batch_token}"
    extracted: list[dict[str, str]] = []

    try:
        members = _valid_image_members(zf)
        if not members:
            raise HTTPException(
                status_code=400,
                detail="压缩包中没有支持的图片,请放入 JPG/JPEG/PNG/WebP 文件",
            )

        extract_dir.mkdir(parents=True, exist_ok=False)
        for info, archive_path in members:
            suffix = PurePosixPath(archive_path).suffix.lower()
            stored_name = f"material_{uuid.uuid4().hex}{suffix}"
            stored_path = extract_dir / stored_name
            with zf.open(info) as source, stored_path.open("wb") as target:
                shutil.copyfileobj(source, target)
            try:
                with Image.open(resolve_material_image_path(str(stored_path))) as image:
                    if (
                        image.width * image.height
                        > settings.material_image_max_pixels
                    ):
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                "单张图片像素不能超过 "
                                f"{settings.material_image_max_pixels}"
                            ),
                        )
                    image.verify()
            except (UnidentifiedImageError, OSError):
                stored_path.unlink(missing_ok=True)
                continue
            extracted.append(
                {
                    "original_filename": PurePosixPath(archive_path).name,
                    "archive_path": archive_path,
                    "stored_path": str(stored_path),
                }
            )

        if not extracted:
            raise HTTPException(status_code=400, detail="压缩包中没有可读取的有效图片")

        zf.close()
        shutil.move(str(source_path), zip_path)
        db.query(MaterialBatch).filter(
            MaterialBatch.owner_id == owner_id,
            MaterialBatch.is_active.is_(True),
        ).update(
            {MaterialBatch.is_active: False},
            synchronize_session=False,
        )
        batch = MaterialBatch(
            owner_id=owner_id,
            original_filename=Path(original_filename).name,
            stored_zip_path=str(zip_path),
            extract_dir=str(extract_dir),
            total_count=len(extracted),
            is_active=True,
        )
        db.add(batch)
        db.flush()
        db.add_all(
            [
                MaterialItem(
                    batch_id=batch.id,
                    sequence=index,
                    original_filename=item["original_filename"],
                    archive_path=item["archive_path"],
                    stored_path=item["stored_path"],
                    status=MATERIAL_STATUS_PENDING,
                )
                for index, item in enumerate(extracted, start=1)
            ]
        )
        db.commit()
        db.refresh(batch)
        return get_summary(db, owner_id)
    except Exception:
        db.rollback()
        zip_path.unlink(missing_ok=True)
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        raise
    finally:
        zf.close()


def create_batch_from_zip(
    db: Session,
    content: bytes,
    original_filename: str,
    owner_id: int,
) -> dict[str, Any]:
    """字节输入包装,主要供服务层测试使用。"""
    source_path = MATERIAL_ZIPS_DIR / f"incoming_{uuid.uuid4().hex}.zip"
    source_path.write_bytes(content)
    try:
        return create_batch_from_zip_path(
            db, source_path, original_filename, owner_id
        )
    finally:
        source_path.unlink(missing_ok=True)


def list_items(
    db: Session,
    status: str | None = None,
    limit: int = 200,
    owner_id: int | None = None,
) -> list[MaterialItem]:
    if owner_id is None:
        raise ValueError("owner_id is required")
    batch = get_active_batch(db, owner_id)
    if batch is None:
        return []
    query = db.query(MaterialItem).filter(MaterialItem.batch_id == batch.id)
    if status:
        query = query.filter(MaterialItem.status == status)
    return query.order_by(MaterialItem.sequence.asc()).limit(limit).all()


def get_linked_item(
    db: Session, record_id: int, owner_id: int | None = None
) -> MaterialItem | None:
    query = (
        db.query(MaterialItem)
        .join(MaterialBatch, MaterialBatch.id == MaterialItem.batch_id)
        .filter(MaterialItem.record_id == record_id)
    )
    if owner_id is not None:
        query = query.filter(MaterialBatch.owner_id == owner_id)
    return query.order_by(MaterialItem.id.desc()).first()


def _copy_for_recognition(item: MaterialItem) -> str:
    source = resolve_material_image_path(item.stored_path)
    if not source.exists():
        raise HTTPException(status_code=404, detail="素材图片文件不存在")
    stored_name = f"img_{uuid.uuid4().hex[:8]}{source.suffix.lower()}"
    target = IMAGES_DIR / stored_name
    shutil.copy2(source, target)
    return str(target)


async def start_next_item(
    db: Session,
    rotation_degrees: int = 0,
    owner_id: int | None = None,
) -> tuple[MaterialItem, SpecimenRecord]:
    if owner_id is None:
        raise ValueError("owner_id is required")
    active_draft = recognition_service.get_active_draft(db, owner_id)
    if active_draft is not None:
        linked = get_linked_item(db, active_draft.id, owner_id)
        if linked is not None:
            return linked, active_draft
        raise HTTPException(
            status_code=409,
            detail="工作台存在手动上传的未完成草稿,请先完成或清空后再处理素材包",
        )

    batch = get_active_batch(db, owner_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="尚未上传数据素材压缩包")
    item = (
        db.query(MaterialItem)
        .filter(
            MaterialItem.batch_id == batch.id,
            MaterialItem.status == MATERIAL_STATUS_PENDING,
        )
        .order_by(MaterialItem.sequence.asc())
        .first()
    )
    if item is None:
        raise HTTPException(status_code=404, detail="当前素材包没有待处理图片")

    # 检查预加载缓存：优先消费 ready，等待 running，避免重复模型调用
    pf = (
        db.query(MaterialPrefetchResult)
        .filter(MaterialPrefetchResult.item_id == item.id)
        .first()
    )

    current_fp = None
    try:
        from app.services.prefetch_service import _get_current_fingerprint
        current_fp = _get_current_fingerprint()
    except Exception:
        pass

    # 如果有 ready 且指纹匹配，直接消费
    if (
        pf is not None
        and pf.status == PREFETCH_STATUS_READY
        and pf.result_json
        and (current_fp is None or pf.config_fingerprint == current_fp)
    ):
        image_path = _copy_for_recognition(item)
        record = SpecimenRecord(
            owner_id=owner_id,
            image_filename=item.original_filename,
            image_path=image_path,
            rotation_degrees=rotation_degrees % 360,
        )
        db.add(record)
        db.flush()
        item.record_id = record.id
        item.status = MATERIAL_STATUS_PROCESSING
        item.error_message = ""
        from app.services import quota_service
        try:
            quota_service.reserve(db, owner_id, record.id)
        except Exception:
            Path(image_path).unlink(missing_ok=True)
            raise

        try:
            precomputed = json.loads(pf.result_json)
            db.delete(pf)
            db.commit()
            record = await recognition_service.extract_image_info(
                db,
                image_path,
                item.original_filename,
                rotation_degrees,
                record=record,
                precomputed_result=precomputed,
            )
            _notify_prefetch_worker()
            return item, record
        except Exception:
            if pf in db:
                db.delete(pf)
                db.commit()
            # 回退到正常识别（record 已创建）
            try:
                record = await recognition_service.extract_image_info(
                    db,
                    image_path,
                    item.original_filename,
                    rotation_degrees,
                    record=record,
                )
                _notify_prefetch_worker()
                return item, record
            except Exception as exc:
                _handle_extraction_failure(db, record, item, exc)
                raise

    # 如果正在预加载(running/queued)，等待它完成（最多120秒），避免重复调用模型
    if pf is not None and pf.status in (PREFETCH_STATUS_RUNNING, PREFETCH_STATUS_QUEUED):
        pf_id = pf.id
        waited = 0
        max_wait = settings.model_timeout_seconds
        while waited < max_wait:
            await asyncio.sleep(0.5)
            waited += 0.5
            db.expire_all()
            pf_refresh = db.get(MaterialPrefetchResult, pf_id)
            if pf_refresh is None:
                break
            if pf_refresh.status == PREFETCH_STATUS_READY and pf_refresh.result_json:
                image_path = _copy_for_recognition(item)
                record = SpecimenRecord(
                    owner_id=owner_id,
                    image_filename=item.original_filename,
                    image_path=image_path,
                    rotation_degrees=rotation_degrees % 360,
                )
                db.add(record)
                db.flush()
                item.record_id = record.id
                item.status = MATERIAL_STATUS_PROCESSING
                item.error_message = ""
                from app.services import quota_service
                try:
                    quota_service.reserve(db, owner_id, record.id)
                except Exception:
                    Path(image_path).unlink(missing_ok=True)
                    raise

                try:
                    precomputed = json.loads(pf_refresh.result_json)
                    db.delete(pf_refresh)
                    db.commit()
                    record = await recognition_service.extract_image_info(
                        db,
                        image_path,
                        item.original_filename,
                        rotation_degrees,
                        record=record,
                        precomputed_result=precomputed,
                    )
                    _notify_prefetch_worker()
                    return item, record
                except Exception:
                    if pf_refresh in db:
                        db.delete(pf_refresh)
                        db.commit()
                    # 缓存无效，回退到正常识别
                    break  # 继续到下面的正常识别流程
            elif pf_refresh.status == PREFETCH_STATUS_FAILED:
                break  # 预加载失败，回退到正常识别

    # 正常同步识别（无缓存、缓存无效或预加载失败时）
    image_path = _copy_for_recognition(item)
    record = SpecimenRecord(
        owner_id=owner_id,
        image_filename=item.original_filename,
        image_path=image_path,
        rotation_degrees=rotation_degrees % 360,
    )
    db.add(record)
    db.flush()
    item.record_id = record.id
    item.status = MATERIAL_STATUS_PROCESSING
    item.error_message = ""
    from app.services import quota_service
    try:
        quota_service.reserve(db, owner_id, record.id)
    except Exception:
        Path(image_path).unlink(missing_ok=True)
        raise

    try:
        record = await recognition_service.extract_image_info(
            db,
            image_path,
            item.original_filename,
            rotation_degrees,
            record=record,
        )
        _notify_prefetch_worker()
        return item, record
    except Exception as exc:
        _handle_extraction_failure(db, record, item, exc)
        raise


def _handle_extraction_failure(
    db: Session,
    record: SpecimenRecord,
    item: MaterialItem,
    exc: Exception,
) -> None:
    """处理识别失败的通用逻辑。"""
    db.refresh(record)
    if record.status in ACTIVE_DRAFT_STATUSES:
        record.status = STATUS_EXTRACTION_FAILED
    item.status = MATERIAL_STATUS_FAILED
    item.error_message = str(getattr(exc, "detail", exc))
    record.warnings_json = json.dumps([item.error_message], ensure_ascii=False)
    from app.services import quota_service
    quota_service.release(db, record.id)
    db.commit()


def _notify_prefetch_worker() -> None:
    """通知预加载 worker 补充窗口。"""
    try:
        from app.services.prefetch_service import notify_worker
        notify_worker()
    except Exception:
        pass


def skip_item(db: Session, item_id: int, owner_id: int) -> dict[str, Any]:
    item = db.get(MaterialItem, item_id)
    batch = get_active_batch(db, owner_id)
    if item is None or batch is None or item.batch_id != batch.id:
        raise HTTPException(status_code=404, detail="素材图片不存在")
    if item.status == MATERIAL_STATUS_COMPLETED:
        raise HTTPException(status_code=409, detail="已完成的素材不能跳过")
    if item.status == MATERIAL_STATUS_SKIPPED:
        return get_summary(db, owner_id)

    if item.record_id is not None:
        record = db.get(SpecimenRecord, item.record_id)
        if record is not None and record.status != "completed":
            record.status = STATUS_DISCARDED
            from app.services import quota_service
            quota_service.release(db, record.id)
            if record.image_path:
                Path(record.image_path).unlink(missing_ok=True)

    # 清除该素材的预加载缓存
    db.query(MaterialPrefetchResult).filter(
        MaterialPrefetchResult.item_id == item_id,
    ).delete(synchronize_session=False)

    item.status = MATERIAL_STATUS_SKIPPED
    item.error_message = ""
    db.commit()
    _notify_prefetch_worker()
    return get_summary(db, owner_id)


def reset_item_for_deleted_record(db: Session, record_id: int) -> None:
    items = (
        db.query(MaterialItem)
        .filter(MaterialItem.record_id == record_id)
        .all()
    )
    for item in items:
        item.record_id = None
        item.status = MATERIAL_STATUS_PENDING
        item.error_message = ""
    db.flush()


def create_skipped_export(db: Session, owner_id: int) -> tuple[Path, int]:
    batch = get_active_batch(db, owner_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="尚未上传数据素材压缩包")
    skipped = (
        db.query(MaterialItem)
        .filter(
            MaterialItem.batch_id == batch.id,
            MaterialItem.status == MATERIAL_STATUS_SKIPPED,
        )
        .order_by(MaterialItem.sequence.asc())
        .all()
    )
    if not skipped:
        raise HTTPException(status_code=400, detail="当前没有已跳过的数据素材")

    export_path = MATERIAL_EXPORTS_DIR / (
        f"跳过的数据素材_批次{batch.id}_{uuid.uuid4().hex[:8]}.zip"
    )
    with zipfile.ZipFile(export_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in skipped:
            source = resolve_material_image_path(item.stored_path)
            if source.exists():
                zf.write(source, arcname=item.archive_path)
    return export_path, len(skipped)


def delete_batch(db: Session, owner_id: int) -> dict[str, Any]:
    """删除当前活跃批次及其全部素材项和磁盘文件。

    安全规则:
    - 有 completed 素材项时禁止删除(已写入 Excel)
    - 自动废弃 processing/failed 状态的关联草稿及其图片
    - 级联删除全部 MaterialItem
    - 清理解压目录和存储的 ZIP 文件
    """
    batch = get_active_batch(db, owner_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="没有可删除的素材批次")

    completed_count = (
        db.query(MaterialItem)
        .filter(
            MaterialItem.batch_id == batch.id,
            MaterialItem.status == MATERIAL_STATUS_COMPLETED,
        )
        .count()
    )
    if completed_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"当前批次有 {completed_count} 张已完成的素材,已写入 Excel,不能删除",
        )

    linked_items = (
        db.query(MaterialItem)
        .filter(
            MaterialItem.batch_id == batch.id,
            MaterialItem.record_id.isnot(None),
        )
        .all()
    )
    for item in linked_items:
        record = db.get(SpecimenRecord, item.record_id)
        if record is not None and record.status in ACTIVE_DRAFT_STATUSES:
            record.status = STATUS_DISCARDED
            from app.services import quota_service
            quota_service.release(db, record.id)
            if record.image_path:
                Path(record.image_path).unlink(missing_ok=True)

    extract_dir = batch.extract_dir
    zip_path = batch.stored_zip_path

    # 清除该批次的所有预加载缓存
    db.query(MaterialPrefetchResult).filter(
        MaterialPrefetchResult.batch_id == batch.id,
    ).delete(synchronize_session=False)

    db.query(MaterialItem).filter(MaterialItem.batch_id == batch.id).delete()
    db.delete(batch)
    db.commit()

    if extract_dir:
        shutil.rmtree(extract_dir, ignore_errors=True)
    if zip_path:
        Path(zip_path).unlink(missing_ok=True)

    _notify_prefetch_worker()
    return get_summary(db, owner_id)


def get_prefetch_status(db: Session, owner_id: int) -> dict[str, Any]:
    """获取当前批次的预加载状态统计。"""
    batch = get_active_batch(db, owner_id)
    if batch is None:
        return {"ready_count": 0, "running_count": 0, "failed_count": 0, "target": settings.material_prefetch_size}

    rows = (
        db.query(MaterialPrefetchResult.status, func.count(MaterialPrefetchResult.id))
        .filter(MaterialPrefetchResult.batch_id == batch.id)
        .group_by(MaterialPrefetchResult.status)
        .all()
    )
    counts = {status: count for status, count in rows}
    return {
        "ready_count": counts.get(PREFETCH_STATUS_READY, 0),
        "running_count": counts.get("running", 0),
        "failed_count": counts.get(PREFETCH_STATUS_FAILED, 0),
        "target": settings.material_prefetch_size,
    }
