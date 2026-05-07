# EEA 实验记录

本文档是 EEA 当前系统的统一实验账本。以后每一次聚类、触发、在线累计、端到端相关尝试都记录在这里，不再拆分多个实验文档。

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
