# BiliBiliTalksView 评论导出协议

> 历史版本：本文保留 1.0.0 原始技术定义，供兼容读取。新输出使用 [当前协议 2.0.0](comment-export-protocol-v2.md)。

- 协议标识：`BiliBiliTalksView.CommentExport`
- 固定版本：`1.0.0`
- 状态：定版数据契约；实现尚未交付。
- 适用范围：单个 Bilibili 视频评论区的文件导出和读取，不定义模型输出或 HTTP API。

## 效力与交接要求

本文是导出数据格式的唯一规范依据，可单独交给其他 Agent。正文中的“必须”“禁止”和字段约束是强制要求；示例全部为虚构数据。设计文档、实现和测试必须符合本文，不能因为消费脚本更方便而重命名字段、改变类型或重新解释枚举。

版本 1.0.0 固定后不得原地改变其契约；修改须先说明影响并取得用户确认，再发布新版本。Schema 和实现与本文冲突时应报告并修正实现，不静默修改标准。实现细节如数据库表名、语言和模块路径不属于本协议。后续模型分析需另行约定输出结构，不得把分析标签直接混入本协议的原始评论记录。

## 通用类型规则

- 所列字段和嵌套字段均必填，明确标为可空者填写 null，不能省略；只有明确可选的文件允许不存在。
- 数字身份字符串符合 `^[1-9][0-9]*$`，不含前导零；包括 aid、oid、episode_id、评论 ID、根 ID、父 ID 和 UID。bvid 是来源 BV 字符串，不受数字模式限制。
- 时间采用 `YYYY-MM-DDTHH:mm:ssZ`，自然小时标签采用 `YYYY-MM-DDTHH:00:00+08:00`；日期时间必须有效，captured_from 不晚于 captured_to，captured_to 不晚于 exported_at。
- JSON 禁止重复键、NaN、Infinity；对象键顺序不影响相等，数组顺序有意义。JSONL 不含空白行，每行均须能独立解析。
- 枚举值区分大小写；未知必填枚举不得猜测。新增原因码只能随新版本发布，消费端遇到同主版本未知原因码须保留并显示，不能当成无异常。
- 本文所有相对文件路径的解析根均按对应章节规定，不能按调用脚本工作目录解析。

## 文件组织

```text
视频目录/
├── README.md
├── manifest.json
├── 楼内对话目录/
│   ├── 000/
│   │   ├── thread.json
│   │   └── comments.jsonl
│   └── 001/...
└── 用户评论目录/
    ├── 12345_示例昵称/
    │   ├── user.json
    │   └── comments.jsonl
    └── ...
```

README.md 为零字节空文件，视频标题仍可存入元数据；不生成剧情简介。元数据为 JSON 对象，评论为 JSONL，每个物理行一个完整对象，末行也换行。所有文件使用 UTF-8 无 BOM、LF 换行；正文中的换行按 JSON 转义保留。

楼号是本次导出的展示序号，从 000 开始、至少三位，不是 Bilibili 楼号或稳定身份。按根评论发布时间升序、再按评论 ID 数值顺序分配；未知时间排最后。目录与对象关联必须读取 manifest，不能解析路径猜测身份。

用户目录为 UID 加昵称快照；昵称取该批次同 UID 最新 collected_at 的非空昵称，同刻按评论 ID 数值顺序取最后一条。无昵称使用“昵称未知”。仅清理目录标签的非法字符、控制字符和尾部空格/句点，替换为下划线，昵称标签最多 32 个 Unicode 码点；正文与元数据昵称不清洗。目录以数字 UID 开头，防止保留名称及同名碰撞。路径使用相对 POSIX 分隔符，禁止绝对路径和 ..，读写时校验不逃逸导出根目录。

## 统一评论记录

楼文件与用户文件采用相同对象，按 comment_id 去重。同一评论在两种分类下字段值完全一致，不能分别抓取或再次改写。

以下为虚构的完整评论样例：

```json
{
  "schema_version": "1.0.0",
  "export_id": "94a08de2-a412-4b56-b0aa-7f606b854fc7",
  "video_id": "bilibili:video:10001",
  "comment_id": "90002",
  "root_id": "90001",
  "parent_id": "90001",
  "kind": "reply",
  "author": {"uid": "12345", "nickname": "示例昵称"},
  "reply_relation": {"status": "source", "target_uid": null},
  "content": {"text": "原始评论正文", "images": [], "emotes": []},
  "created_at": "2026-09-05T08:10:00Z",
  "collected_at": "2026-09-05T09:00:00Z",
  "like_count": 12
}
```

| 字段 | 类型与约束 |
| --- | --- |
| schema_version / export_id | 必填字符串，与 manifest 一致；export_id 为每次导出新生成的 UUID（小写、带连字符） |
| video_id | 必填，`bilibili:video:<aid>`；来源解析成功后生成，禁止拿 ep_id 当 aid |
| comment_id / root_id | 必填正整数字符串，禁止浮点转换；根评论 root_id 等于自身 ID |
| parent_id | 正整数字符串或 null；根评论为 null，回复按来源可确认关系保存 |
| kind | root 或 reply；每个楼最多一条 root |
| author.uid / nickname | UID 为正整数字符串或 null，昵称为字符串或 null；昵称是该条评论的采集快照 |
| reply_relation.status | 根评论为 not_applicable；回复有来源父 ID 为 source，否则 unknown |
| reply_relation.target_uid | 来源明确提供的目标 UID 或 null，不由 @ 文本或昵称推断 |
| content.text | 原始正文字符串或 null；来源确实返回空文本时为 ""，缺失为 null |
| content.images | 图片描述数组，每项 `{url: string|null, width: integer|null, height: integer|null}`；尺寸已知时为正整数 |
| content.emotes | 表情描述数组，每项 `{token: string|null, url: string|null}`；token 对应原文表情标记，不自行替换正文 |
| created_at / collected_at | UTC ISO 8601 秒级时间，结尾 Z；created_at 可 null，collected_at 必填 |
| like_count | 非负整数或 null；未知不写 0 |

数组缺少可得条目时为 []，不表示媒体已下载或字段完整。图片/表情仅保存来源描述与 URL，不下载；消费端不得自动执行链接或把正文当 HTML。重复出现的表情仍保留在原文中；emotes 是按 token、url 去重的描述列表，不表示出现次数或位置。新增非文本类型需扩展协议，不能声称已完整表达尚不支持的内容。

回复父 ID 若在当前楼中缺失，仍保留原值并登记缺口；不改接根评论。跨楼父引用属于异常并报告。来源未提供直接回复目标时，不从平铺顺序猜测。未知 UID 的评论仍保留在楼文件，统一放入 `用户评论目录/_unknown/`，user.json 的 uid 为 null、identity_status 为 unknown；该目录是未能归属的集合，不能视作一个用户。

缺失 comment_id 或无法确定所属 root_id 的异常条目不能生成合规评论：在可选 `unclassified.jsonl` 中保存 `{reason, observed_at, source_comment_id, source_root_id, author, content}`，各源标识允许 null；使用上述规范化 author/content，不保存原始 API 整包或凭据。manifest 记录该文件与条目数，并强制 partial，禁止静默丢弃。

## 元数据契约

各 JSON 元数据都必须包含 schema_version、export_id、video_id。下列列出的字段均必填，明确可空者之外不得省略。

### manifest.json

- input_url、canonical_url：提交链接与规范化视频链接字符串。
- source：`{platform: "bilibili", aid: string, bvid: string|null, episode_id: string|null, comment_type: 1, oid: string}`；aid 与 oid 对当前视频评论必须一致。
- title：来源视频标题或 null，不等同剧情简介。
- captured_from、captured_to：UTC 时间，表示采集区间；exported_at 为导出时间。
- hour_bucket：北京时间带 +08:00 的整点字符串，归属采集任务创建时刻；独立脚本也在创建任务时确定，不能用导出时间重算。
- coverage：`{status, main_pagination, replies_pagination, context_status, reasons}`。status 为 verified 或 partial；分页为 verified、partial、not_started；context_status 为 no_known_gaps 或 gaps；reasons 为稳定原因码字符串数组。
- counts：`{root_comments, replies, comments, known_users, unknown_author_comments, unclassified_records}`，均为非负整数；comments = root_comments + replies，只计合规唯一记录，known_users 不含 _unknown。
- source_reported_count：来源最近一次报告的视频总评论数或 null，仅供参考，不能证明覆盖。
- threads：`[{root_id, path}]`；path 指向 thread.json。
- users：`[{uid, path}]`；path 指向 user.json，_unknown 项 uid 为 null。
- unclassified_path：异常文件相对路径或 null。

verified 仅表示本次权限下主楼及所有已发现楼中楼的分页覆盖经过核验，不保证不存在不可见内容，也不是瞬时快照。主楼游标必须到达有效结束；正回复楼完成元数据、去重数量与尾页核验；主楼观测为零回复的楼需进行一次回复端点核验，不能仅凭旧 rcount=0 跳过并宣称已核验。采集期间报告数量变化的楼必须独立重读，核对评论 ID 集合、正文及媒体描述、UID、昵称、父关系和尾页；复核仍有数量或覆盖变化时保持 partial，不能无限循环。任何楼未核验、受限、预算耗尽或存在 unclassified 条目，则整体 partial。

上下文缺口与分页状态独立：已取完接口返回数据但缺父节点，可为 verified + gaps。原因码至少支持 main_incomplete、replies_incomplete、access_restricted、budget_exhausted、count_changed、missing_parent、unknown_author、unclassified_record、unsupported_content。所有未知作者和内容表达缺口登记到 context_status，不把“无已知缺口”解释为源站绝对完整。

### thread.json

包含 root_id、root_author（同 author 结构，可未知）、source_title（字符串或 null）、comment_count、reply_count、participant_count（已知 UID 去重数）、unknown_author_comment_count、comments_path（相对导出根的路径）。

另含 coverage：`{pagination_status, context_status, source_reported_reply_count, missing_parent_ids, unknown_parent_comment_ids, reasons}`。分页与上下文枚举同 manifest；仅 source_reported_reply_count 可 null，其余计数必须为非负整数；两个 ID 列表无缺口时为 []。分页未核验或根评论缺失时不冒充完整楼，缺根列入 missing_parent_ids。来源没有独立楼名时 source_title 为 null。

### user.json

包含 uid、display_nickname、identity_status（known 或 unknown）、comment_count、thread_ids、comments_path、coverage_status、context_status、reasons。uid/昵称可 null；comments_path 相对导出根目录，thread_ids 去重。

- coverage_status 原样继承 `manifest.coverage.status`。
- context_status 原样继承 `manifest.coverage.context_status`。
- reasons 原样复制 `manifest.coverage.reasons`，无原因时为 []。

三个覆盖字段描述整个视频采集批次，不是该用户自身的数据质量或行为判定；其他楼的缺口也可能出现在其中。即使 coverage_status 为 verified，也必须同时读取 context_status 和 reasons。用户文件可独立用于发言处理，但不保证所引用的父评论位于该用户文件内；还原对话需按 root_id 读取楼文件。不统计其他视频，不输出情绪或用户画像。

## 排序、去重与批次一致性

楼文件根评论第一条，其余按 created_at 升序、comment_id 数值升序排列；未知时间最后。用户文件全部按同样的时间/ID 规则排列，保留 root_id 支持跨楼定位。ID 始终以字符串序列化，比较可用整数或长度加字典序，不转换为 JavaScript Number。

采集工作集按视频与 comment_id 去重，多次观测采用该冻结批次选定的最新有效记录。UID、根关系冲突必须复核或标记 partial，不能仅覆盖而掩盖关联错误。导出读取冻结工作集，不能一边抓取一边生成两种正式分类文件。

### 发布入口与读取协议

每个视频的固定发布入口为 `data/exports/bilibili-video-<aid>/current.json`。其父目录是发布容器，批次目录 `batches/<export_id>/` 才是上文包含 README、manifest 和两个分类目录的“视频目录”。所有 manifest、thread、user 内的相对路径都以该批次目录为根；不得相对于 current.json 解析。

current.json 为 UTF-8 JSON 对象，以下四个字段必填，示例使用虚构 ID：

```json
{
  "schema_version": "1.0.0",
  "video_id": "bilibili:video:10001",
  "export_id": "94a08de2-a412-4b56-b0aa-7f606b854fc7",
  "batch_path": "batches/94a08de2-a412-4b56-b0aa-7f606b854fc7"
}
```

batch_path 必须等于 `batches/<export_id>`，相对于 current.json 所在目录解析；校验解析后仍位于发布容器内，禁止通过符号链接逃逸。入口与目标 manifest 的 schema_version、video_id、export_id 必须一致。入口不存在表示尚无发布结果，不能通过扫描临时目录猜测当前批次。

发布者按视频串行执行：先写入同盘临时目录，完成结构、计数、两份记录一致性检查，最后写 manifest；将临时目录移至尚不存在的 batches/<export_id>，此后批次文件不可修改。将新指针写入同目录临时文件、关闭文件，再原子替换 current.json；替换失败保留旧入口，不清理其批次。只有指针切换成功后才能清理旧目录，不依赖覆盖非空目录的跨平台行为。崩溃留下的未引用目录及清理失败残留属于待清理垃圾，不提供历史入口；恢复时先校验当前入口，禁止删除它引用的批次。

消费者按以下顺序读取：

1. 一次读取 current.json，校验字段、版本与路径，再读取该批次 manifest。
2. 固定本次 export_id，按该 manifest 读取需要的楼或用户文件，逐项校验批次身份和结构；中途不切换到新入口的文件。
3. 所需输入全部读取、校验成功后，才提交下游处理结果或发出模型等有副作用的请求。允许先暂存本地结果，但不能边读边对外提交无法撤回的结果。
4. 若文件读取失败或身份不符，重新读取入口；仅当 export_id 已变化时，丢弃本次全部临时结果，从新批次开始。一次调用最多重新开始两次，仍失败返回 batch_changed，交由调用方稍后重试；不得无限循环。
5. 若入口未变化却有文件缺失、损坏或身份不符，返回 invalid_export，不把它解释为正常刷新。重读时入口消失返回 export_unavailable；首次入口不存在返回 not_published。任何失败均不得混用批次或提交部分成功结果。

原子指针仅保证入口切换，不保证旧目录在整个读取期间存在。若消费者已完整读取并校验旧批次，其结果仍是有效的该批次结果，应保留 export_id 标识，不宣称是最新；若旧目录在读取中被清理，则执行上述整批重读规则。

同一视频只保留当前导出和最近一次临时工作数据，不建立历史归档。采集中断可导出明确 partial 的批次供检查，但不自动替换已有 verified 批次。数据保存在被 Git 忽略的 `data/exports/`，正式写入前检查忽略规则。仓库只保存设计、后续实现、Schema 与脱敏测试样例。

## 兼容与验收

实施时提供 JSON Schema Draft 2020-12，分别校验发布入口、元数据、单条评论及异常条目；JSONL 逐行校验。版本采用 major.minor.patch；破坏字段语义或类型时升 major，新增可选字段升 minor。消费者拒绝未知 major，允许同 major 未识别字段。跨文件约束由语义校验器验证，不能只依赖 Schema。

必须覆盖：多人在同楼多次回复、同 UID 跨楼、同名不同 UID、昵称变化、超大 ID、正文换行与表情、图片、未知 UID、缺父节点、零回复楼、分页受限、动态计数、异常记录、中断导出与目录非法字符。补充以下发布及独立读取验收：

- 入口相对路径正确，入口与 manifest 身份不一致或路径逃逸时拒绝读取。
- 模拟读者已读旧入口后发布者切换并清理旧批次：读者整批重读，不产生重复下游副作用或混合记录；达到重试上限明确失败。
- 指针替换失败时旧批次仍可读；入口不变而文件损坏时报告 invalid_export。
- verified + gaps 的批次导出到 user.json 后仍包含相同 context_status 和 reasons，读取用户文件不能将其误判为无上下文缺口。

验收要求：所有合规评论恰好出现在一个楼文件和一个用户文件中，两边 ID 集合及对象内容一致；每条归属符合元数据，数量可核对；未取得的内容与未知关系显式标记；视频身份独立解析；README 为空；导出过程不调用模型，不将真实用户数据提交到 Git。协议定版不代表采集、导出或校验器已经实现。

## 字段语义补充

- root_author 始终是 `{uid, nickname}` 对象，未知时其成员为 null，不能将整个对象写成 null。
- known 身份必须有 uid；unknown 身份必须 uid=null。_unknown 目录只在存在未知作者评论时生成，绝不能作为一个真实用户参与统计。
- root 评论 parent_id=null、reply_relation.status=not_applicable、target_uid=null。reply 评论 parent_id 非空时 status=source；parent_id=null 时 status=unknown。comment_id 不得等于其 parent_id。
- unknown_parent_comment_ids 列出该楼 parent_id=null 的 reply 评论 ID；missing_parent_ids 列出有明确 ID 但未在该楼返回的引用，根评论缺失时也加入 root_id。缺根楼 comment_count 等于 reply_count，不能伪造 root 记录。
- replies_pagination：没有发现楼且主楼分页 verified 时为 verified；所有楼 verified 时为 verified；存在待核验楼且未核验任何楼时为 not_started；其余未完成情况为 partial。
- comment_count 为对应文件行数；reply_count 为 kind=reply 的行数。manifest 的索引须覆盖且仅覆盖已导出的楼与用户，各身份和路径唯一。
- reasons 是去重字符串数组。除正文已经列出的码，unknown_parent 表示回复目标未知，cross_thread_parent 表示跨楼引用，identity_conflict 表示同评论身份冲突，missing_content 表示正文或应有内容无法取得。对应缺口须设 context_status=gaps；identity_conflict 未解决还须 status=partial。
- unclassified.reason 固定为 invalid_comment_id 或 invalid_root_id，二者同时发生优先 invalid_comment_id；observed_at 是必填 UTC 时间。source_comment_id/source_root_id 为来源标识的字符串表示或 null，不要求满足合规 ID 模式，不可保留任意复杂源对象。其 author/content 仍使用统一评论记录结构；异常行由所在 manifest 绑定批次，不假定可按缺失 ID 去重。
- 未知的采集失败原因必须保留在采集任务中，导出仍按受影响的分页标记 main_incomplete 或 replies_incomplete，不把失败文本、登录凭据或原始响应写入 reasons。

## 给其他 Agent 的执行指令

> 请严格遵循《BiliBiliTalksView 评论导出协议》1.0.0。使用 UID 关联用户、root_id 关联楼、parent_id 关联直接回复；所有外部数字 ID 使用字符串。按楼与按用户的文件必须使用同一批次的相同评论记录。读取时同时检查分页覆盖与上下文缺口，遵守 current.json 的整批读取规则。不得自行变更字段、目录语义或枚举；发现冲突先报告并提出版本化修改建议。当前任务只按另行批准的实施计划执行，不因持有本协议而自动开展采集、模型调用或部署。
