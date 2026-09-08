# AnalysisInput 2.0 离线任务包模块 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement task-by-task with failing tests before implementation. 在当前 feature/codex-agents-thinks worktree 顺序推进，不重复询问已批准范围。

**Goal:** 为已固定的评论输入提供 AnalysisInput 2.0.0 Schema、离线任务包组装和语义校验，不调用模型。

**Architecture:** 从 prepare_input 产物校验加载原文与上下文，生成新的完整分析运行目录和分阶段不可变清单。结构校验与跨文件语义校验分离；后序任务仅消费显式的已接受结果目录，由测试提供合成结果，生产验收权限保留给后续执行器。

**Tech Stack:** Python 3.12、现有 jsonschema/referencing、pathlib、dataclasses、pytest；复用 analysis_input 的精确摘要、文件边界及锁，不新增网络或数据库依赖。

**Spec:** [AnalysisInput 2.0.0](../references/analysis-input-protocol-v2.md)、[分析标准](2026-09-05-discussion-analysis-standard.md)、[存储设计](2026-09-06-analysis-storage-design.md)。

## Global Constraints

- 固定协议名 BiliBiliTalksView.AnalysisInput，当前支持模型任务包 major=2；不要与源评论 CommentExport 1.x／2.x 混淆。
- 不改已确认协议字段、八方向和目录职责；发现结构冲突先反馈并版本化修订，不通过弱化 Schema 绕过。
- 本阶段完全离线：不调用 Claude、不读取认证、不连接数据库或采集队列、不下载媒体，不生成实际用户画像。
- 源 inputs 和 prep 运行不可变；组装输出使用同视频 runs/<新的 UUID>/，不得写入 prep-摘要目录。
- 输入准备 ready 不表示规则齐全或模型已获准；离线构造可检查 waiting_policy 数据但输出必须标明 offline_only，不能自动转为执行授权。
- schema_ref=null 仅离线合法；执行模式的预检必须拒绝，但本模块不包含发起模型调用的路径。
- 阶段主清单、任务包、索引和成员登记是不可变文件；可变接受状态不由模型声明，本阶段不实现生产“接受结果”的 HTTP/CLI 接口。
- 真实评论、任务包和检查证据仅保存于忽略目录；仓库 fixture 必须合成，测试不得依赖 D:\Data。

## 任务分解与模块边界

| 文件 | 责任 |
| --- | --- |
| backend/app/analysis_packets/contract.py、schemas/*.json | 本地 Schema 资源、严格版本分派和结构校验 |
| backend/app/analysis_packets/source.py | 验证准备记录、规则文件和原评论索引，产生只读 SourceBundle |
| backend/app/analysis_packets/budget.py | 离线容量估算接口和限制对象，不声称已校准的模型 tokenizer |
| backend/app/analysis_packets/context.py | 目标原文、祖先链、缺口和辅助上下文选择 |
| backend/app/analysis_packets/partition.py | 分块、group_coverage 分区与待处理目标 |
| backend/app/analysis_packets/validation.py | 身份、投影、引用摘要、覆盖、DAG、阶段和层级语义校验 |
| backend/app/analysis_packets/builder.py | primary、reconcile、synthesis 的纯组装函数 |
| backend/app/analysis_packets/publication.py | 新运行与阶段清单原子发布、历史引用保留 |
| backend/app/analysis_packets/cli.py | 离线 assemble-primary 和 validate 入口，输出稳定错误码 |
| config/prompts/discussion-analyst.md | 供各子成员使用的通用分析角色，不新增维度 |
| backend/tests/analysis_packets/ | Schema、上下文、分块、依赖和文件发布的合成回归测试 |

## Task 1：固定结构 Schema 和合成夹具

**Files:** 创建 analysis_packets/__init__.py、contract.py、errors.py、schemas/common.json、packet.json、manifest.json、task-index-row.json、target-index-row.json、group-coverage.json、member-registry.json、observation.json；修改 backend/pyproject.toml 的 package-data。测试 test_packet_contract.py、conftest.py。

**Interfaces:** `validate_document(kind: str, value: dict, *, execution: bool = False) -> None`；`PacketError(ValueError)` 仅保存稳定错误码。Schema 注册表只从本地包资源解析 $ref，禁止网络解析。

- [x] 从协议逐字段录入 required、可空类型、枚举、非空字符串、UUID、SHA256、正整数／非负整数及 if/then 关系；不以 Python True=1 的相等性替代 JSON 类型校验。
- [x] 添加合成夹具 make_source_case：视频 bilibili:video:10001、UID 10/20、楼100/200，含 UID10 在两楼发言、UID20直接回复和一条未知作者；source CommentExport 版本参数化为 1.0.0/2.0.0。用现有 build_batch/prepare_input 生成有效准备记录，context 含背景、规则、角色、协调四项。
- [x] 添加合法最小 packet_v2、空 member-registry 和两级综合样例，原文均为短合成句；引用摘要按实际 fixture 文件字节计算，不能使用占位 hash 冒充可校验资源。
- [x] 先执行失败测试，再实现 validate_document：

```python
@pytest.mark.parametrize("level", [-1, True, "0"])
def test_rejects_invalid_synthesis_level(packet_v2, level):
    packet_v2.update(task_type="user_synthesis", synthesis_level=level)
    with pytest.raises(PacketError):
        validate_document("packet", packet_v2)
```

- [x] 测试非综合任务 level 必须 null、综合 level 非负、未知 task_type/phase 拒绝、成员空登记结构、main/成员 ID 归属相关结构条件及 required_identity_fields。
- [x] 离线 schema_ref=null 可接受；execution=True 必须报 output_contract_missing。测试未知 major 拒绝、同 major 扩展字段保留、重复 JSON 键／NaN 拒绝。
- [x] 运行本任务测试和 Ruff，构建 wheel 检查所有新增 schemas 可通过 importlib.resources 读取，未引入外部网络 $ref。

## Task 2：验证来源并冻结资源

**Files:** source.py、budget.py、context.py、config/prompts/discussion-analyst.md；测试 test_packet_source.py、test_packet_context.py。

**Interfaces:**

- `load_source(analysis_root: Path, prepared_run_id: str, video_id: str) -> SourceBundle`。SourceBundle 为 frozen dataclass，包含 video_id、export_id、export_schema_version、prepared_run_id、input_fingerprint、manifest、comments_by_id、users、threads、context_bytes；集合使用不可变容器或防御复制。
- `Budget(max_input_tokens: int, reserved_output_tokens: int, context_window: int, max_active_members: int)`，四者为正整数，输入限额加输出预留不超过窗口。
- `estimate_input(serialized_packet: bytes, loaded_rules: bytes) -> int` 首版离线方法标识 utf8-byte-estimate-v1，以总 UTF-8 字节数作为容量代理，并明确未计入真实会话历史／工具声明，不能声称是精确 token 数。未来执行器须重新计量并保留额外开销，不依据此离线估算直接执行。
- `select_context(bundle, target_ids: tuple[str, ...]) -> tuple[list[dict], list[dict]]` 返回协议 comments 投影及 context_gaps。

- [x] 验证 prep 记录身份、input 精确摘要、context 摘要、实际路径和两视图一致性；资源必须从已固定 context 读取，不重新读取原 current。
- [x] 组装配置显式映射 analysis_rules、role_prompt、coordination 的文件名和版本；拒绝缺文件／重复映射。background 固定 context/README.md，原文原字节保留。
- [x] 现有准备记录若缺少通用角色文件，用 prepare_input 的 context_files 或 CLI --context 加入新角色，重新生成准备运行；禁止直接向旧 prep/context 增补文件。
- [x] 初始化 discussion-analyst.md：每轮按当前包重读规则，判断八方向适用性，区分 target/context，允许旧结论被修正，证据追溯，禁止改原文或自行调用其他工具；不得把任务范围写成永远不能调整的角色权限。
- [x] comments 投影仅取协议列出的字段，保留原 content 与时间，省略昵称和点赞；按 UID/root_id 显式字段映射。
- [x] 每个目标先加入可得根评论和父链，递归追溯使用 visited 集合；缺根／父、unknown parent、跨楼父引用、循环各自登记，不能自动改接。相关后续上下文首版确定为目标的直接回复，按协议顺序排序；其他远端分支不自动塞入。
- [x] 先用以下测试验证关系还原再实现：

```python
def test_same_uid_across_floors_preserves_targets(source_bundle):
    comments, gaps = select_context(source_bundle, ("100", "200"))
    assert {row["comment_id"] for row in comments if row["input_role"] == "target"} == {"100", "200"}
    assert all("nickname" not in row and "like_count" not in row for row in comments)
```

- [x] 增加混合未知作者、正文引用指令、U+2028、空正文、媒体描述测试，确保不将文本命令提升成规则、不拉取 URL。

## Task 3：初步任务分块与覆盖台账

**Files:** partition.py、builder.py；测试 test_packet_partition.py、test_packet_primary.py。

**Interfaces:** `build_primary(bundle: SourceBundle, run_id: str, resources: dict, budget: Budget) -> Assembly`。Assembly 包含 packets、task_rows、target_indexes、group_coverage、resources、member_registry，不执行文件写入。

- [x] 对每个已知 UID 生成 user_initial/primary 组，对每个楼生成 thread_context/primary 组；未知作者只参与楼原文上下文，不生成用户组。两类 prior_observations 均为空。
- [x] 对排序后的目标逐条加入候选块，附必要上下文，按整个资源与包重新估算；超限则结束当前块并尝试新块。单目标及必要输入仍超限时登记待处理；只在上下文可省略且明确 context_budget 时移除可选直接回复，不裁剪目标原文。
- [x] group target_index 是完整总体集合。覆盖恒等式 U=P∪各T_i，两两不交；pending 不计 chunk.count，无可发布块时不生成空包。
- [x] 在试分块完成后分配 UUID 和最终 count/index，再重新估算序列化最终包；最终身份／台账引用造成超限时继续细分或登记待处理，不能使用未含元数据的估算直接发布。
- [x] 背景和规则本身超限返回 input_budget_exceeded，不截断。执行 limits 来自命令行显式值，不默认使用某个模型的窗口。
- [x] 先通过小容量强制分块测试：

```python
def test_pending_target_not_counted_as_packet(partition_case):
    group, packets = partition_case("100-fits-200-oversize")
    assert len(packets) == 1
    assert packets[0]["chunk"]["count"] == 1
    assert group["pending_targets"][0]["comment_id"] == "200"
```

- [x] 覆盖跨楼 UID 两包的 root_ids 子集／并集、重复祖先不重复计数、全部目标待处理、分块确定性和超长正文不截断场景。

## Task 4：语义校验与后序纯组装

**Files:** validation.py、builder.py；测试 test_packet_semantics.py、test_packet_reconcile.py、test_packet_synthesis.py。

**Interfaces:**

- `validate_assembly(assembly: Assembly, bundle: SourceBundle, accepted: AcceptedCatalog, *, execution: bool = False) -> None`。
- AcceptedCatalog 为由调用方提供的只读映射；键是 task_id，值包括来源已发布 packet、冻结结果 path/hash、校验后的 observations 和接受状态。此阶段只由合成 fixture 构造，不能从任意结果文件的 self-declared success 直接构造可信目录。
- `build_reconcile(bundle, primary_group, accepted, resources, budget) -> Assembly`。
- `build_synthesis(bundle, target_uid: str, synthesis_level: int, accepted, resources, budget) -> Assembly`。

- [x] 验证 packet 与原文投影、UID、来源 export_id、规则 hash、subject 对象证据及 quoted text；高精度未知字段或 JSON 数值不能因浮点转换错误归并。
- [x] 验证 task_rows 与 input 对齐、所有模型结果读取具有直接依赖、origin_result hash/任务身份/类型/接受状态一致、DAG 无环。路径和摘要来自实际文件内容，不相信模型陈述。
- [x] reconcile 强制包含覆盖其目标的 primary 直接依赖，即使零 observations；缺失任务目标进入 dependency_missing，不能发布伪造空前序结果。
- [x] 层0综合只接收目标 UID 的初步／楼内观察；层L>0只接同 UID 的L-1层综合。各层target_index都是完整UID集合，目标计数只在本层唯一；final_merge_task_id只有完整单包且P为空时可设置。
- [x] 先写来源依赖失败测试：

```python
def test_observation_requires_direct_dependency(assembly_case):
    assembly, bundle, accepted = assembly_case("reconcile-with-observation")
    assembly.task_rows[-1]["depends_on"] = []
    with pytest.raises(PacketError, match="undeclared_result_dependency"):
        validate_assembly(assembly, bundle, accepted)
```

- [x] 合成验证层0 S1(A)/S2(B)、层1 S3(A,B)，不重复样本；同时验证同层重复覆盖、跨UID观察、旧规则、漏primary、假quoted text、依赖环均被拒绝。
- [x] 后序组装保持纯函数；没有真实 AcceptedCatalog 时不发布生产 reconcile/synthesis 任务。最终画像输出 Schema 与生产接受过程仍在后续执行阶段确定，不伪装已完成。

## Task 5：发布与离线 CLI

**Files:** publication.py、cli.py、README.md；测试 test_packet_publication.py、test_packet_cli.py。

**Interfaces:**

- `publish_assembly(analysis_root: Path, assembly: Assembly) -> dict` 在锁内原子发布新运行或新的 manifests/<manifest_id>；所有路径相对该run根，最终文件不可覆盖。
- `python -m app.analysis_packets.cli assemble-primary --analysis-root PATH --video-id ID --prepared-run-id ID --resources-config PATH --max-input-tokens N --reserved-output-tokens N --context-window N --max-active-members N`。
- `python -m app.analysis_packets.cli validate --analysis-root PATH --run-id ID --manifest-id ID`；只读离线校验。

- [x] resources-config 为本地JSON，固定 analysis_rules/role_prompt/coordination 三项，每项{name,version}，name必须来自准备context；只指定映射，不从源位置动态重新读规则。README 内含说明不替代缺失role文件。
- [x] 初次组装产生新UUID run目录、context资源副本、tasks、indexes、group coverage、空成员登记及manifests。run.json 明确 status=offline_prepared、model_execution_authorized=false、prepared_run_id，schema_ref=null；不写users/<UID>.json。
- [x] 在全部文件结构与语义验证成功后原子发布，失败只清理自己创建的准备目录，不能影响inputs/prep/其他run。task-index和资源引用hash要在最终序列化后计算，避免循环hash引用。
- [x] 同一运行的后续清单完整列出有效任务和组，previous_manifest_sha256串联历史；不覆盖旧清单。现有任务包可被新清单引用，但如果count/index/scope变化则必须新包，不能修改旧包。
- [x] CLI有效离线组装返回0与清单路径，输入／协议错误返回2，文件系统／锁错误返回3；JSON输出不包含真实原文或凭据。任何状态都不触发执行器。
- [x] 用合成数据验证部分覆盖、缺role、未知协议、清单hash错误和原子发布失败；使用socket禁用测试确认无网络。不启动Claude验证“是否可用”。
- [x] 运行新增测试、现有analysis_input回归、全后端测试与Ruff；数据库测试仍只允许隔离TEST_DATABASE_URL，未配置如实报告跳过。检查wheel含新增本地Schema。

## 当前阶段验收与交付

交付所有输入结构Schema、原文上下文选择、覆盖分块、primary任务生成、后序纯组装及合成依赖验证、离线发布和校验命令。真实数据可用于本地primary组装观察，不产生真实模型结果；不将合成AcceptedCatalog当作实际画像能力。

正式模型执行仍须补齐输出Schema、实际token计量／历史开销、费用控制、可信结果接受和原成员规则切换。以上不是本离线模块的隐含交付，不因Schema通过就宣布模型端到端可用。

完成后保留本功能分支的验证记录；用户验收后按AGENTS进行squash集成和正常推送。计划执行前先自审所有字段及接口是否与2.0.0一致，未明确事项不能通过添加私有必填字段解决。


## 当前交付与实现选择

本离线阶段已实现：analysis_packets 下的本地Schema/contract、来源、上下文、精确JSON codec、容量分块、覆盖与依赖语义校验、advanced纯组装、不可变发布和CLI。具体接口如下：

- build_primary(bundle, run_id, resources, budget, *, resource_files)。
- build_reconcile(bundle, primary_assembly, accepted, budget)。
- build_synthesis(bundle, target_uid, synthesis_level, prior_assembly, accepted, budget)。
- publish_assembly(analysis_root, assembly, bundle, accepted=None)。
- validate_published(analysis_root, run_id, manifest_id, accepted=None)。

相对计划采用 Assembly 携带资源文件字节和已发布前阶段结构，避免后序函数缺少 run_id 或资源来源。AcceptedResult.result_bytes 中的原始观察与输入投影分离，origin_result 的自引用hash在接受后由组装器添加；不要求结果文件包含自身hash。

依赖必须在本次或前序实际任务索引中存在并与接受目录一致；重读时检查磁盘结果字节，不能用内存目录掩盖文件损坏。清单链绑定固定来源和first_manifest_id。资源文件集合必须精确等于四项引用，输出命名空间及Windows别名冲突受控，不允许额外资源写进users目录。

综合所需的新增缺口、summary及limitations在分块计量前注入，不在完成分块后偷偷增加超限内容。Windows 5/32/33发布占用采用最多5次有界重命名重试，目标存在时拒绝覆盖。离线字节估算不保证实际模型token数。

当前自动测试仅使用合成数据，缺少TEST_DATABASE_URL的现有数据库测试跳过不等于通过；未改前端，不运行其构建。真实数据仅离线组装并存于忽略目录，具体结果在交付对话说明。未接入真实结果接受、模型调用或最终画像。

## 正反例回归约定

新增 test_packet_positive_negative.py 采用“合法基线通过 → 单项变化 → 精确错误码拒绝 → 原基线仍通过”的方式。修改任务包后重新计算容量及输入摘要，避免仅因传输摘要过期而偶然失败。

覆盖原文／作者归属、采集覆盖与上下文缺口、完整 UID 目标清单、证据引用、观察对象、前序完成状态、直接依赖、综合层级，以及两份不可变清单发布后的磁盘结果完整性。相同标签的不同 subject 必须原样保留；此类合成观察不代表模型已经正确理解配乐或配音。

已明确的运行规则：assessment_status=assessable 必须有非空支持 evidence，且引用须通过原文校验；insufficient 状态可在对象未知、无标签且无证据时保留，不为满足结构强行生成判断。该约束是既定输入协议证据要求的实现补全，不新增画像类别。

这些测试验证数据与流程规则，不能替代模型画像质量评估。后续接入模型时仍需人工审阅的标注样例、误判分析和已授权的小批调用。
