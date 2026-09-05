# BiliBili Talk View

Bilibili 视频讨论分析网站。当前阶段为开发环境与部署基础，尚未实现评论采集、数据库存储或大模型分析。

## 贡献与后续开发

开始任务前阅读 [AGENTS.md](AGENTS.md)、[产品需求与数据约定](docs/references/product-requirements.md) 和 [分阶段交付路线](docs/references/delivery-roadmap.md)。每次只实施已确认阶段，先需求分析，再设计和计划，验收后推进下一阶段。

## 技术与目录

- `frontend/`：React、TypeScript、Vite；开发端口 5173。
- `backend/`：Python 3.12、FastAPI；开发端口 8000。
- `docs/environment-plan.md`：实施范围和后续数据对应约定。
- `Dockerfile`、`compose.yaml`：Linux 服务器容器部署。
- `.github/workflows/ci.yml`：构建、测试、静态检查和容器构建。

需要 Node.js 24、Python 3.12、Git。Docker 仅用于容器部署，本地开发无需启动 Docker。

## Windows 本地开发

在项目根目录运行（首次安装）：

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e backend
Set-Location frontend
npm.cmd ci
```

终端一，在项目根目录启动后端：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000
```

终端二，启动前端：

```powershell
Set-Location frontend
npm.cmd run dev
```

访问 http://127.0.0.1:5173 。健康接口为 http://127.0.0.1:8000/api/health ，开发 API 文档为 http://127.0.0.1:8000/api/docs 。

前端使用相对路径 `/api`；开发代理连接后端，生产同源提供服务，无需将后端地址或密钥放入前端。

Linux/macOS 对应虚拟环境命令为 `.venv/bin/python`，npm 使用 `npm`。

## 验证

在根目录运行：

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests
.\.venv\Scripts\python.exe -m ruff check backend
npm.cmd --prefix frontend run build
docker compose config --quiet
```

构建后启动后端即可通过 http://127.0.0.1:8000 查看生产构建页面；静态文件在后端启动时检测，首次构建后需要重启后端。

## 服务器部署

服务器安装 Docker Engine 与 Compose 插件后，将仓库克隆至服务器：

```bash
cp .env.example .env
docker compose up -d --build
curl --fail http://127.0.0.1:8080/api/health
```

容器默认绑定服务器回环地址 `127.0.0.1:8080`。将域名解析到服务器，并通过 Nginx/Caddy 配置 HTTPS 反向代理到该地址，外部用户即可通过域名访问。域名、证书和服务器部署在后续配置；本次没有发布到公网。

更新代码后重新运行 `docker compose up -d --build`。`docker compose logs --tail=100 web` 查看日志，`docker compose down` 停止服务。生产模式关闭交互式 API 文档。

当前没有数据库或采集任务，后续添加数据库、持久化卷与任务进程时需同步调整备份和部署配置。

## GitHub

主分支为 `main`，远端为 `git@github.com:dwwcqu/BiliBiliTalksView.git`。每个独立功能从更新后的 `main` 创建开发分支，完成该功能并验证后合并回 `main`，随后自动推送远端。首次关联命令如下：

```bash
git add .
git commit -m "chore: initialize development environment"
git remote add origin git@github.com:dwwcqu/BiliBiliTalksView.git
git push -u origin main
```

`.env`、虚拟环境、依赖目录、构建产物、数据与日志目录已忽略。Bilibili Cookie、模型 API Key 和评论导出数据不要提交至 Git；模型密钥后续仅配置在后端。

## 数据约定

UID 为用户唯一标识，昵称作为快照保存并展示。评论 ID、所属楼 ID、父评论 ID 和回复目标 UID 用于还原关系；所有外部 ID 以字符串传输。无法确认的回复对象标记未知。每次采集保存覆盖情况，不能将受限、空响应或中断误标为全量完成。
