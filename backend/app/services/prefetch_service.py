"""后台预加载 worker：并行填充 ready 窗口，减少工作台等待。

关键设计：
- 并行模型调用（受 asyncio.Semaphore 控制），串行不满足 2-3秒/张消费速率
- 水位控制：低于低水位全速补，达到高水位暂停
- asyncio.Event 即时唤醒（上传/消费/跳过后通知）
- failed 任务指数退避重试，不永久阻塞
- 状态完全持久化，Docker 重启后恢复
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from app.config import settings
from app.database import SessionLocal
from app.models import (
    MATERIAL_STATUS_PENDING,
    MaterialBatch,
    MaterialItem,
    MaterialPrefetchResult,
    PREFETCH_STATUS_FAILED,
    PREFETCH_STATUS_QUEUED,
    PREFETCH_STATUS_READY,
    PREFETCH_STATUS_RUNNING,
    ROLE_ADMIN,
    User,
)
from app.services import recognition_service

logger = logging.getLogger(__name__)

# 全局 worker 单例
_global_worker: PrefetchWorker | None = None


def compute_config_fingerprint(
    base_url: str,
    model_name: str,
    recognition_prompt: str,
    rotation_degrees: int = 0,
) -> str:
    """计算配置指纹，用于检测模型/提示词变化后缓存失效。"""
    raw = f"{base_url}|{model_name}|{recognition_prompt}|{rotation_degrees}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _get_current_fingerprint() -> str | None:
    """读取当前数据库中的配置指纹，未配置模型时返回 None。"""
    from app.routers.settings import _get_or_create_settings

    db = SessionLocal()
    try:
        s = _get_or_create_settings(db)
        if not s.base_url or not s.api_key or not s.model_name:
            return None
        prompt = recognition_service._load_prompt(db, "recognition_prompt", "recognition_prompt.txt")
        return compute_config_fingerprint(s.base_url, s.model_name, prompt)
    finally:
        db.close()


class PrefetchWorker:
    """后台并行预加载 worker。

    - asyncio task + Semaphore 控制并发
    - 水位控制：ready < low 全速补充，>= high 暂停
    - asyncio.Event 即时唤醒
    - failed 指数退避重试
    - 状态持久化，重启可恢复
    """

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._wakeup_event = asyncio.Event()
        self._semaphore: asyncio.Semaphore | None = None
        self._last_batch_id: int = 0

    async def start(self) -> None:
        global _global_worker
        if self._task is not None:
            return
        self._recover_stale_running()
        self._semaphore = asyncio.Semaphore(settings.material_prefetch_concurrency)
        _global_worker = self
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        global _global_worker
        self._stop_event.set()
        self._wakeup_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        _global_worker = None
        self._stop_event.clear()
        self._wakeup_event.clear()

    def notify(self) -> None:
        """通知 worker 立即检查窗口（上传/消费/跳过后调用）。"""
        self._wakeup_event.set()

    # ============================================================
    # 启动恢复
    # ============================================================

    def _recover_stale_running(self) -> None:
        """重启后把遗留的 running/queued 任务重置为 queued，等待重新调度。"""
        db = SessionLocal()
        try:
            db.query(MaterialPrefetchResult).filter(
                MaterialPrefetchResult.status.in_([PREFETCH_STATUS_RUNNING, PREFETCH_STATUS_QUEUED]),
            ).update(
                {
                    "status": PREFETCH_STATUS_QUEUED,
                    "next_retry_at": None,
                },
                synchronize_session=False,
            )
            db.commit()
        finally:
            db.close()

    # ============================================================
    # 主循环
    # ============================================================

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._fill_window()
            except Exception:
                logger.exception("预加载 worker 异常")
            self._wakeup_event.clear()
            try:
                await asyncio.wait_for(
                    self._wakeup_event.wait(),
                    timeout=settings.material_prefetch_interval,
                )
            except asyncio.TimeoutError:
                pass  # 正常超时，继续下一轮

    async def _fill_window(self) -> None:
        """检查并并行填充预加载窗口。"""
        fingerprint = _get_current_fingerprint()
        if fingerprint is None:
            self._clear_all()
            return

        db = SessionLocal()
        try:
            batches = (
                db.query(MaterialBatch)
                .join(User, User.id == MaterialBatch.owner_id)
                .filter(MaterialBatch.is_active.is_(True))
                .filter(User.is_active.is_(True))
                .filter(
                    (User.role == ROLE_ADMIN)
                    | (User.workflow_quota.is_(None))
                    | (
                        User.workflow_reserved + User.workflow_charged
                        < User.workflow_quota
                    )
                )
                .order_by(MaterialBatch.id.asc())
                .all()
            )
            if not batches:
                return
            batch = next(
                (candidate for candidate in batches if candidate.id > self._last_batch_id),
                batches[0],
            )
            self._last_batch_id = batch.id
            owner = db.get(User, batch.owner_id)
            if owner is None:
                return

            # 清理指纹不匹配的旧结果
            stale = (
                db.query(MaterialPrefetchResult)
                .filter(
                    MaterialPrefetchResult.batch_id == batch.id,
                    MaterialPrefetchResult.config_fingerprint != fingerprint,
                )
                .all()
            )
            for s in stale:
                db.delete(s)
            if stale:
                db.commit()

            # 重试到期且未超限的 failed 任务 → queued
            now = datetime.utcnow()
            retry_candidates = (
                db.query(MaterialPrefetchResult)
                .filter(
                    MaterialPrefetchResult.batch_id == batch.id,
                    MaterialPrefetchResult.status == PREFETCH_STATUS_FAILED,
                    MaterialPrefetchResult.attempt_count < settings.material_prefetch_max_retries,
                    MaterialPrefetchResult.next_retry_at.isnot(None),
                    MaterialPrefetchResult.next_retry_at <= now,
                )
                .all()
            )
            for rc in retry_candidates:
                rc.status = PREFETCH_STATUS_QUEUED
                rc.error_message = ""
            if retry_candidates:
                db.commit()

            # 统计当前活跃任务数(queued + running + ready)
            active_count = (
                db.query(MaterialPrefetchResult)
                .filter(
                    MaterialPrefetchResult.batch_id == batch.id,
                    MaterialPrefetchResult.status.in_([
                        PREFETCH_STATUS_QUEUED,
                        PREFETCH_STATUS_RUNNING,
                        PREFETCH_STATUS_READY,
                    ]),
                    MaterialPrefetchResult.config_fingerprint == fingerprint,
                )
                .count()
            )

            ready_count = (
                db.query(MaterialPrefetchResult)
                .filter(
                    MaterialPrefetchResult.batch_id == batch.id,
                    MaterialPrefetchResult.status == PREFETCH_STATUS_READY,
                    MaterialPrefetchResult.config_fingerprint == fingerprint,
                )
                .count()
            )

            target = settings.material_prefetch_size
            if owner.role == ROLE_ADMIN or owner.workflow_quota is None:
                remaining = target
            else:
                remaining = max(
                    0,
                    owner.workflow_quota
                    - owner.workflow_reserved
                    - owner.workflow_charged,
                )
            max_lookahead = min(
                settings.material_prefetch_max_lookahead,
                target,
                remaining,
            )

            # 如果已达高水位或最大前瞻，不再创建新任务
            if active_count >= max_lookahead or ready_count >= target:
                # 但仍然需要处理已有的 queued 任务
                pass
            else:
                # 创建新 queued 任务直到达到目标水位或前瞻上限
                already_ids_rows = (
                    db.query(MaterialPrefetchResult.item_id)
                    .filter(MaterialPrefetchResult.batch_id == batch.id)
                    .all()
                )
                already_ids = [row[0] for row in already_ids_rows]

                while (
                    active_count < max_lookahead
                    and ready_count + (active_count - ready_count) < max_lookahead
                ):
                    item_q = (
                        db.query(MaterialItem)
                        .filter(
                            MaterialItem.batch_id == batch.id,
                            MaterialItem.status == MATERIAL_STATUS_PENDING,
                        )
                    )
                    if already_ids:
                        item_q = item_q.filter(~MaterialItem.id.in_(already_ids))
                    item = item_q.order_by(MaterialItem.sequence.asc()).first()

                    if item is None:
                        break

                    pf = MaterialPrefetchResult(
                        batch_id=batch.id,
                        item_id=item.id,
                        status=PREFETCH_STATUS_QUEUED,
                        config_fingerprint=fingerprint,
                    )
                    db.add(pf)
                    db.commit()
                    db.refresh(pf)
                    already_ids.append(item.id)
                    active_count += 1

                    if active_count >= max_lookahead:
                        break

            # 收集所有 queued 任务并并行执行
            queued_items = (
                db.query(MaterialPrefetchResult)
                .filter(
                    MaterialPrefetchResult.batch_id == batch.id,
                    MaterialPrefetchResult.status == PREFETCH_STATUS_QUEUED,
                    MaterialPrefetchResult.config_fingerprint == fingerprint,
                )
                .limit(settings.material_prefetch_concurrency)
                .all()
            )
        finally:
            db.close()

        # 并行执行 queued 任务
        if queued_items:
            # 动态调整并发：ready < target 时全速，接近 high 时降速
            tasks = []
            for pf in queued_items:
                # 再次检查 running 数量，避免超过并发限制
                tasks.append(self._run_prefetch_task(pf.id, pf.item_id, fingerprint))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_prefetch_task(self, pf_id: int, item_id: int, fingerprint: str) -> None:
        """单个预加载任务：原子领取 → 调用模型 → 保存结果。"""
        async with self._semaphore:
            if self._stop_event.is_set():
                return
            await self._prefetch_one(item_id, pf_id, fingerprint)

    async def _prefetch_one(
        self,
        item_id: int,
        pf_id: int,
        fingerprint: str,
    ) -> bool:
        """对单张素材调用模型识别并保存结果。"""
        db = SessionLocal()
        try:
            # 原子领取：queued → running
            pf = db.get(MaterialPrefetchResult, pf_id)
            if pf is None or pf.status != PREFETCH_STATUS_QUEUED:
                return False

            fresh = db.get(MaterialItem, item_id)
            if fresh is None or fresh.status != MATERIAL_STATUS_PENDING:
                db.delete(pf)
                db.commit()
                return False
            batch = db.get(MaterialBatch, fresh.batch_id)
            owner = db.get(User, batch.owner_id) if batch is not None else None
            if (
                batch is None
                or not batch.is_active
                or owner is None
                or not owner.is_active
                or (
                    owner.role != ROLE_ADMIN
                    and owner.workflow_quota is not None
                    and owner.workflow_reserved + owner.workflow_charged
                    >= owner.workflow_quota
                )
            ):
                db.delete(pf)
                db.commit()
                return False

            pf.status = PREFETCH_STATUS_RUNNING
            pf.attempt_count = (pf.attempt_count or 0) + 1
            pf.next_retry_at = None
            db.commit()

            stored_path = fresh.stored_path
            original_filename = fresh.original_filename
        finally:
            db.close()

        # 调用模型（在独立 DB session 中）
        from app.services.materials_service import resolve_material_image_path
        source = resolve_material_image_path(stored_path)
        if not source.exists():
            self._mark_failed(pf_id, "素材图片文件不存在")
            return False

        try:
            db2 = SessionLocal()
            try:
                client = recognition_service._get_model_client(db2)
                prompt = recognition_service._load_prompt(
                    db2, "recognition_prompt", "recognition_prompt.txt"
                )
            finally:
                db2.close()

            result = await client.recognize_image(str(source), prompt, 0)
        except Exception as exc:
            self._mark_failed(pf_id, str(getattr(exc, "detail", exc)))
            return False

        # 保存成功结果
        db3 = SessionLocal()
        try:
            pf = db3.get(MaterialPrefetchResult, pf_id)
            if pf is None:
                return False

            re_check = db3.get(MaterialItem, item_id)
            if re_check is None or re_check.status != MATERIAL_STATUS_PENDING:
                db3.delete(pf)
                db3.commit()
                return False
            batch = db3.get(MaterialBatch, re_check.batch_id)
            owner = db3.get(User, batch.owner_id) if batch is not None else None
            if batch is None or owner is None or not owner.is_active:
                db3.delete(pf)
                db3.commit()
                return False

            new_fp = _get_current_fingerprint()
            if new_fp != fingerprint:
                db3.delete(pf)
                db3.commit()
                return False

            pf.status = PREFETCH_STATUS_READY
            pf.result_json = json.dumps(result, ensure_ascii=False)
            pf.error_message = ""
            pf.next_retry_at = None
            db3.commit()
            return True
        finally:
            db3.close()

    def _mark_failed(self, pf_id: int, error_message: str) -> None:
        """标记任务为失败，设置指数退避重试时间。"""
        db = SessionLocal()
        try:
            pf = db.get(MaterialPrefetchResult, pf_id)
            if pf is None:
                return
            re_check = db.get(MaterialItem, pf.item_id)
            if re_check is None or re_check.status != MATERIAL_STATUS_PENDING:
                db.delete(pf)
                db.commit()
                return

            attempts = pf.attempt_count or 0
            if attempts >= settings.material_prefetch_max_retries:
                # 永久失败，不重试
                pf.status = PREFETCH_STATUS_FAILED
                pf.error_message = error_message
                pf.next_retry_at = None
            else:
                # 指数退避
                delay = settings.material_prefetch_retry_delay * (2 ** (attempts - 1))
                pf.status = PREFETCH_STATUS_FAILED
                pf.error_message = error_message
                pf.next_retry_at = datetime.utcnow() + timedelta(seconds=delay)
            db.commit()
        finally:
            db.close()

    # ============================================================
    # 清理方法
    # ============================================================

    def _clear_all(self) -> None:
        """清除所有非 running 的预加载结果。"""
        db = SessionLocal()
        try:
            db.query(MaterialPrefetchResult).filter(
                MaterialPrefetchResult.status != PREFETCH_STATUS_RUNNING,
            ).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

    def clear_for_batch(self, batch_id: int) -> None:
        """清除指定批次的所有预加载结果。"""
        db = SessionLocal()
        try:
            db.query(MaterialPrefetchResult).filter(
                MaterialPrefetchResult.batch_id == batch_id,
            ).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

    def clear_for_item(self, item_id: int) -> None:
        """清除指定素材的预加载结果。"""
        db = SessionLocal()
        try:
            db.query(MaterialPrefetchResult).filter(
                MaterialPrefetchResult.item_id == item_id,
            ).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

    def clear_all(self) -> None:
        """清除所有预加载结果(不区分批次)。"""
        db = SessionLocal()
        try:
            db.query(MaterialPrefetchResult).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

    def invalidate_all(self) -> None:
        """配置变化后使所有结果失效(删除以便重新预加载)。"""
        db = SessionLocal()
        try:
            db.query(MaterialPrefetchResult).filter(
                MaterialPrefetchResult.status != PREFETCH_STATUS_RUNNING,
            ).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()


def get_worker() -> PrefetchWorker | None:
    """获取全局 worker 实例。"""
    return _global_worker


def notify_worker() -> None:
    """通知全局 worker 立即检查窗口。"""
    w = _global_worker
    if w is not None:
        w.notify()
