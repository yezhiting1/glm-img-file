"""FastAPI 应用入口。开发时前后端分离(CORS),生产时 mount 前端 dist。"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import FRONTEND_DIST, settings
from app.database import SessionLocal, init_db
from app.routers import settings as settings_router
from app.routers import templates as templates_router
from app.routers import recognition as recognition_router
from app.routers import excel_preview as excel_preview_router
from app.routers import records as records_router
from app.routers import export as export_router
from app.routers import materials as materials_router
from app.routers import auth as auth_router
from app.routers import admin as admin_router
from app.services.prefetch_service import PrefetchWorker


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化数据库与目录,启动后台预加载 worker。"""
    init_db()
    worker = PrefetchWorker()
    await worker.start()
    yield
    await worker.stop()


app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
)


class MaterialUploadLimitMiddleware:
    def __init__(self, asgi_app):
        self.app = asgi_app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != "/api/materials/upload"
        ):
            await self.app(scope, receive, send)
            return

        limit = (settings.material_zip_max_size_mb + 1) * 1024 * 1024
        headers = dict(scope.get("headers", []))
        try:
            content_length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            content_length = 0
        if content_length > limit:
            response = JSONResponse(
                status_code=413,
                content={"detail": "素材上传请求体过大"},
            )
            await response(scope, receive, send)
            return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(status_code=413, detail="素材上传请求体过大")
            return message

        await self.app(scope, limited_receive, send)


app.add_middleware(MaterialUploadLimitMiddleware)

# 开发模式: 允许 Vite 开发服务器跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict:
    """健康检查接口,用于前后端连通测试。"""
    return {"status": "ok", "app": settings.app_name}


# 注册路由
app.include_router(auth_router.router)
app.include_router(admin_router.router)
app.include_router(settings_router.router)
app.include_router(templates_router.router)
app.include_router(recognition_router.router)
app.include_router(excel_preview_router.router)
app.include_router(records_router.router)
app.include_router(export_router.router)
app.include_router(materials_router.router)


# 生产模式: 托管前端静态资源,其他前端路由回退到 index.html
if FRONTEND_DIST.exists():
    assets_dir = FRONTEND_DIST / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def frontend_app(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        root = FRONTEND_DIST.resolve()
        candidate = (FRONTEND_DIST / full_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Not Found") from exc
        if candidate.is_file():
            return FileResponse(str(candidate))
        if Path(full_path).suffix:
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(str(FRONTEND_DIST / "index.html"))
