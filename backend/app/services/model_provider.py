"""OpenAI 兼容多模态模型客户端。

清单第 9 节要求:
- base_url 按 API 根地址处理,客户端统一在内部拼接 /chat/completions。
- 同一个 model_name 同时用于图片提取和文本分类。
- 图片传输使用 Base64 Data URL。
- 使用 OpenAI 兼容的 messages 图文混合格式。
- 模型返回非 JSON 时尝试提取 Markdown 代码块或首尾 {} 截取。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import re
from typing import Any

import httpx
from PIL import Image, ImageOps

from app.config import settings


class ModelError(Exception):
    """模型调用错误。"""


class VisionModelClient:
    """通用 OpenAI 兼容多模态客户端。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model_name: str,
        timeout: int | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model_name = model_name
        self.timeout = timeout or settings.model_timeout_seconds

    @property
    def _chat_url(self) -> str:
        """拼接 chat completions 接口地址。"""
        return f"{self.base_url}/chat/completions"

    @property
    def _models_url(self) -> str:
        """拼接 models 列表接口地址。"""
        return f"{self.base_url}/models"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ============================================================
    # 获取可用模型列表
    # ============================================================

    async def list_models(self) -> list[str]:
        """调用 GET /models 获取该 API Key 下所有可用模型名称。

        返回按字母排序的模型 ID 列表。
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(self._models_url, headers=self._headers)
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            raise ModelError(f"连接失败,请检查 Base URL: {e}") from e

        if resp.status_code == 401:
            raise ModelError("API Key 无效或未授权")
        if resp.status_code == 404:
            raise ModelError("Base URL 不是 API 根地址,无法找到 /models 接口")
        if resp.status_code != 200:
            raise ModelError(f"获取模型列表失败: HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            data = resp.json()
            models = data.get("data", [])
            ids = sorted(m["id"] for m in models if "id" in m)
            if not ids:
                raise ModelError("API 返回了空模型列表")
            return ids
        except (json.JSONDecodeError, KeyError) as e:
            raise ModelError(f"解析模型列表失败: {e}") from e

    # ============================================================
    # 核心调用
    # ============================================================

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int = 2000,
    ) -> str:
        """发送 chat completions 请求,返回模型文本内容。"""
        payload = {
            "model": self.model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.1,
        }

        last_error: Exception | None = None
        retries = settings.model_max_retries + 1
        for attempt in range(retries):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(
                        self._chat_url,
                        json=payload,
                        headers=self._headers,
                    )
                if resp.status_code != 200:
                    body = resp.text[:500]
                    raise ModelError(
                        f"模型接口返回 HTTP {resp.status_code}: {body}"
                    )
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                if not content or not content.strip():
                    raise ModelError("模型返回空内容")
                return content.strip()
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_error = e
                if attempt < retries - 1:
                    continue
                raise ModelError(f"模型连接失败(重试{retries}次后): {e}") from e
            except (KeyError, json.JSONDecodeError) as e:
                raise ModelError(f"模型响应格式错误: {e}") from e

        raise ModelError(f"模型调用失败: {last_error}")

    # ============================================================
    # 图片预处理(清单第 9 节)
    # ============================================================

    def prepare_image_base64(
        self,
        image_path: str,
        rotation_degrees: int = 0,
    ) -> str:
        """读取图片,EXIF 纠正 + 旋转 + RGB + 长边压缩 -> Base64 Data URL。

        原图不动,只生成内存中的 Base64。
        """
        img = Image.open(image_path)
        # 1. EXIF 方向纠正
        img = ImageOps.exif_transpose(img)
        # 2. 应用旋转(清单要求:前端旋转角度必须影响真实模型输入)
        if rotation_degrees in (90, 180, 270):
            img = img.rotate(-rotation_degrees, expand=True)
        # 3. 转 RGB
        if img.mode != "RGB":
            img = img.convert("RGB")
        # 4. 长边超过阈值时等比缩小
        max_edge = settings.image_max_long_edge
        w, h = img.size
        if max(w, h) > max_edge:
            ratio = max_edge / max(w, h)
            img = img.resize(
                (int(w * ratio), int(h * ratio)),
                Image.Resampling.LANCZOS,
            )
        # 5. JPEG 质量 -> Base64
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=settings.image_jpeg_quality)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"

    # ============================================================
    # 图片识别(清单第 10.1 节)
    # ============================================================

    async def recognize_image(
        self,
        image_path: str,
        prompt: str,
        rotation_degrees: int = 0,
    ) -> dict[str, Any]:
        """调用视觉模型提取图片信息,返回解析后的 JSON 字典。"""
        # PIL 预处理放入线程池，避免阻塞 FastAPI 事件循环
        image_data_url = await asyncio.to_thread(
            self.prepare_image_base64, image_path, rotation_degrees
        )

        messages = [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": image_data_url},
                    },
                ],
            },
        ]

        raw = await self._chat(messages, max_tokens=1000)
        return self._parse_json_response(raw)

    # ============================================================
    # 文本分类(清单第 10.2 节)
    # ============================================================

    async def complete_taxonomy(
        self,
        common_name: str,
        prompt: str,
    ) -> dict[str, Any]:
        """调用文本模型补全分类信息,返回解析后的 JSON 字典。"""
        filled_prompt = prompt.replace("{{confirmed_common_name}}", common_name)

        messages = [
            {"role": "system", "content": filled_prompt},
            {
                "role": "user",
                "content": f"确认后的中名：{common_name}",
            },
        ]

        raw = await self._chat(messages, max_tokens=800)
        return self._parse_json_response(raw)

    # ============================================================
    # JSON 解析(清单第 9 节:处理非合法 JSON)
    # ============================================================

    @staticmethod
    def _parse_json_response(raw: str) -> dict[str, Any]:
        """尝试多种方式从模型响应中提取 JSON。"""
        # 1. 直接解析
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # 2. 提取 Markdown 代码块中的 JSON
        code_block = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
        if code_block:
            try:
                return json.loads(code_block.group(1))
            except json.JSONDecodeError:
                pass

        # 3. 截取第一个 { 到最后一个 }
        first = raw.find("{")
        last = raw.rfind("}")
        if first != -1 and last != -1 and last > first:
            try:
                return json.loads(raw[first : last + 1])
            except json.JSONDecodeError:
                pass

        raise ModelError(f"无法从模型响应中提取 JSON: {raw[:200]}")

    # ============================================================
    # 测试连接(清单第 5.4 节:分别测试图片和文本JSON)
    # ============================================================

    async def test_image_input(self) -> tuple[bool, str]:
        """最小图片输入测试:确认模型支持图片。"""
        try:
            # 生成一个 1x1 红色测试图
            img = Image.new("RGB", (10, 10), color=(255, 0, 0))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            data_url = f"data:image/jpeg;base64,{b64}"

            messages = [
                {
                    "role": "system",
                    "content": "请用JSON回答。描述这张测试图片的主色调。",
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        },
                    ],
                },
            ]
            content = await self._chat(messages, max_tokens=50)
            return True, "图片输入测试通过"
        except ModelError as e:
            msg = str(e)
            if "404" in msg or "Not Found" in msg:
                return False, "Base URL 不是 API 根地址,无法找到接口"
            if "image" in msg.lower() or "multimodal" in msg.lower():
                return False, "当前模型不支持图片输入"
            return False, f"图片输入测试失败: {msg}"
        except Exception as e:
            return False, f"图片输入测试异常: {e}"

    async def test_text_json(self) -> tuple[bool, str]:
        """最小文本 JSON 分类测试:确认模型能返回合法 JSON。"""
        try:
            messages = [
                {
                    "role": "system",
                    "content": "请只返回JSON,不要Markdown或解释。返回: {\"test\": true}",
                },
                {"role": "user", "content": "请返回测试JSON。"},
            ]
            raw = await self._chat(messages, max_tokens=50)
            parsed = self._parse_json_response(raw)
            if isinstance(parsed, dict):
                return True, "文本JSON分类测试通过"
            return False, "模型未返回合法JSON对象"
        except ModelError as e:
            msg = str(e)
            if "401" in msg or "Unauthorized" in msg:
                return False, "API Key 无效或未授权"
            if "404" in msg or "Not Found" in msg:
                return False, "Base URL 不是 API 根地址,无法找到接口"
            return False, f"文本JSON分类测试失败: {msg}"
        except Exception as e:
            return False, f"文本JSON分类测试异常: {e}"
