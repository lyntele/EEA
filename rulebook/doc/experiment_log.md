# EEA 实验记录

本文档是 EEA 当前系统的统一实验账本。以后每一次聚类、触发、在线累计、端到端相关尝试都记录在这里，不再拆分多个实验文档。

注意：早期实验段落保留为历史记录，用来解释为什么某些路线被采用、回退或废弃；如果旧段落与后续实现决策冲突，以日期更晚的“决策/本轮修改”段落和 `current_implementation_overview.md` 为准。

## 记录规范

每个实验必须记录：

- 实验编号和日期。
- 对应 commit；如果当时源码是 dirty baseline，必须写明。
- 实验假设或验证目标。
- 相关流程：构建、触发、实例化、rewrite、promotion、在线累计等涉及哪些环节。
- 输入数据和输出目录。
- 指标：按实验目标记录，例如 strict pattern TP/FP/FN、precision、recall、F1、pattern/singleton 数量、trigger recall、noise false trigger、端到端 improved/regressed。
- 强 pattern 覆盖、误升、误触发、漏触发、修错案例。
- 最终决策：保留、修改、回退或废弃。

提交纪律：

- 实验记录、实现改动、验证结果原则上分开记录；如果用户要求一次 commit，则本次相关实现和记录一起提交。
- 不提交生成输出、runtime state、缓存、`__pycache__`、大型 pkl/sqlite 文件。
- 如果某次实验在 dirty baseline 上跑，必须在记录中说明，不能把结果伪装成干净 commit 的结果。

当前仓库状态说明：

- 创建实验账本时的 HEAD：`4038584 Add toxicology14 r3 workspace analysis snapshot`。
- 首次提交实验账本的 commit：`387c9a6 Document pattern clustering experiment ledger`。
- 当前工作区已有较多历史遗留脏改动。本账本只记录 EEA 实验，不代表整个 dirty source state 已经被完整 checkpoint。

## 术语

- 粗筛：代码或轻量信号只负责召回“值得比较的 singleton/card”，不直接决定 pattern。
- 语义判断：判断候选案例之间是 formal pattern、bounded branch pattern、family-only、partial 还是 conflict。
- Formal pattern：拥有稳定 source misconception、target preference、可绑定槽位和 answer-blind 实例化接口的正式记忆对象。
- Family：有重复经验或相近偏差，但还没有稳定到可以实例化为统一动作接口。
- 强 pattern：人工标注中的 formal pattern 组，只用于审计和验收，不进入代码逻辑。

## Pattern 聚类实验

### E-20260509-03：Phase 1 WU5 结构依赖补齐

Commit：

- 待本次提交。

背景：

- `toxicology_focus18_postsel_v1_biasrec_retryfix_20260509_055736` 在 q268 首次证明 pattern runtime 路径可以端到端修对。
- q302 也能触发 `grp-pat-toxicology-206-253-93286776`，但 EEA 输出 hint 只包含删除 `a2.element`，没有携带 `SELECT DISTINCT` 和 dependent JOIN cleanup。
- 这不是新的 case 特判，而是已有 WU5 设计没有贯通到 canonical/pattern action 与 post-selection hint：接入端真正消费的是 hint，不能依赖 EEA 内部未暴露的 rewrite contract。

实现变更：

- `common/runtime/action_compiler.py`
  - canonical/pattern 候选进入 `_annotate_canonical_candidates` 时传入当前 `RuntimeCaseView`。
  - 对 `DROP_SELECT_SLOT` 候选补充通用 alias cleanup 依赖：如果被删除 SELECT 表达式的表 alias 后续不再被引用，则允许并要求清理对应 JOIN，`required_edit_scopes` 加入 `JOIN`。
  - 对 `DROP_SELECT_SLOT` / `DROP_SIDE` 的 canonical 候选统一应用 `pair_role_side_output + no_distinct` 推导出的 `SELECT_ENFORCE_DISTINCT` 依赖，避免只在非 canonical 枚举路径生效。
- `common/runtime/runtime.py`
  - action brief 渲染时把结构依赖写入 raw hint：`SELECT DISTINCT` 和条件 JOIN cleanup。
  - hint instantiation 后增加 contract-preservation check：如果 LLM 简化 wording 时丢掉 raw hint 中的依赖义务，则自动补回对应通用依赖句。

验证：

- `python -m py_compile common/runtime/action_compiler.py common/runtime/runtime.py` 通过。
- 使用 q302 的真实 runtime request 与当前 run 的 `library_latest.json` 做本地 probe：
  - `status=ready`
  - matched pattern：`grp-pat-toxicology-206-253-93286776`
  - hint 包含：删除 `a2.element`、添加 `SELECT DISTINCT`、删除 alias 失依后的 JOIN block。
  - compiler action 的 `allowed_edit_scope` 为 `SELECT, JOIN`，不再只有 `SELECT`。
- 随后的真实 focus18 run 发现 q268 的 raw hint 正确，但 hint-instantiation LLM 把 JOIN cleanup 改成了“保留 a2 JOIN”。已补 contract-preservation：当实例化 hint 与 raw hint 的结构依赖相反时，最终 hint 回退到 code-rendered raw hint，避免 LLM 覆盖已绑定 contract。

决策：

- 保留该改动，作为 WU5 的通用结构依赖实现。
- 下一轮 focus18 验证重点看 q302 是否从“触发但 hint 不完整”变成“触发且 rewrite 可执行”，同时确认 q249/q268/q277/q285 的 pattern/singleton 触发不退化。

### E-20260505-01：保守 Pattern Admission

Baseline：

- 输出目录：`outputs/manual_pattern_family_probe_20260505_pattern_admission_full11_v1`
- 输入 singleton：`outputs/manual_pattern_family_probe_20260504_insight_case_local_v1`
- 范围：11 个有人工标注的数据库。

实验假设：

- 使用保守 admission。
- 优先要求稳定 shared program / repair interface。
- 如果只是语义相似但没有统一实例化接口，则保留为 family 或 singleton。

聚类流程：

- 粗筛：基于 family formation 信号、program compatibility 和 pair/component 生成候选。
- 语义判断：`pattern_admission_judge` 判断候选是否有稳定最小修复接口。
- Promotion：只有 admission 明确通过，才形成 formal pattern。

结果：

| TP | FP | FN | Precision | Recall | F1 |
|---:|---:|---:|---:|---:|---:|
| 34 | 7 | 173 | 0.8293 | 0.1643 | 0.2742 |

得到的高可信 pattern：

- `toxicology 206/249/253/268/277/285/302/307`：TP 28，FP 0。
- `formula_1 849/855/921`：TP 3，FP 0。
- `codebase_community 616/617`：TP 1，FP 0。
- `codebase_community 709/710`：TP 1，FP 0。

主要漏掉：

- `toxicology 198/201/207/263/269/306/326/328/335/338`：0/45 pair 覆盖。
- `formula_1 891/893/896/902/903/905/995`：0/21 pair 覆盖。
- 多个跨 SQL 路径但人工认为是强 pattern 的组没有升 pattern。

诊断：

- 精度可接受，但召回太低。
- 过度依赖 shared SQL repair path / core program signature。
- 能稳定产出较准 pattern，适合作为安全基线。

决策：

- 作为当前回退目标和高精度基线。
- 后续不能继续只靠这个方法解决跨路径强 pattern。

### E-20260505-02：Wide Recall + Partial Blocking

Baseline：

- 输出目录：`outputs/manual_pattern_family_probe_20260505_wide_recall_partial_block_v2`
- 范围：11 个有人工标注的数据库。

实验假设：

- 先宽召回，再用语义 `partial` 阻断不安全 promotion。
- 宁可形成 family，也不把不稳定接口升成 pattern。

观察结果：

- 基本没有 formal pattern promotion。
- 形成了不少看起来合理的 family，例如：
  - `toxicology 206/249/253/268/277`
  - `toxicology 263/269/335`
  - `formula_1 849/855/921`
  - `formula_1 891/902`
  - `student_club 1418/1422`
  - `codebase_community 616/617`

诊断：

- 这说明“粗筛后语义判断”之前已经尝试过。
- 问题不在于没有语义层，而在于 `partial` 太粗：真实强 pattern 有时需要有限分支，但当时被阻断。

决策：

- 不作为最终 promotion 策略。
- 保留经验：语义判断必须区分 `formal_pattern`、`bounded_branch_pattern`、`family_only`、`partial_uncertain`、`conflict`，不能只有 compatible/partial/conflict。

### E-20260505-03：Insight-First Canary

Baseline：

- `outputs/manual_pattern_family_probe_20260505_insight_first_canary_v1`
- `outputs/manual_pattern_family_probe_20260505_insight_first_canary_v2`
- `outputs/manual_pattern_family_probe_20260505_insight_first_canary_v3`
- `outputs/manual_pattern_family_probe_20260505_insight_first_canary_v4`

实验假设：

- 把 stable-bias insight 前移。
- 先按 source/target insight 分组，再把 SQL repair path 差异当作 branch evidence。

关键观察：

- `v3` 抓到了 `toxicology 207/326/328/338`，这是保守版漏掉的 scope-reroute 强 pattern 片段。
- `v3` 抓到了 `formula_1 893/902/905/995`，是 standings 强 pattern 片段。
- `formula_1 849/855/921` 保持稳定。
- `toxicology 206/249/253/268/277/285/302/307` 被拆成不完整子集，经常漏 `253/285/302`。

诊断：

- insight-first 能帮助发现跨 SQL 路径的强 pattern 片段。
- 但宽泛 stable bias 本身不足以定义 formal pattern。
- 缺少更严格的“修复契约”：source misconception、target preference、槽位绑定、branch policy、actionability。

决策：

- 保留为设计证据。
- 不继续通过扩大 slicer 或增加 prompt 直接修。

### E-20260505-04：Insight-First Full 11 DB

Baseline：

- 输出目录：`outputs/manual_pattern_family_probe_20260505_insight_first_full11_v1`
- 输入 singleton：`outputs/manual_pattern_family_probe_20260504_insight_case_local_v1`
- 范围：11 个有人工标注的数据库。

实验假设：

- 在 11 库上使用 insight-first candidate slicing。
- 允许不同 SQL program signature 作为 branch 进入 admission。

结果：

| TP | FP | FN | Precision | Recall | F1 |
|---:|---:|---:|---:|---:|---:|
| 33 | 76 | 174 | 0.3028 | 0.1594 | 0.2089 |

正向覆盖：

- `toxicology 207/326/328/335/338`：TP 10，FP 0。
- `financial 142/173`：TP 1，FP 0。
- `student_club 1418/1422`：TP 1，FP 0。
- `superhero 726/728`：TP 1，FP 0。
- `formula_1 849/855/921`：TP 3，FP 0。
- `formula_1 1001/1006`：TP 1，FP 0。

相对保守版的退化：

- `toxicology 206/249/253/268/277/285/302/307` 从完整 pattern 退化成 `206/249/268/277/307`。
- toxicology strict TP 从 28 降到 21，FN 从 47 升到 54。

典型误升：

- `codebase_community 593/600/639/672/686/693`：宽泛 “aggregated proxy -> canonical identifier”，FP 15。
- `european_football_2 1119/1120/1121/1126/1127`：人工 family-only，被升成 pattern，FP 10。
- `thrombosis_prediction 1167/1249/1271/1274/1308`：宽泛 canonical column，FP 10。
- `california_schools 23/28/81`：混合不同人工组，FP 3。

诊断：

- insight-first 没有提升整体 strict recall，反而显著降低 precision。
- 它把 family 级 insight 错误提升成 formal pattern。
- 宽泛语义如 `proxy -> canonical`、`row -> entity`、`canonical field` 只能做粗筛，不应直接 promotion。

决策：

- 不作为默认聚类策略。
- 回退到保守 admission。

### E-20260505-05：回退到保守 Admission 默认策略

Commit：

- 待本次实现提交。

实验目标：

- 先恢复高精度 pattern 聚类基线。
- pattern 少可以接受，优先保证后续 hint 准、trigger 准、rewrite 能修对。

实现变更：

- `core_program_signature_conflict` 不再作为 pattern 候选边。
- 默认 pattern candidate 不再调用 `insight_pattern_slicer`。
- 大组件按 `core_program_signature` 分桶后分别交给 `pattern_admission_judge`。
- `pattern_admission_judge` 改回保守 formal gate：宽泛 stable bias 不能压过 repair interface 冲突。
- 报告字段中 `insight_slicer_candidates` 置空，`pattern_candidate_generation_policy` 改为 conservative admission。

已完成验证：

- `python -m py_compile common/family_formation_v2.py common/prompts_v2/pattern_admission_judge.py common/prompts_v2/insight_pattern_slicer.py tests/test_canonical_program_v2.py` 通过。
- `PYTHONPATH=/data/liuyining/ace4sql pytest -q tests/test_canonical_program_v2.py` 通过：`121 passed, 5 warnings`。

未完成验证：

- focused toxicology canary `206/249/253/268/277/285/302/307` 启动后长时间等待 LLM admission 返回，已手动终止，未得到结果。
- 11 库离线复跑未在本 commit 前完成，避免把回退提交阻塞在模型侧。

预期下一步：

- 单独运行 11 库离线验证，确认结果接近 E-20260505-01。
- 如果保守结果恢复，再进入在线触发设计阶段。

### 当前总判断

下一轮不能重复两个极端：

- 太保守：SQL edit path / core program signature 主导，强 pattern 被拆散。
- 太宽松：stable bias / effect axis 主导，family 被误升成 pattern。

当前先回到高精度保守基线。后续真正要补的是“修复契约判断”，而不是继续给 insight-first 加 gate。

## 在线触发与日志实验

### E-20260506-01：下线 Family 与旧 Overlap 决策信号

Commit：

- 待本次实现提交。

实验假设：

- 当前收益目标只看 singleton 和 strict pattern。
- Family 级相似性会混淆“共同偏差”与“可实例化修复程序”，暂时不进入 runtime 或 promotion。
- 旧版 question/manifest/structural/slot overlap 只能说明表面相似，不能证明 shared repair program，应从决策路径删除。

实现变更：

- `form_offline_families()` 保持兼容入口名，但主流程改为 singleton -> strict pattern。
- evolved library 的 `experience_families` 固定为空；runtime trigger 不再扫描 `experience_families`。
- `focus_case_ids` 只作为“本轮由哪个新案例触发”的审计信息，不再裁剪参与聚合的 singleton 池、pattern 输出或 replay/promotion 候选；在线逐例进入时，每一步都用当前完整记忆前缀做演化。
- 重复 rediscover 的离线 pattern 按 case set + synthesized-program identity 去重，优先保留 runtime-usable 版本。
- 缺少 `canonical_repair_ir`、`repair_insight_signature`、effect candidate 的对象不再走 legacy fallback，保持 singleton 并在 pair audit 中记录 `signal_missing`。
- 删除 `legacy_question_overlap`、`legacy_manifest_overlap`、`legacy_structural_overlap`、`legacy_slot_overlap`、`shared_legacy_family` 对候选召回和 promotion 的作用。
- 删除旧 structural compatibility threshold 和 required slot disjoint 对 pattern/family 决策的作用。
- `pattern_admission_judge` 的主输入保留全量 case cards；pair 输入改为 relation counts + representative pairs，完整 pair decisions 不再进入 prompt。
- 新增可选 `EEA_LLM_TRACE_PATH`，记录 EEA LLM 调用 stage、prompt chars、line count、context 和重试结果。

验证计划：

- 先跑 py_compile。
- 再跑逐例进入的离线在线构建测试，检查每步 update 后 singleton/pattern 变化、pattern admission blocker 和 LLM trace。
- 若 strong pattern 仍不形成，优先检查是否是 effect/insight 抽取缺失，而不是恢复旧 overlap。

已完成验证：

- `python -m py_compile` 覆盖本次改动的 EEA 核心文件，通过。
- 使用已有 toxicology singleton 中间库，按 `206 -> 249 -> 253 -> 307` 模拟逐例进入：
  - 修复前：`focus_case_ids` 把聚合池裁剪成当前单例，每一步都是 `candidates=0`、`patterns=0`。
  - 修复后：step2 形成 `206/249` pattern，step3 形成 `206/249/253` pattern，step4 形成包含新案例的 `206/249/307` pattern 候选；说明在线累计已经不是一例一聚，而是前缀库整体演化。
- 旧 pytest 中仍有大量 family 语义断言；本轮只确认非 family 基础用例通过，旧 `candidate_family_count >= 1` 断言按当前设计应废弃。

### E-20260506-02：wrong59 低触发与日志膨胀改造

Baseline：

- 输入：`toxicology145_baseline_wrong59_cases_20260506.json`。
- 旧输出目录：`method/deepeye/DeepEye-SQL/workspace/rulebook_runs/toxicology_wrong59_postsel_v1_qwen3next_quick_20260506_r1`。
- 旧现象：58 条已处理时 `runtime ready=2 / no_match=56`，结果目录约 `1GB`，`per_case_log.jsonl` 约 `262MB`。

问题拆解：

- 低触发主要发生在 runtime match/compiler 之前或 pattern 尚未 runtime usable，不是 post-selection guard 本身造成。
- 旧 run 仍出现大量 `experience_families`，不代表当前 strict singleton/pattern 路径的最终行为。
- 日志变慢来自在线 update 后把完整 `local_evolve.report`、promotion replay rows、trigger/compiler audit 反复写入 case 级 response 和 `per_case_log.jsonl`。
- guard 有必要保留，但必须和 action contract 自洽；例如 drop select slot 伴随 join cleanup 时，guard 必须允许 `SELECT/JOIN`，不能只允许 `JOIN`。

本次实现决策：

- EEA runtime 在 hint/guard 产出前归一化 action scope：`allowed_edit_scope` = primitive 自身必改 scope + candidate `required_edit_scopes` + repair program dependency scopes。
- 只对 replay 通过且有 synthesized program 的 `pattern` 启用 required-signal soft miss：required signal 漏匹配时允许进入 binder dry-run 兜底；`singleton` 仍保持 exact trigger，不做泛化。
- EEA runtime 增加 `runtime_audit_summary`，只记录 matched ids、top blocker、compiler action summary、guard summary，不再要求接入端消费完整对象。
- EEA evolution 增加 `compact_evolution_report`，供在线接入端记录 counts、pattern 摘要、promotion 摘要，避免把完整 replay 报告嵌入每个 update response。
- DeepEye post-selection adapter 改为默认写 compact runtime/update/finalize audit；SQL 和 execution comparison 只保留 hash/count/summary。

待验证：

- 冷启动重跑 wrong59，检查前 20 条是否不再长期 `ready=0`。
- 重点看 `221/223/249/253/268/277/307`：应出现 matched pattern 或明确 compact blocker。
- `per_case_log.jsonl` 不应再随 prefix 膨胀到数百 MB。
- q249 的 rewrite 如果只删除输出列和对应 join，不应再因 guard 缺少 `SELECT` 被拒绝。

### E-20260507-01：Branch-Level Runtime Trigger 与性能优化实现

Commit：

- 待本次实现提交。

背景：

- `toxicology_wrong59` 冷启动中，pattern 积累后仍大量 `no_match`。
- 已有 formal pattern 内部可能包含不同 lowering branch，例如是否需要删除
  join、是否需要 distinct、是否只删输出列。
- 旧 runtime 只做 group 级触发，pattern 一旦有多个 bundle/branch，compiler
  仍能看到 sibling branch，导致两类问题：
  - 触发端不知道当前案例应该落到哪个 branch。
  - compiler 端可能枚举或选择不属于当前分支的动作。

实现变更：

- `ProgramEnvelope` 新增 `runtime_branches`。
- shared program synthesis 在 `lowering_branches` 之外生成 runtime branch contract：
  - branch id 与 bundle ids。
  - answer-blind required/negative signals。
  - allowed primitives / allowed edit scopes。
  - preserve/audit constraints。
  - 初始 `runtime_usable=false`，等待 replay promotion 决定。
- promotion 阶段把 formal replay 结果写回 branch：
  - 从 formal replay rows 中按 branch `support_case_ids` 切出 branch 级 replay
    子集。
  - 只有该 branch 的 replay 子集覆盖完整 support、全部 formal row 通过、
    compile/improvement/regression/action-count 满足阈值，branch 才标记
    `runtime_usable=true`。
  - branch runtime usable 不再被整组 pattern promotion 一票否决；整组 formal
    blocker 只记录在 `pattern_level_blockers` 中。
  - pattern 顶层 `runtime_usable` 必须来自至少一个 runtime-usable branch。
  - 未通过时，branch 保留 `runtime_blockers`，runtime 不可选择。
- runtime 触发从 group-level 下钻到 branch-level：
  - pattern 先过原有 trigger/applicability gate。
  - 再只在 runtime-usable branch 里匹配当前信号。
  - 每个候选 branch 必须通过 binder dry-run，并覆盖 branch 的 required bundles。
  - 0 个 branch 命中：no match。
  - 多个 branch 命中：`branch_selection_ambiguous`。
  - 唯一 branch 命中：复制出 branch-scoped pattern，只保留该 branch 的 bundles。
  - branch-scoped pattern 同时裁剪 source antipatterns、target effects、
    target invariants、required role slots、negative guards 和 program ops，
    避免 sibling branch 影响当前触发与 compiler prompt。
- `prepare_rewrite_plan()` 在 compiler 前再次过滤 candidate sets：
  - 非 pattern 候选不变。
  - pattern 候选只保留 selected branch bundle 对应 candidates。
- guard 策略调整：
  - scope/preserve 约束改为 audit-only。
  - EEA 输出 `audited_allowed_edit_scope`、`audited_must_preserve_tables`、
    `audited_must_preserve_predicates` 供分析。
  - 传给接入端硬 guard 的 `allowed_edit_scope` 暂时放宽为全 scope，
    `must_preserve_*` 置空。
  - parse/schema/execution 安全网仍由接入端保留。
- ActionCompiler prompt 输入收窄：
  - schema summary 改为 candidate-linked schema。
  - 只传当前 SQL role refs、candidate arguments 涉及的表列、相关 FK/PK edges 和
    semantic hints。
  - 没有可抽取 refs 时才 fallback 到全 schema。
- 性能优化：
  - pair score 按 singleton 信号/契约 hash 缓存。
  - promotion replay 按 memory hash + holdout + replay mode 缓存。
  - runtime binder dry-run 按 group/case/pred SQL/contract hash 缓存。
  - insight slicer pair decisions 从最多 120 条改为按 relation 与 case coverage
    选代表性样本，上限 40 条。

当前验证：

- `python -m py_compile common/data_structures_v2.py common/shared_program_synthesizer_v2.py common/promotion_v2.py common/runtime_v2.py common/llm_nodes_v2.py common/family_formation_v2.py` 通过。

待验证：

- 用 toxicology wrong59 冷启动重跑，检查 ready 覆盖是否提升。
- 检查 `trigger_result.selected_branch_ids`、`branch_selection_audit` 是否能解释
  `no_match`、`branch_selection_ambiguous`、compiler no action。
- 重点检查 `206/249/253/268/277/285/302/307` 是否能在 pattern 形成后命中正确 branch。
- 检查日志体积和后期速度，确认缓存与 prompt 收窄有效。

## Pattern 触发实验

本文节记录 pattern runtime trigger 的审计实验。目标不是证明端到端收益，而是把问题拆成：pattern 是否已形成、未见同组案例是否能触发、近似噪声是否误触发、触发后是否能枚举出可实例化动作。

### T-20260505-01：manual pattern LOO trigger smoke / toxicology

- 代码版本：新增 `cli/evaluate_manual_pattern_trigger_loo_v2.py` 后的本地版本。
- 评估模式：`project_existing_library`。
- 输入 library：
  `outputs/manual_pattern_family_probe_20260505_pattern_admission_full11_v1/toxicology/library_families.json`
- 输入 work root：
  `/data/liuyining/ace4sql/method/deepeye/DeepEye-SQL/workspace/rulebook_runs/rulebook_single_db_toxicology_full_c24_primaryedit_jponly_20260410_083736/.state/work`
- 输出目录：
  `outputs/manual_pattern_trigger_loo_smoke_20260505_toxicology_fullwork`

命令：

```bash
python cli/evaluate_manual_pattern_trigger_loo_v2.py \
  --manual_groups_json /data/liuyining/ace4sql/method/EEA/doc/db_pattern_groups.json \
  --library_root outputs/manual_pattern_family_probe_20260505_pattern_admission_full11_v1 \
  --library_filename library_families.json \
  --bird_db_root /data/liuyining/ace4sql/bench/bird/dev/dev_databases \
  --db_ids toxicology \
  --work_root /data/liuyining/ace4sql/method/deepeye/DeepEye-SQL/workspace/rulebook_runs/rulebook_single_db_toxicology_full_c24_primaryedit_jponly_20260410_083736/.state/work \
  --max_noise_targets_per_positive 2 \
  --max_noise_memory_groups 8 \
  --output_dir outputs/manual_pattern_trigger_loo_smoke_20260505_toxicology_fullwork
```

结果摘要：

- `positive_trials = 8`
- `positive_hits = 8`
- `positive_trigger_recall = 1.0`
- `positive_bindable_hits = 8`
- `noise_trials = 16`
- `noise_false_triggers = 0`
- `noise_false_trigger_rate = 0.0`
- `skipped_trials = 14`

有效覆盖的人工 pattern：

- toxicology pattern 3：
  `206 / 249 / 253 / 268 / 277 / 285 / 302 / 307`
- 这组当前已存在 formal pattern memory：
  `grp-pat-toxicology-206-307-10c45185`
- LOO 下 8 个 held-out target 都触发该 pattern。
- 每次触发后都有 compiler candidate：
  `DROP_SELECT_SLOT = 1`，`DROP_SIDE = 1`。
- 相似噪声 target 没有误触发该 pattern。

跳过原因：

- `no_existing_pattern_memory_for_held_in_cases = 14`
- 含义：toxicology 另外 3 个人工 formal pattern 当前没有形成对应 pattern memory，所以这轮不是 runtime trigger 没触发，而是构建阶段没有可触发对象。

阶段性结论：

- 对于当前保守构建已经形成的强 pattern，runtime trigger 在同组未见案例和近似噪声下表现稳定。
- 当前主要瓶颈不是这组 pattern 的 trigger gate，而是 formal pattern 构建覆盖率太低。
- 下一步触发设计应继续使用这个审计脚本区分两类问题：已形成 pattern 的 runtime 泛化能力，以及未形成 pattern 的构建覆盖缺口。

### T-20260505-02：修复在线累计更新的演化语义

背景：

- 离线聚类测试可以形成 toxicology 强 pattern，例如
  `206 / 249 / 253 / 268 / 277 / 285 / 302 / 307`。
- 但 post-selection 冷启动在线跑时，前面错例已经累计进库，后续 case 仍经常 `no_match`，并且在线 `library_snapshots` 中长期 `patterns = 0`。

定位：

- DeepEye adapter 在每个错例后调用 `update_from_selected_sql()`。
- 该函数内部确实会调用 local evolve，但之前把 `focus_case_ids={当前 case}` 传给 EEA 聚合函数。
- EEA 聚合函数把 `focus_case_ids` 当成候选过滤器使用，导致本轮演化只看当前 singleton，看不到历史 singleton。
- 因此在线流程没有等价于“对当前前缀库重新跑离线构建”，而是退化成“每次只对当前 singleton 单独构建”。

修复：

- adapter 的 local evolve 改为对整个 `LibraryStateV2` 前缀库运行 `evolve_library_with_replay()`。
- `focus_case_ids` 只保留为审计字段，表示本轮由哪个新 case 触发。
- `evolve_library_with_replay()` 修正为同时消费 formation 产出的 pattern candidates 和 family candidates。此前它只 replay `experience_families`，导致 `form_offline_families()` 明明能形成 pattern，在线 evolve 却把 pattern candidate 丢掉。
- post-selection runner 在调用 update 前先落盘当前 case 的 `eea_update_request.json`，保证 replay loader 能读到当前 case 的 `gold_sql`。
- runner 向 `update_from_selected_sql()` 传入当前 `.state/work` 作为 `work_root`。
- 当 `work_root + db_path` 可用时，在线 local evolve 使用 `replay_gated`；否则只保留累计 singleton，不把离线 family/pattern 强行放到 runtime。
- runtime 多 memory 兼容选择改为比较语义 action contract，忽略 `case_id / bundle_id / op_id` 等来源标识，避免同一修复程序因来源不同被误判为 `conflicting_action_contracts`。

待验收：

- 用 toxicology 强 pattern 序列按 case 顺序模拟冷启动在线输入。
- 重点检查 `206 -> 249 -> 253 -> 307` 后，库内是否出现 replay-gated runtime-usable pattern/family。
- 再跑 post-selection toxicology 子集，确认 q253 不再因为只能看到多个 singleton 或 action contract 来源差异而被全挡。

当前快速验证：

- 使用真实 post-selection work files 从空库模拟 `206 -> 249 -> 253` 在线累计。
- `249` 累计后，local evolve 看到 `candidate_pattern_count = 1`，并把 `grp-pat-toxicology-206-249-b5991530` replay-gated 成 `runtime_family_replay_gated`。
- `253` 累计后，local evolve 继续运行，库内有 2 个 family、3 个 singleton。
- 用该前缀库测试 q268，runtime 返回 `ready`，匹配 `grp-pat-toxicology-206-249-b5991530`，并产出删除冗余 `a2.element` 的 hint。
- 当前三例前缀仍未稳定升为 formal pattern，而是 runtime family；这属于 pattern admission / formal promotion 的下一阶段，不再是“累计后只看当前 singleton”或“evolution 丢掉 pattern candidate”的更新语义问题。

### T-20260507-01：branch runtime 后真实短序列验证

背景：

- 按用户要求恢复本地 API key 后，使用真实 Qwen3/OpenRouter 配置跑 toxicology 强 pattern 小序列。
- 目标不是全库性能，而是验证“新案例逐个进入后，206/249 是否能积累出可触发记忆，253 是否能稳定触发并修对”，同时混入 221/252 作为干扰。

先发现的测试脚本问题：

- `run_online_e2e_validation_v2.py` 之前把 `--case_ids` 解析成 `set`，再按 qid 数字排序。
- 因此传入 `206,249,253,221,252` 时，实际顺序会被改写，破坏在线累计测试语义。
- 已修复为保留用户输入顺序，并在 `summary.json.config.case_id_order_effective` 和 `missing_requested_case_ids` 中记录真实执行顺序和缺失 qid。

运行命令摘要：

```bash
env PYTHONPATH=/data/liuyining/ace4sql \
  EEA_LLM_TRACE_PATH=outputs/branch_runtime_smoke_toxicology_20260507_v6/llm_trace.jsonl \
  RULEBOOK_LLM_HARD_TIMEOUT_SECONDS=120 \
  python cli/run_online_e2e_validation_v2.py \
  --db_id toxicology \
  --work_root /data/liuyining/ace4sql/method/deepeye/DeepEye-SQL/workspace/rulebook_runs/rulebook_single_db_toxicology_full_v2stack_phase15e_20260426_052756_retry/.state/work \
  --output_dir outputs/branch_runtime_smoke_toxicology_20260507_v6 \
  --case_ids 206,249,253,221,252 \
  --family_runtime_policy replay_gated \
  --promotion_interval 1 \
  --promotion_min_support 2 \
  --max_neighbor_edges 5 \
  --strict_contract_policy continue \
  --save_library_snapshots
```

结果摘要：

- 有效执行顺序：`206 -> 249 -> 253 -> 221 -> 252`，无缺失 qid。
- `total_cases = 5`
- `baseline_equivalent_count = 0`
- `final_equivalent_count = 1`
- `net_improvement = +1`
- `improved_cases = [253]`
- `regression_cases = []`
- `ready_cases = 1`
- `triggered_cases = 1`
- 最终库：`patterns = 0, experience_families = 0, singletons = 4`

逐题结论：

- `206`：无记忆可触发，最终错误，积累为 singleton。
- `249`：无记忆可触发，最终错误，积累为 singleton。
- `253`：触发 `grp-sing-toxicology-249`，rewrite 成功，最终等价。
- `221`：未触发，最终错误，积累为 singleton。
- `252`：未触发，最终错误，积累为 singleton。

关键诊断：

- 206/249 没有形成 pattern，瓶颈发生在构建阶段，不是 runtime branch trigger。
- `local_evolve_after_qid_249.json` 中，206/249 的边被判为 `case_local_insight_conflict`，虽然粗召回理由包括 `shared_action_lowering_family / shared_effect_axis / shared_output_shape_delta`。
- 具体原因来自 case-local insight 抽取不稳定：
- 206 被描述成 “drop extra output column and distinctify”，程序包含 `SELECT_DROP_SLOT + SELECT_ADD_MODIFIER(DISTINCT)`。
- 249 被描述成 “drop extra output side preserving scope”，程序只包含 `SELECT_DROP_SLOT`。
- 两者实际共享核心偏差是“模型把 bond 的两个端点都输出为两列，而 gold 偏好保留 canonical atom-side 的一列”，但 DISTINCT 被抽成了核心差异，导致强正例被拆散。
- 253 能修对说明 runtime singleton 触发和 action compiler/rewrite 链路可工作，但这不是目标中的 formal pattern 收益。

性能诊断：

- 多数 case 的 `wrong_case_auditor` 约 7-8k chars，`error_instance_extractor` 约 22-23k chars。
- `253` 的 `action_compiler` prompt 达到 `171,789 chars`，明显异常膨胀。
- 这说明 compiler 阶段仍传入了过大的 schema/candidate/memory payload；后续必须精简为候选相关的必要 schema summary 和已选 memory contract，否则全库在线会越跑越慢且 token 成本过高。

下一步方向：

- 构建侧需要把 case-local insight 区分为“核心修复偏差”和“附属实现条件”。例如 DISTINCT 应从修复轨迹保留为 branch/action accessory，而不应阻止 206/249 合并。
- pattern admission 应允许同一核心程序下存在 accessory action variation，但 runtime branch selection 必须在新案例上重新判定是否需要 DISTINCT。
- ActionCompiler prompt 要做硬预算与内容审查，优先检查为什么单个 singleton 触发也会生成 17 万字符 prompt。

### T-20260507-02：rewrite contract 与 prompt 精简

背景：

- 在 `branch_runtime_smoke_toxicology_20260507_v7` 中，q253 已经正确触发 `grp-sing-toxicology-249`，ActionCompiler 也选出了删除冗余输出侧的动作。
- 但旧 `memory_rewrite` prompt 仍把动作、完整 schema 上下文和较多审计信息混在一起交给 LLM，LLM 返回了未修改 SQL，并把“删除 `a2` 的 JOIN”误判为不安全。
- 这说明当前主要问题不是 q253 的触发，而是 rewrite 阶段的职责边界不清：LLM 同时被要求理解 pattern、选择动作、判断依赖、改 SQL，容易拒绝已经被前序阶段绑定好的编辑。

本轮修改：

- 新增 `rewrite_contract`：runtime 在调用 rewrite 前，把已选 actions 编译成一个小合同，明确说明触发、记忆选择、branch 选择、候选动作选择和参数绑定都已经完成。
- `memory_rewrite` prompt 改成只接收 `S0 + rewrite_contract + minimal schema_context`，不再接收完整 actions JSON 和完整 local schema。
- 对 `DROP_SELECT_SLOT`，合同会绑定当前 SQL 中要删除的 SELECT 表达式，并根据该表达式 alias 绑定可删除 JOIN block；例如 q253 绑定：
  - 删除 `a2.element`
  - 删除 `JOIN atom a2 ON c.atom_id2 = a2.atom_id`
  - 标记 `a2` 在删除 SELECT 后没有外部引用
- 增加 `required_absence_checks`：如果 LLM 声称完成删除，但 rewrite_sql 中仍包含被删除文本，则代码 fail-closed 回退原 SQL。
- 增加 `prompt_payload_audit`：记录 rewrite prompt 中各 payload 的字符数、顶层 key 和 sha1，便于后续定位 token 膨胀。
- `ActionCompiler` prompt 删除不必要的 full trigger contract / full trigger signature / full guardrails / full program envelope，只保留触发后的匹配摘要、核心接口、候选动作和压缩 program summary。
- 把 DISTINCT、target-only predicate、ranking 等从 root pattern 身份中降为 `branch_accessory`，用于后续 branch/action 选择，不再把同一核心错误强行拆成不同 pattern。

重要边界修正：

- 本轮删除了 rewrite 后的确定性 SQL 修补路径。
- 代码不再在 LLM 返回后直接执行 `SELECT_ENFORCE_DISTINCT`、补 WHERE、重建 join route 或自动 alias rebind。
- 现在代码只负责生成合同和校验合同是否真实落地；如果 LLM 没执行合同，系统回退原 SQL，不由代码替 LLM 改 SQL。
- scope guard 也改成 fail-closed，不再“恢复局部 SELECT 后继续保留部分 rewrite”。

真实 probe：

- 输出目录：`outputs/rewrite_contract_q253_probe_20260507_r2`
- q253 的 rewrite 结果：

```sql
SELECT DISTINCT a1.element
FROM bond b
JOIN connected c ON b.bond_id = c.bond_id
JOIN atom a1 ON c.atom_id = a1.atom_id
WHERE b.bond_type = '#'
```

- 这条 probe 说明新的 rewrite_contract prompt 可以让 LLM 正确执行删除冗余输出侧和冗余 JOIN 的合同。
- 但 trace 中 `edit_kind` 仍为空字符串，虽然 SQL 和 absence checks 通过；后续如果要依赖 trace 做更强审计，需要让 prompt 或 parser 更严格。

prompt 规模观察：

- q253 rewrite payload 从旧 prompt 约 `12703 chars` 降到新 prompt 约 `4325 chars`。
- q253 `rewrite_contract` JSON 约 `2158 chars`，`schema_context` 约 `241 chars`。
- ActionCompiler 的 memory objects payload 已去掉完整 trigger/guard/program envelope，但 candidate sets 仍可能偏大，后续 token 膨胀优先查候选动作集合与 schema summary。

未解决问题：

- 本轮没有解决 pattern runtime usable / formal promotion 不稳定的问题。
- `outputs/rewrite_contract_online_toxicology_206_249_253_20260507` 的在线短序列在 q206 的上游 LLM 调用阶段卡住，未形成有效在线结论；这不是 rewrite_contract 的结果。
- 后续需要继续把触发、pattern admission、branch matching 单独验证，避免把 rewrite 成败和构建成败混在一起分析。

### T-20260507-03：online evolution 膨胀与低触发诊断修复

背景：

- DeepEye 端的 wrong59 冷启动 run `toxicology_wrong59_postsel_v1_qwen3next_quick_20260507_r3` 到 19/59 时仍然 `ready=0/no_match=19`，且越跑越慢。
- LLM trace 显示慢点不在 rewrite：已完成 case 没有进入 rewrite，`rewrite attempted=0`。
- 主要调用集中在离线更新后的演化阶段：`shared_insight_judge=48`、`pattern_admission_judge=12`。
- 最大 prompt 来自 `pattern_admission_judge`，单次输入达到约 6-14 万字符；典型原因是把多个 singleton 的完整 canonical IR、synthesized program、trigger contract、pair context 一起塞进 admission prompt。

本轮判断：

- 问题不是“需要更多真实测试用例”，而是当前在线演化实现和理想流程有偏差：每个新错例进入后确实会触发局部 evolve，但 evolve 内部对 active singletons 的候选 pair 和 admission prompt 没有在线预算边界。
- 当前应保留“逐例进入，每例后整体更新”的流程，但整体更新不等于每轮把全库所有 singleton 做全量两两 LLM 审查。
- 正确边界是：代码先用 case-derived 的抽象信号产生高召回候选 pair；只有候选 pair 才进入共享程序/语义判断；pattern admission 只看代表性 compact cards 和关系统计，不看完整历史 payload。

本轮修改：

- `family_formation_v2` 新增 compact `evolution_card`，只包含：
  - `db_id`
  - `case_ids`
  - primary effect core
  - delta axes
  - output shape direction/grain/subset
  - canonical lowering families
  - repair insight interface
- pair cache key 改为基于 compact card，不再 hash 完整 `formation_signals` 和完整 `trigger_contract`。
- 在线 evolve 有 `focus_case_ids` 时，只比较新进入 case 对应 singleton 与索引召回的历史候选；不再每轮全库 O(n^2) 打分。
- 无 focus 的 final/offline evolve 仍会在抽象索引桶内产生全部候选 pair，但不会枚举完全无共享抽象信号的 pair。
- pattern admission case card 改为 compact view：
  - 保留 stable bias frame、effect、shape delta、core/dependency signature、repair insight 和 program core。
  - 删除每个 op 中巨大的 `operation_signature` 全量结构，只保留 op_type/locus/lowering/is_dependency/required/role_delta/slot_signature/invariants。
- pair decision context 改为“关系计数 + 每类少量代表 pair + covered/uncovered case ids”，不再为了覆盖每个 case 额外加入大量 pair payload。
- pattern admission 增加 prompt budget：超过预算时只采样前几个 case card，并把全量 case ids、sampled ids 和 sampling policy 写入 summary，避免因为大 component 造成 10 万字符级 prompt。
- 审查后补充修正：超预算采样不再简单取最早 qid，而是优先覆盖 representative pair 涉及的 case，再用稳定顺序补齐，避免长序列里新进入 case 或关键边完全不在 LLM 可见证据中。
- `signal_summary_v2` 新增 memory compaction：
  - formation_signals 中的 `canonical_repair_ir` 改为 compact IR。
  - formation_signals 中的 `synthesized_program` 改为 compact executable program，保留 ops/envelope 必要字段，但压缩 role refs、effect/insight、branch/accessory 信息。
  - trigger_contract.action_contract 不再重复保存完整 `canonical_repair_ir`、完整 `synthesized_program`、完整 `program_envelope`，改为 summary 字段。
- `repair_program_normalizer_v2` 中每个 canonical op 的 `role_refs` 只挂 output-side refs；predicate/join raw refs 不再复制到每个 op，相关信息保留在 operation signature 的 delta 摘要中。

只读 sanity：

- `py_compile` 通过：`family_formation_v2.py`、`signal_summary_v2.py`、`accumulate_v2.py`、`repair_program_normalizer_v2.py`。
- 用 r3 已有 `.state/library_latest.json` 中第一个 singleton 做只读验证：
  - compact canonical IR 可被 `CanonicalRepairIR` 校验，示例体积约 `65989 -> 36562 bytes`。
  - compact synthesized program 可被 `CanonicalRepairProgram` 校验，示例体积约 `88583 -> 68681 bytes`。

预期影响：

- wrong59 这类长序列中，每轮 local evolve 的 pair 数应明显低于 active singleton 全量两两组合。
- pattern admission 的 prompt 不应再出现 6-14 万字符级别输入。
- 这轮修改不改变“什么是可合并 pattern”的核心语义，只减少候选枚举和 LLM 输入膨胀；如果后续仍低触发，应继续看 trigger/branch 匹配本身，而不是先怀疑 rewrite。

补充诊断输出：

- EEA formation report 新增 `retrieval_audit`，用于定位每轮 local evolve 的候选召回效果：
  - 当前 focus case / focus singleton 是谁。
  - focus card 的 effect core、delta axes、shape key、lowering families、repair insight interface。
  - focus 生成了哪些 retrieval keys。
  - 每个 key 命中了哪些历史 peer singleton。
  - 哪些 pair 被召回、召回理由是什么、是否进入 score、score 后被什么 blocker 挡住。
  - focus 完全没召回时记录 `unrecalled_focus_summary`。
- DeepEye adapter 会把该审计单独落到每个 case 目录的 `eea_retrieval_audit.json`，同时保留在 `eea_evolution_detail.json` 内。
- DeepEye adapter 另新增 `eea_update_timing.json`，记录：
  - `accumulate_seconds`
  - `local_evolve_seconds`
  - `diagnostic_write_seconds`
  - `total_update_seconds`
- 后续判断低触发时优先按三段定位：
  - `eea_retrieval_audit.json` 无候选：信号召回问题。
  - 有候选但 blocker 多：pair score / shared program / admission 问题。
  - 有 pattern 但 runtime 不触发：trigger / branch matching / compiler 问题。

### T-20260507-04：focus 召回补齐与 runtime 两类信号分层

背景：

- DeepEye 端 run `toxicology14_postsel_v1_qwen3coderflash_20260507_r1` 最终 `ready=0/no_match=14`，没有任何 rewrite。
- q225 之后 6 个 online update 均报 `KeyError: ('grp-sing-toxicology-211', 'grp-sing-toxicology-223')`。
- q307 对 q206 的 runtime 审计显示 `variant_required_signals_matched=true`、`binder_dry_run_success=true`，但仍被 `required_role_slots_unbound` 挡成 `no_match`。

本轮判断：

- `KeyError` 是在线加速后 focus pair map 不完整导致的实现问题：focus 召回只算了新 case 相关 pair，但 pattern 构建/报告阶段仍假设 component 内所有 pair 都已存在。
- q307 漏触发是 runtime 把两类信号混用：source-state 已匹配，但 target/action slot 预绑定失败被当成触发硬门。

本轮修改：

- `family_formation_v2` 保留 focus 召回加速，但新增 component/admitted group 内 all-pairs 局部补齐。
- `_central_member()`、`_member_pair_scores()`、`_build_family()` 都会先补齐本地 group 内 pair score，不再直接假设 focus `pair_scores` 完整。
- formation report 增加 `formation_audit` 和 `formation_pair_scope`，区分 retrieval 召回 pair 与 component 内补齐 pair。
- `runtime_v2` 新增 `source_trigger_passed`、`hard_gate_reasons`、`deferred_instantiation_reasons`、`compiler_candidate_reasons`。
- `required_role_slots_unbound` 与 singleton 的 `singleton_requires_unique_action` 在 source-state 已通过时降级为 deferred instantiation reason，交给 compiler/rewrite 后续阶段处理。
- 审查后补充收窄：compiler dry-run 不再整体 deferred，只对白名单中的实例化候选问题延迟；bundle budget 超限、branch ambiguity、exception 等仍保留 hard gate。
- pattern branch selection 改为先按 branch source signals 选唯一分支；branch binder/bundle 候选问题只进入 branch 的 deferred diagnostics，不再把 source 唯一匹配的 branch 直接挡掉。
- db/status/runtime_usable/negative signal/source-state mismatch/branch ambiguity/executable contract 缺失仍是 hard gate。

只读 sanity：

- `py_compile` 通过：`family_formation_v2.py`、`runtime_v2.py`、`data_structures_v2.py`。
- 用 r1 旧库和 q307 真实请求跑 EEA runtime plan，q307 现在选中 `grp-sing-toxicology-206`：
  - `reason=ready`
  - `action_count=1`
  - `primitive=DROP_SELECT_SLOT`
  - `hint_len=83`
  - `gate_passed=true`
  - `source_trigger_passed=true`
  - `deferred_instantiation_reasons` 包含 `required_role_slots_unbound`
  - `hard_gate_reasons=[]`
- 用 r1 旧库中的 q211/q223 singleton 调 `_build_family(..., pair_scores={})`，能自动补齐 pair 并构建成功，不再依赖外部完整 pair map。

预期影响：

- toxicology14 冷启动中 q225 以后不应再因缺少旧 pair score 导致 update error。
- q307 不应再被 q206 的 role slot 预绑定挡成 `runtime=no_match`；若后续失败，应落在 compiler/no_action、rewrite 或 selection，并有明确诊断。

### T-20260508-01：root pattern / branch runtime 语义修正

背景：

- wrong59 端到端结果显示：库内可以积累 pattern 候选，但 formal replay/promotion 全失败，runtime 大量仍落在多个同类 singleton 或 `conflicting_action_contracts`。
- 对照本账本前文和 `expert_report_3.md` 后确认：两段信号、branch runtime、pattern root-first 都已经讨论过，但代码主路径仍有偏差。
- 偏差包括：
  - component 已召回成员后，代码仍按 `core_program_signature` 先切桶，再让 LLM admission 决定子集。
  - component 内未被 admission 显式列出的 case 会静默留在 `uncovered_case_ids` / `retrieved_but_not_admitted`，没有 root closure。
  - pattern 顶层 formal replay 失败会使整个 pattern runtime 不可用，即使部分 branch 可能可编译、可 replay。
  - 多个同 root singleton 通过时，runtime 仍可能因为 action contract 不同直接 `conflicting_action_contracts`。

本轮原则：

- Pattern 不是最终执行单元，也不只是粗筛索引；它是共同 `root bias / shared misconception / target preference` 容器。
- Branch 是 runtime 执行单元；实例化、compiler candidate、rewrite hint 均应以已选 branch 为准。
- Pattern 整体 replay 只作为诊断指标，不再一票否决所有 branch。
- Root 信号决定归属，branch/action/accessory 信号决定实例化路径。

本轮修改：

- `family_formation_v2` 将 pattern admission 改为 root-first：
  - `core_program_signature_conflict` 可以进入 root pattern review，作为 branch evidence，而不是预切分。
  - component 不再按 core signature 先切桶；LLM 先审查整个 root component。
  - admission 后执行 generic mechanical branch closure：如果未被显式排除的 component 成员与 accepted seed 有 case-derived root pair evidence，则闭包进 root pattern，并标为 branch 待分配。
  - 自动补齐 branch specs 覆盖所有 root-admitted cases；这些 specs 是 admission 审计合同，runtime branch 仍由 repair program synthesis + replay gate 决定。
  - formation report 增加 `root_membership_status_by_case`、`retrieved_but_not_admitted_case_ids`、`mechanical_branch_closure_added_case_ids`、`mechanical_branch_spec_added_case_ids`。
- `pattern_admission_judge` prompt 改为先判断 root，再分 branch；明确禁止因为 DISTINCT、join cleanup、route/grain/action path 差异直接拆 root。
- `promotion_v2` 改为 branch-level runtime gate：
  - branch 可用性由该 branch support rows 的 compile/replay/regression/action-count 决定。
  - `pattern.runtime_usable = any(runtime_usable_branch)`。
  - pattern formal blocker 保留为 `pattern_level_blockers`，不再自动否决 branch。
  - 只有 runtime-usable branch 的 support case 对应 singleton 会被 supersede；未通过 branch 的 singleton 保持 active。
- `runtime_v2` 在多个候选 action contract 冲突前增加 root-bias contract 合并：
  - 如果多个候选同 root，只保留排序最高的 root-compatible 候选，不再全挡成 `conflicting_action_contracts`。
  - pattern 仍先选唯一 runtime branch，再将 branch-scoped memory 交给 compiler。
- `evolution_v2` compact report 增加 branch runtime 摘要，方便查看 runtime-usable branch 数量和被 supersede 的 case ids。

TODO / 自检项：

- [x] 检查 strong pattern 组是否不再因 `core_program_signature` / DISTINCT / join cleanup 被提前拆散：代码不再按 core signature 预切桶，action/path 差异交给 branch。
- [x] 检查每个 component member 是否都有 `root_membership_status_by_case`，不得 silent omission：admission response 与 formation report 均记录 accepted/excluded/closed/retrieved-not-admitted。
- [x] 检查 closure 是否只使用 case-derived pair evidence，不能出现 db/table/qid 规则：closure 只读 pair score 的 root membership 证据和 LLM membership，不读具体案例枚举。
- [x] 检查 branch specs 是否覆盖 admitted root cases；未覆盖必须报告 blocker：缺口进入 `mechanical_branch_spec_added_case_ids` / status，而不是静默丢弃。
- [x] 检查 promotion 结果中 branch runtime 可用性是否独立于 pattern formal blocker：整组 formal blocker 只保留为 `pattern_level_blockers`，branch runtime 独立计算。
- [x] 检查只有 runtime-usable branch support 的 singletons 被 supersede：`integrate_promoted_groups` 只废弃 runtime-usable branch support cases。
- [x] 检查 runtime 是否先 root，再 branch，再 compiler；多个同 root singleton 不应再直接互相挡：root-bias contract 一致时保留 top root-compatible candidates，再做 branch/compiler。
- [x] 实验失败时先按 `not_retrieved / retrieved_not_admitted / admitted_no_branch / branch_replay_failed / runtime_no_match / compiler_no_action / rewrite_failed` 定位。

审查后补充修正：

- 第一次 high 审查指出 branch runtime 仍借用 formal replay 行，不能证明 replay 实际选中了同一 branch。
- 已补 `branch_member_replay`：
  - 每个 branch 先构造成 branch-scoped memory，只保留该 branch 的 bundles/contracts。
  - branch support case replay 时记录 `forced_branch_id`、`selected_branch_id`、`selected_bundle_ids`。
  - branch runtime 只有在 replay 选中同一 branch 或其 bundle、compile 通过、action 数合法、rewrite 改善且不退化时才置为 usable。
- 审查还指出 root closure 不是 fixed-point、LLM `membership_by_case` 未合并、root-bias key 混入 effect/action 字段、same-root runtime 只返回 top1。
- 已补：
  - root closure 改为 fixed-point。
  - LLM membership 与 mechanical closure 合并进 member status。
  - root-bias key 仅保留 stable bias / repair interface / source misread / target preference 等 root 字段。
  - same-root conflict 时返回 selection budget 内的 root-compatible candidates，不再 top1。

2026-05-08 追加修正：

- 复查 `toxicology_focus18_postsel_v1_qwen3coderflash_20260508_r2` 后发现上面最后两项仍未完全落到主路径：
  - branch-scoped replay 在进入真实 replay 前仍调用 group-level member coverage，因此 branch memory 被 `member_candidate_binding_failed` 提前判成 `training_memory_contract_invalid`。
  - same-root singleton 选择仍把 learned source program 的大段来源字段放入 action/root key，导致 `206/249/268/285/307` 在 q253 当前案例上都能枚举动作，却被拆成多个 action/root bucket。
- 本次实现改为：
  - `_contract_program_issues(..., require_member_coverage=False)` 用于 `branch_member_replay`。branch-scoped replay 只检查是否有可执行 program 和 case-derived repair steps，不再要求整组 member coverage；member coverage 仍保留在 pattern-level diagnostics。
  - branch runtime policy 改成 `branch_support_all_safe_any_improved`：branch support 必须全部选中同 branch、compile pass、无 comparison unknown、无 regression、action count 合法；但只要求至少一个 support member 改善，不再要求每个 member 都改善，也不再用 pattern-level improvement ratio 阈值一票否决。
  - runtime root key 改为优先使用结构化 root effect/action shape，不再使用自然语言 interface 的微小措辞差异切 root；自然语言 interface 只在结构化 root 字段缺失时兜底。
  - same-root conflict resolution 增加 current-case transform key：先用代码枚举当前 case 的可执行 candidate，只比较 primitive + 当前绑定参数 + dependency repair program，不比较 source case id、bundle id、canonical program id、support evidence 等来源字段。多个同 root singleton 必须存在共同 current transform 才允许进入 compiler；如果当前变换集合确实不同，则报告 `ambiguous_current_transform`，不能按多数派放行。
- high 审查后的追加收窄：
  - transform key 从正向字段白名单改为“保留全部当前 executable arguments，剔除 identity/provenance/audit/LLM rationale 字段”，避免遗漏 `MOVE_CONDITION`、`CHANGE_GRAIN`、ranking 等 primitive 的关键参数。
  - transform key 显式携带 `ActionCandidateSet.primitive`，不再依赖 candidate 上不存在的 primitive 字段。
  - transform key 最终只由 `ActionCandidateSet.primitive + non-empty executable args` 组成；不再混入 `bundle_primary_primitive/effect_kind` 等 learned metadata。
  - `canonical_op_type/counts_as_action` 也从 transform key 中移除；metadata-only candidate 会返回空 key，不能进入 same-root transform 放行。
  - nested `repair_program.arguments` 同样走 executable-args 清洗；只有 canonical metadata 的 dependency step 不会单独形成 transform key。
  - 如果 nested dependency step 清洗后没有可执行参数，不保留空 `repair_program` shell；同一 action bucket 多 memory 也必须验证共同 current transform。
  - all-empty transform key 不再 fallback 放行；同 root 多 action bucket 必须有共同 current transform，否则统一 `ambiguous_current_transform`。
  - root fallback 不再使用 `locus/op_family/target_family` 这类 action-level 字段切 root，也不再让这些字段把无 root evidence 的对象推进 same-root transform 阶段；这些字段只用于 current transform/branch 层。
  - branch replay support completeness 改为 holdout case-id set 精确比较，重复 replay row 不能掩盖缺失 support member。
- 最小真实复现：
  - 用 r2 的 `final_library.json` 和 q253 `eea_runtime_request.json` 重跑 `prepare_rewrite_plan`。
  - 修改前：`passthrough_no_match`，`206/249/268/285/307` 均通过基础 gate 后被 conflict 打掉。
  - 修改后：完整 r2 库会报告 `ambiguous_current_transform`，因为 `206/249/307` 和 `268/285` 在当前 q253 上对应两个不同可执行变换分支，这不再被多数派强行放行。
  - 用只包含 `206/249/307` 的同变换子库重跑 q253：`reason=ready`，matched `grp-sing-toxicology-307 / grp-sing-toxicology-249`，产出 1 个 `DROP_SELECT_SLOT` action，hint 为删除 `a2.element`，说明同根同当前变换不再被 conflict 误挡。
  - 对 `grp-pat-toxicology-206-253-93286776` branch-scoped memory 检查：group coverage 模式仍有 `member_candidate_binding_failed`，branch-scoped 模式已无 contract issue，说明 promotion 的早期错误 blocker 已拆开。

## 2026-05-08 代码结构重构：去除 v2 文件命名并收口 EEA API

目标：

- 将原先平铺在 `common/` 根目录的实现按主流程归并，避免 runtime、learning、analysis、LLM、IO 互相散乱引用。
- 文件和目录命名不再带 `v2`，真实实现迁入语义化目录。
- 新增 `rulebook/api.py` 作为 DeepEye 接入侧唯一正式入口；DeepEye post-selection 主链不再直接 import EEA 内部模块。
- 不保留旧路径 shim；DeepEye 接入端同步更新。

结构调整：

- `common/core/`：数据结构、枚举、配置、基础 schema 类型。
- `common/io/`：执行比较、数据库 schema 访问、本地 schema view。
- `common/analysis/`：SQL 结构分析、信号构建、role graph、修复轨迹归一化。
- `common/learning/`：wrong-case accumulate、pattern formation、shared program、promotion、evolution/freeze。
- `common/runtime/`：runtime case view、trigger、branch/action 编译、hint 产出。
- `common/llm/`：LLM client、JSON call helper、prompt builders。
- `common/reporting/`：coverage、trigger observability、version/run metadata。

验证要求：

- EEA 内部 `py_compile` 必须通过。
- `method.EEA.rulebook.api` 必须可导入 runtime/update/evolution/reporting 公开入口。
- DeepEye post-selection 主链的 `run_single_db_e2e.py --help` 必须正常。
- DeepEye 主链不得再引用 `method.EEA.rulebook.common.*_v2`、`prompts_v2`、`pool_coverage/versioning/trigger_observability` 旧路径。

## 2026-05-08 Phase A：解除 replay hard gate 与修复同根 transform 选择

背景：

- `toxicology_focus18_postsel_v1_qwen3coderflash_20260508_r4` 已证明调用与在线积累正常，但 runtime 只有 `ready=1/18`，finalize 后 `patterns=10` 却 `promoted_runtime_objects=0`。
- 主要次生黑盒包括：同根候选因全体 transform key 求交集被 `ambiguous_current_transform` 全挡、pattern 被 promotion/replay hard gate 挡在 runtime 外、replay row 缺少 trigger 失败诊断。

本次改动：

- runtime 同根选择由全体 transform key 交集改为最大同心子集：
  - 按当前 case 上 dry-run 得到的 transform key 反向分组。
  - 选择覆盖 memory 数最多的 transform key 对应子集进入 compiler。
  - audit 记录 `max_shared_current_transform_subset`、选中 key hash、选中/丢弃 group ids 和 key 覆盖分布。
- pattern promotion/replay 暂时从 runtime hard gate 降级为审计：
  - 多成员 pattern 候选直接进入 runtime 观测路径；replay/formal 不再负责提前决定是否可见。
  - trigger contract materialization 失败仍记录 `runtime_contract_status` / `runtime_blockers`，但 `apply_promotion_decision` 和 `materialize_library_runtime_contracts` 都不再把 pattern 改回 `runtime_usable=False`。
  - replay/formal blockers 仍写入 replay history 和 quarantine reason，但不再阻止 runtime 可见。
  - promotion state 标记为 `runtime_visible_replay_audit_only`。
  - audit-only runtime-visible pattern 不废弃 source singleton，避免未验证 pattern 吃掉可用 singleton 触发路径。
- replay row 增加 trigger 诊断：
  - 写入 `rewrite_enabled_reason`、`trigger_blocker_counts`、`top_candidate_reasons`、`selected_group_ids`、`selected_branch_ids`、`memory_selection_audit` 和 compiler empty reason 摘要。

未改动：

- 未修 DISTINCT / redundant JOIN cleanup 编译。
- 未 materialize admission branch specs 到 runtime branch。
- 未收紧 root membership closure。
- 未去字面化 transform key。

预期验证：

- focus18 中 `ambiguous_current_transform` 不应再因为少数不同 transform 把可用同心子集全部挡掉。
- pattern 应能进入 runtime 观测路径；若仍 no_match，可从 replay/runtime audit 直接看到是 source signal、branch 缺失、binder dry-run 还是 compiler action 缺失。

## 2026-05-08 主路径闭合修复：closure、branch materialization、action invariant

背景：

- r5 已能把 RoleGraph 局部修复从 singleton 迁移到 `249/253/268/277/285/302/307`，但真正起作用的仍主要是 singleton，不是 formal pattern/branch。
- 核心断点明确为：admission LLM 产出的 `branch_specs` 写在审计字段，promotion/runtime 实际读取的是 `program_envelope.runtime_branches`，两者没有同步。
- 同时 `_pair_supports_root_membership` 把 `direct_merge_veto` / `core_program_signature_conflict` 这种否定语义 pair 也作为 root closure 证据，导致 `198-338` 这类跨 misconception 的大 root。

本次改动：

- DeepEye `eea_contract_adapter` 不再过滤 EEA replay 诊断字段，`eea_promotion_replay_rows.jsonl` 将透传 `rewrite_enabled_reason`、`trigger_blocker_counts`、`top_candidate_reasons`、`replay_trigger_diagnostics`、`memory_selection_audit`、branch/bundle 选择字段。
- root closure 收紧：
  - `compatible` 仍可闭包。
  - `direct_merge_veto` 与 `core_program_signature_conflict` 不再作为 root membership 正证据。
  - `partial` 只有在两侧共享 primary repair locus 时才可闭包。
- admission branch materialization：
  - `_build_pattern_candidate` 后把 `formation_signals.pattern_admission.branch_specs` 转写到 `synthesized_program.program_envelope.runtime_branches`。
  - materialized branch 只复用已有 synthesized program/runtime branch 的 executable bundle；如果没有可执行 bundle，只记录 `admission_branch_no_executable_bundle`，不发明动作。
  - trigger contract 的 `action_contract.program_envelope` 同步更新，promotion/runtime 读取同一份 runtime branch 对象。
- action invariant：
  - `REROUTE_FACT` 候选在 output arity/grain 不变时标记 `answer_unit_preserve`，并带 `preserve_select_projection`、`preserve_aggregation_grain`。
  - rewrite brief 对 route 修复明确要求保留当前 SELECT/COUNT 与 aggregation grain，除非另一个 selected action 显式修改。
  - dependency repair steps 中的 DISTINCT/JOIN cleanup 会进入 rewrite brief，不再只靠 LLM 自己推断。
- 追加修复：
  - focus18 首轮中 q253 被 `ambiguous_current_transform` 挡住，原因是 singleton 候选池也要求至少两个 memory 共享同一 current transform。
  - 该约束对 pattern 合理，但对高精度 singleton 迁移过严；现在仅在非 pattern 候选池中允许 top1 singleton fallback，pattern 仍保持严格 branch/transform 选择。
  - focus18 完整实验发现仍有 `runtime_branch_replay_gated` 但 `runtime_branches=[]` 的矛盾状态。
  - promotion 决策改为按 actual runtime branch rows 判断 branch 可用性；无 branch 时显式返回 `runtime_branch_contract_missing`，不能进入 branch-gated 状态。
  - branch replay 全部 `runtime_usable_false` 的原因是 branch-scoped memory 只裁剪了 envelope，未把 selected branch 的 required/negative signals 同步到 trigger contract 顶层。
  - `_filter_group_to_runtime_branch` 现在会把 branch required signals 写入 `required_signals` 和 `decisive_pred_signals`，branch replay 才能真实测试 branch/compiler，而不是在 contract executable gate 提前失败。

预期验证：

- focus18 不能再生成把 RoleGraph 与 source-route 大量混合的 16-case root。
- final library 中通过 branch replay 的 pattern 不应再出现 `runtime_branch_replay_gated` 但 `runtime_branches=[]`。
- r5 已有 RoleGraph 收益应保持；source-route 失败应能通过 replay rows 明确定位到 branch、compiler 或 rewrite。
- `q335` 类 route 修复不得把 answer unit 从 molecule count 拉成 bond count；`q302` 类 DISTINCT 依赖应在 action/hint 中可见。

### 2026-05-08 追加：branch-first runtime gate 校正

验证现象：

- `toxicology_focus18_postsel_v1_qwen3coderflash_20260508_142851` 完整跑完：
  - `baseline_correct=0/18`
  - `enhanced_correct=7/18`
  - 改善题：`249/253/268/277/285/302/307`
  - 退化题：无
  - runtime：`ready=7`，`no_match=11`
- replay row 诊断已透传，`branch_member_replay` 从 r4/r5 的几乎全 no-match 变为：
  - `ready=240`
  - `passthrough_no_match=56`
  - no-match 主因集中为 `source_antipattern_output_subset_not_present`
- final library 已能生成 runtime branches，但在线收益仍主要来自 singleton：
  - runtime 触发的 7 个 case 均匹配 `grp-sing-*`
  - pattern 虽然有 branch，但 runtime gate 仍先按 pattern 顶层 trigger contract 做硬判断，导致 branch 没机会参与选择。

确认的问题：

- 带 `runtime_branches` 的 pattern 语义应是：
  - pattern 顶层只表示共同偏差/root bias，不能作为最终实例化入口。
  - branch 才是可执行修复动作入口，应由 branch required/negative signals 和 branch dry-run 决定是否触发。
- 旧实现顺序相反：
  - `_gate_group` 先要求 pattern 顶层 `trigger_contract` 可执行、required signals 命中、decisive signals 命中。
  - 只有顶层通过后才进入 `_select_runtime_branch`。
  - 因此很多 pattern 被 `invalid_or_empty_trigger_contract`、`trigger_contract_missing_required_signals`、`runtime_group_missing_decisive_optional_signal_hit` 提前挡掉。

本次修正：

- 对带 `runtime_branches` 的 pattern，顶层 trigger contract 不再作为硬拦截：
  - 顶层 executable/required/variant/decisive/optional 缺失改为审计原因：`pattern_*_deferred_to_branch`。
  - 是否触发交给 `_select_runtime_branch`，它继续检查 branch required signals、negative signals 和 branch dry-run。
  - branch 选中后，将 `source_trigger_passed=True`，后续 compiler dry-run 以 branch-scoped memory 为准。
- 为避免多个模糊 pattern 把可用 singleton 挡死：
  - pattern 候选池若因 `ambiguous_current_transform` 或 root-bias 冲突无法唯一选择，而存在通过 gate 的 singleton，则回退到 top1 singleton。
  - 这不是降低 pattern 安全阈值，而是防止未稳定的 pattern 影响已验证 singleton 收益。
- hard gate 诊断中过滤 `pattern_*_deferred_to_branch` 审计原因，避免把“交给 branch 判断”的正常路径误报为 blocker。

局部 probe：

- 用 `142851/final_library.json` 对 `q249` 重新跑 `prepare_rewrite_plan`：
  - 多个 pattern branch 可通过，但互相 transform 不一致。
  - selection 正确回退到 `grp-sing-toxicology-206`，`reason=ready`。
- 用同一库 probe `q335/q338/q306`：
  - 仍 `passthrough_no_match`。
  - 主要原因已经不是 pattern 顶层 contract，而是相关 source-route branch 本身没有可用 required signals / executable branch。
  - 这说明 source-route 仍需要后续改 branch admission/materialization 或 action 表达，不应继续放宽顶层 trigger。

### 2026-05-08 追加：pattern 轻识别 / branch 严实例化改造

参考计划：`doc/pattern_recongnize.md`。

重新定位：`pattern` 的价值是用更少、更本质的信号识别“是否是同一种错”；`branch` 负责“这题怎么具体改”，因此 branch 仍需要 required signals、binder dry-run、compiler dry-run。不能让 pattern 触发比 singleton 更重，也不能在 branch 不可绑定时强行 rewrite。

本轮 todo 与完成情况：

- WU1a DeepEye 字段透传：已补齐 `compiler_empty_reason_counts`，原先已透传 `rewrite_enabled_reason`、`trigger_blocker_counts`、`top_candidate_reasons`、`replay_trigger_diagnostics`、`memory_selection_audit`、branch/bundle 选择字段。
- WU1b closure 收紧：`_pair_supports_root_membership` 仅直接接受 `compatible`；`partial` 必须带 `shared_primary_repair_locus` 或 `shared_root_effect_axis_with_same_target_invariant_family` 强证据；不再接受 `direct_merge_veto` / `core_program_signature_conflict`。
- WU2a 数据结构：新增 `BiasRecognitionContract`，挂到 `InstantiationProgram.bias_recognition_contract`；`RuntimeCaseView` 新增 `bias_recognition_signals`；`TriggerCandidateAudit` 新增 bias 识别审计字段。
- WU2b vocabulary/builder：新增封闭 `BIAS_RECOGNITION_SIGNAL_VOCABULARY`；runtime 从当前 SQL/schema 计算 `has_pair_role_side_output`、`select_arity_ge_2`、`no_distinct_on_pair_output`、aggregate/route/order/group 等现象级信号。
- WU2c prompt：`pattern_admission_judge` 要求 admit pattern 时输出 `bias_recognition_contract`，信号必须来自白名单，不允许具体表/列/alias/case id。
- WU2d 落地：admission response 校验并写入 `bias_recognition_contract_validated`；构建 pattern 时写入 `InstantiationProgram.bias_recognition_contract`；若 LLM 没输出，使用已有 runtime trigger signals 投票生成 fallback contract。
- WU3 trigger 两段化：pattern 若带 bias contract，先做轻识别；识别成功后跳过 pattern 顶层 strict required-signal gate，进入 branch selection；branch 不可绑定时记录 `pattern_recognized_branch_unbindable` 且不进入 selection pool。
- WU3 feature flag：`EEA_PATTERN_TWO_STAGE_TRIGGER=0` 可关闭两段 trigger，保留 WU2/WU4/WU5。
- WU3 audit：runtime summary 增加 `stage_1_bias_recognized_count`、`stage_1_bias_signals_missed_count`、`stage_2_branch_ready_count`、`stage_2_branch_unbindable_count`。
- WU4 trigger_contract 同步：pattern 构建后把 `program_envelope.runtime_branches` 同步到 `trigger_contract.runtime_branches`，并把 branch required signals 并集、action envelope、主 op locus/op_family 同步到 trigger contract。
- WU5 accessory action：DROP_SELECT_SLOT / DROP_SIDE 在 pair-role-side output 且 source 无 DISTINCT 时携带 `SELECT_ENFORCE_DISTINCT` 依赖；rewrite contract 自动绑定删除 select alias 后失依的 JOIN block，并为 DISTINCT 加 required presence check。

已做的静态验证：

- `python -m py_compile` 覆盖 EEA 修改文件与 DeepEye `rulebook_experiments/eea_contract_adapter.py`。
- prompt format probe 通过：`bias_recognition_contract` 和 vocabulary 能正常渲染。
- runtime signal probe 通过：toxicology 双端点 SQL 可生成 14 个 bias signals，其中 `has_pair_role_side_output=True`、`no_distinct_on_pair_output=True`、`select_arity_ge_2=True`。

尚未完成：

- 未跑完整 r6 focus18 端到端。上一轮同命令运行时间较长，并被用户要求回退；本轮先完成代码与静态验证，完整 r6 需要单独启动。

风险点：

- WU2 fallback 只在已有 runtime signals 足够时生成 contract；如果 LLM admission 大面积不产 `bias_recognition_contract` 且已有 signals 不足 3 个，pattern 仍不会走两段 trigger。
- WU5 DISTINCT 目前按“当前 S0 是 pair-role-side 输出且无 DISTINCT”携带依赖；这是现象级机制，不绑定 toxicology，但仍需 r6 验证是否过宽。

### 2026-05-09 追加：bias recognition 首轮验证校准

验证对象：

- `toxicology_focus18_postsel_v1_biasrec_20260508_183507`
- `toxicology_focus18_postsel_v1_biasrecfix_20260508_190932`

RUN1 结果：

- `baseline_correct=0/18`
- `enhanced_correct=7/18`
- 改善题：`249/253/268/277/285/302/307`
- 退化题：无
- 但 final library 仍为 `patterns=0, singletons=18`。

RUN1 根因：

- final evolution 已经召回出强 role-side component：`206/249/253/268/277/285/302/307`。
- 该 component 内部 28 个 pair 全部为 `compatible`，不是聚类召回失败。
- pattern admission 阶段统一失败，错误为 `bad character range \\- at position 9`。
- 具体原因是 `_sanitize_bias_text` 中的正则字符类写成 `r"[^a-z0-9_\\- ]+"`，Python `re` 会把其中的 `\\- ` 解释成非法范围。

修复：

- `_sanitize_bias_text` 改为 `r"[^a-z0-9_ -]+"`，把 `-` 放在字符类末尾，避免非法 range。

RUN2 观察：

- regex 修复后，pattern admission 不再 fast-fail，在线 update 开始真实进入 LLM admission / bias contract 抽取，因此运行明显变慢。
- RUN2 未完整跑完，实测已完成到 `q277` 后，尚未生成 `summary.json`。
- 已触发并修对：`253/268/277`。
- `q268/q277` 首次由 `grp-pat-toxicology-206-253-93286776` 这类 pattern 触发并修对，说明 `bias_recognition_contract` 路径首次真实进入 runtime。
- `q263/q269` 被 `bias_anti_signal_hit:has_aggregate_in_select` 拦下，说明 anti-signal 在区分 role-side 输出类与 aggregate/source-route 类时生效。

RUN2 q249 no-match 根因：

- `q249` 没有触发不是泛泛的“新演化状态影响”，而是 singleton exact gate 中的 `unsupported_singleton_program_type`。
- regex 修复后，admission / synthesis 会把 `grp-sing-toxicology-206` 的 program type 从单一 `select_drop` 升级为复合 `select_drop+where_side_edit`。
- 旧 gate 只接受 7 个单一 program type 字符串，不接受由已知原子 type 组成的复合轨迹。
- 这会导致 source/binder 已匹配的 singleton 被硬挡。

修复：

- singleton exact gate 不再按完整字符串白名单判断。
- 现在将 `program_type` 按 `+` 拆成原子 type，只要每个 atom 都属于已有允许集合，就允许继续进入后续 shape / binder / compiler 检查。
- 这是对复合修复轨迹的通用兼容，不增加 case/db 特判。

下一轮 r6 验收重点：

- `q249` 应恢复 ready。
- `q268/q277` 应继续由 pattern 触发，而不是退回 singleton。
- `q285/q302/q307` 需要确认是否保持可修。
- final library 应至少出现一个 pattern；否则继续检查 admission response、pattern materialization 和 promotion/finalize 链路。

### 2026-05-09 追加：LLM API 波动下的 update 稳定性

现象：

- `toxicology_focus18_postsel_v1_biasrec_gatefix_20260509_053737` 中 `q198/q207/q268` 出现 `update=error`。
- 错误来自 `RuntimeError: Failed to parse LLM JSON response: LLM call failed: Internal Server Error`。
- 这类错误会让当前错例没有沉淀成 singleton，破坏“逐例在线积累”的阶段 1 验证前提。

修复：

- EEA `LLMClient` 的底层请求重试次数通过 `RULEBOOK_LLM_MAX_RETRIES` 控制。
- 默认从 1 次提高到 3 次。
- 该修改只影响 EEA 自己的 auditor/extractor/admission 等 LLM 调用；DeepEye rewrite/selector 使用的 `app.llm.llm` 仍由接入端控制。

验证要求：

- 重新跑 focus18，若仍有 `update=error`，该 run 不作为阶段 1 收益结论。
- 至少要求在线 update 不丢关键前缀 case，才能评估 pattern 触发和最终收益。
