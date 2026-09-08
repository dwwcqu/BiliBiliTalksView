# BiliBili Talk View

Bilibili 视频讨论分析网站。当前提供基础采集页面、采集/增量导出 CLI、PostgreSQL 存储、持久队列/worker 和 HTTP API。页面支持输入视频链接、查看任务进度与保存摘要；讨论浏览和大模型分析尚未实现。

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
.\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --reload --no-proxy-headers --host 127.0.0.1 --port 8000
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
npm.cmd --prefix frontend test
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

网站 API 已接入 PostgreSQL 和持久队列；采集由独立 worker 执行，单独启动 web 容器不会执行采集。正式部署仍须配置数据库、持久化工作目录、worker 和访问凭据，并核验备份恢复及服务器采集能力。

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


## 独立评论采集与导出

安装 `backend[dev]` 后，可从仓库根目录运行下列命令。脚本接受 HTTPS 的 Bilibili BV 视频链接或 ep 剧集链接，输出格式遵守 [评论导出协议 2.0.0](docs/references/comment-export-protocol-v2.md)。调试视频不是默认输入。

PowerShell 示例：

```powershell
$env:BILIBILI_COOKIE_FILE = 'D:\BiliBiliTalkView\data\verification\bilibili-cookie.txt'
.\.venv\Scripts\python.exe -m app.comment_export.cli collect --url 'https://www.bilibili.com/bangumi/play/ep4292435/' --output 'D:\Data'
```

Linux 可设置自己的 Cookie 文件路径，并指定 `/data` 输出根（需有写入权限）：

```bash
export BILIBILI_COOKIE_FILE=/secure/bilibili-cookie.txt
python -m app.comment_export.cli collect --url 'https://www.bilibili.com/bangumi/play/ep4292435/' --output /data
```

Windows 和 Linux 使用相同的 `--output` 参数；路径含空格时加引号。Cookie 文件不传到前端或版本库，不在命令行参数内直接填写 Cookie。

`--output` 默认为 `data/exports`，可改为用户选择的目录；`--work-dir` 默认为 `data/collection`。collect 会在工作根下按输入路径生成恢复子目录，执行结果返回具体 work_dir。相同输入恢复使用原命令追加 `--resume`；请求预算不会因重启重置，访问受限的任务不会自动解禁。新运行须使用新的工作根，不能用它绕过源站限制。

```text
python -m app.comment_export.cli export --work-dir <结果中的work_dir> --output <输出根>
python -m app.comment_export.cli validate --batch <结果中的批次路径>
```

export 只读取已有工作集，不联网。collect 结束会自动导出，包括可用的 partial 结果。来源拒绝或覆盖未核验时退出码为 3；成功的 collect 为 0。参数错误为 2，发布/文件操作失败为非零。以返回的 coverage 判断可用范围，不以文件存在推断抓取完整。

下游从 `<输出根>/bilibili-video-<aid>/current.json` 找到批次，再读取 manifest 以及楼/用户文件；Python 可用 `app.comment_export.publication.read_current(Path(...))` 整批读取。该函数返回 manifest 与去重评论列表，按楼和用户的独立 JSONL 仍保留在批次目录中。必须同时检查 coverage.status、context_status 和 reasons。当前只保留最新发布结果与最近一次工作数据；下游持有的旧路径可能过期，应遵循协议整批重读。

图片和表情仅保存描述与来源 URL；脚本不下载媒体、不调用模型。真实用户数据不进入 Git。目标 Linux 服务器的实际采集可用性仍需部署阶段验证。


## PostgreSQL 存储子系统

文件采集 CLI 与数据库 CLI 分离，数据库不要求桌面浏览器。安装 backend[dev] 后，在本地配置 POSTGRES_PASSWORD 和 DATABASE_URL；迁移命令只对明确选择的开发库执行。

```text
docker compose --env-file <本地数据库配置文件> -f compose.postgres.yaml up -d --wait db
python -m alembic -c backend/alembic.ini upgrade head
python -m app.storage.cli import --input <批次目录或current.json> --publish
python -m app.storage.cli query --state-id <状态ID> --root-id <根评论ID> --limit 100
python -m app.storage.cli query --state-id <状态ID> --uid <用户UID> --limit 100
python -m app.storage.cli export --video-id bilibili:video:<aid> --output <输出目录>
```

DATABASE_URL 形式为 `postgresql+psycopg://用户:密码@主机:端口/数据库`，仅写本地环境配置，不粘贴进聊天或提交Git。开发容器默认端口5433，仅绑定127.0.0.1；端口不可用可设置POSTGRES_PORT。compose使用命名卷持久化，不要用 down -v 做重启或更新。

import 默认只导入，加 --publish 才请求发布；partial 保持工作状态且不替换已有完整状态。首次仅有partial时可以按返回state_id查询，coverage会明确显示限制。重复同批次导入不增加记录；同批次ID内容变化会拒绝。命令失败返回安全错误码，JSON输出中published=false不能理解成完整发布成功。

数据库再导出沿用文件协议2.0.0，支持 Windows 的 D:\Data 和 Linux 的 /data；验收时用独立目录，避免覆盖下游已编辑文件。源文件必须符合固定协议，包括空README.md；如果下游添加了说明内容，应放在批次目录外，不要原地改已发布批次。

数据库测试需明确配置 TEST_DATABASE_URL，且库名必须为 bilibili_talks_test。测试服务与开发服务隔离；每个测试创建独立schema，只清理它自己创建的schema。未配置测试连接时集成测试会skip，这不算数据库验收通过。

```text
docker compose --env-file <本地数据库配置文件> -f compose.postgres.yaml --profile test up -d --wait db-test
python -m pytest backend/tests/storage -q
```

首次数据迁移、真实导入及目标服务器部署应分别核验。当前模块已连接小时刷新任务和基础采集页面；模型分析尚未接入。

### 正文复用与已有数据库升级

迁移 `0002_comment_payloads` 将大段正文及未知扩展存入 `comment_payloads`，`comments` 保存状态成员和载荷引用。同一视频/评论ID的相同正文在保留状态间复用；昵称、点赞等已知观测字段变化不重新写入正文。载荷只在没有任何状态引用时回收。按楼/UID查询与协议v1/v2导入、v2再导出的字段语义保持不变。

这项改造减少数据库正文 INSERT；来源选择性增量抓取使用下文独立 refresh 命令，collect 继续采用原全量流程。文件导出仍生成完整新批次。

已有0001数据库切换前应停止存储写入及旧版读取进程，并备份所选数据库；先在独立测试实例验证备份可恢复，再用已明确配置的 `DATABASE_URL` 执行上述 `upgrade head`。迁移在事务中回填并逐条验证后才移除重复正文列，失败应整体回滚。新旧存储代码不能混跑。回退时停用新代码，执行 `python -m alembic -c backend/alembic.ini downgrade 0001_discussion_storage` 恢复旧列后再切回旧代码；这不会恢复之前已按保留政策回收的历史状态。

常规实现验收只迁移隔离测试schema，不会自动升级现有开发数据库或改写 D:\Data 导出样本。



## 访问诊断与受限恢复

```text
python -m app.comment_export.cli diagnose --work-dir <具体任务目录>
python -m app.comment_export.cli login-check
python -m app.comment_export.cli probe --work-dir <具体任务目录> [--next-pending]
python -m app.comment_export.cli recover --work-dir <具体任务目录> --output <输出根> [--next-pending]
```

diagnose只读本地，不读取Cookie、不联网；显示具体失败码（如果原任务确实记录了）、剩余预算、目标摘要及已知复测限制。probe是网络操作，成功仍不解除blocked，也不会写入评论。recover必须即时复测，使用同一client/凭据；首个有效页的数据库事务提交后才解除阻塞，任何旧probe摘要都不能作为恢复票据。

旧检查点没有原始错误码时显示unknown_legacy。使用--next-pending明确选择下一待采页；首次登记只建立固定30分钟保护冷却，不发网络请求。重试与进程重启不会重置该期限。之后若实际来源返回新的拒绝，冷却可按新响应延长；不能据本地冷却结束推断来源已恢复。

BILIBILI_CONTROL_DIR默认为data/collection-control。一个部署的全部CLI必须共用该目录并持久保留，不能每任务单设目录或用重启清零；正式客户端工厂统一注入共享访问策略。多个容器/服务器的共享协调与后台任务仍需后续部署设计。

当前最低请求间隔2秒；429/403/412及未知非零业务拒绝触发至少30分钟冷却，遵守更晚的有效Retry-After。登录检查同样受冷却约束，且登录成功不等于评论端点恢复。网络/服务器错误不自动循环重试。

Cookie只通过BILIBILI_COOKIE_FILE配置，正常登录/验证由管理员完成。任务诊断不包含Cookie、响应正文或账号资料。probe成功退出0只表示该次请求通过；recover仍blocked/partial返回3。采集与文件导出状态分别输出，已有下游批次被编辑时请选择新的--output，不能清空对方README绕过校验。


回复端点返回HTTP200且业务码12022/12006时，采集器把该楼记为源端当前不可用：保留已取得评论、保持回复分页partial，继续其他楼。recover也可在即时复测确认后原子保存这一局部终态。全部可处理楼遍历结束时finished可为true，但只要仍有这些缺口，coverage仍为partial；不能把该楼当作已验证零回复。HTTP429/403等优先作为访问问题处理，仍会停止并触发冷却。


## 当前英文导出目录（协议2.0.0）

新CLI导出和数据库再导出默认使用英文目录；旧1.0.0数据仍可读取、校验和导入。用户昵称只保留在JSON中，不能拼入文件夹名。生成的发布目录使用小写ASCII字母、数字、下划线和连字符，不允许中文、空格、换行或制表符。JSON正文和昵称保持原文。

```text
<output>/bilibili-video-<aid>/
├── current.json
└── batches/<export_id>/
    ├── README.md
    ├── manifest.json
    ├── threads/000/
    │   ├── thread.json
    │   └── comments.jsonl
    └── users/uid_<uid>/
        ├── user.json
        └── comments.jsonl
```

未知作者放users/unknown。comments.jsonl每行一条评论；有回复的楼包含根评论及已取得的全部回复，单条文件不自动表示错误：应看thread.json的reply_count和coverage.pagination_status。not_started/partial不能当已采全，verified且reply_count=0才表示该次核验没有回复。

本机当前数据交接根为D:/Data/exports，请从其视频current.json找最新批次。早期postgresql-validation及其他调试目录保留的是旧批次，不会随着采集自动补写到旧UUID目录中；不要把旧样例当最新数据。


## 增量刷新 CLI

`refresh` 已支持从先前采集工作集或协议批次建立新任务。先完整读取主楼列表，再重抓新增楼、回复数/根正文变化的楼及尚未完成核验的楼；其余楼回复可复用。相同数量不能证明评论没有变化，复用范围会明确标记partial。

```powershell
python -m app.comment_export.cli refresh --url "https://www.bilibili.com/bangumi/play/ep4292435/" --baseline-work "data/collection/previous-task" --work-dir "data/refresh-task" --output "D:\Data\exports"
```

上面网址仅为调试示例；工作目录填写实际旧任务路径，新任务目录必须尚无work.sqlite3。输出根可以换成Linux的 `/data`。也可使用 `--baseline-batch <批次目录或current.json>` 替代 `--baseline-work`；没有基线时按full进行首次采集。

- 默认 `--mode auto`：有可信工作集核验依据且最近完整范围检查未超过24小时，采用incremental；无可信依据、上次full未完成或到期，采用full。可用 `--mode full` 主动要求全量核对，`--full-interval-hours` 设置正数小时周期。到期本身不发请求。
- 单独协议文件不包含内部核验证据，首次auto会full；后续使用该次工作集作为基线，才可安全连续快刷。下游无需合并增量片段，每次仍生成完整JSONL批次。
- 同一任务中断后使用 `refresh --url <原链接> --work-dir <该任务目录> --output <输出根> --resume`。恢复不再指定基线，实际URL、预算和模式取已冻结值；首次视频解析失败也可恢复。访问受限仍须沿用diagnose/probe/recover流程，不因刷新绕过冷却。
- 输出JSON包含mode、stats、coverage与实际path。退出码0表示verified，完成但partial为3；有完整current时，新partial保留在返回的工作路径，current.json仍指向完整结果。不要把“本次未发现新增”解读为所有评论均无变化。
- 当前CLI没有网站共享小时配额、后台队列或自动清理用户指定的旧任务目录；这些由后续后台任务单元接入。刷新不会将缺失旧评论自动删除，也不会把旧评论的collected_at改为当前时间。


## 零回复楼详情请求优化

网页首次提交和无基线的 `refresh --mode auto` 可以省去符合条件的零回复详情请求：主列表必须明确返回整数0，身份、辅助计数、预览和重复观察均无矛盾，而且没有已保存回复或历史反证。楼主评论始终保存。

```powershell
python -m app.comment_export.cli refresh --url "https://www.bilibili.com/bangumi/play/ep4292435/" --work-dir "data/first-auto-task" --output "D:\Data\exports" --mode auto
```

工作目录应为新任务目录，输出路径可换成Linux路径。原 `collect`、实际新建的 `--mode full` 及到期完整核对仍访问详情。活动任务继续按原策略运行，不因重复提交full改变已冻结的策略，也不绕过小时资格。

仅依据主列表零报告处理的楼保持partial；页面按每个结果显示相应楼数，不表示详情已核验为空。历史计数为非零或曾不可用的楼不会因此清空旧记录；未知旧历史不能当作无历史。原有增量复用和大楼尾部更新继续工作，缺少新证据的旧楼不会被强行认定具备零省略资格。

完成证明与详情核验时间分开，普通更新不能延后已经固定的完整核对期限。worker默认24小时，CLI沿用full_interval_hours；跨期恢复可以结束原observe任务，但下次实际新建auto必须完整核对。没有显式刷新就不会自动发起采集，24小时不是无人操作时的数据时效保证。

新证据仅在工作集及内部refresh_context中保存，公开导出协议不变。升级前停止旧worker并备份工作集；本次无需新增PostgreSQL迁移，旧数据仍可读取。

## 大楼尾部增量优化

当 `auto` 的有效模式是 incremental，且已保存可信分页边界、旧回复至少占4页、回复数增加、根身份和正文未变时，采集器从旧末页前一页开始回读。逐页检查重叠ID、顺序、计数和归属，通过后才原子合并候选记录。397→398条的固定追加场景读取第19、20页；400→401条读取第19～21页。

- 新楼、小楼、显式full、完整核对周期到期、回复数减少或无可信证据时继续完整读取该楼。旧任务不会靠ID或时间排序伪造分页边界；需要更新时先完整获取一次，之后才可加速。
- 重叠错位、计数或身份异常会立即回退整楼读取。来源拒绝、取消和预算限制仍按原规则处理，回退及恢复不重置请求预算。
- 候选页与正式工作集隔离；中断后先重新核对重叠边界。尾部成功仍属于partial，未读区域沿用旧记录，完整核对时间不会被连续追加延后。
- 分页证据仅保存于工作集和PostgreSQL内部刷新上下文，不改变公开JSON/JSONL协议。没有新增分页大小、并发或限速参数，首次采集和主评论扫描方式不变。
- 更新本地或服务器worker前先停止旧worker并备份工作集；SQLite候选表会幂等创建，本次无需新增PostgreSQL迁移。已有API响应和页面仍兼容；不要让旧版worker与新版恢复状态混跑。

## 后台数据交接接口（C1）

后台数据交接模块已由下述队列、worker 和网站任务接口使用：

- `app.storage.baseline.freeze_baseline(conn, video_id, target)`：从一致数据库快照冻结最新可读current/partial工作状态及核验证据，返回state_id/cache_version。SQLite在临时目录写完并关闭后才原子发布，已有目标不覆盖；无可读缓存时返回无基线。
- `app.storage.handoff.prepare_handoff(frozen, records, metadata, job_id, baseline_version)`：将采集工作集、协议批次、任务身份和版本绑定，校验后生成不可变交接对象。数据库只保存精简核验证据，不复制正文到上下文。
- `app.storage.handoff.materialize(conn, frozen, handoff, commit_guard)`：原子导入、保存上下文和发布；版本已变、数据失败、guard抛错或显式返回False均回滚。guard由后续worker实现，在同一连接/事务内核对任务所有权和取消并保存任务结果；不得自行提交事务。

普通文件CLI仍按原分批方式导入，不信任文件里的内部刷新字段。冻结基线可在数据库旧状态回收后独立读取；它不是面向用户的历史快照列表。默认浏览仍优先完整current，刷新基线可以选择更新的partial。

`0003_cache_handoff`新增缓存版本及内部上下文。数据库触发器覆盖CLI的指针和可见状态变化，防止旧任务覆盖更新后的缓存。本地开发数据库已通过后续联调升级至 0005；其他环境仍按前文停用旧进程、备份、核验后执行`upgrade head`。如需回到仅正文复用版本，停写后执行`python -m alembic -c backend/alembic.ini downgrade 0002_comment_payloads`，该降级保留评论及载荷，但移除版本和核验证据，下一次刷新需保守全量核验。


## 持久队列与后台 Worker（C2）

先停止旧存储进程、备份并核验所选数据库，再执行 `python -m alembic -c backend/alembic.ini upgrade head` 升级到0004。代码已通过隔离测试库验收，现有开发库已在备份恢复与迁移演练通过后升级到 0005；新环境仍需自行执行迁移。配置DATABASE_URL、BILIBILI_COOKIE_FILE及所有采集进程共享的BILIBILI_CONTROL_DIR；工作根可通过JOB_DATA_ROOT或worker的--data-root指定。

```powershell
python -m app.jobs.cli submit --url "https://www.bilibili.com/bangumi/play/ep4292435/"
python -m app.jobs.cli worker --once
python -m app.jobs.cli worker
python -m app.jobs.cli video --video-id "bilibili:video:117213600155829"
python -m app.jobs.cli refresh --video-id "bilibili:video:117213600155829" --mode auto
```

链接和视频ID仅为调试示例。submit只做本地校验/查询/排队，不访问Bilibili；worker才执行来源请求。首次未知链接先返回request_id，用`request --request-id <id>`查看解析；解析后通过`job --job-id <id>`查看采集。--once只处理一个意图，因此首次运行通常先完成解析，下一次才采集；循环模式在无任务时每2秒检查一次。

- 同视频同小时及跨小时仍活动的任务会归并。小时保留原接受时间；普通缓存访问不自动刷新。导入缓存同小时仍视为新鲜。
- 仅一个worker可持部署会话锁。60秒租约、10秒心跳与请求/页提交/物化前校验共同保护执行；新worker在旧租约到期且文件锁可取得后从原工作集恢复。
- 网络/5xx最多自动续跑两次（60秒、300秒），解析最多6请求，采集默认12000；重启和重试不重置预算。来源拒绝暂停全局访问，冷却到期也不自动解除闸门。
- 本地管理命令为`cancel/retry/recover --job-id <id>`、`request-retry/request-recover --request-id <id>`及`source-revalidate`。取消任务不解除来源限制；取消后的复测仅恢复闸门，不重新启动已取消任务。普通权限/来源缺失问题应查看安全错误码，不自行清空控制数据库。
- 任务结果经C1原子入库，verified发布current，partial只替换working；worker不在数据库提交前发布文件。文件交付继续使用独立数据库export CLI。
- 工作集仅清理登记UUID下的数据，保留闸门引用的诊断及最近失败工作集；7天元数据清理前先清工作数据，并保留仍被解析或结果状态引用的记录。视频保留一个曾请求采集的布尔标记，避免历史元数据清理后普通submit重新触发首次采集。此时无缓存会返回refresh_required。

这些仍是本地服务器管理命令；HTTP入口、ADMIN_TOKEN鉴权和按客户端配额已由下文D单元提供。现有D:/Data调试样本不在工作集自动清理范围内。


## HTTP API（D）

API前缀为`/api/v1`，只做数据库查询和入队，来源请求仍由独立worker执行。开发模式可访问`/api/docs`；生产模式不开放交互文档。

启用前，按已有备份/停写流程将所选数据库升级到`0005_api_rate_limits`。配置稳定的`RATE_LIMIT_SECRET`用于客户端HMAC标识和持久限额，管理接口另需`ADMIN_TOKEN`。未配置相应密钥时写入/管理接口返回503，读取和健康接口可独立使用。不要将这两个密钥放入前端构建或URL。

```powershell
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/v1/video-requests' -ContentType 'application/json' -Body '{"url":"https://www.bilibili.com/bangumi/play/ep4292435/"}'
```

| 接口 | 用途 |
| --- | --- |
| POST /video-requests，GET /video-requests/{request_id} | 提交链接、查询解析状态 |
| GET /videos/{video_id} | 默认/部分状态摘要、刷新资格、来源暂停状态 |
| POST /videos/{video_id}/refresh，body={"mode":"auto"} | 手动刷新，可选full；复用已有任务不新建 |
| GET /jobs/{job_id} | 阶段、计数、限制及结果入口 |
| GET /states/{state_id}/threads | 分页楼索引 |
| GET /states/{state_id}/threads/{root_id}/comments | 根评论优先的楼内对话 |
| GET /states/{state_id}/users/{uid}/comments | 当前视频状态中该UID全部发言 |
| GET /states/{state_id}/users/unknown/comments | 未知作者发言 |
| POST /admin/jobs/{job_id}/cancel、retry、recover | 管理意图，需Authorization: Bearer请求头 |
| POST /admin/video-requests/{request_id}/retry，POST /admin/source/revalidate | 解析重试、来源复测意图，同样需Bearer |

新意图返回202，缓存或重复意图返回200。每个客户端写请求30次/分钟，新来源意图3次/小时；缓存命中与重复意图不占新意图额度。限额与入队使用同一接受时间，新增意图超限时整个入队事务回滚。429含Retry-After。

请求体最多8KiB（分块体同样限制），链接最多2048字符，不接受cookie或服务器路径字段。分页使用limit/cursor，默认50、最大200。响应保留评论ID/UID字符串和原始文本；comment.schema_version反映原入库批次，字段语义兼容v1/v2，不等同于HTTP API版本。

只公开current或partial working；未发布或已回收状态统一返回410。游标不可跨状态、楼或用户使用；遇到410需重新获取视频入口，不能拼接两个状态的数据。非法请求422，不存在404，刷新/控制冲突409，数据库不可用503。错误不回显提交正文或底层SQL，任务响应不包含所有者令牌和内部核验证据。

所有受支持启动命令必须保留`--no-proxy-headers`。默认忽略X-Forwarded-For；只有TRUSTED_PROXIES明确配置的实际代理地址/CIDR才可提供地址链，重复头按顺序合并并从右侧识别客户端。密钥保持稳定才能在进程重启后继续限额。Compose已传递API配置；容器中的DATABASE_URL须使用容器可达地址，例如同网络数据库服务名，不能照搬宿主机的127.0.0.1地址。

当前已有 API 代码及隔离数据库验收；本地开发库已升级到 0005，本机 API 与 worker 已进行真实页面联调。生产 HTTP 服务尚未部署；真实采集任务的原子入库、旧评论保留及页面结果一致性已验收；源端不可得内容保留 partial。

## 基础采集页面

输入 Bilibili BV 视频或 ep 剧集链接，点击“获取并保存”，页面提交后台任务并只读轮询解析、采集和保存状态。保存目标是 PostgreSQL；按目录导出 JSON 继续使用上述 CLI。页面展示评论/主楼数量、北京时间小时状态和实际采集时间，并区分已保存结果、部分结果与上下文缺口。

“更新讨论数据”仅在服务端允许时手动触发，沿用共享小时缓存和 auto 刷新规则；页面重载、恢复可见和读取状态不会自动提交采集。真实使用前须完成所选数据库的备份与迁移，配置 DATABASE_URL、RATE_LIMIT_SECRET 和 worker 的访问配置，再同时启动 API、worker 与前端。

前端已通过模拟 API 交互验收，以及真实缓存读取、页面恢复和手动刷新入队验证；真实 worker 已完成采集和入库，页面数量与部分覆盖提示已核对。楼内对话、用户评论浏览及 AI 分析页面留待后续设计。


## 检查点与入库性能优化

新工作集使用 SQLite 检查点格式 2：请求预算与心跳摘要单独保存，评论页仍逐页持久化。格式版本和采集 revision 分开；恢复采用较大的已用请求数和较小的适用上限。摘要损坏不伪造新进度，安全控制损坏仍停止采集。

旧工作集在持有 `.collect.lock` 的写入入口自动事务升级；只读导出不会迁移，未知格式拒绝写入。切换已有服务前，先等待任务空闲并停止写入进程，使用 SQLite backup API 备份工作集，再启动新版。不要直接复制仍在写入的 SQLite 主文件，也不要让旧版进程继续写已升级工作集。本次不新增 PostgreSQL 迁移。

Worker 将采集结果及核验证据冻结为同一只读数据集，通过共享校验后直接原子入库，不再生成、复制和读回临时导出目录。文件导入保留路径/符号链接安全检查，显式导出仍产生既有协议目录；下游调用和 `--output` 用法不变。同一固定批次保持原 canonical digest，任务终态、刷新上下文与状态发布仍在同一事务中提交。

优化针对本地检查点和数据准备开销；Bilibili 请求等待、限速及可见性限制仍会影响总耗时。不会因提速将不可获取的评论标记为完整。
## 分析输入准备（不调用模型）

在独立环境安装当前 backend 后，可将 CommentExport 1.x／2.x 的发布容器固定为分析输入：

```powershell
.\.venv\Scripts\python.exe -m app.analysis_input.cli prepare --export-container "D:\Data\bilibili-video-<aid>" --analysis-root "data/analysis" --context "analysis-standard.md=docs/plans/2026-09-05-discussion-analysis-standard.md" --context "coordination.md=config/prompts/video-agent-coordination.md"
```

`--export-container` 指向包含 `current.json` 的目录。分析阶段已有内容的 README 会原样固定到本轮 `context/README.md`；源目录只读。数据和上下文全部校验、固定成功之前不会交给下游，本命令始终不启动 Agent、不连接数据库、不发出模型请求。

输出为单个 JSON 对象，`status` 分别为 `ready`（输入准备完成）、`waiting_policy`（partial 等待明确策略）、`no_analyzable_users`（无已知 UID），均不等于画像已生成，且 `model_execution_authorized` 始终为 false。`--allow-partial` 只允许把部分输入准备为 ready，不授权模型调用。需要上下文文件时重复传入 `--context NAME=PATH`；名字不能与 README 或其他名字大小写冲突。

保存位置：

```text
data/analysis/bilibili-video-<aid>/
├── sessions/                         # 预留会话登记，准备阶段不创建 Agent
├── inputs/<export_id>/               # 完整固定的导出副本
└── runs/prep-<request-digest>/
    ├── run.json                      # 不可变准备记录
    ├── context/README.md             # 本轮视频背景
    ├── context/<规则文件名>
    ├── intermediate/                 # 预留，尚无模型产物
    └── users/                        # 预留，后续保存 UID.json
```

同输入同上下文和策略重复调用返回同一个准备运行。README 或规则变化生成新运行，旧 context 保留；相同 export_id 的评论内容发生变化会报 input_conflict。已固定目录损坏会明确失败，不静默覆盖。准备去重不等于后续模型结果去重。

退出码：0 表示准备流程返回有效状态（包括 waiting_policy）；2 为输入／协议／配置错误；3 为保存、锁或已存数据错误。调用方必须读取 JSON 的 status，不得仅凭退出码 0 启动分析。文件位于 Git 仓库中时，分析输出根必须被忽略；输出根与源容器不得相同或相互包含。失败时只输出稳定错误码，不打印评论或凭据。

上游导出入口会切换并可能清理旧批次，程序按协议整批重读，最多重新开始两次；已完整读完的旧批次仍按其 export_id 保留。本模块尚未接入数据库采集任务自动触发或网页分析入口。

## 离线大模型任务包（AnalysisInput 2.0.0）

先用上面的输入准备命令将三份规则冻结到 context，其中额外加入：

```powershell
--context "role.md=config/prompts/discussion-analyst.md"
```

已有准备运行缺少 role.md 时重新准备，不直接修改旧 context。资源映射示例位于 config/analysis-packets-resources.json；映射的版本与文件内容应由调用方维护，SHA-256 以实际冻结文件为准。

使用输入准备命令返回的 prepared ID 进行离线组装：

```powershell
.\.venv\Scripts\python.exe -m app.analysis_packets.cli assemble-primary --analysis-root data/analysis --video-id bilibili:video:<aid> --prepared-run-id prep-<digest> --resources-config config/analysis-packets-resources.json --max-input-tokens 160000 --reserved-output-tokens 4000 --context-window 200000 --max-active-members 2
```

这些数字只是离线容量配置示例，不表示任何实际模型的可用窗口。utf8-byte-estimate-v1 以 UTF-8 字节数作容量代理，包含序列化包和加载规则，不是已校准 tokenizer，也未包含未来会话历史及工具声明；模型执行前必须重新计量。

组装产生新的 UUID 运行，不写入 prep-摘要目录。返回 `offline_prepared`、run_id、manifest_id、清单路径和摘要；`model_execution_authorized=false`。缺依赖／超大评论进入 group_coverage.pending_targets，不生成假任务或截断原文；目标与重复辅助上下文分开统计。

```powershell
.\.venv\Scripts\python.exe -m app.analysis_packets.cli validate --analysis-root data/analysis --run-id <run-uuid> --manifest-id <manifest-uuid>
```

验证按显式清单读取并核对固定来源、任务包、覆盖、资源摘要和前序清单链。首次 CLI 生成 user_initial/primary 与 thread_context/primary，规则、角色、背景都来自固定 context；默认省略昵称、点赞，不改变原文或媒体描述，不访问媒体 URL。

库函数 advanced.build_reconcile 和 advanced.build_synthesis 支持后序纯组装，必须由受信调用方显式提供已接受的冻结结果目录。CLI 不提供将任意 JSON 自报成功转换成“已接受结果”的命令；后序发布／重新校验还会核对真实当前／历史任务依据和磁盘结果字节。没有该目录时不把后序结果当作已验证。

本模块不调用 Claude、不连接数据库或采集队列，也不生成最终 users/<UID>.json。输出 schema_ref=null 仅适用于离线任务；生产模型输出 Schema、结果接受流程、实际 token/费用管理和 Agent 执行属于下一阶段。

## 输出结果接受与用户画像保存

`app.analysis_results` 提供本地服务接口，不提供让任意 JSON 自报“已接受”的公共 CLI，也不启动模型：

- `bind_output_schema(assembly)`：为尚未发布的新任务绑定正式输出 Schema；Schema 文件独立保存，不混入四项角色／背景资源。已发布输入不可原地修改。
- `accept_response(root, run_id, manifest_id, task_id, response_bytes, execution=..., rule_catalog=...)`：核对任务响应并写接受、拒绝或未完成回执。
- `load_catalog(root, run_id, manifest_id, rule_catalog)`：从实际磁盘结果、接受回执、执行绑定和完整历史依赖重新构建 AcceptedCatalog。
- `save_profile(root, run_id, manifest_id, uid, rule_catalog, execution_ref)`：只从已重新验证的任务结果生成文件，返回实际相对路径与覆盖状态。

调用方必须是可信执行器。ExecutionContext 包含 attempt_id、input_sha256、authorized、delivery_verified、execution_ref、agent_id；不能从待验收模型返回的同名字段构造授权。实际发起调用、用户授权与完整输入交付由执行器确认，本库验证绑定关系，不替代认证服务或模型调用逻辑。

执行器须冻结本运行的 `execution.json`，记录 run_id、rules_sha256、CLI版本、configured_model、reported_model（未知为null）、provider、session_id、agent_ids、输入／输出协议版本及limits。接受时将这些内容与清单中的成员登记、角色及任务归属绑定。角色标签目录由可信配置提供并与规则摘要绑定，不能让模型提供允许标签列表。

每次尝试的执行绑定位于 `validation/<task_id>/<attempt_id>/execution-binding.json`，程序回执附加 `execution_binding_ref` 摘要引用。结果文件先写入、接受回执后提交；无接受回执的孤儿结果不能消费，重试可恢复相同内容，不覆盖冲突数据。

最终文件路径为 `runs/<run_id>/users/<UID>.json`，包含八方向观察、对象、证据、批次、背景规则与执行来源。必需任务未齐全时仅写 `intermediate/profiles/<UID>/<candidate_id>.json`。源采集partial或context gaps原样保留，不因为分析覆盖complete而宣称源站全量。重复保存相同最终内容保留首次created_at；不同内容报告冲突。

输出接受与保存先通过合成响应验证，现已通过下文普通CLI会话池完成真实模型配合合成评论的文件保存闭环。真实评论画像质量评估和网页展示仍待后续阶段。

### Claude CLI 进程适配层（本地库）

`app.analysis_execution.command.CliRequest` 描述一次调用；`build_command` 构造固定参数数组，正文通过 UTF-8 stdin 传递。`app.analysis_execution.runner.execute(request, records_root=..., environment=..., authorized=True)` 由可信服务调用，默认拒绝执行。调用方必须显式选择可执行文件、稳定工作目录、主会话 UUID、attempt UUID、模型、单次预算、超时和输出上限。Windows 使用用户指定的 `C:\Users\dengww\.local\bin\claude.exe`。

普通传输默认 `--bare`；原生初始化/恢复使用下文的显式管理模式。认证环境由可信调用方提供，程序不保存令牌、不修改全局认证配置、不自动切换模型。兼容服务的 AUTH_TOKEN 已通过普通 bare 请求验证，不能仅凭 auth status 判断 print 请求必然失败。工具允许列表不是文件系统沙箱；正式服务仍需独立账户、目录权限及经过验证的成员配置。

调用记录保存于调用方指定的私有、Git 忽略或仓库外目录：`<records_root>/<session_id>/<attempt_id>/request.json`、`stdout.jsonl`、`stderr.txt`、`outcome.json`。响应可能包含评论和模型内部消息，目录不能作为公共静态文件服务。请求只记录正文摘要，不保存正文或环境；响应及 stderr 原样保留在本地受控目录。调用前建立 request，完成后写 outcome；已有 attempt 一律不重发，包括崩溃后只有 request 的情况。一个会话使用同一 records_root，首次与恢复共用单写锁。

`transport_succeeded` 只说明进程及主事件通过传输检查，`business_acceptance` 始终为 `not_evaluated`。本层不创建可信成员证据，不调用结果接受器；主/子身份、规则切换、任务调度及累计预算需继续接入。费用字段 `estimated_cost_usd` 是 CLI 估算；超时、无费用或多结果费用含义无法确认时为 null/unknown，不能当作零或服务商实际账单。该适配层已用于下文普通会话池的真实模型合成验证。

Windows 终止采用 taskkill /T 加主进程兜底，尚不提供完整进程树隔离保证。清理未确认返回 `process_cleanup_unverified`；缺少 outcome 或清理未确认的调用会阻止同一会话的新 attempt（`session_recovery_required`）。需人工核对残留进程后恢复，本层不自动解除隔离。`trigger_reason` 保留 timeout/output_limit 等原始原因。正式部署前仍需操作系统级进程生命周期验收；本轮没有在 Linux 上实测。

### 已登记成员的任务投递闭环

`app.analysis_execution.dispatch.dispatch_task` 已连接固定任务包、原生 SendMessage 恢复证据、`accept_response` 和用户汇总后的 `save_profile`。这是可信后端内部的单任务接口；参数为分析根、run/manifest/task ID，以及 `CliRequest`、登记 agent_id、标签目录、执行配置、环境和显式授权。主会话必须复用登记 session，cwd 必须是对应视频分析目录。原生首次创建与受限修复见下文实验性接口，自动批量调度尚未接入；离线清单中的 uncreated 成员不能运行本接口。

每次把评论 packet、冻结规则/背景、允许标签及完整输出 Schema 引用闭包投递给原成员。主与子输入均按实际 UTF-8 字节重新检查预算，不截断正文。采集准备必须为 ready；partial 来源必须在固定准备请求中显式 allow_partial=True，单独设置执行授权不能绕过覆盖策略。运行中标签目录、规则或执行配置变化会拒绝复用该运行。

程序核对本次 SendMessage 收件人/完整正文、真实 resumedAgentId、task_started 与 task_notification 的关联，只从匹配的原生完成通知解析严格 JSON。多次主结果会继续读取；主 Agent 自述、旧摘要、错成员、截断、Markdown 包裹均不能当成业务响应。规则摘要确认仅证明接口上的本轮内容一致，不证明模型内部理解或画像质量。

投递及证明保存在 `runs/<run_id>/deliveries/<task_id>/<attempt_id>/`：delivery.json、prompt.txt、stdout.jsonl、cli-request.json、cli-outcome.json、delivery-proof.json。验收回执同时绑定原生结果摘要和证明文件；重建接受目录及画像时会复核证据。CLI 已完成而验收中断时，同 attempt 可从匹配的私有记录继续验收，不重发模型；已经接受的任务复用前也会重新校验。不同 attempt 不被当作同一次调用。

通过验收的 user_synthesis 自动触发画像保存，最终仍由完整依赖/覆盖检查决定输出 `users/<UID>.json` 或候选文件。`execution.json` 中 `reported_model_scope=main_session_only`、`member_models_verified=false` 明确表示当前只核验主会话报告的模型；子成员实际模型未独立归属验证，该限制同时写入画像。`label_catalog_sha256` 固定本轮标签目录。provider 是调用方配置的服务标识，不是从模型文本推断。

本闭环通过合成原生事件和真实本地文件验证，尚未调用真实模型。普通 `runner.execute` 仍只提供传输结果；原生路径使用 dispatch_task，普通会话池使用 dispatch_pool_task；不应从上传的 JSON 自行构造 ExecutionContext。

### 原生成员初始化与恢复（实验性接口）

`initialize_team` 已提供按视频持久保存意图、逐个创建自定义只读成员、保存真实主/子ID与原生证明；`load_team` 重验完整团队，`bind_team` 发布新登记清单并保留旧清单。成员数量/角色由调用方显式指定。原生管理流程使用 `bare=False` 的明确配置模式：禁用隐式 settings 来源、hooks 和 MCP；自定义定义与创建权限分开，普通恢复可以加载原定义而不开放 Agent 创建工具。CLI 2.1.261 的本次实测 bare 模式未提供所需 Agent/SendMessage 工具，不能用于该原生管理流程。

调用记录位于 `<video>/sessions/cli-calls/`；冻结初始化意图、成员证明和团队文件位于 `sessions/team/`；CLI历史位于 `sessions/claude-state/`。`recover_empty=True` 仅允许完整证据确认首次调用没有任何工具/创建活动时，归档旧计划并恢复同一主会话。一般中断或身份不明不得通过换 attempt 重建。

`repair_member_handshake` 仅对原生证据确认已创建、旧任务已结束的原ID发送握手修复，禁止 Agent 创建。原始创建数据保持不变；新的恢复与原始出生记录共同证明握手。重复成功调用或完整响应后本地发布中断可复用原记录，不重发模型。错误/缺失记录、未知费用或CLI明确拒绝恢复都会阻塞。

`initialization_status(video)` 是只读诊断接口，区分 `verified`、`created_unverified`、`resume_refused`、`not_started` 等状态。观察到ID不代表通过握手，更不能授权分析。只有完整团队通过重验才能进入登记绑定；诊断文件和主Agent复述的预期JSON均不能冒充子成员结果。

`spend_limit` 是当前初始化计划的显式费用准入和记账额度，不是供应商硬账单上限。单次 `--max-budget-usd` 已观察到请求完成后的超额；出现超额或预算停止后，后续调用还需至少两倍已观察最大单次费用的余量，并覆盖剩余预留。费用未知会停止。独立旧计划和前置检查的费用仍需调用方计入总预算，不能因恢复换计划清零。预算停止时，只有完整原生握手证据才能保留已验证身份；分析结果接受规则不因此放宽。

**真实整组验收尚未通过。** 当前CLI可拒绝自动恢复已取消的原生子成员，单靠保存ID无法保证其恢复。不得伪造完成状态、编辑厂商停止标记或静默替换成员。用户已确认 [独立普通会话池策略](docs/plans/2026-09-08-agent-resume-strategy-proposal.md)，实现见下文。原生显式替换及长期历史备份尚未实现；旧原生记录继续保留。

### 独立普通 CLI 会话池

当前默认开发方向改为应用层会话池。`prepare_session_pool` 从 ready 的冻结输入登记一个协调主会话和调用方指定的工作成员，每个成员保存普通 session UUID；离线登记不表示 CLI 已启动。`bind_session_pool` 发布新的任务成员映射。`run_pool_turn` 首次使用 `--session-id`，后续对原 UUID 使用 `--resume`；只开放 Read，不使用原生 Agent/SendMessage。

服务端接入接口位于 `app.analysis_execution.pool_binding`、`session_pool` 和 `pool_dispatch`：

```python
pool = prepare_session_pool(
    root, run_id, manifest_id, rule_catalog=rule_catalog,
    model=configured_model, provider=provider_name,
    members=[
        {"member_id": "initial-reader", "role": "user_initial"},
        {"member_id": "context-reader", "role": "thread_context"},
        {"member_id": "synthesis-reader", "role": "user_synthesis"},
    ],
)
bound = bind_session_pool(root, run_id, manifest_id, rule_catalog)
# 从新 manifest 的 registry 选择当前任务对应的 worker UUID，放入 CliRequest.session_id。
# dispatch_pool_task(..., request=request, agent_id=worker_uuid,
#                    rule_catalog=rule_catalog, execution_config=execution_config,
#                    environment=explicit_environment, spend_limit=Decimal("5"), authorized=True)
```

每个任务直接输入完整冻结 AgentDelivery（当前评论、上下文、先前观察、角色/规则/背景、输出 schema），结果通过普通会话独立证明与原 AnalysisOutput 校验后接受。最终综合任务仍保存到 `runs/<analysis_run_id>/users/<uid>.json`。协调主会话通过下文 `run_coordinator_turn` 统一加载背景；模型自动分配/汇总批量任务尚未接入，本阶段提供持久会话与单任务闭环接口。

持久数据存于视频目录下的 `sessions/pool/pool.json`、`sessions/pool-state/`、`sessions/pool-calls/`，旧 `sessions/team/` 保留。新 manifest 的 `agent_id` 对会话池适配器表示普通 worker UUID，执行记录使用独立 adapter 标识。已存在原生 `execution.json` 的旧分析 run 必须新建分析 run 后绑定池，不能覆盖旧执行来源；准备输入可复用。后续新批次改变角色文件时，在本批 registry 和任务包内冻结新摘要，继续使用原 UUID，pool 内最初的角色摘要仅作初始化来源记录。

每次新调用检查整个池的既有费用和显式 `spend_limit`；未知费用或未完成记录阻止继续。相同 attempt 只读取旧证据，不再次调用。CLI 预算是估算准入，不能保证兼容服务账单硬上限。会话文件需随应用数据备份；保存 UUID 不保证历史被删除后仍能恢复。当前采用 CLI 2.1.261 适配器。

已使用用户本地 CLI 2.1.261 完成普通主会话创建/同 UUID 跨进程恢复，以及六个合成评论分析任务到最终 UID JSON 的真实闭环；重复执行最终任务复用已接受结果，不新增模型调用。该验证说明接口和文件链路可用，不代表真实评论上的画像质量已评估。

### 主协调会话的视频背景输入

业务协调入口使用 `app.analysis_execution.coordinator.run_coordinator_turn`：参数为 root、run_id、manifest_id，以及 request、rule_catalog、environment、expected_model、spend_limit、authorized。`request.session_id` 必须是已登记的主 UUID；`request.prompt` 只填写本次协调要求，入口自动从当前分析批次固定的上下文加载 README、角色、规则和协调说明，调用方不必手工拼接背景。

`prepare_coordinator_delivery` 提供同一输入的只读预览。README 正文既在 `resources.background.text` 中，也包含在资源正文映射内，与工作任务使用同一快照和摘要；空白 README 显式标为 `background_status=missing`。更新原目录 README 不会改动旧分析，需重新准备输入并创建新分析 run，再绑定原会话池；主 UUID 保持不变。超过输入预算时拒绝调用，不截断视频介绍。

主模型须返回严格 JSON `{context_sha256, response}`，摘要与完整本轮输入匹配后，才在 `runs/<run_id>/coordinator/<attempt_id>/` 保存 input.json、response.json、receipt.json。协调回执不等于分析任务已接受，也不能直接生成画像；自动分派仍待后续阶段。README 作为视频背景，不构成用户行为证据或更改分析规则的指令。
