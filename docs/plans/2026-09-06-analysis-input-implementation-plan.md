# 分析输入准备模块 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking. 在当前会话顺序执行，沿用已批准范围，不要求再次选择执行方式。

**Baseline:** 当前工作树已同步 main 3ebb86a；本计划替代旧的上游文件提取步骤。

**Goal:** 将上游 JSON／JSONL 导出和分析 README 转为已校验、持久化且可复用的分析输入及运行目录，不调用模型。

**Architecture:** 输入读取器严格消费 current 指针与单一 export_id；验证后由存储模块原子固定 inputs 和每轮 context。Python 入口返回可机器读取的状态与路径，网页和 Agent 执行器后续复用。

**Tech Stack:** Python 3.12、pathlib、hashlib、jsonschema Draft 2020-12、pytest、现有 Ruff 配置。不引入队列、数据库或前端测试框架。

**Spec:** [输入与画像存储设计](2026-09-06-analysis-storage-design.md)、[交接契约](2026-09-05-collection-analysis-handoff-design.md)。

## Global Constraints

- 使用当前 feature/codex-agents-thinks worktree，不切换其他人的工作分支。
- CommentExport 2.0.0 为新导出基线，兼容 1.0.0；按明确版本选择现有 Schema，未知 major 拒绝，不原地迁移；分析阶段 README 允许已有内容。
- 数据只保存在 Git 忽略目录，合成夹具可以入库，真实评论和调试输出不得提交。
- 当前不启动 Claude 或调用模型；不实现八方向推理、网页、部署或会话修复。
- partial 默认返回 waiting_policy；显式允许部分输入只是准备状态策略，不授权付费分析。
- inputs 的评论版本与 runs/context 的 README 版本分别固定，同 export_id 更换背景不能覆盖旧运行。
- 首期不宣称同时支持任意输入协议或数据库，避免为尚未出现的扩展增加抽象层。

## Task 1: 复用当前主线校验与冻结接口，补齐分析 README 模式

**Files:**
- 修改 backend/app/comment_export/validation.py、backend/app/storage/frozen.py 的兼容扩展；不复制或替换旧提交模块。
- 测试 backend/tests/test_export_contract.py、backend/tests/test_export_v2.py、backend/tests/storage/test_frozen.py；复用已有 backend/tests/conftest.py 的 frozen_case。
- backend/pyproject.toml 已包含 jsonschema 与两版 Schema package-data；scripts/lock_dependencies.py 与锁文件已有依赖，仅核对，不为此重建或升级全部依赖。

**Interfaces:** 保留现有 parse_json、validate_record、safe_path、read_json/read_lines。扩展 `validate_batch(batch_dir: Path, *, analysis_readme: bool = False) -> dict` 与 `freeze_batch(input_path: Path, work_root: Path, *, analysis_readme: bool = False)`；后者仍作为上下文管理器交付现有 FrozenBatch(directory, manifest, digest)。默认 False 保持原始导出／入库行为，分析入口显式 True。

- [x] 在当前 worktree 建立独立 Python 3.12 环境，从当前 requirements-dev.lock 安装并 editable 安装当前 backend；检查 app.__file__ 不指向其他工作目录。禁止直接依赖主目录可变代码。
- [x] 阅读当前验证器、freeze_batch、canonical_digest、read_current 及两版 Schema，列明需要扩展的分析模式，保留数据库调用方的默认参数行为。
- [x] 增加以下测试先确认新增行为失败，再实现最小兼容扩展：

```python
@pytest.mark.parametrize("version", ["1.0.0", "2.0.0"])
def test_analysis_readme_is_preserved(frozen_case, tmp_path, version):
    records, metadata = frozen_case
    metadata = dict(metadata, schema_version=version)
    records = [dict(row, schema_version=version) for row in records]
    batch = build_batch(records, metadata, tmp_path / "batch")
    readme = batch / "README.md"
    readme.write_text("虚构视频背景", encoding="utf-8")
    with pytest.raises(ContractError, match="nonempty_readme"):
        validate_batch(batch)
    assert validate_batch(batch, analysis_readme=True)["schema_version"] == version
    assert readme.read_text(encoding="utf-8") == "虚构视频背景"
```

- [x] freeze_batch 的 analysis_readme 显式透传给 validate_batch；默认不接受非空 README，分析模式允许 UTF-8 背景且保持原字节。不得清空真实文件或修改数据库入库默认约束。
- [x] 数据库 canonical_digest 默认算法不变；经回归发现默认 float 解析会合并合法高精度数值，故分析侧新增 digest.py 精确树摘要，并通过 freeze_batch 的 digest_function 可选回调注入。测试 JSON 键重排摘要不变、数组顺序有意义、README 内容变更不影响评论摘要、两版不误当同一输入。
- [x] 审阅元数据 UTF-8、BOM／LF、符号链接与新增字段的校验覆盖。补充分析读取前置检查，避免无意放宽原协议；不修改定版 Schema 字段或枚举。
- [x] 运行原有导出、v2 和冻结测试；构建 wheel 检查两版 schemas 均可从包资源读取。缺少 TEST_DATABASE_URL 时如实报告数据库测试跳过，不使用开发／生产库替代。
- [x] 通过后只提交本任务需要的文件。最终功能采用 squash 集成为一个详细提交；不在此阶段合并主线。

## Task 2: 读取和冻结单一导出批次

**Files:** 创建 backend/app/analysis_input/__init__.py、errors.py、reader.py；测试 backend/tests/analysis_input/test_analysis_input_reader.py。

**Interfaces:**

`InputPreparationError(ValueError)` 保存稳定错误码。`stage_current(container: Path, staging: Path) -> dict` 使用共享 freeze_batch 的分析模式，成功时返回 manifest 并在上下文退出前将数据固定到 staging；不得返回随后删除的 FrozenBatch.directory。输入是发布容器，不接受任意数据库 state_id。

- [x] 合成发布容器 fixture 使用上游 build_batch；读取入口、严格 JSON 编码及路径均走可复用检查。
- [x] 先写会失败的测试：首次无入口 not_published；入口不变但数据损坏 invalid_export；读取／校验失败且 export_id 变化后整批重新开始；重读入口消失 export_unavailable；重试上限后仍失败返回 batch_changed；完整读完旧批次后入口切换仍返回旧 export_id。
- [x] 通过共享冻结流程暂存并校验，薄适配层负责分析错误码和持久副本，不复制整套数据库导入逻辑。固定 current 身份，完成全部所需输入后才返回；只有读取失败／身份不符且 export_id 已变化时整批重启。完整读取旧批次成功后即使入口变更也接受旧结果，保留旧 ID，不重新追赶最新。总共最多初次加两次重启，不扫描 batches。
- [x] 共享 freeze_batch 的异常映射与 current 契约有差异：首次缺入口需要 not_published，重读缺入口为 export_unavailable，重读入口损坏为 invalid_export。为共享函数补齐精确区分并回归原数据库读取场景；不得将所有重读异常合并为 export_unavailable。
- [x] 每次准备使用独立临时目录；重启时只清理自己的临时产物，路径解析后检查不逃逸目标根。源目录只读。
- [x] 增加 README 非空、符号链接逃逸、BOM／CRLF、重复 JSON 键、超大数字 ID、_unknown、异常记录测试。所需文本和元数据校验必须在发布之前完成。
- [x] 运行 reader 测试与上游默认校验回归，检查临时失败不会留下完成标记。

## Task 3: 发布 inputs 和每轮 context

**Files:** 创建 backend/app/analysis_input/storage.py、locking.py、digest.py；测试 backend/tests/analysis_input/test_analysis_input_storage.py。

**Interfaces:**

`prepare_input(container: Path, analysis_root: Path, *, context_files: dict[str, Path] | None = None, allow_partial: bool = False) -> dict` 返回 status、video_id、export_id、analysis_run_id、input_path、run_path。

- [x] 先写合成测试：同输入同背景重复准备返回同运行；README 更新得到新运行且旧 context 不变；相同 export_id 正文改变返回 input_conflict；partial 不开启 ready。
- [x] 使用分析侧精确摘要作为评论指纹，登记 analysis-exact-json-v1，README 纳入 context 指纹；不重写数据库摘要规则，补充小数精度、有限大数、跨视图数值差异与 Unicode 正文分行测试。测试 JSON 对象键重排不触发 input_conflict、数组重排不视为相同；所有指纹计算前必须完成协议校验。
- [x] 创建目录前拒绝源容器与目标根相同／双向包含，验证目标内部路径不经链接越界；测试源批次、源祖先、相同目录及链接逃逸。复用 comment_export.publication.exclusive_lock 的 OS 锁实现，仅在 analysis_input/locking.py 包装 busy 的有界重试，最多等 5 秒、每 50 毫秒重试；不重建一套 flock/msvcrt 机制，不调用 PostgreSQL advisory lock；文件句柄由上下文管理释放，不通过删除锁文件抢锁。测试两个写入者不能同时发布。
- [x] 输入副本在同盘临时位置完整写入后，原子发布到 inputs/<export_id>。已有输入重新执行完整结构校验和语义指纹检查；损坏报 stored_input_invalid、语义不同报 input_conflict，不覆盖冲突文件。inputs 内不添加下游标记文件。
- [x] 本轮 README 与显式 context_files 内容固定到新 run 准备区；映射名按存储设计的跨平台文件名规则校验，拒绝 README（大小写不敏感）、重名和路径逃逸。最终发布 context、空 intermediate/users 及 run.json。
- [x] 按存储设计对 preparation_version、video_id/export_id、input/context 指纹、whole_video 范围和 allow_partial 的规范请求计算 SHA-256，固定 analysis_run_id=prep-<digest>，不建独立运行索引。context、run.json 和空目录准备完成后 rename 发布，唯一提交点为最终运行目录。已存在运行须验证元记录、context 和输入依赖后返回，损坏报 stored_run_invalid。测试 rename 前崩溃、rename 后调用方未收到返回、已有 context 被修改及 inputs 成功而 run 失败；重试不得产生第二个运行或重置既有执行成果。
- [x] 阶段状态：partial 且未 allow_partial 为 waiting_policy；已知用户为零为 no_analyzable_users；其余满足准备要求为 ready，保留 coverage 原值与限制；此状态不代表模型配置完整或允许调用模型。verified+gaps 不变更为 no_known_gaps。
- [x] 运行存储、并发和崩溃模拟测试，再运行 reader 回归。

## Task 4: 提供脚本入口和集成验收

**Files:** 创建 backend/app/analysis_input/cli.py；测试 backend/tests/analysis_input/test_analysis_input_cli.py；更新 README.md 的输入准备用法。

**Interfaces:** `python -m app.analysis_input.cli prepare --export-container PATH --analysis-root PATH [--allow-partial] [--context NAME=PATH]`。

- [x] CLI 使用 argparse，调用 prepare_input 并输出一个 JSON 对象；成功准备／等待策略／无对象返回 0，协议错误返回 2，存储或锁错误返回 3。调用者以 status 决定后续行为，不能将退出码 0 等同于分析已完成。
- [x] context 参数重复名称拒绝，缺少文件返回上下文输入错误；输出不包含真实评论正文、昵称或凭据。
- [x] 写集成测试：有效合成导出产生确认目录；partial 输出 waiting_policy；损坏导出不产生 ready 运行；新增输入测试全过程无模型和网络调用，不触发采集队列。测试 --context readme.md、A.md/a.md、尾部点／空格和 Windows 保留设备名被明确拒绝，不发生覆盖。
- [x] 在当前真实样例上以默认 partial 策略执行一次输入准备，本地检查得到 waiting_policy 且保留 README；核对源数据未改、真实副本被 Git 忽略。不将真实数据路径固化为默认值或自动测试依赖。
- [x] 先执行导出／v2／storage/test_frozen.py 与新增输入测试，再执行 `python -m pytest backend/tests`、`python -m ruff check backend scripts`、`git diff --check`。使用本 worktree Python 3.12；数据库集成测试仅在显式隔离 TEST_DATABASE_URL 上运行，报告跳过项。未改前端不重复执行其构建。新增依赖才同步锁文件，现有 SQLAlchemy／psycopg 等不因文件准备而移除。
- [x] 更新设计中的实现状态，报告实际通过项及残留限制。验收完成前不合并 main；集成时更新主线、git merge --squash 本功能分支并创建一个详细功能提交，再正常 push 并核对远端。

## 验收界限

本计划交付一个可运行的输入准备模块，不交付 Agent 分析执行器或最终用户画像。最终 users/<UID>.json 的写入契约属于下一阶段；本期不能生成假内容证明目录“可用”。

该计划没有采用真实模型调用，故不涉及前次合成试验的剩余费用，也不需要为此追加模型预算。用户画像格式、成员切换、成本控制和网页展示各自在其阶段单独验收。

## 后续自动衔接的范围隔离

本计划不接 PostgreSQL、不实现 state_id 导出桥接、不创建分析队列或 HTTP API。文件输入模式可先完成并独立验收；网页采集完成自动分析必须在下一阶段明确以 result_state_id 选择状态、处理 state_expired、绑定新 export_id 及部分覆盖策略后再接入。现有 worker 的临时导出不能作为持久输入路径。

当前真实 v1 partial 样例保留为本地调试；v2、回复关系、未知作者和刷新竞争使用合成数据覆盖，不修改旧数据迁移到 v2。正常准备状态 ready 仍不构成模型调用授权。

## 实现说明与验收界限

当前阶段已实现并完成合成测试、原有后端回归、代码规范检查、真实 partial 输入准备及独立只读审阅。数据库集成测试只在显式 TEST_DATABASE_URL 时执行；当前环境未配置，跳过不算已验证。操作系统不允许创建真实 symbolic link 的用例跳过，另有复制前链接／junction 检查回归。未修改前端，不运行付费模型。打包与代码检查结果在交付对话中说明，运行证据只保存在忽略目录。最终功能尚未合并 main。

## 与数据集存储接口的兼容约定

分析输入冻结复用 main 的 ValidatedDataset 和 FrozenBatch 委托读取接口。默认导出与数据库入口继续要求空 README，并沿用既有数据库 canonical digest；不得以分析的字节级摘要替换数据库幂等标识。

显式 analysis_readme 模式接受并保留 UTF-8 背景，解析时保留有限 Decimal 数值；分析调用方继续传入 exact_digest，核验两份评论投影及高精度数值。分析快照可携带该自定义摘要，默认数据集的摘要计算和读取副本语义不变。编码、链接/junction 和发布指针重读规则同样保留。
