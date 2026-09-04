"""ORM 模型定义。

6 张表:
  - app_settings: 单例配置(模型API + 提示词)
  - excel_templates: Excel 模板配置
  - specimen_records: 标本记录(含草稿状态机)
  - taxonomy_cache: 分类缓存
  - material_batches: 数据素材压缩包批次
  - material_items: 批次中的单张素材图片

状态机(specimen_records.status):
  uploaded -> extracting -> awaiting_confirmation -> classifying -> completed
                                                                -> classification_failed
                -> extraction_failed
  awaiting_confirmation -> extracting  (重新识别)
  任意非 completed 状态 -> discarded  (用户放弃草稿)

参考清单第 5.1 节、第 7 节。
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


# ============================================================
# 状态常量(字符串,避免枚举在 JSON 序列化时的复杂性)
# ============================================================

# 记录状态:与清单 5.1 节一致 + discarded(用户决策:草稿放弃)
STATUS_UPLOADED = "uploaded"
STATUS_EXTRACTING = "extracting"
STATUS_AWAITING_CONFIRMATION = "awaiting_confirmation"
STATUS_CLASSIFYING = "classifying"
STATUS_COMPLETED = "completed"
STATUS_EXTRACTION_FAILED = "extraction_failed"
STATUS_CLASSIFICATION_FAILED = "classification_failed"
STATUS_DISCARDED = "discarded"

# 素材图片状态
MATERIAL_STATUS_PENDING = "pending"
MATERIAL_STATUS_PROCESSING = "processing"
MATERIAL_STATUS_COMPLETED = "completed"
MATERIAL_STATUS_SKIPPED = "skipped"
MATERIAL_STATUS_FAILED = "failed"

# 尚未完成的活跃状态(用于前端恢复当前工作区草稿)
ACTIVE_DRAFT_STATUSES = frozenset(
    {
        STATUS_UPLOADED,
        STATUS_EXTRACTING,
        STATUS_AWAITING_CONFIRMATION,
        STATUS_CLASSIFYING,
        STATUS_EXTRACTION_FAILED,
        STATUS_CLASSIFICATION_FAILED,
    }
)

# 13 个目标字段列名(与 Excel 字段一一对应)
FIELD_ZHONGMING = "中名"  # 中名
FIELD_PHYLUM = "Phylum"
FIELD_GANG = "纲"  # 纲
FIELD_CLASS = "Class"
FIELD_ORDER = "Order"
FIELD_ZHONGWEN_KE = "中文科名"
FIELD_KE = "科名"  # 科名
FIELD_SHU = "属名"  # 属名
FIELD_ZHONG = "种名"  # 种名
FIELD_CHANDI3 = "产地3"
FIELD_TUXIANG = "图像"  # 图像
FIELD_CAIJIREN = "采集人"
FIELD_CAIJI_RIQI = "采集日期"


# ============================================================
# 1. app_settings —— 单例配置
# ============================================================

class AppSettings(Base):
    """应用配置单例。id 固定为 1。"""

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    base_url: Mapped[str] = mapped_column(String(500), default="")
    api_key: Mapped[str] = mapped_column(String(500), default="")
    model_name: Mapped[str] = mapped_column(String(200), default="")
    recognition_prompt: Mapped[str] = mapped_column(Text, default="")
    taxonomy_prompt: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


# ============================================================
# 2. excel_templates —— Excel 模板配置
# ============================================================

class ExcelTemplate(Base):
    """Excel 模板配置。同一时间只有一个 is_active=True。"""

    __tablename__ = "excel_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, default=1
    )
    original_filename: Mapped[str] = mapped_column(String(500))
    stored_path: Mapped[str] = mapped_column(String(1000))
    target_sheet: Mapped[str] = mapped_column(String(200), default="")
    header_row: Mapped[int] = mapped_column(Integer, default=1)
    start_row: Mapped[int] = mapped_column(Integer, default=2)
    # base_write_row 在保存映射时确定,预览和导出始终使用此值
    base_write_row: Mapped[int] = mapped_column(Integer, default=2)
    style_source_row: Mapped[int] = mapped_column(Integer, default=2)
    # JSON: {"中名": "E", "Phylum": "G", ...}
    field_mapping_json: Mapped[str] = mapped_column(Text, default="{}")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )


# ============================================================
# 3. specimen_records —— 标本记录(含草稿)
# ============================================================

class SpecimenRecord(Base):
    """标本记录。包含从上传到完成的完整生命周期。

    草稿阶段(非 completed)的记录保存在此表,
    通过 status 区分是否为草稿、是否已废弃。
    """

    __tablename__ = "specimen_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, default=1
    )

    # 图片信息
    image_filename: Mapped[str] = mapped_column(String(500), default="")
    image_path: Mapped[str] = mapped_column(String(1000), default="")
    processed_image_path: Mapped[str] = mapped_column(String(1000), default="")
    rotation_degrees: Mapped[int] = mapped_column(Integer, default=0)

    # 状态
    status: Mapped[str] = mapped_column(
        String(50), default=STATUS_UPLOADED, index=True
    )

    # 模型原始响应与各阶段 JSON
    raw_model_response: Mapped[str] = mapped_column(Text, default="")
    extracted_draft_json: Mapped[str] = mapped_column(Text, default="")
    confirmed_extraction_json: Mapped[str] = mapped_column(Text, default="")
    taxonomy_result_json: Mapped[str] = mapped_column(Text, default="")
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")

    # 13 个最终字段(扁平化,便于查询和排序)
    zhongming: Mapped[str] = mapped_column(String(200), default="", index=True)
    phylum: Mapped[str] = mapped_column(String(200), default="")
    gang: Mapped[str] = mapped_column(String(200), default="")
    klass: Mapped[str] = mapped_column(String(200), default="")  # Class 保留字
    order_field: Mapped[str] = mapped_column(String(200), default="")  # order 保留字
    zhongwen_ke: Mapped[str] = mapped_column(String(200), default="")
    ke: Mapped[str] = mapped_column(String(200), default="")
    shu: Mapped[str] = mapped_column(String(200), default="")
    zhong: Mapped[str] = mapped_column(String(200), default="")
    chandi3: Mapped[str] = mapped_column(String(500), default="")
    tuxiang: Mapped[str] = mapped_column(String(200), default="", index=True)
    caijiren: Mapped[str] = mapped_column(String(200), default="")
    caiji_riqi: Mapped[str] = mapped_column(String(20), default="")

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


Index(
    "uq_specimen_owner_tuxiang_completed",
    SpecimenRecord.owner_id,
    SpecimenRecord.tuxiang,
    unique=True,
    sqlite_where=SpecimenRecord.status == STATUS_COMPLETED,
)


# ============================================================
# 4. taxonomy_cache —— 分类缓存
# ============================================================

class TaxonomyCache(Base):
    """已通过校验的中名 -> 分类信息缓存。

    只有 8 个分类字段全部通过自动校验后才能写入或更新。
    """

    __tablename__ = "taxonomy_cache"
    __table_args__ = (UniqueConstraint("owner_id", "zhongming"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    zhongming: Mapped[str] = mapped_column(String(200), index=True)
    phylum: Mapped[str] = mapped_column(String(200), default="")
    gang: Mapped[str] = mapped_column(String(200), default="")
    klass: Mapped[str] = mapped_column(String(200), default="")
    order_field: Mapped[str] = mapped_column(String(200), default="")
    zhongwen_ke: Mapped[str] = mapped_column(String(200), default="")
    ke: Mapped[str] = mapped_column(String(200), default="")
    shu: Mapped[str] = mapped_column(String(200), default="")
    zhong: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


# ============================================================
# 5. material_batches —— 数据素材批次
# ============================================================

class MaterialBatch(Base):
    """用户上传的数据素材 ZIP 批次。一次只有一个活跃批次。"""

    __tablename__ = "material_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, default=1
    )
    original_filename: Mapped[str] = mapped_column(String(500))
    stored_zip_path: Mapped[str] = mapped_column(String(1000))
    extract_dir: Mapped[str] = mapped_column(String(1000))
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


Index(
    "uq_template_owner_active",
    ExcelTemplate.owner_id,
    unique=True,
    sqlite_where=ExcelTemplate.is_active.is_(True),
)
Index(
    "uq_batch_owner_active",
    MaterialBatch.owner_id,
    unique=True,
    sqlite_where=MaterialBatch.is_active.is_(True),
)


# ============================================================
# 6. material_items —— 单张数据素材图片
# ============================================================

class MaterialItem(Base):
    """素材批次中的图片及其处理状态。"""

    __tablename__ = "material_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("material_batches.id", ondelete="CASCADE"), index=True
    )
    sequence: Mapped[int] = mapped_column(Integer)
    original_filename: Mapped[str] = mapped_column(String(500))
    archive_path: Mapped[str] = mapped_column(String(1000))
    stored_path: Mapped[str] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(
        String(50), default=MATERIAL_STATUS_PENDING, index=True
    )
    record_id: Mapped[int | None] = mapped_column(
        ForeignKey("specimen_records.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


# ============================================================
# 7. material_prefetch_results —— 后台预加载识别结果
# ============================================================

# 预加载任务状态
PREFETCH_STATUS_QUEUED = "queued"
PREFETCH_STATUS_RUNNING = "running"
PREFETCH_STATUS_READY = "ready"
PREFETCH_STATUS_FAILED = "failed"


class MaterialPrefetchResult(Base):
    """后台预加载的模型识别结果缓存。

    每张素材图片最多一条记录(item_id 唯一)。
    worker 并行填充窗口(默认保持20张ready),工作台消费后删除。
    """

    __tablename__ = "material_prefetch_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("material_batches.id", ondelete="CASCADE"), index=True
    )
    item_id: Mapped[int] = mapped_column(
        ForeignKey("material_items.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(50), default=PREFETCH_STATUS_QUEUED, index=True
    )
    result_json: Mapped[str] = mapped_column(Text, default="")
    config_fingerprint: Mapped[str] = mapped_column(String(200), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


# ============================================================
# 用户、会话、配额与导出审计
# ============================================================

ROLE_ADMIN = "admin"
ROLE_USER = "user"
USAGE_RESERVED = "reserved"
USAGE_CHARGED = "charged"
USAGE_RELEASED = "released"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(500))
    role: Mapped[str] = mapped_column(String(20), default=ROLE_USER, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    workflow_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    workflow_reserved: Mapped[int] = mapped_column(Integer, default=0)
    workflow_charged: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )


class WorkflowUsage(Base):
    __tablename__ = "workflow_usages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    record_id: Mapped[int] = mapped_column(
        ForeignKey("specimen_records.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(20), default=USAGE_RESERVED, index=True)
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )
    charged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class QuotaAdjustment(Base):
    __tablename__ = "quota_adjustments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    actor_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    old_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_quota: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )


class ExportArtifact(Base):
    __tablename__ = "export_artifacts"
    __table_args__ = (UniqueConstraint("owner_id", "filename"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(500))
    stored_path: Mapped[str] = mapped_column(String(1000))
    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.current_timestamp()
    )
