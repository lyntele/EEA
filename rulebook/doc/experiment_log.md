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

### 2026-05-09 追加：Phase 1 focus18 r6 观察

验证对象：

- `toxicology_focus18_postsel_v1_qwen3coderflash_20260509_080224_phase1wu5b`

总体结果：

- `baseline_correct=0/18`
- `enhanced_correct=2/18`
- 改善题：`249/277`
- 退化题：无
- runtime：`ready=7, no_match=11`
- rewrite：`attempted=7, parse_fail=3, selector_choose_s1=2, selector_keep_s0=2`
- update：`called=18, accumulated=17, error=1`
- final library：`patterns=10, singletons=18, families=0, runtime_usable_patterns=10`

关键正向信号：

- final library 不再是纯 singleton，已经形成 10 个 pattern，且没有旧版本那种 16-case 巨型 pattern。
- `q268/q277/q285/q302/q307` 均由 `grp-pat-toxicology-206-253-93286776` 这类 pattern 触发，不再只是 singleton 迁移。
- RoleGraph / pair-output-drop 类 pattern 的 hint 已能携带三类必要动作：删除多余输出列、补 `SELECT DISTINCT`、删除因输出列移除而失依的 JOIN。
- `q302` 的 EEA hint 已包含 `add SELECT DISTINCT` 和 `remove JOIN block involving a2`，说明 WU5 的 accessory dependency 已进入 runtime hint。

具体失败归因：

- `q249`：由 `grp-sing-toxicology-206` 触发并修对，说明复合 `program_type` gate 修复有效。
- `q253`：rewrite 生成了可疑似正确的 S1，但 DeepEye S0/S1 selector 选回 S0；这是接入端 selector 问题，不是 EEA 未触发。
- `q268/q285/q302`：EEA 已触发并给出正确方向 hint，但 DeepEye rewrite LLM 三次 `Internal Server Error`，最终没有可用 S1；这是 rewrite/API 稳定性问题。
- `q277`：pattern 触发，rewrite 第三次成功，selector 选 S1，最终修对。
- `q307`：pattern 触发并产生 S1，但 online update 阶段 `error_instance_extractor` 返回非严格 JSON，导致 `update=error`；官方 reconcile 后补收，但这说明在线积累稳定性仍不足。
- `q335/q338`：存在相关 source-route/scope pattern，但当前在线触发仍 no_match；原因集中在 branch 不可绑定、`JOIN_DROP_TABLE` 等 canonical op 未能 materialize 成可执行分支/action，而不是 RoleGraph 触发问题。

本轮结论：

- Phase 1 的 RoleGraph 主链路已经打通：在线积累 -> pattern 形成 -> bias recognition trigger -> branch/action hint。
- Phase 1 还不能判定完成，因为在线 update 仍有 1 个 error，source-route/scope 类 branch/action 表达仍未完成，且部分收益被 DeepEye rewrite API 和 selector 吞掉。

随后修复：

- EEA JSON 解析器增加最后一级 YAML-like fallback，处理 LLM 偶发输出的非严格 JSON 对象，例如 unquoted enum value。
- `RULEBOOK_LLM_JSON_ATTEMPTS` 允许配置 JSON 解析重试次数，默认仍为 3。
- 该修改是通用 update 稳定性修复，不改变 trigger、pattern、action 规则。

### 2026-05-09 追加：q249 再次 no-match 的实现 bug

验证对象：

- `toxicology_focus18_postsel_v1_qwen3coderflash_20260509_100441_jsonfix`

现象：

- 该 run 在 `q249` 再次出现 `runtime=no_match`。
- runtime 审计中 `grp-sing-toxicology-206` 的 source/binder 已匹配，但被 `singleton_exact:unsupported_singleton_program_type` 挡掉。

根因：

- 当前 `grp-sing-toxicology-206` 的 synthesized `program_type` 为 `select_drop+select_output_patch`。
- `select_drop` 是主修复动作，`select_output_patch` 是 DISTINCT 这类依赖动作。
- singleton exact gate 错误地把整个复合 `program_type` 当成“主修复类型”做白名单判断，导致依赖动作影响触发。
- 这和上一轮 `select_drop+where_side_edit` 的问题同源：trigger 应判断主修复类型是否可执行，依赖动作应该留给 compiler/rewrite 处理，不能在触发阶段硬挡。

修复：

- runtime 新增从 executable ops 中抽取“非依赖主动作”的程序类型。
- 若复合 `program_type` 含有 dependency atom 导致完整字符串不在允许集合内，则回退到 canonical ops 的非依赖主动作判断。
- `SELECT_OUTPUT_PATCH` 等依赖动作不再影响 singleton exact trigger。
- 这不是新增 case/db 规则，而是把 trigger gate 从“完整轨迹字符串”改回“主修复动作”。

局部验证：

- 使用同一 run 的 `q249` runtime request 和当前 library 重新 probe：
  - `status=ready`
  - `matched_group_ids=["grp-sing-toxicology-206"]`
  - hint 包含删除第二输出列、添加 `DISTINCT`、清理失依 JOIN。

下一轮要求：

- 重新冷启动跑 focus18。
- `q249` 不应再被 `unsupported_singleton_program_type` 挡掉。
- `q268/q277/q285/q302/q307` 的 pattern 触发应保持。
- 若仍有 no-match，应继续按 runtime audit 中的具体 gate reason 定位，而不是放宽整体阈值。

### 2026-05-09 追加：local evolution 深 replay 导致在线阶段不可用

验证对象：

- `toxicology_focus18_postsel_v1_qwen3coderflash_20260509_101951_primaryfix`

现象：

- `q249` 已恢复 `runtime=ready matched=1 s1=yes final=True update=accumulated`。
- `q253` 也为 `runtime=ready matched=2 s1=yes`，但 DeepEye selector 仍选回 S0，因此 `final=False`。
- 从 `q249/q253/q263` 开始，每条 case 的 online update 非常慢。
- 中断栈明确停在 `update_from_selected_sql -> _local_evolve_library -> evolve_library_with_replay -> run_promotion_test -> _replay_one_holdout -> prepare_rewrite_plan -> run_action_compiler`。

根因：

- 当前 local_evolve 在每个新错例 update 后都会立即执行完整 replay-gated promotion。
- replay 内部会对候选 pattern 的成员做 LOO / full-group / branch replay，并调用 `action_compiler`、`hint_instantiation`、`memory_rewrite`。
- 这会导致在线阶段每个关键 case 都触发多次 4 万字符级 prompt 和下游 rewrite LLM 调用。
- 对“逐例在线积累”的目标来说，local_evolve 的职责应是让新形成的 pattern/contract 对后续 case 可见；完整 replay 质量评估应在 final_evolve_and_freeze 做。

修复：

- `local_evolve` 默认改为 replay-deferred：
  - 仍然执行 singleton -> pattern formation。
  - 仍然 materialize trigger contract，使后续 case 可以在线触发。
  - 将候选 pattern 标记为 `runtime_visible_local_evolve_audit_only`。
  - 不在每个 case 的 update 阶段做完整 `run_promotion_test`。
- `final_evolve_and_freeze` 保持原来的 replay-gated promotion，不受影响。
- 若需要恢复旧行为，可设置 `EEA_LOCAL_EVOLVE_REPLAY=1`。

设计边界：

- 这不是放弃 replay/promotion，而是把 replay 从在线热路径移到最终冻结边界。
- 在线阶段仍由 bias recognition + branch required signals + binder/compiler 控制是否真正 rewrite。
- source singleton 保持可用，避免 pattern 质量不稳时破坏已有 singleton 收益。

下一轮要求：

- focus18 冷启动应明显加速，不能再在 `q249/q253/q263` 的 update 阶段长时间卡住 replay。
- `q268/q277/q285/q302/q307` 应能看到前缀形成的 RoleGraph pattern。
- final_evolve 阶段仍应产生 replay/promotion 诊断，用于判断哪些 pattern 真正稳定。

### 2026-05-09 追加：localfast 不应吸收源 singleton

验证对象：

- `toxicology_focus18_postsel_v1_qwen3coderflash_20260509_105908_localfast`

现象：

- `q249` 已修对并快速 update。
- 但 `q253` 变成 `runtime=no_match`。
- runtime 审计显示：
  - `grp-sing-toxicology-206` / `grp-sing-toxicology-249` 均被 `status_not_active` 或 `runtime_usable_false` 挡住。
  - `grp-pat-toxicology-206-249-b5991530` 被 `pattern_recognized_branch_unbindable` 挡住。

根因：

- localfast 将新形成的 pattern 标记为 runtime visible 后，仍调用 `integrate_promoted_groups`。
- `integrate_promoted_groups` 会把 runtime usable pattern 的源 singleton 吸收/废弃。
- 但 localfast 的 pattern 只是 audit-visible，branch 可能还未稳定可绑定；此时废弃源 singleton 会破坏原本可用的 singleton 收益。

修复：

- local_evolve 的 replay-deferred pattern 合并时不再调用 `integrate_promoted_groups`。
- 新增 `_merge_patterns_without_absorbing_singletons`：只把 audit-visible pattern 放入 `library.patterns`，不修改 `library.singletons` 的 active/runtime 状态。
- final_evolve 仍使用原来的 `integrate_promoted_groups`，只有最终 replay/promotion 后才允许正式吸收 source singleton。

下一轮要求：

- `q253` 应恢复 singleton 触发。
- `q268/q277/...` 仍应能看到 localfast 形成的 pattern。
- 若 pattern branch unbindable，必须回退到 singleton，而不是 no_match。

### 2026-05-09 追加：阶段一收尾前的 transform key 去字面化

背景：

- 阶段一的“最大同心子集”机制已经替代了旧的全交集判断，但 `_candidate_transform_key` 仍保留了 alias / 列名 / 具体表达式级字段。
- 这会把同一个可执行修复动作按 `from_expr=a2.element AS element1`、`from_expr=a2.element AS element2` 这类 SQL 表达差异拆成不同 transform key。
- 对 toxicology 的 RoleGraph pattern 来说，这会削弱 q206/q249/q253/q268/q277 等同根修复的聚合与 runtime 选择。

修复：

- 在 `common/runtime/runtime.py` 的 `_TRANSFORM_ARGUMENT_NON_EXECUTABLE_KEYS` 中补充以下字段：
  - `from_expr` / `from_exprs`
  - `source_alias` / `target_alias`
  - `source_columns` / `target_columns`
  - `source_table` / `target_table`
  - `source_table_column` / `target_table_column`
  - `source_expression` / `target_expression`
- 这些字段不再参与 current transform key 的 identity，只作为 compiler/rewrite 的具体绑定信息保留在 candidate arguments 里。

设计边界：

- 这不是放宽 trigger，也不是合并不同动作。
- transform key 仍保留 primitive、repair program 中的语义级可执行字段和必要参数。
- alias / 表达式 / 具体列名只影响当前 SQL 如何落地，不应决定“多个 memory 是否在当前 case 上枚举出同一类 transform”。

下一轮 r6 验收重点：

- `q249` 应保持 ready，不能再被 `unsupported_singleton_program_type` 或 transform key 分裂影响。
- `q268/q277` 应继续出现 pattern 触发。
- runtime audit 中至少应出现一个 transform key 覆盖多个同根 group，而不是每个 group 都被 alias 字面拆开。
- focus18 完整冷启动必须重新跑完，阶段一是否关闭以 r6 的 P0 验收清单为准。

### 2026-05-09 r6：DMXAPI focus18 阶段一验收

运行：

- 结果目录：`method/deepeye/DeepEye-SQL/workspace/rulebook_runs/toxicology_focus18_postsel_v1_dmxapi_r6_20260509_124318`
- Provider / model：DMXAPI，`Qwen3-Next-80B-A3B-Instruct`
- post-selection 策略：rewrite guard 通过后直接输出 S1，不再跑 S0/S1 selector。

结果：

- `baseline_correct = 0/18`
- `enhanced_correct = 7/18`
- `improved_qids = [249, 253, 268, 277, 285, 302, 307]`
- `regressed_qids = []`
- `runtime ready = 7`，`no_match = 11`
- `rewrite attempted = 7`
- `direct_accept_s1 = 7`
- `online_update = 18 accumulated / 18 called`
- `finalize status = ok`
- `final library = 7 patterns + 18 singletons + 0 families`

达成项：

- `q249` 恢复 ready，并修对。
- `q253` 恢复 ready，并修对。
- `q268/q277/q285/q302/q307` 均 ready 并修对。
- `unsupported_singleton_program_type` 没有再阻断 RoleGraph 收益链。
- `SELECT DISTINCT` 和 dependent JOIN cleanup 已进入真实 rewrite，q249/q253/q268/q277/q285/q302/q307 的 S1 都直接输出并通过执行。
- 没有 regression。
- final freeze 能生成 pattern，且其中两个 pattern 为 `runtime_branch_replay_gated`。

未达成项：

- 在线 ready 的 7 个 case 全部由 singleton 触发，不是 pattern 触发：
  - `q249 -> grp-sing-toxicology-206`
  - `q253 -> grp-sing-toxicology-249 / grp-sing-toxicology-206`
  - `q268 -> grp-sing-toxicology-253 / grp-sing-toxicology-249`
  - `q277/q285/q302/q307 -> later singleton pairs`
- 因此阶段一“让现有收益由 pattern 路径而非 singleton 路径完成”的目标未完成。
- 总耗时超过 1 小时；在线 18 条完成后，final freeze 又花费大量时间。
- `eea_llm_trace.jsonl` 显示本轮共有：
  - `shared_insight_judge = 140`
  - `pattern_admission_judge = 16`
  - `action_compiler = 39`
  - `hint_instantiation = 39`
  - `memory_rewrite = 32`
  - 最大 prompt：`action_compiler 70509 chars`，`pattern_admission_judge 61666 chars`，`shared_insight_judge 53502 chars`
  - 总 prompt chars 约 `7.7M`

关键根因：

- pattern 不是完全没被识别。
- 以 `q268` 为例，runtime audit 中 pattern candidate 已经出现：
  - `grp-pat-toxicology-206-253-93286776`
  - `grp-pat-toxicology-206-249-b5991530`
- 它们的状态是：
  - `bias_recognized = true`
  - `required_hit_count = 3`
  - `required_miss_count = 0`
  - 但 `gate_passed = false`
  - hard gate reason 为 `pattern_recognized_branch_unbindable` / `runtime_usable_branch_missing`
- 也就是说，阶段一当前卡点不是 bias recognition，也不是 trigger 粗筛；而是在线 local pattern 的 branch 没有及时 materialize 成 runtime usable branch，runtime 只能退回 singleton。

阶段一结论：

- 收益侧达成 RUN1 水平：7/18，0 regression。
- pattern 构建侧有进展：final library 有 7 个 pattern，且部分有 branch replay gated。
- 但阶段一不能关闭，因为在线收益仍来自 singleton；pattern 只在 final freeze 后出现可用状态，没能在逐例在线 runtime 中承担主路径。

下一步：

- 修在线 local_evolve 形成 pattern 后的 branch runtime usable materialization。
- 目标不是扩大 trigger，而是让已经 `bias_recognized=True` 的 pattern 在在线阶段具备可绑定 branch。
- 同时削减 final freeze 的重复 admission/replay 调用，否则 18 条 focus set 就需要约 1 小时以上，无法支撑后续 145 全量或多库实验。

### 2026-05-09 追加：解除在线 pattern branch usable 死锁

背景：

- r6 证明 bias recognition 已经工作：RoleGraph 后续案例的 pattern candidate 均能 `bias_recognized=True`。
- 但 runtime 段 2 只检查 `runtime_usable=True` 的 branch。
- local evolve 为了性能推迟 replay 到 final freeze，导致在线 pattern 的 branch 均未被 replay 标记 usable。
- 结果是 `bias_recognized=True` 后被 `runtime_usable_branch_missing` / `pattern_recognized_branch_unbindable` 挡掉，只能退回 singleton。

修复：

- local evolve 形成 audit-visible pattern 时，对已有 executable binding 的 branch 标记：
  - `runtime_usable=True`
  - `runtime_validation_policy=local_evolve_lightweight_binder_gated`
  - `cross_case_replay_pending=True`
- 这不是 formal replay 通过，只表示该 branch 可进入 runtime 的段 2 binder 检查。
- runtime `_select_runtime_branch` 不再把 `runtime_usable=False` 当作绝对硬门。
  - 若没有 replay-usable branch，但存在带 bundle / allowed primitive 的 branch，则进入 binder dry-run fallback。
  - 只有当前 case 的 branch required signals 命中且 binder dry-run 成功，才允许该 branch 进入 selection。

同时修复：

- local evolve 合并 pattern 时增加同根嵌套去重。
- 同一 root key 下，如果一个 pattern 的 case_ids 是另一个 pattern 的真子集，则保留超集，丢弃子集。
- 目标是避免 RoleGraph 这类 pattern 每进一个新 case 就留下一个嵌套旧版本。

下一轮 r7 验收：

- `q268/q277/q285/q302/q307` 中至少 3 个 `matched_group_ids` 应包含 `grp-pat-...`。
- pattern candidate 不应再因为 `runtime_usable_branch_missing` 全部进入 diagnostic-only。
- RoleGraph final library 不应再保留 5 个嵌套子集 pattern。
- 收益至少保持 r6：`enhanced_correct >= 7/18`，`regressed_qids=[]`。

### 2026-05-09 r7 中断观察：branch usable 已解锁，但等价 branch 被误判 ambiguous

中断点：

- `toxicology_focus18_postsel_v1_dmxapi_r7_20260509_143152`
- 已跑到 q268。

观察：

- `runtime_usable_branch_missing` 已经消失。
- q268 的 pattern candidate 已有 usable branch：
  - `grp-pat-toxicology-206-253-93286776`: `branch_runtime_usable_count=3`
  - `grp-pat-toxicology-206-249-b5991530`: `branch_runtime_usable_count=2`
- 但仍未进入 selection，原因变成 `branch_selection_ambiguous`。

根因：

- 多个 branch 同时 gate_passed，但它们指向同一个 bundle/action。
- 例如 q268 中多个 branch 的 `bundle_ids` 都是 `bundle:0b216f98` 或 `bundle:26d3db30`。
- 这不是多个不同修复程序冲突，而是 admission 产出的 branch label 更细，但落地动作相同。

修复：

- runtime branch selection 在多个 matched branches 存在时，按 `(bundle_ids, allowed_primitives)` 形成 branch signature。
- 如果所有 matched branches signature 相同，则视为等价 branch，选择一个 canonical branch，不再报 `branch_selection_ambiguous`。
- 如果 signature 不同，仍然保持 ambiguous hard gate。

下一轮 r8 验收：

- q268 应至少有一个 `grp-pat-...` 进入 matched group。
- q277/q285/q302/q307 应继续验证 pattern 路径是否稳定。
- 若仍失败，下一层看是否是 pattern 与 singleton 同时进入后 current-transform 选择偏向 singleton。

### 2026-05-09 r8 中断观察：runtime helper 缺失

中断点：

- `toxicology_focus18_postsel_v1_dmxapi_r8_20260509_144639`
- 已跑到 q268。

现象：

- q268 `runtime=error`。
- `eea_runtime_response.json` 中：
  - `reason = retrieval_exception:NameError`
  - `blocked_reasons = retrieval_error:NameError: name '_runtime_branch_support' is not defined`

根因：

- `Resolve equivalent runtime branches` 在 runtime 里引用了 promotion 模块中已有的 `_runtime_branch_support` 语义，但 runtime 模块本地没有该 helper。

修复：

- 在 `common/runtime/runtime.py` 本地补 `_runtime_branch_support(branch, fallback_case_ids)`。
- 语义：优先使用 branch 的 `support_case_ids`，没有时回退到 group 的 `case_ids`。

下一轮 r9：

- 重新跑到 q268，确认不再 runtime error。
- 若 q268 pattern 仍不进 matched group，再继续看 current-transform 选择是否偏向 singleton。

### 2026-05-09 r9 中断观察：pattern 分支已可执行，但 root-bias 冲突策略退回 singleton

中断点：

- `toxicology_focus18_postsel_v1_dmxapi_r9_20260509_150202`
- 已跑到 q306 前后，停止原因是该轮未包含最新 root-bias bucket 选择修复，继续等待 final freeze 意义不大。

观察：

- q253 已经通过 pattern 路径修对：
  - `matched_group_ids = ["grp-pat-toxicology-206-249-b5991530"]`
- q268 的 pattern candidate 已通过段 2：
  - `runtime_branch_selected:join_drop_required`
  - `compiler_dry_run:passed`
  - `branch_runtime_usable_count=4`
  - `bias_recognized=True`
- 但 q268 最终仍选择 singleton：
  - `matched_group_ids = ["grp-sing-toxicology-253"]`
  - `selection_pool_kind = pattern`
  - `fallback_reason = conflicting_root_bias_contracts`
  - `resolution = singleton_top1_after_pattern_ambiguity`

根因：

- 多个 pattern root-bias bucket 同时通过时，旧逻辑直接把该状态视为 pattern ambiguity。
- 即使其中一个 bucket 明显更强，也退回 singleton。
- 这和阶段一目标冲突：pattern 已经被识别、branch 已经可执行时，应优先让最强 pattern bucket 进入实例化，而不是直接放弃 pattern。

修复：

- 在 `_select_compatible_groups` 的 `root_bias_conflict` 分支中增加 best-pattern-bucket 选择。
- 只在最强 root bucket 的 rank 明确高于第二名时启用。
- rank 只使用 `(final_score, type_priority, support_count)`，不使用 `group_id` 这类字面 tie-breaker，避免任意选择。
- 若最强 bucket 内仍有多个 group，则继续走 shared current transform 选择；否则选择该 bucket 内最高分 pattern。
- 如果没有明确最强 bucket，仍保持原来的 singleton fallback，避免过宽误触发。

下一轮 r10 验收：

- q253 应保持 pattern 修对。
- q268 应从 singleton fallback 切到 `grp-pat-...`。
- q277/q285/q302/q307 中至少再有 2 个由 `grp-pat-...` 触发。
- 总收益不低于 r6 的 7/18，且无 regression。

### 2026-05-09 r10 在线结果与剩余计划项收尾

运行：

- `toxicology_focus18_postsel_v1_dmxapi_r10_20260509_153406`
- 在线 18 条完整跑完；final freeze 因 replay/rewrite 调用过重被中止，不作为本轮验收对象。

在线结果：

- `enhanced_correct = 7/18`
- `regression = 0`
- 修对：`249,253,268,277,285,302,307`
- pattern 路径修对：`253,268,277,285,302,307`
- singleton 路径修对：`249`

结论：

- WU2/WU3 的两段触发已生效：RoleGraph pattern 不再只是 final library 里的事后对象，已经在线触发并产生收益。
- WU5a/WU5b 已生效：q302 从上一轮未修对变为 pattern 触发修对，rewrite 中包含 DISTINCT / JOIN cleanup 约束。
- source-route 组仍未完成：`326/328/335/338` 在线仍 no_match。

source-route 断点：

- q328 后已经形成 source-route pattern：`263/269/328`。
- q338 后又形成 source-route pattern：`269/335/338`。
- 但 q335/q338 到达时 runtime top candidates 没有 pattern 候选，说明问题在 source-route pattern 的在线识别/契约可见性，而不是 rewrite 后改坏。
- q335 的目标是 route repair 但必须保留 `COUNT(DISTINCT molecule)` 这类 answer unit；因此 WU5c 需要同时完成 source-route recognition 与 `REROUTE_FACT + answer_unit_preserve`。

本次修复：

- `JOIN_REROUTE` / `FACT_ROUTE_REROUTE` / `SELECT_DROP_DISTINCT` 接入 canonical lowering，避免 source-route singleton/pattern 被 unsupported op type 卡住。
- source-route bias contract 归一化：aggregate answer-unit 信号不再作为 anti-signal；`has_join_chain_via_bridge_table` 被补为 source-route 正向识别信号。
- source-route fallback contract：当多个成员的 action contract 指向 reroute 时，补 `wrong_join_route` / `preserved_aggregate_unit` 的 fallback recognition。
- REROUTE_FACT rewrite contract：若 `answer_unit_preserve=True`，rewrite 必须保留 SELECT 聚合表达式、COUNT/DISTINCT 语义、GROUP BY 和输出粒度。
- REROUTE_FACT deterministic rewrite：在 answer-unit preserve 场景中保留原 SELECT 表达式，只改 join route。
- pattern 嵌套去重：不再用完整 recognition_signals 作为 root key；真子集 pattern 默认被同 action family 的超集替代，避免 RoleGraph 保存 `[206,249]`, `[206,249,253]`, ... 多个嵌套版本。

下一轮快速验收：

- 在线部分保持 `enhanced_correct >= 7/18` 且 `regression=0`。
- RoleGraph 收益仍至少 6 个走 pattern。
- q335/q338 至少出现 source-route pattern candidate，理想状态为 `bias_recognized=True` 或 `pattern_recognized_branch_unbindable`，不再是完全 no pattern candidate。
- library snapshots 中 RoleGraph 嵌套 pattern 明显减少，最终只应保留最大超集或少数真正不同 action family 的 pattern。

### 2026-05-09 r11-r15：完成 pattern 去重与 source-route 候选拆分，稳定基线为 7/18

运行与中断：

- r11：`toxicology_focus18_postsel_v1_dmxapi_r11_20260509_174227`，中断于早期退化排查。
- r12：`toxicology_focus18_postsel_v1_dmxapi_r12_20260509_175620`，在线完整，final freeze 中止。
- r13：`toxicology_focus18_postsel_v1_dmxapi_r13_20260509_183147`，在线完整，final freeze 中止。
- r14：`toxicology_focus18_postsel_v1_dmxapi_r14_20260509_190516`，在线完整，final freeze 中止。
- r15：`toxicology_focus18_postsel_v1_dmxapi_r15_20260509_193352`，完整结束并生成 `summary.json`。

r11 退化根因：

- q253/q268 触发到了 pattern，但 selected branch 的 binder dry-run 实际无候选。
- 旧逻辑把 `branch_binder_no_candidates` 当作可延迟到 compiler 的原因，导致 pattern 以 `gate_passed=True` 压过可工作的 singleton，最终 `action_count=0`。
- 修复：`branch_binder_no_candidates` 不再是 deferable；bias 已识别但 branch 无候选时必须保持 `pattern_recognized_branch_unbindable` / no pass，让 selection 回退到可编译对象。

r12 结果：

- 在线结果恢复到 `7/18`，无 regression。
- 修对：`249,253,268,277,285,302,307`。
- q253/q268 不再出现 no_action 退化。
- source-route 仍未形成收益，原因是构建阶段把 `269/326/328/335/338` 与其他弱相关 case 混进一个大 component，admission 整体拒绝。

r13 改动与结果：

- 将 pattern 候选连通边从 `{compatible, partial, direct_merge_veto, core_program_signature_conflict}` 收紧为 `{compatible, partial}`。
- 目的：direct-merge veto / core conflict 不能作为 root seed，只能作为审计或后续 branch 证据，避免 source-route 被大杂烩 component 整体拒绝。
- nested pattern 去重改为看主动作族 / core op，不让 dependency/accessory primitive 阻止超集替代。
- 在线结果一度达到 `8/18`，新增 q338，但该收益来自 `grp-sing-toxicology-326` singleton，不是 source-route pattern。
- 形成了 `grp-pat-toxicology-269-335-6c28b752` source-route pattern，说明 source-route 小 component 开始能被构建出来。

r14/r15 校准：

- 过滤 `ops=[] / branches=[]` 的无执行 pattern，避免 admission-only 对象进入 runtime library。
- bias recognition contract 校验中删除 recognition 与 anti 的交集，避免同一信号既要求命中又要求排除。
- q338 在 r14 变 no_match，定位为 q326 singleton 被抽成多动作程序后触发被 `singleton_contract_max_actions_gt_one` 硬挡。
- 修复：多动作 singleton 在 source trigger / binder 证据成立时不再一票否决，交给 compiler dry-run 做实例化校验。
- r15 结果：`enhanced_correct=7/18`，`regression=0`，q338 runtime ready 但 rewrite 后未修对，因此不计收益。

当前状态：

- 必要硬约束满足：不低于 r10/r12 的 7 个稳定收益，且无 regression。
- pattern 去重已改善：无执行程序 pattern 被过滤；RoleGraph 不再保存长串嵌套版本。
- source-route 已能形成小 pattern，但在线收益尚不稳定；q338 的一次收益来自 singleton，r15 证明触发可到达但 rewrite/动作实例化还未稳定。
- final freeze 不是本轮优化目标；r15 final library 中 branch runtime 状态会被 replay-gated 策略重写，在线判断仍以 `.state/work/qid_*/eea_runtime_response.json` 和 per-case summary 为准。

剩余问题：

- source-route pattern 的 branch 表达仍偏弱，`grp-pat-toxicology-269-335` 对 q338 的 bias overlap 只有 `1/5` 或 `0/5`，说明 recognition_signals 仍没有抽到“直接关系替代桥表路径”的运行时可见核心信号。
- q338 触发后 rewrite hint 能表达“改 SELECT / 去 bridge”，但生成 SQL 未通过执行，下一步应看 `eea_rewrite_result.json` 与 rewritten SQL，区分是 action candidate 指向不准还是 rewrite prompt 没落实。
- r15 `enhanced_correct` 仍未达到期望 `>=9/18`；进入下一轮时优先处理 source-route 的运行时识别与实例化，而不是继续放宽 RoleGraph gate。

### 2026-05-09 审查修复与 r16 验证

审查发现：

- `branch_binder_no_candidates` 已经不再 defer，但相邻的 `branch_binder_missing_bundles`、`compiler_dry_run_missing_required_bundles`、`compiler_dry_run_no_candidates` 仍可能被延迟，和“branch 严实例化”不一致。
- 初始 component union 仍允许所有 `partial`，而计划要求 partial 必须叠加强 root 证据。
- admission-only pattern 过滤只在 local evolution merge 路径存在，formation / promotion 路径仍可能漏入。
- nested pattern 主动作族识别只看 op 顶层 `is_dependency`，没有看 operation/shared signature 内的 dependency 标记。

修复：

- compiler dry-run 的 no-candidate / missing-bundle 不再作为 deferable reason；可执行性失败必须阻断 trigger。
- pattern 初始连通边复用 `_pair_supports_root_membership`，因此 `partial` 必须含 `shared_primary_repair_locus` 或 `shared_root_effect_axis_with_same_target_invariant_family` 才能 seed component。
- `_dedupe_patterns` 与 `integrate_promoted_groups` 都过滤无 `synthesized_program.ops` 的 pattern，避免 `ops=[]/branches=[]` 的 admission-only 对象进入库。
- nested pattern 去重的 action family key 同时读取 `op.arguments.operation_signature/shared_signature.is_dependency`，避免 dependency op 被当成 root 主动作。

r16 验证：

- 运行：`toxicology_focus18_postsel_v1_dmxapi_r16_20260509_200826`
- 在线阶段完整；final freeze 按当前策略中止，不作为在线验收。
- `final_correct = 7/18`
- `runtime ready = 8/18`
- 修对：`249,253,268,277,285,302,307`
- regression：`0`
- pattern 中不再出现 recognition 与 anti 的交集。
- q338 仍 `runtime=ready` 但未修对，仍属于 source-route 实例化 / rewrite 质量问题。

当前结论：

- 审查阻塞项已修；必要硬约束仍满足：不退化 7/18 且无 regression。
- 期望目标 `>=9/18` 未达成，剩余主要瓶颈不是 RoleGraph trigger，而是 source-route pattern 的运行时识别和可执行修复动作还不稳定。

### 2026-05-09 r17：source-route answer-unit preserve 修复，focus18 达到 8/18

运行：

- `toxicology_focus18_postsel_v1_dmxapi_r17_20260509_204440`
- 在线 18 题完整跑完；官方 baseline / rewrite_only / full_pipeline evaluation 已落盘。
- final freeze 在官方评估后中止；本轮目标是在线触发与 rewrite 效果，不以 final freeze 为验收对象。

本轮修复：

- `REROUTE_FACT` 枚举不再要求 output 同时变化。只要修复轨迹里 `target_relation_equalities` 相比 source relation 发生变化，就可以形成 route repair candidate。
- singleton 没有 member variant 时，从 canonical op 的 `operation_signature.relation_delta` / `repair_effect_signature.relation_effect` 中反推出 `target_relation_edges`，避免 source-route singleton 永远枚举不出 reroute candidate。
- 对“source-route + 输出形状不变 + output_contract=UNCHANGED 或 source/target 输出列语义相同”的 `select_replace`，不再按 SELECT 替换执行，而是降到 `REROUTE_FACT + answer_unit_preserve`。
- hint 后处理增加契约保持：如果原始结构动作带 `answer_unit_preserve`，最终给 rewrite 的 hint 必须包含“保留当前答案单位和 SELECT 语义，只在 join route 改变时重绑定 alias”的约束，防止 hint instantiation LLM 丢掉 preserve 约束。

r17 结果：

- `final_correct = 8/18`
- `runtime ready = 9/18`
- 修对：`249,253,268,277,285,302,307,338`
- regression：`0`
- 官方 EX：baseline `0/18`，rewrite/full pipeline `8/18 = 44.44%`

关键案例：

- q338 从 r16 的错误“把 `atom_id` 改成 `molecule_id`”变为正确 route-only rewrite：
  `SELECT DISTINCT a.atom_id FROM bond b JOIN atom a ON b.molecule_id = a.molecule_id ...`
- q338 runtime action 现在是 `REROUTE_FACT`，`answer_unit_preserve=True`，required scopes 只有 `FROM/JOIN`，不再产生 `REPLACE_SELECT_SLOT` / `SWITCH_CANONICAL_FIELD`。
- q335 仍未修对，但失败形态改善：它不再把 `COUNT(DISTINCT m.molecule_id)` 改成 `COUNT(bond_id)`，answer unit 被保住；失败原因是命中 `grp-sing-toxicology-269` 后采用了 q269 的 molecule-bridge route，结果保留了 `molecule` 表，执行为 211，而 gold 的 atom-bond direct route 为 273。

库形态：

- 在线库中 patterns 已明显去重：`patterns=3`，`singletons=18`。
- RoleGraph 主 pattern：`[206,249,253,268,277,302,307]`，不再保存 n=2/3/4/6/7 的长串嵌套版本。
- source-route pattern 已形成两个小组：`[269,335]` 与 `[328,338]`，说明 closure 收紧后不再混成大 root，但 source-route branch 选择还没统一到“按当前答案单位选择 route 分支”。

当前结论：

- `doc/pattern_recongnize.md` 的必要硬约束继续满足：不低于 7/18、无 regression、pattern 去重生效、RoleGraph 收益保持。
- r17 首次把 source-route 的 q338 修对，说明 `REROUTE_FACT + answer_unit_preserve` 的主链路已经可用。
- 未达成期望 `>=9/18`：q335 暴露的是 source-route 分支选择问题，不是 rewrite LLM 不遵守 preserve。下一步应让 branch 选择区分“当前答案单位需要保留但输出表可重绑定”与“历史 case 目标输出单位不同”的情况，避免 q269 这类 count-bond 记忆驱动 q335 的 count-molecule 问题。

### 2026-05-09 r18：收紧 reroute 当前绑定，去掉 q335 误触发

审查反馈：

- r17 的 `REROUTE_FACT` fallback 仍有过宽风险：它从历史 canonical op 直接枚举目标 route，没有充分检查当前 SQL 是否确实需要这条 route。
- 对 aggregate answer unit，历史 target output 如果是另一个聚合主体，不能被当作 answer-unit-preserve reroute；q335 命中 q269 后虽然保住了 `COUNT(DISTINCT m.molecule_id)`，但 route 仍按 q269 的 molecule bridge 走，执行错误。

本轮补充修复：

- `REROUTE_FACT` 必须和当前 SQL 的实际 join route 绑定：目标 relation 不能已经存在于当前 SQL，且历史 source route 至少要和当前 SQL 的 join route 有交集。
- 如果当前 SELECT 是 aggregate，且历史 target output refs 的列集合与当前 aggregate 主体不一致，则阻断该 reroute / grain candidate，避免把 `COUNT(DISTINCT molecule_id)` 推向 `COUNT(bond_id)` 这类答案单位变化。
- `answer_unit_preserve=True` 时不再把 `target_output_refs` 传给 rewrite hint，避免 hint 同时说“保留当前答案单位”和“改成历史 target output”。
- online pattern 去重继续收紧：同 action family、同 answer shape、recognition signal 高重叠且 case 集有交集的 pattern 会合并保存，避免 RoleGraph 从嵌套重复变成重叠重复。

r18 结果：

- 运行：`toxicology_focus18_postsel_v1_dmxapi_r18_20260509_212128`
- `final_correct = 8/18`
- `runtime ready = 8/18`
- 修对：`249,253,268,277,285,302,307,338`
- `ready_false = []`
- q335 从 r17 的 `ready but false` 变成 `no_match`，不再误触发。
- q338 保持修对，rewrite 为 route-only：保留 `SELECT DISTINCT a.atom_id`，删除 `connected`，直接 `JOIN atom a ON b.molecule_id = a.molecule_id`。
- 官方 EX：baseline `0/18`，rewrite/full pipeline `8/18 = 44.44%`。

去重检查：

- r18 落盘在线库在本次去重补丁前仍有 6 个 pattern，其中 RoleGraph 有 3 个重叠版本。
- 对 r18 library 静态套用新去重逻辑后，pattern 从 6 个降为 4 个；RoleGraph 合并为一个保存对象，case 集为 `[206,249,253,268,277,302]`。
- 这说明新增的 overlap-merge 能解决“不是严格子集但同根重叠”的重复保存问题；后续真实 run 会在在线演化时直接应用。

审查后补丁：

- reroute 当前 relation 绑定改为 fail-closed：如果当前 SQL 解析不出任何 join relation key，不再允许历史 route fallback 通过。
- aggregate answer-unit mismatch 从“只比较列名”升级为“表+列”比较，避免同名列但不同实体的聚合主体被误认为一致。
- overlap pattern merge 增加共享 case 下限：非子集关系至少共享 2 个 case 才能合并，降低一个桥接 case 把不同 root 串起来的风险。
- 静态探针确认：q335 仍 `passthrough_no_match`；q338 仍 `ready`，动作仍为 `REROUTE_FACT + answer_unit_preserve`，target output refs 不再传入 hint。

当前剩余：

- q335 的正确修复需要一个更准确的 source-route branch：当前系统能阻止 q269 误触发，但还不能从已有记忆中推出 gold 所需的 atom-bond direct route 且重绑定 `COUNT(DISTINCT molecule_id)` 到可保留完整答案域的表。
- RoleGraph 主 pattern 仍缺 q285/307 的完整合并证据，虽然这两个 case 在线已能修对；这属于 pattern 支持集完整度问题，不影响当前 8/18 在线收益。

### 2026-05-10 r19 前补丁：pattern 前置扩充、anti-signal 校验、路径诊断与 final freeze 减负

本轮目标：

- 对齐 `pattern_recongnize.md` 后续要求：新 case 进入时优先扩充已有 pattern，只有扩充失败才重跑完整 admission LLM。
- 降低 toxicology focus18 中重复 admission / 重复 pattern candidate 导致的 LLM 调用和 final freeze 负载。
- 让 q335 这类 source-route pattern 不再被自身 `anti_signals` 排斥。
- 下次路径变化时能直接看到 pattern 为什么被挡、最终为什么走 singleton 或 pattern。

实现改动：

- `pattern_formation.py` 新增 `_try_extend_existing_pattern`：对 focus singleton，先用现有 pattern 的 `bias_recognition_contract` 做 recognition overlap 检查，再用 case-local `score_pair + _pair_supports_root_membership` 做 root membership 确认；通过后直接扩充 pattern 的 `case_ids`，标记 `extended_in_place_v1`，并按 runtime branch signals 轻量挂到最匹配 branch。
- `form_offline_families` 在 `_build_pattern_admission_candidates` 前调用扩充 fast-path；成功扩充的 focus singleton 不再进入完整 admission judge。
- `pattern_formation.py::_validated_bias_recognition_contract_payload` 增加 motif/anti-signal sanity check：source-route / bridge-table-misuse / wrong-join-route 类 motif 不允许把 aggregate answer-unit 信号作为 anti；被丢弃项写入 `anti_signals_dropped_by_motif_conflict`。
- `pattern_admission_judge` prompt 增加 anti-signal 边界：anti 必须表示对立 bias，不得把同一 bias 内的 branch/accessory detail 当排斥条件。
- `evolution.py` 的 same-root dedup 判据改为返回 `(decision, audit)`；失败原因会写入 `pattern_dedup_audit`，包括 case overlap、action family、bias shape、signal jaccard 等字段。
- `runtime.py` trigger audit 增加 `path_choice`：记录 selected kind、通过的 pattern/singleton 候选，以及 `bias_recognized=True` 但被后续 gate 挡掉的 pattern blocker。
- `promotion.py` 在 `branch_member_replay` 中识别 `local_evolve_lightweight_binder_gated` / `extended_in_place_lightweight_signal_gated` branch；final freeze 对这些 branch 跳过 rewrite SQL execution，只保留 binder/compile 路径诊断，避免重复外部 IO。

静态验证：

- `python -m py_compile common/learning/pattern_formation.py common/learning/evolution.py common/runtime/runtime.py common/learning/promotion.py common/llm/prompts/pattern_admission_judge.py` 通过。
- anti-signal 校验探针通过：source-route motif 下 `has_aggregate_in_select` / `answer_unit_scalar_aggregate` 不再保留为 anti，且会记录 dropped audit。
- trigger compact audit 探针通过：`path_choice` 可被落入 runtime audit summary。

待真实 run 验收：

- r19 focus18 期望：在线收益不低于 r18 的 8/18、无 regression。
- `pattern_extension_count > 0`，且 shared_insight / pattern_admission LLM 调用明显少于 r18。
- final library 中 RoleGraph 不再重复保存多个嵌套/重叠 pattern。
- q335 至少能看到 source-route pattern 的 recognition 不再被 `anti_signal_hit:has_aggregate_in_select` 挡掉；是否最终修对取决于 branch route 实例化质量。

r19 实测：

- 运行：`toxicology_focus18_postsel_v1_qwen3coderflash_20260510_054923`
- `final_correct = 8/18`，`runtime ready = 8/18`，`regression = 0`，与 r18 持平。
- 修对：`249,253,268,277,285,302,307,338`。
- 路径质量改善：q253/q268/q277/q285/q302/q307 均已通过 pattern 触发；q249 仍走 singleton，原因是它到达时只有 q206 一个先例，尚未形成 pattern。
- final library：`patterns=5`，`singletons=18`。RoleGraph 主 pattern 已包含 `[206,249,253,268,277,285,302,307]`，完整覆盖本轮强 RoleGraph 组。
- q335 仍 `no_match`。本轮 anti-signal 不再保留 `has_aggregate_in_select` / `answer_unit_scalar_aggregate`，但 q335 在线时 source-route pattern 仍未进入 top candidate 的 bias-recognized 路径；后续需要检查 source-route pattern 的 recognition signals 是否能从 q335 当前 SQL 抽出，而不只是去掉错误 anti。
- final freeze 完成但耗时约 1 小时。trace：`shared_insight_judge=124`、`action_compiler=43`、`hint_instantiation=43`、`memory_rewrite=25`。说明本轮 E 只减少 branch_member SQL execution，未解决 final freeze 中 full/LOO replay 的 LLM 复算负载。
- A 的第一版实现没有真正生效：`extended_pattern_candidates=0`。原因是扩充阶段从 singleton memory 抽取的 bias signals 少于 runtime case view，RoleGraph singleton 只能得到粗粒度 pair/output 信号，达不到 pattern recognition overlap。

r19 后补丁：

- `_bias_signals_for_group` 增加从 singleton `trigger_contract` / `canonical_discriminants` 反推出 recognition signals 的逻辑：`has_direct_relation_join`、`same_relation_two_role_sides`、`no_distinct_on_pair_output` 等现在可从已沉淀 singleton 中得到。
- 静态探针确认：q302 singleton 对 RoleGraph pattern 的 recognition overlap 从不足阈值补齐为 `6/6`。
- `_try_extend_existing_pattern` 不再在 fast-path 内补跑缺失 pair 的 `score_pair`，避免“快速扩充”反而触发新的 shared-insight LLM 调用；它只使用本轮 retrieval 已经算好的 pair_scores。
- `compact_evolution_report` 现在透传 `pattern_extension_candidates` 和 `pattern_dedup_audit`，下轮可以直接看到扩充失败原因。

当前判断：

- 在线收益和 pattern 路径已经稳定达到 r18 水平；RoleGraph 的 pattern 化目标基本达成。
- 还未完成的是 A 的真实在线减负验收与 q335 source-route 召回。下一轮必须先看 `pattern_extension_count` 是否大于 0，以及 final freeze LLM trace 是否下降；如果仍慢，问题不在 admission 扩充，而在 final freeze formal/LOO replay 仍重跑完整 compiler/rewrite。

### 2026-05-10 r19 复用产物局部验证与补丁

复用验证结论：

- D/E 已在 r19 实测有效：`path_choice` 正常落盘，`promotion_replay_rows` 中出现 `lightweight_validated_in_local_evolve`。
- A 的 f3a2b3a 后补丁已做局部函数级验证：从 r19 `final_library.json` 中取 RoleGraph pattern，临时移除 q302，再用 q302 singleton 走 `_try_extend_existing_pattern`，结果 `extended=True`，recognition overlap `6/6=1.0`，成功扩回 `[206,249,253,268,277,285,302,307]`。
- B 的 dedup 函数本身有效：对 r19 `final_library.json` 静态调用 `_merge_patterns_without_absorbing_singletons`，RoleGraph 8-case 与 7-case 真子集会合并，只保留 8-case；`pattern_dedup_audit=12`。

新定位：

- r19 final library 中 8-case validated pattern 与 7-case audit-only pattern 并存，不是 dedup 判据错，而是 replay-gated promotion 分支 `integrate_promoted_groups` 之后没有再跑 same-root dedup。
- 已补：`evolve_library_with_replay` 在 replay-gated promotion 和 no-replay integrate 分支之后都会调用 `_merge_patterns_without_absorbing_singletons`。下次 final freeze 不应再保留 RoleGraph 7-case 子集副本。
- 上一轮从 `library_snapshots/step_*.json` 直接验证失败，是因为 snapshot 是 compact 版本，不含 `core_interface/instantiation_program/trigger_signature`，不能作为 full `LibraryStateV2` 输入。快速验证应使用 `final_library.json` 或 case work 中完整 `eea_update_response` 里的非 compact 对象；compact snapshot 只能看计数，不能跑构建逻辑。

下一步快速验收方式：

- 不需要先跑完整 18 条。先构造一个 7-case RoleGraph 小序列，或直接复用 r19 full `final_library.json` 做局部 extension/dedup probe。
- 若要验证在线 A 是否真正减少 LLM 调用，跑最小序列 `206,249,253,268,277,302` 即可：期望 q302 的 `pattern_extension_count=1`，并且本轮 `pattern_admission_judge` 调用数低于 r19 同前缀。
- 只有这个小序列通过后，才值得跑完整 focus18 r20。

### 2026-05-10 涌现原则审计第 1 步：移除 motif↔anti_signal 硬编码互斥表

背景：

- 在系统盘点"预定义规则与硬编码"时确认 `_BIAS_MOTIF_INCOMPATIBLE_ANTI_SIGNALS` 是从 q335/q338 等具体案例反推出来的规则——"source_route / wrong_join_route / bridge_table_misuse motif 不能把 has_aggregate_in_select / answer_unit_scalar_aggregate 设为 anti_signal"。
- 这违反 EEA 的核心设计原则："信号必须从案例中涌现，不能用预定义规则去拟合特定案例"。bias 之间的相容性必须由多案例聚合给出，不应靠人手维一张静态对照表。
- 同时 r19 anti-signal 校验段落里还有一段 source-route 专用补丁：当 motif 文本命中 source_route / bridge / reroute 等关键词时，强行删除 aggregate 类 anti_signal、强行注入 has_join_chain_via_bridge_table、把 sigs 截到 6 项。这段同样是案例拟合。
- prompt 端镜像了同一规则："Forbidden anti_signal example: for source-route / wrong-join-route / bridge-table-misuse patterns, do not set has_aggregate_in_select or answer_unit_scalar_aggregate as anti_signals."

实现改动：

- `pattern_formation.py` 删除 `_BIAS_MOTIF_INCOMPATIBLE_ANTI_SIGNALS` 字典定义。
- `pattern_formation.py::_validated_bias_recognition_contract_payload` 删除：motif_text 拼接、incompatible 集合扫描、source_route_bias 补丁段、anti_signals_dropped_by_motif_conflict 输出字段。函数仅保留：词表过滤、去重、anti 不与 sig 重叠、3-6 个 sig 数量约束、min_signal_overlap 阈值规范化。
- `pattern_admission_judge.py` 删除 prompt 中 "Forbidden anti_signal example" 段。保留前一条抽象指引（"Choose anti_signals only when their presence means the current case exposes a different or opposing bias_motif"），因为这不是案例特定规则。

验证：

- 全部测试基线对比：改动前 / 改动后均 19 项 pre-existing 失败，集合完全一致；本次改动 0 回归、未意外改变行为。
- pre-existing 失败列表与 `experience_families` 已禁用 / runtime gate 名称变更等历史改造一致，与本次改动无关。

涌现机制的替代路径（待实现）：

- bias 之间的相容性应由多案例共现给出否定证据：若某 pattern 的 anti_signal 在历次新案例中频繁出现却没造成误触发，自动降权 / 退役；若频繁误触发，自动加强。
- 该机制属于"涌现原则审计"后续步骤，不在本次改动范围内。本次仅去掉硬规则，先看涌现机制能否自我支撑——即在没有人工互斥表的情况下，q335/q338 等 source-route 案例的 anti_signals 是否仍通过 admission judge 的多案例上下文形成合理设置。

下一步：

- 在 r19 已有库基础上，dump 所有 pattern 的 `bias_recognition_contract` 看 motif/anti_signals 文本分布，作为修改 2（词表开放化）的输入证据。
- 若 r20 在线 trigger 因移除补丁而退化（某些 source-route pattern 因 anti_signals 含 aggregate 信号被自身排除），那就是涌现机制确实欠缺信号——届时再考虑修改 4（信号生成路径重写），而不是把硬规则加回来。

### 2026-05-10 emergence_refactor WU0：默认跳过 final freeze 与 pattern 签名 dump

改动目标：

- 按 `doc/emergence_refactor_plan.md` 启动 WU0，但结合当前执行要求，把 final freeze 默认设为跳过，避免每轮在线验证被末尾 replay/freeze 拖慢。
- 增加独立签名 dump 工具，用于在不重跑实验的情况下审查当前 pattern 的 case_ids、trigger contract、repair insight 和 admission 产物。

实现：

- `common/learning/evolution.py::final_evolve_and_freeze` 新增 `skip_replay_freeze`，跳过时直接保留当前 incremental library，写 `final_freeze_skipped=true` 和 `promotion_skipped_reason=skip_final_freeze`。
- `common/config.toml` 新增 `[evolution] skip_final_freeze = true`；`common/core/config.py` 增加 `EvolutionSettings`，默认跳过。
- `cli/run_online_e2e_validation.py` 增加 `--skip-final-freeze`，默认读取 config；summary/family event 记录 `skip_final_freeze` 和 `final_freeze_skipped`。
- `cli/run_multidb_validation.py` 接受 `--skip-final-freeze` 作为兼容参数，但该 trigger/rewrite runner 本身不执行 final freeze。
- 新增 `cli/audit_pattern_signatures.py`，支持 `--library_json` 或 `--work_root`，输出 compact pattern signature，避免把完整 canonical refs/action payload 打进审计文件。

验证：

- `python -m py_compile common/core/config.py common/learning/evolution.py cli/run_online_e2e_validation.py cli/run_multidb_validation.py cli/audit_pattern_signatures.py` 通过。
- `PYTHONPATH=/data/liuyining/ace4sql python - <<... load_config ...>>` 返回 `True`，确认默认跳过生效。
- `final_evolve_and_freeze(library=empty, skip_replay_freeze=True)` 返回 `final_freeze_skipped=True`，compact report 同步记录。
- `audit_pattern_signatures.py` 在 `toxicology_focus18_postsel_v1_qwen3coderflash_20260508_r5/final_library.json` 上成功导出 9 个 pattern 的摘要。

### 2026-05-10 emergence_refactor WU1：Schema role annotator

改动目标：

- 为后续撤除 `_column_role` 英文启发式建立替代来源：每个 `LocalSchemaView` 的列角色由 schema-level 缓存/LLM 标注得到，`role_family` 是自由短语，不是闭集词表。
- 避免继续从列名硬猜 `identifier/name/measure` 这类预定义角色。

实现：

- 新增 `common/analysis/schema_role_annotator.py`：入口 `annotate_schema_roles(local_schema_view, db_id)`，优先读 `{cache_root}/schema_roles/{db_id}.json`，缺失时调用 `schema_role_annotator` prompt，写回缓存；LLM/cache 失败时不抛错。
- 新增 `common/llm/prompts/schema_role_annotator.py`：要求输出自由命名 `role_family`，不限制词表；强调不能把表名、库名、case id、SQL alias 写进角色名。
- `common/io/local_schema.py::build_local_schema_view` 在构造 `LocalSchemaView` 后调用 annotator。
- `common/io/db_schema_access.py` 不再用 `_guess_role_family`，只保留 database description note；`role_family` 由 annotator 负责。

验证：

- `python -m py_compile common/analysis/schema_role_annotator.py common/io/local_schema.py common/io/db_schema_access.py common/llm/prompts/schema_role_annotator.py common/llm/prompts/__init__.py` 通过。
- 静态检查确认 `_guess_role_family` / naming-pattern role guess 在 `common/io`、`common/analysis` 中已无命中。
- 用临时 cache 验证 `posts.LastEditorUserId` 可被映射为缓存中的 `"editor reference"`，且不会触发 LLM。

### 2026-05-10 emergence_refactor WU2：accumulate 输出 pre-condition 字段

改动目标：

- 在唯一 gold-aware 的 accumulate 阶段，让 `error_instance_extractor` 记录当前案例的前置 question/sql 特征、审计到的失败现象和修复方向。
- 这些字段后续由 admission 抽象成 pattern 级契约，runtime 只读 pre-condition，不读 observed_failure 作为对错判断。

实现：

- `ErrorInstanceV2` 新增 `pre_question_signature_local`、`pre_sql_signature_local`、`observed_failure_local`、`repair_direction_local`。
- 新增 `PatternRecognitionContract`，并在 singleton 的 `InstantiationProgram.pattern_recognition_contract` 上挂载本 case 的局部契约。
- `error_instance_extractor` prompt 新增 4 字段定义；R1/R6 从绝对强制语气软化为机制级强建议，避免 LLM 被旧硬规则绑死。
- `run_error_instance_extractor` 解析并截断 4 字段到 200 字符。
- `build_formation_signals` 写入 `formation_signals.pre_condition_local`；`build_trigger_contract` 同步写入 `trigger_contract.pre_condition`。

验证：

- `python -m py_compile common/core/data_structures.py common/llm/prompts/error_instance_extractor.py common/llm/nodes.py common/analysis/signal_summary.py common/learning/accumulate.py` 通过。
- 构造最小 `ErrorInstanceV2` 静态探针，确认 `formation_signals.pre_condition_local.pre_question_signature_local` 和 `trigger_contract.pre_condition.repair_direction_local` 正常落盘。

### 2026-05-10 emergence_refactor WU3：admission 改为涌现 pre-condition 契约

改动目标：

- pattern admission 不再要求 LLM 从 14 个固定现象信号中选择 `recognition_signals`。
- admission 改为从多个 case 的局部 pre-condition 中抽象出 pattern 级 question/sql signature、audit failure summary 和 repair direction。

实现：

- `pattern_admission_judge` prompt 删除 `BIAS_RECOGNITION_SIGNAL_VOCABULARY` 导入、闭词列表注入和 `bias_recognition_contract` 输出 block。
- prompt 新增 `pre_question_signature`、`pre_sql_signature`、`observed_failure_summary`、`repair_direction` 的定义和输出要求。
- `_pattern_case_card` 与 `_stable_bias_frame` 现在向 LLM 输入每个 member 的 `pre_condition_local` 四字段。
- `_build_pattern_candidate` 将 admission 的四字段校验后写入 `InstantiationProgram.pattern_recognition_contract`；新 pattern 不再写入新的 `bias_recognition_contract`。
- 删除 admission response 的 `_validated_bias_recognition_contract_payload` / `_attach_validated_bias_recognition_contract` 路径。

验证：

- `python -m py_compile common/learning/pattern_formation.py common/llm/prompts/pattern_admission_judge.py` 通过。
- prompt 构造探针确认输出中已无 `bias_recognition_contract`，且包含 `pre_question_signature` / `pre_sql_signature` / `repair_direction`。

### 2026-05-10 emergence_refactor WU4：runtime 两通道 pre-condition trigger

改动目标：

- runtime 不再依赖 14 闭词 jaccard 作为 pattern 轻识别主路径，改为判断当前 question/pred_sql 是否符合 memory 中的 pre-condition 描述。
- 触发仍 answer-blind：只看 question、pred_sql、schema role hints，不看 gold/执行正确性。

实现：

- 新增 `common/llm/prompts/pattern_pre_condition_match.py`，包含 Q 通道和 S 通道两个小 prompt。
- `TriggerCandidateAudit` 新增 `pre_condition_match` / `pre_condition_matched`。
- `runtime.py` 新增 `_pattern_recognition_contract`、`_evaluate_pattern_pre_condition`、Q/S 通道 LLM 调用和磁盘 cache。
- `_gate_group` 中，若 memory object 有 `pattern_recognition_contract` 或 `trigger_contract.pre_condition`，先做两通道匹配；匹配成功后设置 `pre_condition_matched`，旧 required/variant/decisive signal miss 不再阻塞该触发路径。
- pattern 仍必须通过 branch selection / binder dry-run / compiler dry-run；pre-condition 只解决“是不是同类前置状态”，不直接放行 rewrite。
- runtime audit summary 改为记录 `stage_1_pre_condition_matched_count`、`stage_1_pre_condition_missed_count` 和 `pattern_rejected_with_pre_condition_matched`。

验证：

- `python -m py_compile common/runtime/runtime.py common/core/data_structures.py common/llm/prompts/pattern_pre_condition_match.py common/llm/prompts/__init__.py` 通过。
- 静态 monkeypatch 探针验证 `_evaluate_pattern_pre_condition` 在 Q/S 均匹配时返回 `ok=True`，并产生 channel audit。

### 2026-05-10 emergence_refactor WU5：撤除固定 bias 闭词与闭词扩充路径

改动目标：

- 删除 `has_pair_role_side_output` / `same_relation_two_role_sides` / `select_arity_ge_2` 等固定 LEVEL_2 现象词表，不再让 pattern trigger 或 pattern 扩充依赖人工预定义闭词。
- 保留旧库反序列化兼容字段，但新库构建、runtime trigger、pattern extension、dedup 都不再写入或读取 `bias_recognition_contract`。

实现：

- `common/core/vocabulary.py` 删除 `BIAS_RECOGNITION_SIGNAL_VOCABULARY`。
- `common/runtime/runtime.py` 删除 `compute_bias_recognition_signals`、`_bias_recognition_contract`、`_case_bias_recognition_signals`、`_evaluate_bias_recognition`，`build_runtime_case_view` 不再注入 `bias_recognition_signals`。
- `common/learning/pattern_formation.py` 删除 `_bias_signal_from_runtime_signal`、`_bias_signals_for_group`、`_branch_signal_set`、`_fallback_bias_recognition_contract`；`_try_extend_existing_pattern` 改为使用每个案例自然抽取/抽象出的 `pattern_recognition_contract` 与 case-local pair root evidence。
- `common/learning/evolution.py` 的同根去重不再看 `recognition_signals` jaccard，改为比较 pattern pre-condition key 与 action family。
- `common/runtime/action_compiler.py` 的 DISTINCT 依赖不再读 closed bias signal，改为根据当前 SQL 输出形状判断：drop projection 类动作在当前非 distinct、非 aggregate、多列输出上可能需要 `SELECT_ENFORCE_DISTINCT`。
- `common/core/data_structures.py` 将 `bias_recognition_signals` / `bias_recognition_contract` 明确标为 legacy compatibility；`cli/audit_pattern_signatures.py` 输出改名为 `legacy_bias_recognition_contract`。

验证：

- `python -m py_compile common/core/vocabulary.py common/core/data_structures.py common/runtime/runtime.py common/runtime/action_compiler.py common/learning/pattern_formation.py common/learning/evolution.py cli/audit_pattern_signatures.py` 通过。
- `rg "BIAS_RECOGNITION_SIGNAL_VOCABULARY|compute_bias_recognition_signals|_bias_signal_from_runtime_signal|_bias_signals_for_group|_fallback_bias_recognition_contract|_validated_bias_recognition_contract_payload|_attach_validated_bias_recognition_contract" common cli` 0 命中。
- `rg "bias_recognition_contract|recognition_signals|anti_signals|bias_recognition_signals" common/runtime common/learning common/runtime/action_compiler.py` 0 命中；仅数据结构 legacy 字段和 audit 输出兼容项保留。

### 2026-05-10 emergence_refactor WU6：撤除列名角色启发、桶化和 hard-signal 黑名单

改动目标：

- 不再从列名字符串硬猜 `identifier/name/code/measure` 等角色，避免英文 schema 规则污染跨库泛化。
- 不再把计数压成 `0/1/2/3plus` 桶；触发信号保留原始整数。
- 不再维护“这些信号太 broad 所以过滤”的固定黑名单；除 `program.*` 运行期不可复现信号外，其余 answer-blind 信号允许进入 audit/trigger 比对。

实现：

- `common/analysis/role_graph_normalizer.py` 删除 `_column_role`，`_schema_column_role` 只读 `LocalSchemaView.semantic_hints`，缺失返回 `unknown`。
- `common/runtime/action_compiler.py` 删除 `_column_role_from_name`，`_get_column_role` 只查 schema semantic hints，缺失返回 `None`。
- `common/analysis/signal_summary.py` 和 `common/runtime/runtime.py` 的 `_bucket_count` 保留函数名但返回原始 int；相关字段从 `*_bucket` 改为 `join_count` / `table_count` / `predicate_count` / `predicate_literal_count`。
- `signal_summary._non_broad_trigger_signals` 与 `runtime._is_substantive_hard_signal` 只过滤空信号和 `program.*`。
- `error_instance_extractor` 的 R1/R6 强制语气已在 WU2 中软化，本 WU 复核未再改。

验证：

- `python -m py_compile common/analysis/role_graph_normalizer.py common/runtime/action_compiler.py common/analysis/signal_summary.py common/runtime/runtime.py` 通过。
- `rg "join_count_bucket|table_count_bucket|predicate_count_bucket|predicate_literal_count_bucket|3plus" common/analysis common/runtime common/learning` 0 命中。
- `rg "def _column_role\\b|_column_role_from_name|_column_role\\(" common/analysis common/runtime common/io` 仅剩 `_schema_column_role` 与调用点。
- 静态探针确认 `_bucket_count(5) == 5`，`_is_substantive_hard_signal("pred.has_aggregate=False") == True`，`program.*` 仍被过滤。
