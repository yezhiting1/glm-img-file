# 昆虫标本工作台

一个面向昆虫标本数字化录入的多用户 Web 工作台。系统使用兼容 OpenAI API 的视觉模型识别标本图片，自动补全分类信息，并按照每位用户独立配置的 Excel 模板生成可下载文件。

## 功能

- 图片识别：提取中名、图像编号、产地、采集人和采集日期。
- 分类补全：自动生成纲、目、科、亚科、族、属、亚属和种本名。
- 素材批处理：上传 ZIP，按顺序处理图片，并支持后台预加载和失败重试。
- Excel 模板：每位用户拥有独立模板、字段映射、实时预览和导出文件。
- 记录管理：搜索、筛选、编辑、重新分类和安全删除。
- 多用户隔离：记录、素材、模板、图片、预览和导出均按所有者隔离。
- 权限管理：管理员可创建、停用用户，切换数据所有者并调整工作流配额。
- 安全登录：Argon2 密码哈希、服务端会话、HttpOnly Cookie、CSRF 防护和登录限流。

## 技术栈

| 层级 | 技术 |
| --- | --- |
| 后端 | Python 3.12+、FastAPI、SQLAlchemy、SQLite、openpyxl、Pillow |
| 前端 | React 19、TypeScript、Vite、Tailwind CSS、react-data-grid |
| AI | OpenAI Chat Completions 兼容的多模态视觉模型 |
| 测试 | pytest、Vitest、Testing Library |
| 部署 | Docker Compose，或本地 Python + Node.js |

## Windows 便携版

Windows 10/11 x64 用户可以在
[v1.0.0 Release](https://github.com/Ye-feng0510/insect-workbench/releases/tag/v1.0.0)
下载 `insect-workbench-portable-v1.0.0-windows-x64.zip`。

便携版已内置 Python、后端依赖和生产前端，不需要安装 Python、Node.js
或 Docker。完整解压后双击 `启动昆虫标本工作台.bat`，首次启动按提示创建管理员即可。

业务数据保存在便携目录的 `data/`，管理员初始化配置保存在 `.env`。
升级或移动前请同时备份这两部分。

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/Ye-feng0510/insect-workbench.git
cd insect-workbench
```

### 2. 配置首次管理员

复制环境变量模板：

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`，至少设置以下两项：

```ini
INSECT_BOOTSTRAP_ADMIN_USERNAME=admin
INSECT_BOOTSTRAP_ADMIN_PASSWORD=请替换为至少12位的强密码
```

首次成功启动后会创建管理员。已有启用管理员时，后续启动不再依赖这两个变量。不要提交 `.env`、真实密码或模型 API Key。

### 3. 使用 Docker 启动（推荐）

```bash
docker compose up -d --build
```

访问 <http://127.0.0.1:8000>。

```bash
# 查看日志
docker compose logs -f

# 停止服务
docker compose down

# 拉取代码后重新构建
docker compose up -d --build
```

`docker-compose.yml` 会把宿主机的 `./data` 挂载到容器，删除或重建容器不会删除业务数据。

### 4. 使用本地脚本启动

Docker 模式和本地模式默认都使用端口 `8000`，请勿同时运行。切换到本地模式前先执行：

```bash
docker compose down
```

Windows：

```cmd
scripts\start.bat
```

Linux 或 macOS：

```bash
chmod +x scripts/start.sh
./scripts/start.sh
```

Windows 脚本会检查 Docker 实例、端口占用、前端构建新鲜度和后端关键依赖。

## 开发模式

### 后端

Windows PowerShell：

```powershell
python -m venv backend/venv
backend\venv\Scripts\python.exe -m pip install -r backend\requirements.txt
backend\venv\Scripts\python.exe -m uvicorn app.main:app --reload --app-dir backend
```

Linux 或 macOS：

```bash
python -m venv backend/venv
backend/venv/bin/python -m pip install -r backend/requirements.txt
backend/venv/bin/python -m uvicorn app.main:app --reload --app-dir backend
```

### 前端

```bash
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend dev
```

前端开发服务器默认运行在 <http://127.0.0.1:5173>，并将 `/api` 代理到后端。

## 首次使用

1. 使用启动管理员登录。
2. 在「设置」中填写模型 Base URL、API Key 和模型名称，并测试连接。
3. 在「模板与导出」中上传当前用户的 Excel 模板并保存字段映射。
4. 在「识别工作台」上传标本图片，核对识别结果后确认入表。
5. 在「记录管理」中维护数据，或在「模板与导出」中生成 Excel。
6. 管理员可在「用户管理」中创建普通用户并分配工作流配额。

## 用户、权限与配额

| 角色 | 权限 |
| --- | --- |
| 普通用户 | 只能访问自己的记录、素材、模板、图片、预览和导出 |
| 管理员 | 管理用户和配额，并通过明确的所有者上下文管理指定用户数据 |

工作流在开始时预留配额，成功完成后核销，未完成或失败时释放。后台预加载本身不消耗配额。

## 环境变量

完整配置见 [`.env.example`](.env.example)。

| 变量 | 用途 | 默认值 |
| --- | --- | --- |
| `INSECT_BOOTSTRAP_ADMIN_USERNAME` | 首次启动管理员用户名 | 空 |
| `INSECT_BOOTSTRAP_ADMIN_PASSWORD` | 首次启动管理员密码，至少 12 位 | 空 |
| `AUTH_COOKIE_SECURE` | 仅通过 HTTPS 发送登录 Cookie | `false` |
| `AUTH_SESSION_HOURS` | 登录会话有效时间 | `24` |
| `DEFAULT_USER_QUOTA` | 新建普通用户的默认工作流配额 | `100` |
| `MODEL_TIMEOUT_SECONDS` | 模型请求超时秒数 | `120` |
| `MODEL_MAX_RETRIES` | 模型请求重试次数 | `2` |
| `IMAGE_MAX_LONG_EDGE` | 图片压缩后的最长边 | `3000` |
| `IMAGE_JPEG_QUALITY` | JPEG 输出质量 | `90` |
| `CORS_ORIGINS` | 开发环境允许的前端来源 | 本地 Vite 地址 |

模型连接信息通过管理员设置页面保存。请使用支持图片输入且兼容 OpenAI Chat Completions API 的模型服务。

## 数据存储与迁移

运行数据位于项目根目录的 `data/`：

```text
data/
├── app.db
├── templates/
├── images/
├── processed_images/
├── materials/
└── exports/
```

数据库启动时执行版本化迁移。旧数据库迁移前会创建 `app.db.backup-*` 备份。缺少首次管理员配置时，预检会在数据库备份和结构变更之前终止。

请定期备份整个 `data/` 目录。不要只备份 SQLite 文件，因为模板、图片和导出文件也属于业务数据。

## 测试与质量检查

后端：

```powershell
$env:PYTHONPATH="$PWD\backend"
backend\venv\Scripts\python.exe -m pytest backend\tests -q
backend\venv\Scripts\python.exe -m compileall -q backend\app backend\tests
```

前端：

```bash
pnpm --dir frontend exec vitest run
pnpm --dir frontend lint
pnpm --dir frontend build
```

## 项目结构

```text
insect-workbench/
├── backend/
│   ├── app/
│   │   ├── routers/        # API、认证和管理员路由
│   │   ├── services/       # 识别、素材、模板、预览、导出和配额服务
│   │   ├── auth.py         # 会话、CSRF、RBAC 和所有者上下文
│   │   ├── migrations.py   # 数据库迁移与首次管理员
│   │   ├── models.py       # SQLAlchemy 数据模型
│   │   └── main.py         # FastAPI 入口和后台预加载生命周期
│   ├── tests/
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── components/
│   │   ├── contexts/
│   │   ├── pages/
│   │   └── services/
│   ├── package.json
│   └── pnpm-lock.yaml
├── scripts/
├── data/                   # 本地运行数据，不提交到 Git
├── docker-compose.yml
├── Dockerfile
└── .env.example
```

## 部署说明

当前生产模型是一个带持久数据卷的 Docker 容器。SQLite、本地模板、图片、ZIP、导出文件和常驻预加载线程都需要持久磁盘及长生命周期进程。

不要把完整后端直接部署为无状态 Serverless Function。若将前端部署到 Vercel，后端仍应运行在支持 Docker、HTTPS、持久卷和长任务的主机上。

## 常见问题

### 首次启动提示必须设置管理员

确认项目根目录存在 `.env`，并设置：

```ini
INSECT_BOOTSTRAP_ADMIN_USERNAME=admin
INSECT_BOOTSTRAP_ADMIN_PASSWORD=至少12位强密码
```

### 端口 8000 已被占用

Docker 和本地脚本不能同时监听 `8000`。如果 Docker 已运行，可直接访问应用；若要本地启动，请先执行 `docker compose down`。

### 页面提示尚未配置 Excel 模板

每位用户拥有独立模板。请切换到对应数据所有者，然后在「模板与导出」页面上传模板并保存字段映射。

### 图片识别失败

在管理员「设置」页面检查模型 Base URL、API Key 和模型名称，并执行连接测试。

### 支持哪些图片格式

支持 JPG、JPEG、PNG 和 WebP。系统会处理 EXIF 方向并按照环境变量配置压缩图片。
