"""应用配置。所有可配置项通过环境变量传入,不硬编码。"""
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# 项目根目录(backend/ 的父目录)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
TEMPLATES_DIR = DATA_DIR / "templates"
IMAGES_DIR = DATA_DIR / "images"
PROCESSED_IMAGES_DIR = DATA_DIR / "processed_images"
EXPORTS_DIR = DATA_DIR / "exports"
MATERIALS_DIR = DATA_DIR / "materials"
MATERIAL_ZIPS_DIR = MATERIALS_DIR / "zips"
MATERIAL_IMAGES_DIR = MATERIALS_DIR / "images"
MATERIAL_EXPORTS_DIR = MATERIALS_DIR / "skipped_exports"
DB_PATH = DATA_DIR / "app.db"

# 前端构建产物目录
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"


class Settings(BaseSettings):
    """运行时配置。本地单用户应用,默认值即可启动。"""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 服务
    app_name: str = "昆虫标本图片识别与Excel录入工作台"
    backend_host: str = "127.0.0.1"
    backend_port: int = 8000

    # 模型调用内部常量(清单第5.4节:不暴露到设置界面)
    model_timeout_seconds: int = 120
    model_max_retries: int = 2  # 图片提取连续失败上限
    taxonomy_auto_correct_retries: int = 1  # 分类校验失败自动纠正次数

    # 图片预处理(清单第9节)
    image_max_long_edge: int = 3000
    image_jpeg_quality: int = 90

    # 数据素材 ZIP 安全限制
    material_zip_max_size_mb: int = 2048
    material_zip_max_uncompressed_mb: int = 4096
    material_zip_max_images: int = 20000
    material_zip_max_entries: int = 50000
    material_image_max_pixels: int = 40000000

    # 后台预加载(减少工作台图片切换等待时间)
    material_prefetch_size: int = 20  # ready 低水位(目标保持多少张已就绪)
    material_prefetch_concurrency: int = 4  # 初始模型并发数
    material_prefetch_max_concurrency: int = 12  # 最大模型并发数
    material_prefetch_max_lookahead: int = 60  # 最大前瞻排队数(含running/ready/failed)
    material_prefetch_interval: float = 1.0  # worker 轮询间隔(秒)
    material_prefetch_max_retries: int = 3  # 单张素材预加载失败重试次数
    material_prefetch_retry_delay: float = 5.0  # 重试初始延迟(秒)

    # 开发模式: 前端独立运行时允许跨域
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # 认证。首次启动必须通过环境变量提供管理员凭据。
    auth_cookie_name: str = "insect_session"
    auth_csrf_cookie_name: str = "insect_csrf"
    auth_session_hours: int = 24
    auth_login_max_failures: int = 5
    auth_login_window_seconds: int = 300
    auth_cookie_secure: bool = False
    bootstrap_admin_username: str = Field(
        default="", validation_alias="INSECT_BOOTSTRAP_ADMIN_USERNAME"
    )
    bootstrap_admin_password: str = Field(
        default="", validation_alias="INSECT_BOOTSTRAP_ADMIN_PASSWORD"
    )
    default_user_quota: int = 100


settings = Settings()


def ensure_dirs() -> None:
    """首次启动时自动创建数据目录。"""
    for d in (
        TEMPLATES_DIR,
        IMAGES_DIR,
        PROCESSED_IMAGES_DIR,
        EXPORTS_DIR,
        MATERIAL_ZIPS_DIR,
        MATERIAL_IMAGES_DIR,
        MATERIAL_EXPORTS_DIR,
        DATA_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)
