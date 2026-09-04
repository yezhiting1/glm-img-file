"""设置路由:模型API配置、提示词、测试连接。

清单第 8.1 节:
  GET    /api/settings
  PUT    /api/settings
  POST   /api/settings/test-model
  GET    /api/settings/prompts
  PUT    /api/settings/prompts
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import settings as app_config
from app.auth import require_admin
from app.database import get_db
from app.models import AppSettings
from app.schemas import (
    ModelConfig,
    ModelsListRequest,
    ModelsListResponse,
    PromptConfig,
    TestModelRequest,
    TestModelResponse,
    TestResult,
)
from app.services.model_provider import ModelError, VisionModelClient

router = APIRouter(
    prefix="/api/settings",
    tags=["settings"],
    dependencies=[Depends(require_admin)],
)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _get_or_create_settings(db: Session) -> AppSettings:
    """获取单例配置(id=1),不存在则创建。"""
    obj = db.get(AppSettings, 1)
    if obj is None:
        obj = AppSettings(id=1)
        db.add(obj)
        db.commit()
        db.refresh(obj)
    return obj


def _load_default_prompt(filename: str) -> str:
    """读取默认提示词文件。"""
    filepath = PROMPTS_DIR / filename
    if filepath.exists():
        return filepath.read_text(encoding="utf-8")
    return ""


def get_settings_config(db: Session = Depends(get_db)) -> AppSettings:
    """依赖:获取配置对象(供其他模块复用)。"""
    return _get_or_create_settings(db)


# ============================================================
# 模型 API 配置
# ============================================================

@router.get("", response_model=ModelConfig)
async def get_model_settings(db: Session = Depends(get_db)):
    """获取模型 API 配置(API Key 做掩码处理)。"""
    s = _get_or_create_settings(db)
    masked_key = ""
    if s.api_key:
        k = s.api_key
        masked_key = k[:4] + "*" * (len(k) - 8) + k[-4:] if len(k) > 8 else "****"
    return ModelConfig(
        base_url=s.base_url,
        api_key=masked_key,
        model_name=s.model_name,
    )


@router.put("", response_model=ModelConfig)
async def update_model_settings(
    config: ModelConfig,
    db: Session = Depends(get_db),
):
    """更新模型 API 配置。

    前端传回掩码 Key 时不更新(只有真实 Key 才写入)。
    """
    s = _get_or_create_settings(db)
    s.base_url = config.base_url.strip()
    s.model_name = config.model_name.strip()
    # 掩码 Key(含 ****)不覆盖真实 Key
    incoming_key = config.api_key.strip()
    if incoming_key and "****" not in incoming_key and "*" not in incoming_key:
        s.api_key = incoming_key
    elif incoming_key and s.api_key == "":
        # 首次设置
        s.api_key = incoming_key.replace("*", "")
    db.commit()
    db.refresh(s)
    # 配置变更后使预加载缓存失效
    from app.services.prefetch_service import get_worker
    worker = get_worker()
    if worker is not None:
        worker.invalidate_all()
    # 返回掩码
    masked_key = ""
    if s.api_key:
        k = s.api_key
        masked_key = k[:4] + "*" * (len(k) - 8) + k[-4:] if len(k) > 8 else "****"
    return ModelConfig(
        base_url=s.base_url,
        api_key=masked_key,
        model_name=s.model_name,
    )


# ============================================================
# 提示词
# ============================================================

@router.get("/prompts", response_model=PromptConfig)
async def get_prompts(db: Session = Depends(get_db)):
    """获取提示词。若数据库为空则返回默认值。"""
    s = _get_or_create_settings(db)
    rec = s.recognition_prompt or _load_default_prompt("recognition_prompt.txt")
    tax = s.taxonomy_prompt or _load_default_prompt("taxonomy_prompt.txt")
    return PromptConfig(
        recognition_prompt=rec,
        taxonomy_prompt=tax,
    )


@router.put("/prompts", response_model=PromptConfig)
async def update_prompts(
    config: PromptConfig,
    db: Session = Depends(get_db),
):
    """保存提示词。"""
    s = _get_or_create_settings(db)
    s.recognition_prompt = config.recognition_prompt
    s.taxonomy_prompt = config.taxonomy_prompt
    db.commit()
    # 提示词变更后使预加载缓存失效
    from app.services.prefetch_service import get_worker
    worker = get_worker()
    if worker is not None:
        worker.invalidate_all()
    return PromptConfig(
        recognition_prompt=s.recognition_prompt,
        taxonomy_prompt=s.taxonomy_prompt,
    )


# ============================================================
# 获取可用模型列表
# ============================================================

@router.post("/models", response_model=ModelsListResponse)
async def list_models(
    req: ModelsListRequest,
    db: Session = Depends(get_db),
):
    """根据 Base URL 和 API Key 获取可用模型列表。

    前端填完 Base URL + API Key 后调用此接口获取下拉列表。
    """
    base_url = req.base_url.strip()
    api_key = req.api_key.strip()

    if not base_url:
        raise HTTPException(status_code=400, detail="请先填写 Base URL")
    if not api_key or "*" in api_key:
        # 如果 API Key 是掩码(含 *),尝试用已保存的真实 Key
        s = _get_or_create_settings(db)
        api_key = s.api_key
        if not api_key:
            raise HTTPException(status_code=400, detail="请先填写 API Key")

    if "/chat/completions" in base_url:
        raise HTTPException(
            status_code=400,
            detail="Base URL 应填写 API 根地址(如 https://example.com/v1),不要包含 /chat/completions",
        )

    client = VisionModelClient(base_url, api_key, "")
    try:
        models = await client.list_models()
    except ModelError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return ModelsListResponse(models=models)


# ============================================================
# 测试连接(清单第 5.4 节:必须分别测试图片和文本JSON)
# ============================================================

@router.post("/test-model", response_model=TestModelResponse)
async def test_model(
    req: TestModelRequest,
    db: Session = Depends(get_db),
):
    """测试模型连接:分别执行图片输入测试和文本JSON分类测试。"""
    # 确定使用请求中的配置还是已保存配置
    s = _get_or_create_settings(db)
    base_url = (req.base_url or s.base_url).strip()
    # 如果 API Key 是掩码(含 *),用已保存的真实 Key
    incoming_key = (req.api_key or "").strip()
    if incoming_key and "*" not in incoming_key:
        api_key = incoming_key
    else:
        api_key = s.api_key
    model_name = (req.model_name or s.model_name).strip()

    # 校验必填
    missing = []
    if not base_url:
        missing.append("Base URL")
    if not api_key:
        missing.append("API Key")
    if not model_name:
        missing.append("模型名称")
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"缺少必填项: {'、'.join(missing)}",
        )

    # 校验 base_url 不含具体接口路径
    if "/chat/completions" in base_url:
        raise HTTPException(
            status_code=400,
            detail="Base URL 应填写 API 根地址(如 https://example.com/v1),不要包含 /chat/completions",
        )

    client = VisionModelClient(base_url, api_key, model_name)

    # 分别测试两种能力
    image_ok, image_msg = await client.test_image_input()
    text_ok, text_msg = await client.test_text_json()

    return TestModelResponse(
        image_test=TestResult(passed=image_ok, message=image_msg),
        text_json_test=TestResult(passed=text_ok, message=text_msg),
        overall=image_ok and text_ok,
    )
