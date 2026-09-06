# PostgreSQL 评论存储与协议导入：设计

## 状态、目标与边界

用户已确认 PostgreSQL 用于网站查询/更新，JSON/JSONL 用于交接；现有文件可经校验后导入。本设计已按子 Agent 审阅修订，并由对应实施计划完成存储模块和本地数据库验收；网站与后台任务仍未接入。沿用 feat/discussion-cache，同一功能的设计和实现不另建分支。

本子阶段目标是可测试的持久存储模块：导入协议批次、按楼/UID 查询、原子发布和无历史保留。输入页面、后台采集调度、小时刷新资格控制、模型分析和公网部署继续分阶段实施。调试视频不写死；连接串由 DATABASE_URL 提供，外部数据目录为命令参数。

依据：[产品需求](../references/product-requirements.md)、[缓存设计](discussion-cache-design.md)、[固定导出协议](../references/comment-export-protocol-v1.md)。不修改导出协议 1.0.0。

## 数据流与方案选择

```text
当前：已发布 JSONL → 冻结并校验 → 导入临时数据库状态 → 校验 → 发布
后续：采集归一化记录 → 同一个存储模块 → PostgreSQL → 协议导出
                                            ↓
                                    按楼 / 按 UID 查询
```

采用评论行加必要 JSONB 的混合模型。整楼 JSONB 每次更新整楼且不利于跨楼用户查询；将所有表情/图片再拆表会增加本阶段无需承担的复杂度。因此身份、关系和排序字段使用普通列，正文及媒体保留结构化 JSONB。

导入只将楼目录的评论作为唯一写入来源。用户目录用于校验投影一致性，不作为第二次入库来源。未来直接采集入库不要求先生成文件；现有 CLI 的 SQLite 继续仅负责运行中断恢复，迁移采集工作集属于后台任务阶段。

## 物理表与关键约束

外部数字 ID 全部 TEXT，应用和 CHECK 约束要求正整数字符串；不得使用浮点类型转换。内部状态/操作 ID 使用 UUID，时间使用 TIMESTAMPTZ。下列字段是数据库字段，不重命名外部协议字段。

| 表 | 主要字段与约束 |
| --- | --- |
| videos | video_id TEXT 主键；platform、aid、oid、comment_type、bvid、episode_id；(platform, comment_type, oid) 唯一；current_state_id、working_state_id 可空 UUID |
| video_links | normalized_url TEXT 主键、video_id 外键；保存已校验来源别名，去掉分享查询参数，不自动联网拓展别名 |
| discussion_states | state_id UUID 主键、video_id 外键、source_export_id、schema_version、hour_bucket、captured_from/to、exported_at、coverage JSONB、title、source_metadata JSONB、lifecycle |
| threads | (state_id, root_id) 主键；root_author_uid/name、source_title、comment_count、reply_count、participant_count、unknown_author_comment_count、coverage JSONB；state_id 外键 |
| comments | (state_id, comment_id) 主键；root_id、parent_id、kind、author_uid、nickname、created_at、collected_at、like_count、reply_relation JSONB、content JSONB、extra_fields JSONB |
| unclassified_comments | (state_id, ordinal) 主键；规范异常条目 payload JSONB，按文件顺序保存；不进入正常用户/评论计数 |
| import_receipts | (video_id, source_export_id) 主键；canonical_digest、state_id 可空、status、created_at/completed_at、safe_error_code；status 固定为 loading、ready、partial、published、failed、expired；不保存历史正文 |

discussion_states.lifecycle 为 loading、ready、current、partial、failed。来源覆盖状态仍为协议里的 verified/partial，与数据库事务生命周期不同。verified + gaps 合法，禁止因数据库导入成功提升来源覆盖状态。

comments 的 (state_id, root_id) 外键指向 threads；缺根评论时仍创建该楼的元数据行。parent_id 不设指向 comments 的强制外键，因为来源可能不返回父节点；缺父、跨楼和未知关系按协议保留并报告。author_uid 可 null，不建虚假的 UID=0 用户。

首版不单设用户实体表，昵称是每条发言的快照。按 UID 聚合并按协议选择显示昵称即可；同名不合并，改名不换 UID。以后若需要视频外用户信息，再单独设计用户表。

videos 的两个状态指针必须引用同一视频状态，通过 (video_id, state_id) 组合外键保证；current 和 working 不得指向同一状态。导入、发布和工作状态清理共用每视频的执行所有权锁，短事务另外锁定 videos 行。discussion_states 对 video_id 建立排除 current 的部分唯一约束，确保 loading/ready/partial/failed 总共最多一个工作状态。不能只约束 loading/ready，遗漏部分结果和失败状态。相关状态创建、指针与 receipt 的修改必须同事务完成。

### JSONB 与无损还原

content 无损保存 text/images/emotes 的逻辑结构，reply_relation 保存状态和目标 UID；自由文本在写入时使用下面定义的内部编码，读取时还原。extra_fields 保存协议允许的未知扩展字段，按层级保留，避免导入时静默丢弃；导出时不允许扩展字段覆盖明确列值。threads 和 states 同样保留完整来源元数据中未映射的字段。

数据库内部 state_id 不等于文件 export_id。首次导入将 source_export_id 保留为来源信息；再次导出生成新的 export_id，并更新所有记录和元数据的同批次标识。规范化正文、关系、身份、时间、媒体内容必须保持一致。

### PostgreSQL 特殊字符的可逆存储

PostgreSQL 的 TEXT/JSONB 不能保存 NUL，JSONB 还要求合法的 Unicode 代理对；不能由“协议 JSON 校验通过”推断可直接入库。依据 [PostgreSQL JSON 类型说明](https://www.postgresql.org/docs/18/datatype-json.html)，本设计采用内部 `pg-text-v1` 编码，不修改外部协议，也不删改源内容。

- 对所有自由文本列，以及 JSONB 内的字符串值和对象键应用同一个可逆编码；固定 ID、状态枚举、时间列不编码。数据库使用 UTF8。
- 从原字符串逐字符扫描：反斜杠编码为两个反斜杠，U+0000 编码为字面量 `\0`（一个反斜杠加数字 0），未配对代理码点 U+D800–U+DFFF 编码为字面量 `\uXXXX`（一个反斜杠、u 和四位大写十六进制）；其他字符不变。这里记号展示的是编码规则，序列在 SQL 参数中仍由驱动正常转义。
- 解码也只扫描一次：双反斜杠还原为一个，反斜杠加 0 还原 NUL，反斜杠加 uXXXX 仅还原上述代理码点；无效转义返回 storage_decode_error，禁止猜测或循环替换。编码一律作用于原始值，不能把已编码值再次编码。
- 例如真实 NUL 与原正文中的字面量反斜杠加 0 必须存为不同序列，防止解码后混淆。迁移固定本数据库存储 codec 版本；未来升级必须迁移或增加显式版本，不能混用。
- 查询/导出/用户显示昵称选择在解码后进行；应用不得把内部编码串直接交给前端或模型。幂等摘要和回读比较基于解码后的协议对象。

此方案只处理字符表示差异，不绕过字段约束。若遇到 PostgreSQL 数值范围、单值容量等存储限制，整批导入返回 unsupported_storage_value 并保留 current，不截断或替换内容；不承诺任意大小的合法 JSON 都可存储。

同一个状态内每条评论只有一行，不分别存“楼副本”和“用户副本”。刷新期间当前状态和临时状态可以各有该评论的一行，这是保证读取隔离的暂时复制；新状态发布后清理旧状态。首版不引入跨状态内容去重表，减少引用回收错误；逻辑上保留未来复用未变观测的接口。

## 查询与索引

- 楼内查询：WHERE state_id = ? AND root_id = ?，根评论第一条，其余按 created_at NULLS LAST、comment_id 数值顺序。
- 视频内用户查询：WHERE state_id = ? AND author_uid = ?，按时间和评论 ID 排序；未知作者作为专门筛选，不当成同一用户。
- 对应索引为 (state_id, root_id, root_rank, created_at, id_length, comment_id) 与 (state_id, author_uid, created_at, id_length, comment_id)。root_rank 为 CASE WHEN kind = 'root' THEN 0 ELSE 1 END 的生成列，查询、索引和游标统一使用它；created_at 显式 ASC NULLS LAST。id_length 为 length(comment_id) 的生成列；ID 字典比较指定 C 排序，长度加字符串等价于无前导零正整数排序，不转换 BIGINT。
- 所有查询绑定 state_id；分页游标携带 state_id、root_rank（仅楼查询）、created_at 的空值标记与值、id_length、comment_id，不只携带 offset；比较逻辑按空值标记分支，不能使用含 SQL NULL 的普通元组比较跳过未知时间记录。状态已被回收时返回 state_expired，调用方重新读当前指针。
- 单次查询事务使用一致快照，保证读取指针后仍能读取同状态记录；跨请求不保证旧状态仍存在。数据库导出使用一次 REPEATABLE READ 只读事务完成所需数据读取，再在事务外生成/发布文件，避免网络模型调用占用数据库事务。

本阶段实现存储查询方法和测试，不新增 HTTP 接口或查询页面。

## 协议文件导入

1. 参数接受明确批次目录或 current.json 入口，均为本地文件；禁止导入过程访问来源 API。读取入口时遵循协议的整批重读规则。
2. 在独立的临时工作目录冻结所有所需输入，完成 Schema、身份集合、投影一致性、计数和缺口校验后再写数据库。不能校验一次后又从持续变化的原目录重读入库。
3. 计算规范摘要：对冻结批次的 JSON 对象排序键编码，对数组保持顺序，JSONL 保持记录顺序，将逻辑文件相对路径与内容一起 SHA-256。空格/缩进不影响摘要，任何语义数据变更都影响摘要；README 必须为空。
4. 取得下述每视频所有权锁，锁定/创建视频并检查 import_receipts；任何状态下同 ID 不同摘要均拒绝 export_id_conflict。同摘要按下述状态表分支处理，不能一律返回成功，也不能只凭 state_id=null 判断过期。
5. 按所有权与恢复规则处理唯一的旧工作状态，建立 loading 状态及 loading receipt 并更新 working_state_id，在同一短事务中写入来源元数据、楼记录。评论分批写入临时状态，每批事务提交；任何记录不合规停止，状态不可发布。普通失败保留 safe_error_code，不记录正文到日志。
6. 校验已入库计数、归属、ID 集合及导入字段的规范摘要；用户视图从数据库重建后与冻结用户文件比较。通过后标 ready（来源 verified）或 partial（来源 partial），更新 receipt。
7. 发布时持有同一所有权锁并再次锁定视频行，检查状态资格和当前指针，按下述清理事务原子切换。导入成功与发布成功分别返回，不能将未发布状态当当前状态。

### 执行所有权与崩溃恢复

文件冻结与校验完成后，使用专用 PostgreSQL 连接取得该视频的会话级 advisory lock；取不到立即返回 import_busy，不等待整轮导入，也不清理另一进程数据。锁键为 UTF-8 字符串 `bilibili-import:` 加 video_id 的 SHA-256 前 8 字节按有符号大端整数解释，禁止使用每进程随机化的语言 hash。极小概率的锁键碰撞只使不同视频串行，不允许改变数据归属。

从取得所有权直到导入及可选发布结束始终保持这条连接；按批次提交事务不释放会话锁。所有写入必须使用该连接，不能拿另一条无锁连接继续写。进程或连接退出后锁由数据库释放；恢复前不能仅凭时间久或 lifecycle=loading 判断原任务已死。

成功取得锁且发现遗留 loading，才可认定上一执行者已中断；在 videos 行锁事务下先标 failed、记录 interrupted，再按同摘要重试规则重导。连接意外中断时立即停止，不自动换连接继续原进度；再次运行必须重新取锁。正常退出显式解锁；确认解锁失败的连接必须关闭，不能返回连接池。

### receipt 幂等与重试动作

| receipt 状态 | 相同摘要的再次请求 |
| --- | --- |
| loading | 无所有权返回 import_busy；成功取锁后视作中断，转 failed 并重新导入 |
| failed | 清理该失败工作状态后重新导入；复用 receipt，不返回成功 |
| ready | 复用已校验状态，不重新插入；请求包含发布动作时重新尝试发布 |
| partial | 复用部分工作状态并返回其限制，不提升为完整，不重复写入 |
| published | 返回既有当前状态，不重复发布 |
| expired | 返回 already_imported_expired，不复活旧批次 |

只有已成功导入的 ready/partial/published 状态被回收后才将 receipt 标 expired；失败清理保留 failed 语义。若 receipt 指向与其状态矛盾的数据，返回 inconsistent_import_state，不将其当成可自动覆盖的旧状态。

加载失败不恢复半成品：重试同摘要时删除该失败状态及其正文，再建立新的 loading 状态，receipt 的 state_id 原子改为新 ID。首版不增加第二套导入断点，采集仍由 CLI 的检查点恢复。

不同批次准备替换已有 ready/partial/failed 工作状态时，必须先在内存完成输入校验，并按发布时间规则核对候选不早于当前及旧 working。满足规则后，在一个短事务内回收旧 working、更新其 receipt、建立新 working；current 不变。遗留 loading 必须先按所有权规则处理，不允许旁路插入第二个工作状态。

### 发布与外键清理事务

一次发布事务内：锁定视频行，核对候选恰为 working 且 lifecycle=ready、coverage.status=verified；将原 current 的 receipt 标 expired 并清空其 state_id；将视频 current_state_id 指向候选、working_state_id 置 null；删除旧 current 及其楼/评论/异常记录；候选改为 current、其 receipt 改 published，最后提交。任一步失败全部回滚，旧 current 仍有效。

父子正文外键按状态使用 ON DELETE CASCADE，receipt 在删除父状态前显式清空引用；不能让删除 working 的级联操作影响 videos。创建组合外键前为 discussion_states 声明 UNIQUE(video_id,state_id)。所有指针更新和状态删除在同一事务中完成，不产生等待异步回收的第二个已发布状态。

单次读取使用 REPEATABLE READ，已取得快照的读者可完成旧状态读取；后续请求用旧 state_id 得到 state_expired。数据库的 MVCC 物理回收由其维护机制处理，这不等同于提供应用历史查询。

### 发布时间与覆盖规则

- 有 current 时，新 partial 保留为最近 working，可显式查看，不替换 current。
- 没有 current 时，partial 仍保留为 working，查询服务返回它并明确未发布/不完整；它不冒充 verified current。
- 新 verified 才可发布；上下文 gaps 原样保留，不作为自动丢弃数据的理由。
- 为防止旧文件倒灌，候选 hour_bucket 不得早于当前；同小时要求 captured_to 不早于当前，且来源时间区间不能倒退。相同小时、相同采集截止时间但不同 export_id 且数据冲突时拒绝 ambiguous_replacement，不猜测谁更新。
- 导入不是向 Bilibili 发起刷新，不消耗“每小时一个采集任务”的资格。允许同一任务不同 export_id 的 partial 后续结果导入；未来小时资格由采集任务表控制，不能在 discussion_states 上加 (video_id,hour_bucket) 唯一约束来误拦导入。
- 当前状态之外最多一个工作状态。按发布/替换事务清理旧 current 和已替换 working 的评论/楼/异常正文；receipt 仅保留无正文的幂等凭据，state_id 清空。

## 新旧评论和增量边界

同视频 comment_id 首次出现为新增；同 ID 比较正文、媒体、UID/昵称、父关系与其他观测字段判断变化。数据库更新必须限定临时状态，不修改 current 正文。仅点赞变化是否影响未来模型缓存留到模型阶段。

新完整状态只包含该批次实际观察到的集合；旧 ID 未再出现表示“不在新采集结果”，不自动标为源站已删除。partial 不替换 current，避免覆盖不全看起来像大量删除。此阶段仍以完整批次导入为主，不承诺源 API 只请求新增评论。

## 失败、配置与验收

DATABASE_URL 只在环境变量/受保护配置中提供，日志不打印连接串。数据库迁移与隔离测试数据库在实施计划中落地；Compose 新增数据库服务时默认不向公网开放端口，数据卷持久化，禁止对用户现有数据库运行 drop/reset。

需要 PostgreSQL 实例验证，而不是用 SQLite 模拟通过：同视频并发导入、重复批次、同 ID 摘要冲突、中途失败、partial 不覆盖 current、陈旧批次拒绝、发布与读者竞争、状态过期、无历史正文回收、超大 ID、未知 UID/父节点、媒体/特殊 Unicode、NUL 与字面量转义串的无损区分、存储映射只编码一次并正确还原、进程被杀后的所有权恢复、ready 后发布前崩溃、所有 receipt 状态重试，以及数据库导出后再通过现有协议校验。

验收标准是：同批次记录无重复，两种查询重建结果与文件一致；失败不破坏当前状态；partial/gaps 不丢失；相同批次重复导入不重复写；数据库重启后当前结果仍可查。所有测试使用虚构样例，真实导入由该子阶段授权后执行。

## 审阅后安排

本文件定义存储设计，实施内容见对应计划。页面、采集后台任务与目标服务器部署仍需独立设计与验收。

## 实施计划

见 [PostgreSQL 存储实施计划](2026-09-06-postgresql-storage-plan.md)。计划已完成本地实现和存储验收，来源数据完整性不由数据库验收提升。

## 后续增量扩展

本文件描述已完成的整批存储基线。用户新确认的正文复用要求见 [增量刷新设计](2026-09-06-incremental-refresh-design.md)，已新增0002_comment_payloads及载荷引用读写，并通过隔离测试数据库验收；原开发数据库尚未升级。独立CLI增量采集已实现，逐楼核验证据保存在SQLite工作集，0003_cache_handoff及后台交接接口已支持精简证据入库和原子物化；现已由C2本地worker接入，HTTP层尚未接入。
