# EEA 涌现化改造执行计划 v2(机制重建版)

> 维护：本文档是基于 v1 计划 (`emergence_refactor_plan.md`) 全量落地(WU0-WU13)+ 11 库实测结果 + 独立诊断报告综合而成的下一轮改造方案。
>
> 目标：把 v1 用 LLM 自由文本贯穿全链路的设计**收敛为 5 个接口改造**,每个接口有代码端结构化兜底,杜绝 LLM 跨上下文不一致导致的级联失效。
>
> 起点 commit：`667a940`(本轮 11 库实测基线 commit;v1 WU0-WU13 全部落地)。
>
> 主分支：`eea-repair-interface-v1`(v1 改造分支),v2 新分支 `eea-mechanism-rebuild-v2`。
>
> 关联文档：
> - `emergence_refactor_plan.md` — v1 执行计划(WU0-WU13 + 阶段 A-F)
> - `full11_analysis_r1.md` — 11 库全量实测分析 + 5 改造提案(§7)
> - `experiment_log.md` — 历次改动决策日志

---

## 0. 背景与目标

### 0.1 v1 完成情况与暴露的根本问题

**v1 落地结果**(11 库 baseline 1117 → enhanced 1101,净 **-16**):

| 库 | total | baseline | enhanced | Δ |
|---|---|---|---|---|
| california_schools | 89 | 61 | 61 | 0 |
| card_games | 191 | 126 | 121 | -5 |
| codebase_community | 186 | 137 | 137 | 0 |
| debit_card_specializing | 64 | 47 | 45 | -2 |
| european_football_2 | 129 | 99 | 100 | +1 |
| financial | 106 | 77 | 77 | 0 |
| formula_1 | 174 | 120 | 119 | -1 |
| student_club | 158 | 135 | 133 | -2 |
| superhero | 129 | 114 | 114 | 0 |
| thrombosis_prediction | 163 | 106 | 94 | **-12** |
| toxicology | 145 | 95 | **100** | **+5** |
| **总计** | **1534** | **1117** | **1101** | **-16** |

WU0-WU13 全部按设计落地(代码层面),但 9 个 WU 的核心假设被实测证伪(详见 `full11_analysis_r1.md §6`)。

**实测暴露的根本问题分四类**:

| 类 | 卡在哪一层 | 典型案例 | 损失 |
|---|---|---|---|
| **类 I 没聚类** | learning(retrieval bucket 错位) | financial 0 pattern;codebase q581/q582 同源 case 不同 bucket | ~30+ case |
| **类 II 聚了但不触发** | runtime(LLM Q+S abstract↔concrete 不对齐) | EF2 pat-1035-1045 13 member 0 触发 | ~15 case |
| **类 III 触发但不能实例化** | compiler(canonical_op 在新 schema 不可绑定) | toxicology q335 `branch_binder_no_candidates` | ~5 case |
| **类 IV 错误触发并改坏** | runtime+compiler+deepeye 三层失守 | thrombosis 13 + card_games 5 + ... | **22 case (= 全部 -16 损失)** |

**v1 设计方法论错位**:

1. **LLM 自由文本贯穿全链路**:retrieval bucket key(`pattern_formation.py:129 _repair_insight_interface_key`)、admission contract(LLM 写的 pre_question_signature / pre_sql_signature)、runtime 匹配(`runtime.py:1431 _pre_condition_channel_call`)三处 LLM 协作,**没有代码端约束**。同源 case (q581/q582) 写不同 interface_key → 不同 bucket → 永远不会被 admission 见到。
2. **多处 LLM 抽象度不一致**:`pattern_extension_pre_condition_equivalence`(`pattern_formation.py:2694`)看两段抽象 contract 倾向宽容,`pattern_pre_condition_q/s`(`runtime.py:1431`)看抽象 vs 具体倾向严格 → EF2 pat-1035-1045 通过 extension 接收 13 member 但 runtime 0 触发。
3. **canonical_op IR 存 seed gold 字面值**(`action_compiler.py:1398 _target_output_refs_for_action`):`synthesized_program.ops[*].role_refs` 里 source="target_sql" 的项保留了 seed gold 的 table/column/expression 字面值,ActionCompiler 跨 case 复用 → 5 个库 22 个 regress 同根因。
4. **regression 没有负反馈**(`run_single_db_e2e.py:2716 local_gate_wrong = baseline_correct is not True`):s0=对、final=错 全跳过 accumulate,EEA 不学,同 pattern 连环误触发。
5. **DeepEye selector 默认关闭**(`run_single_db_e2e.py:2615 if not args.postsel_select_after_rewrite`):default False → `guard_accepted_direct_s1`。thrombosis 18 触发中 16 个 `direct_accept_s1`,0 个 `selector_choose_s1`,**EEA 改坏一律直接进入用户结果,无安全网**。
6. **self_recall 是过拟合检验不是泛化检验**:thrombosis pat-1172-1257 self_recall=1.0 通过 WU9 gate,后 16 次触发 13 次造灾(`final_library.json` 实证)。
7. **confidence 字段实测无区分度**:读 `workspace/pre_condition_cache.json` 1000 条 entry,matches=true 平均 confidence=0.97,matches=false 平均 0.947,加阈值方案实测无效。

### 0.2 v2 改造目标

把 v1 的"LLM 自由文本贯穿"改造为"**5 个结构化接口 + LLM 在每个接口下从事限定职能**":

1. **retrieval 用代码端结构化 repair card 做召回**,LLM 只判断候选 pair 是否同根偏差(不负责生成 key)
2. **pattern 拆三 contract**(recognition / applicability / binding),每个 contract 有代码端可检验字段,**LLM 输出受结构约束**
3. **canonical_op IR 剥离 seed 字面值**,改存 role_family / path_role,ActionCompiler 在新 case 重新派生 target
4. **runtime 拆五层判断**(recognition → applicability → binding → compile → rewrite),任何一层失败 no_action
5. **regression 进入 negative feedback**,给已触发 group 加 negative_guard;DeepEye 端默认开启 S0/S1 selector 作为运营层安全网

### 0.3 不在范围内的事项

- 重新引入 family 层(v1 已取消,决策不变)
- 删除 `effect_axis` 12 闭值(保留作为 retrieval 粗筛轴大类)
- 删除 Locus / OpFamily / TargetFamily / ActionPrimitive 等结构事实 Enum(SQL 编辑坐标系,保留)
- 增量加 WU14/15/16 这种 prompt 补丁路径(v1 经验:增量补丁救不了根本问题)
- ContrastiveRepairEffect 的 source_state runtime 投影(独立大改造,留 v3)
- 重新引入旧 14 phenomenon 闭词或 `_column_role` 启发式(已撤,不回退)

### 0.4 与 v1 的关系

| v1 设计 | v2 处理 | 理由 |
|---|---|---|
| WU0 final_freeze skip | 保留 | 节省 1h,虽然损失 cross-pattern replay 安全网,但 v2 用 negative feedback 替代 |
| WU1 schema_role_annotator | 保留 + 配套预热 | annotator 工作正常,但加启动时全表预热(`io/local_schema.py:202`) |
| WU2/WU3 pre_condition 字段 | **重写为三 contract**(改造 ④) | 单一 contract 不足以表达 applicability/binding,LLM 写自由文本会失控 |
| WU4 runtime 2 通道 | **重写为五层判断**(改造 ④) | 单一 LLM Q+S gate 是 abstract↔concrete 不对齐根因 |
| WU5-WU8 cleanup | 保留 | 撤 14 闭词、_column_role 等已落地,不回退 |
| WU9 self_recall gate | 降级为最低保护 | 不再当泛化检验,只做"对自己 seed 不能识别就废弃"的兜底 |
| WU11 admission self_check | **替换为 grounded anchors 验证**(改造 ⑤) | LLM 自报 estimated_recall 与实际 runtime 脱节 |
| WU12 online self_recall | 保留(配合 WU9) | 落地正确 |
| WU13 prefilter | 保留(配合改造 ①+②) | top-5 仍有效控制成本 |

v2 不是"v1 的补丁",而是**在 v1 基础设施上重建 5 个接口**。

---

## 1. 设计原则

### 1.0 WUv2-2 与 WUv2-5 的职责分工(2026-05-11 修订加入)

第一次执行 WUv2-2 时把边界扩到"撤字面值 + 在新 schema 用 role_family 重新派生 target"。实测 q366 探针发现这条路径让"seed 字面列泄漏"变成"seed target role 泄漏",随后只能用 answer-focus guard 这类启发式 patch 拦截 —— 这正是 v1 反思的"看到错误后加 case-specific 启发式"反模式。

修订后的硬职责边界:

| 阶段 | 职责 | **绝对不允许做** |
|---|---|---|
| **WUv2-2** | 保守阻断:撤 seed 字面值 + **阻断所有依赖 seed target binding 的 primitive 在新 case 上工作**(直接 no_action)| 不允许在新 case schema 用 role_family / path_role / evidence 文本对齐等任何方式**派生 target_columns**;不允许加 answer-focus / token-match 等启发式 guard |
| **WUv2-5** | 用结构化恢复:LLM admission 时输出 `binding_contract`(含 source/target slots 的 role_family,**不含字面列名**),配合 `applicability_contract` 谓词代码端检验;runtime 时只有声明了完整 binding_contract 的 pattern 才允许走 SELECT_REPLACE/SWITCH/REROUTE 等改变 answer unit 的 primitive | 不允许把 WUv2-2 阻断的逻辑放宽;不允许跳过 applicability_contract 检验 |

**核心原则**:WUv2-2 是"保守阻断,不负责恢复 helped";WUv2-5 是"用结构化 binding 合法恢复"。两阶段之间的 helped 数下降是预期行为,不在 WUv2-2 内救。

### 1.1 LLM 职能边界(从"贯穿"到"在结构化接口下做有限判断")

| 角色 | v1 现状 | v2 改造 |
|---|---|---|
| **retrieval bucket key 生成** | LLM 写自由 interface_key(`_repair_insight_interface_key`)| 代码端从 schema_role_annotator + role_graph 派生 `(source_answer_unit, target_answer_unit, operation_family)` 四元组 |
| **pair 候选评分** | code 端 score_pair 已存在 | 保留,但召回扩大(WU2) |
| **pair 同根偏差判断** | shared_insight_judge | 保留,LLM 只判 "yes/no" 不再生成 key |
| **pattern recognition_contract 生成** | admission LLM 写自由 signature | LLM 在 schema_role_annotator 的角色词汇下写 signature + 强制 grounded_anchors 数组(改造 ⑤) |
| **pattern applicability_contract 生成** | 无 | LLM 写 `must_hold_before_rewrite` + `negative_guards`,字段必须可代码端检验(改造 ④) |
| **pattern binding_contract 生成** | 隐含在 synthesized_program 中,含字面 table/column | LLM 写 source/target role profile,**不写字面值**(改造 ② + ④) |
| **runtime 触发判断** | 单一 LLM Q+S 通道 | 五层判断,LLM 只在 recognition 层判 "matches=bool"(改造 ④) |
| **runtime rewrite SQL 生成** | LLM 写最终 SQL | 保留(DeepEye memory_rewrite) |

### 1.2 Runtime 边界(强化 answer-blind 硬约束)

v1 已明确"runtime 只看 case 已携带的特征",但实测仍有滑动倾向(`full11_analysis_r1.md §7.1.1 修正 4`)。v2 强化为:

- **recognition 层**:LLM 只判"特征是否落入 pattern 描述区域",**不暗示对错**(prompt 末尾不出现 "correct"/"wrong"/"error"/"needs repair" 等字眼)
- **applicability 层**:**纯代码检验**,对照 pattern.applicability_contract 的 `must_hold_before_rewrite` 字段(都是结构化可机械判定的谓词,如 `current_select_uses_count_star: true` / `join_path_includes_one_to_many: true`)
- **binding 层**:**纯代码派生**,在当前 LocalSchemaView 上根据 binding_contract.source_slots/target_slots 的 role_family 找候选列
- 任何一层失败 → no_action;不靠下游 LLM 自由发挥兜底

### 1.3 Pattern 三 contract 字段语义

| Contract | 字段 | 性质 | runtime 用法 |
|---|---|---|---|
| **recognition_contract** | `question_precondition` | 自然语言,LLM 写 | LLM 通道 Q 匹配 |
| | `sql_precondition` | 自然语言,LLM 写 | LLM 通道 S 匹配 |
| | `grounded_anchors[]` | 结构化 `{kind, role/path/relation, value}` | 代码端短路:无任何 anchor 在新 case 命中直接 false |
| **applicability_contract** | `required_answer_unit_change` | 枚举值(`row_count→distinct_entity_count` 等) | 代码端检验当前 case 是否符合 |
| | `must_hold_before_rewrite[]` | 谓词数组(代码可机械判定)| 代码端检验全部 hold |
| | `negative_guards[]` | 谓词数组 | 代码端检验全部 not hold |
| **binding_contract** | `source_slots[]` | `{kind, role_family, optional}` 元组 | 代码端在当前 schema 找候选 |
| | `target_slots[]` | 同上 | 同上 |
| | `allowed_operations[]` | ActionPrimitive 集合 | 限制 ActionCompiler 候选 |

**关键约束**:
- recognition_contract 不允许 LLM 不填 grounded_anchors。无 anchor 的 pattern 由 admission 直接拒绝
- applicability_contract 不允许 LLM 写自由文本谓词,只能从 v2 引入的有限谓词词表选(可扩展但 admission 时必须从词表内选)
- binding_contract 的 source_slots/target_slots **不允许字面 table/column**,只能用 role_family

---

## 2. 关键概念定义

### 2.1 结构化 repair card(替换 v1 LLM-written interface_key)

每个 wrong case 入 accumulate 时,**代码端**派生 repair card:

```json
{
  "db_id": "thrombosis_prediction",
  "case_id": "1172",
  "effect_axis": "aggregation_unit_delta",
  "source_answer_unit": {
    "kind": "row_count",
    "primary_table": "Laboratory",
    "join_path_signature": "Patient -[1:m]-> Laboratory"
  },
  "target_answer_unit": {
    "kind": "distinct_entity_count",
    "entity_table": "Patient",
    "entity_role_family": "primary identifier"
  },
  "operation_family": ["replace_aggregate_arg", "add_distinct"],
  "changed_locus": ["SELECT"],
  "preserve_invariants": ["where_predicates", "join_scope"],
  "negative_conditions": []
}
```

字段说明:
- `effect_axis`:沿用 v1 12 闭值大类
- `source/target_answer_unit`:**代码端**从 schema_role_annotator + role_graph_normalizer 派生(不依赖 LLM)
- `operation_family`:从 structure_delta_v2 派生(ADD_SELECT_SLOT / DROP_SELECT_SLOT / REPLACE_SELECT_SLOT / ADD_DISTINCT 等)
- `changed_locus`:沿用 Locus enum
- `preserve_invariants`:从 AST diff 派生

**作为 retrieval bucket key 的哈希**:`(effect_axis, source_answer_unit.kind, target_answer_unit.kind, sorted(operation_family))` → 同源 case 自然汇入同一 bucket

### 2.2 Grounded anchors(替换 v1 LLM 自由 signature 文本)

每个 pattern.recognition_contract 必须含 ≥ 2 个 grounded_anchors,每个 anchor 是:

```json
{"kind": "column_role", "role_family": "primary identifier", "table_hint": "Patient"}
{"kind": "path_role", "expression": "Patient.ID = Laboratory.ID", "role": "one_to_many_bridge"}
{"kind": "relation_role", "value": "root_table:Patient"}
{"kind": "aggregate_kind", "value": "row_count"}
{"kind": "operation_family", "value": "add_distinct"}
```

代码端 runtime 时:从新 case 的 question/pred_sql/schema 抽取所有 anchors 集合,与 pattern.grounded_anchors 求交集。**无交集直接 matches=false,不调 LLM**;有交集才调 LLM 通道 Q+S 做语义判断。

例 toxicology pat-206-253 的 grounded_anchors 自然含 `{kind: "column_role", role_family: "first atom reference"}` + `{kind: "column_role", role_family: "second atom reference"}` → 任何新 case 涉及 atom_id + atom_id2 的 SQL 都至少含这 2 个 anchors → LLM 进一步判断;EF2 pat-1035-1045 由于 anchors 太抽象("entity attribute" 不是有效 anchor),admission 阶段就被拒绝(改造 ⑤)。

### 2.3 Applicability contract 词表(谓词必须代码可检)

v2 定义有限 applicability 谓词词表(初始 ~30 个,可扩展):

```python
APPLICABILITY_PREDICATES = {
    # SELECT 形态
    "current_select_uses_count_star": lambda case: ...,
    "current_select_uses_count_distinct": ...,
    "current_select_uses_aggregate": ...,
    "current_select_arity_eq": lambda case, n: ...,
    "current_select_arity_gt": ...,
    # JOIN 路径
    "join_path_includes_one_to_many": ...,
    "join_path_includes_bridge_table": ...,
    "join_path_includes_aliased_self_join": ...,
    # WHERE
    "where_predicates_constrain_unique_per_row": ...,
    "where_uses_literal_string_filter": ...,
    # answer unit
    "answer_unit_role_eq": lambda case, role_family: ...,
    "answer_unit_question_focus_eq": ...,
    # ...
}
```

admission_judge prompt 提供这个词表,LLM 必须从词表中选;不能选时降级为不形成 pattern(`admit_pattern=false`)。

### 2.4 Negative guard(改造 ④ 引入)

每次 regression(s0_correct=True, final_correct=False),自动给已触发的 group 加 negative_guard。例 thrombosis 第一次 regress(q1267)后:

```json
{
  "applicability_contract": {
    "must_hold_before_rewrite": ["join_path_includes_one_to_many", "current_select_uses_count_star"],
    "negative_guards": [
      "where_predicates_constrain_unique_per_row"  // ← q1267 触发该 guard, 后续阻止
    ]
  }
}
```

后续 q1278-q1304 触发同 pattern 时,applicability 层检查 `where_predicates_constrain_unique_per_row=true`(IGG / ALP / GOT 等 lab 指标在 WHERE 锁定单一 patient × lab 组合),negative_guard 触发 → no_action。

---

## 3. 工作单元清单(执行顺序)

### 颗粒度与 commit 约定

沿用 v1:每个 WU 一个原子 commit,内部可拆 commits 增量调试。Commit 命名:`WUv2-{N}: {short subject}`。

回滚由 git 直接管理。不在代码中保留双轨 fallback / env flag(沿用 v1 §6.2 决策)。

### 阶段 A:运营层止血(必须先做,1.75d)

#### **WUv2-1** — DeepEye selector 默认开启(立即止血)

**目的**:把 DeepEye 端 `guard_accepted_direct_s1` 默认行为改为"始终走 S0/S1 selector",在 EEA 触发改坏的 case 上有挽救机会。

**现状**:

- `run_single_db_e2e.py:2105` argparse 定义 `--postsel_select_after_rewrite`,默认 False
- `run_single_db_e2e.py:2615` 当 not arg → 直接 `setattr(selected_item, "final_selected_sql", s1_sql)`
- thrombosis 实测 16/16 触发 `direct_accept_s1`,**完全跳过 selector**

**改动**:

1. **`run_single_db_e2e.py:2105`** argparse 改默认 True:
   ```python
   parser.add_argument(
       "--postsel_select_after_rewrite",
       dest="postsel_select_after_rewrite",
       action=argparse.BooleanOptionalAction,
       default=True,
   )
   ```
2. **`run_single_db_e2e.py:2615`** 配套清理:
   - if-not 分支保留(仍允许 explicit `--no-postsel_select_after_rewrite` 关闭)
   - 默认走 else 分支 `_run_single_case_selection_only`
3. **新增 eea_rewrite_stats 字段**:
   - `selector_choose_s1`(已有)
   - `selector_keep_s0`(已有)
   - `selector_choose_s1_correctly`(新,sel 选 S1 且 S1 正确)
   - `selector_keep_s0_correctly`(新,sel 保 S0 且 S0 正确)
   - `selector_wrong`(新,sel 选错)
   用于诊断 selector 准确率

**验收**:

- 重跑 thrombosis 单库,`direct_accept_s1` 数显著下降(原 16 → ≤ 5),`selector_*` 总数 ≥ 11
- thrombosis regression 数从 13 下降到 ≤ 6(selector 至少 50% 准确率)
- 11 库整体净 Δ 从 -16 提升至 -8 ~ -12 区间
- 总耗时增加 ≤ 20%(selector 调用增加 LLM 成本)

**回滚**:`git revert <WUv2-1 commit>`。回滚后默认值变回 False,代码自动恢复 `guard_accepted_direct_s1` 行为。

**工时**:0.25d(切默认值 + 配套诊断字段)

**依赖**:无

**风险**:
- selector 自身可能误选 S1(把已知正确的 S0 替换为错误的 S1)。缓解:加 selector_choose_s1 / selector_keep_s0 监控,若 selector_wrong > 0 显著增加,需调整 selector prompt 或 fallback 策略
- selector 调用 LLM 成本上升。缓解:WU13 prefilter 已限制候选数,触发数本身就 ≤ 5% 总 case,实际 LLM 成本上升 ≤ 5%

**完成 commit**: DeepEye `17d9196 WUv2-1: enable post-selection selector by default`

**静态验证**:
- `python -m py_compile /data/liuyining/ace4sql/method/deepeye/DeepEye-SQL/rulebook_experiments/run_single_db_e2e.py`
- `python /data/liuyining/ace4sql/method/deepeye/DeepEye-SQL/rulebook_experiments/run_single_db_e2e.py --help`

---

#### **WUv2-2** — Compiler 撤销 seed target binding(保守阻断,不试图恢复 helped case)

**核心边界声明**(2026-05-11 修订,基于第一次 WUv2-2 执行越界后的教训):

> **WUv2-2 只做保守阻断,不负责恢复 helped case**。具体:
> - 撤销 `synthesized_program.ops[*].role_refs` 中 source="target_sql" 项的 `table / column / expression` 字面值
> - **同时撤销** 用 role_family / path_role 等任何 seed 携带的 target 信号在新 case schema 上**重新派生** target_columns 的路径
> - 任何"会改变新 case answer unit / SELECT 投影 role"的 primitive,在没有 WUv2-5 引入的 `binding_contract` 之前 → **直接 0 candidates → no_action**
> - **不加 answer-focus guard、不加 evidence 文本对齐启发式**(这些都是看到错误后补的 patch,属于本计划要避免的)
>
> 真正的"在新 case 上合法重建 target binding"由 WUv2-5 用 `binding_contract` + `applicability_contract` 完成。WUv2-2 阶段的损失(部分 helped case 转 no_action)是预期行为,WUv2-5 后用结构化 binding 恢复。

**第一次执行 WUv2-2 时的教训(实测探针 q366)**:

第一版 WUv2-2 按"撤字面值 + 用 role_family 在新 schema 派生 target_columns + 加 answer-focus guard"的方向写。代码改完后用 q366 探针:

- q366 当前 SQL 输出 `l.format`,evidence "rule refers to format",S0 本来正确
- 触发的 singleton `grp-sing-card_games-351` 的 seed target role 是 `primary name`
- 撤掉 seed 字面 `cards.name` 后,compiler 仍然通过 `column_role="primary name"` 在当前 schema 找到 `cards.name`,产出 hint "把 l.format 换成 T1.name"
- 问题从"seed 字面列泄漏"**变成"seed target role 泄漏"**
- 临时补的 answer-focus guard(用"当前 SELECT 列是否被题面支持 + target role 是否不同"挡 q366)虽然没写死 q366,但仍是看到错误后加启发式拦截

**根因**:仅靠 role profile 在新 schema 派生 target,**仍是把 seed 的"要改输出"决定泄漏到新 case**。要真正决定新 case 是否应该改输出,必须有"当前 case 自己的 binding contract"(WUv2-5 引入)。**WUv2-2 不能承担这个职责**。

**现状**:

- `action_compiler.py:1398 _target_output_refs_for_action`:返回 seed canonical_op 中 source="target_sql" 的 role_refs,**含 table / column / expression 字面值 + role_family 等抽象信号**
- `action_compiler.py:1650 _schema_column_option_from_ref`:用 ref 的 table/column 字面值在 LocalSchemaView 查找
- `action_compiler.py:3655 _enumerate_switch_canonical_field`:把 refs 拼成 `target_expr = "cards.name"` 字面字符串
- 实测影响:card_games q366/q440/q480/q521 / student_club q1382 / formula_1 q988 / toxicology q255/q292,共 **9 例 regress** 全部同根因
- 同时影响 EF2 q1087 helped(凑巧 seed gold column `Player.id` 与 q1087 gold column 同名字面命中)

**ActionPrimitive 阻断范围(精确边界)**:

`vocabulary.py:195 ActionPrimitive` 共 11 个 primitive。按"是否依赖 seed target binding"分类:

**WUv2-2 阻断的 primitive(在没有 WUv2-5 binding_contract 之前一律产 0 candidates)**:

| Primitive | enumerator | 为什么必须阻断 |
|---|---|---|
| `ADD_SELECT_SLOT` | `:2610 _enumerate_add_select_slot` | 加新 SELECT 列,必须知道"加什么列",当前实现走 seed target_refs 派生 |
| `REPLACE_SELECT_SLOT` | `:2783 _enumerate_replace_select_slot` | 替换 SELECT 列,target 来自 seed |
| `SWITCH_CANONICAL_FIELD` | `:3793 _enumerate_switch_canonical_field` | 切换投影到 canonical 列,target 来自 seed |
| `REROUTE_FACT` | `:3508 _enumerate_reroute_fact` | 改变 fact table,target table 来自 seed |
| `CHANGE_GRAIN` | `:3717 _enumerate_change_grain` | 改聚合粒度,新 GROUP BY 列来自 seed |
| `MATERIALIZE_RANKING_OUTPUT` | `:3851 _enumerate_materialize_ranking_output` | 加 ranking output column,metric column 来自 seed |

**WUv2-2 保留的 primitive(不依赖 seed target binding,可正常工作)**:

| Primitive | enumerator | 为什么可保留 |
|---|---|---|
| `DROP_SELECT_SLOT` | `:3077 _enumerate_drop_select_slot` | 从当前 pred SQL 的 columns 中选择删除,只看当前 case,不需要 seed target |
| `SELECT_ENFORCE_DISTINCT` | (无独立 enumerator,挂在 dependency)| 在 SELECT 加 DISTINCT,不改 column,只加修饰 |
| `INSERT_BRIDGE` | `:3567 _enumerate_insert_bridge` | bridge 由当前 schema FK 派生 |
| `MOVE_CONDITION` | `:3648 _enumerate_move_condition` | WHERE 谓词同 schema 内部移动,不需要 seed target |
| `DROP_SIDE` | `:3172 _enumerate_drop_side` | 删除关系一侧,由当前 join 形态决定 |

**改动**:

1. **新增 `data_structures_v2.py::RoleRefV2`**(IR 不变,仍撤字面):
   ```python
   class RoleRefV2(BaseModel):
       source: Literal["pred_sql", "target_sql"]
       column_role_family: Optional[str] = None
       path_role: Optional[str] = None
       relation_role: Optional[str] = None
       role_side_group: Optional[str] = None
       sql_role: Optional[str] = None
       evidence: Dict[str, Any] = {}
       # ↓ 撤销字段(audit-only 保留在 evidence 子段)
       # table / column / expression 不在主 schema
   ```
2. **改 `repair_program_normalizer_v2.py`**:从 pred/gold AST 派生 role_ref 时,不写 table/column/expression 到主字段,塞进 `evidence.original_*` 供 audit
3. **改 `action_compiler.py:1398 _target_output_refs_for_action`**:
   - **关键改动**:当 seed canonical_op 中 source="target_sql" 项无 `binding_contract` 关联时(WUv2-5 前所有 case 都是这样),返回**空 list**
   - 不返回 role_family / path_role 等抽象信号 — 因为 ActionCompiler 没有合法路径把它们转成新 case target_columns(那需要 binding_contract)
   - 注释明确:"WUv2-2 阶段 seed target ref 仅作 audit;binding_contract 由 WUv2-5 接管"
4. **改 6 个被阻断的 enumerator**(`_enumerate_replace_select_slot` / `_enumerate_switch_canonical_field` / `_enumerate_add_select_slot` / `_enumerate_reroute_fact` / `_enumerate_change_grain` / `_enumerate_materialize_ranking_output`):
   - 入口检查:如果 `_target_output_refs_for_action` 返回空 list,立即返回空 ActionCandidateSet + empty_reason="wuv2_2_seed_target_binding_disabled_pending_binding_contract"
   - **不再做** "在 LocalSchemaView 查找 column_role 匹配列"的派生
   - **不再做** answer-focus guard / evidence 文本对齐 / 任何启发式判断
5. **保留 5 个 enumerator 工作**(`_enumerate_drop_select_slot` / `_enumerate_drop_side` / `_enumerate_insert_bridge` / `_enumerate_move_condition` / SELECT_ENFORCE_DISTINCT dependency):
   - 它们的逻辑不依赖 seed target binding,保留现状
6. **`_schema_column_option_from_ref`**:函数保留(供 evidence audit 使用),但**不再被被阻断的 enumerator 调用**
7. **改 rewrite hint**:
   - 被阻断的 primitive 不再产生 candidate → no instantiated_hint
   - 保留工作的 primitive(DROP / DISTINCT / 等)hint 由结构变更派生,不涉及 seed target column
8. **诊断字段**:`per_case_log.eea_update.compiler.disabled_primitives_count` 记录每个 case 被阻断的 primitive 数,便于验证 WUv2-2 阻断范围
9. **改动 11(补):hint 越界 audit-only 字段**:
   - `per_case_log.eea_runtime.hint_audit` 新增 `hint_introduces_non_primitive_action: bool`
   - 判定只比较 `raw_hint` 与 `instantiated_hint` 中是否保留了当前 `ActionPrimitive` 对应的正向核心动词,例如 `MOVE_CONDITION -> move`, `DROP_SELECT_SLOT -> drop/remove`
   - 这不是 negative 黑名单:不检查 alias / rename / prefix 等具体坏词,只判断 raw_hint 的 primitive 核心动词是否仍可识别
   - 该字段只用于 `r_v2_a` / `r_v2_e` 验收统计,不参与 runtime 决策,不 fallback,不拦截

**验收**:

- **代码层面**:
  - `grep -rn "ref\.get..table.\|ref\.get..column." /data/liuyining/ace4sql/method/EEA/rulebook/common/runtime/action_compiler.py` 0 命中(IR 字面已撤)
  - 被阻断的 6 个 enumerator 在 seed target refs 为空时直接返回 empty set,empty_reason 含 `wuv2_2_seed_target_binding_disabled`
  - 不存在任何 answer-focus / evidence 文本对齐 guard 代码
- **行为层面**:
  - card_games q366/q440/q480/q521 全部转 no_action(saved-s0)
  - student_club q1382 / formula_1 q988 转 no_action
  - thrombosis 13 regress 中 q1267-q1304 全部转 no_action(saved-s0)
  - toxicology q255/q292 转 no_action
  - **22 例 regress 几乎全部转 saved-s0 → 净 +22**(对比 v1 r1)
  - EF2 q1087 helped 丢失(SELECT_REPLACE_SLOT 被阻断)→ 净 -1
  - toxicology 7 helped(q249/253/268/277/285/302/307,全走 DROP + DISTINCT)**保留**(不依赖 seed target binding)
- **整体**:11 库整体净 Δ 从 (WUv2-1 后)-8 ~ -12 提升至 **+3 ~ +5**(WUv2-1 selector 至少 50% 准确率 + WUv2-2 阻断 22 regress - 1 helped 损失)

**回滚**:`git revert <WUv2-2 commit>`。IR schema 改动需要 library 兼容:已有 library 加载时 model_validator 把旧版 table/column 字段迁到 evidence 子段。回滚需配套 `cli/migrate_library_v1_to_v2.py`。

**工时**:1d(IR schema + 改 6 个 enumerator 短路 + 测试)。**比第一版 1.5d 短**,因为撤销了"派生 + guard"的复杂路径。

**依赖**:无(可与 WUv2-1 并行)

**风险**:

- **预期 helped 数下降**:EF2 q1087(凑巧字面命中)+ 任何走 SELECT_REPLACE/SWITCH/ADD 等阻断 primitive 凑巧改对的 case 都会转 no_action。这是**设计上接受的代价** — WUv2-2 是保守阻断,WUv2-5 用 binding_contract 合法恢复。
  - **不缓解**:不允许在 WUv2-2 加任何"恢复 helped"的启发式
  - **观察指标**:验收时记录 helped 数,作为 WUv2-5 的恢复目标基线(WUv2-5 后 helped 应 ≥ WUv2-2 时的水平 + WUv2-5 引入的新 helped)
- **toxicology 7 helped 保留**:这是 DROP/DISTINCT 类 primitive 不需要 seed target binding 的自然结果。如果验收时发现 toxicology 7 helped 也部分丢失,说明分类有误(某 helped case 实际走 SELECT_REPLACE 而非 DROP),需重新检查 `_enumerate_*` 调用 path
- **类 III(`pre_condition_matched_branch_unbindable`)数量上升**:更多 case 走 no_action 而非 regress。这是预期行为,no_action 比 regress 安全

**与 WUv2-5 的对接**:

WUv2-5 引入 `binding_contract` 后,被阻断的 6 个 enumerator 改为:

```python
def _enumerate_replace_select_slot(*, case_view, ..., group):
    binding_contract = _payload(group.instantiation_program.pattern_recognition_contract).get("binding")
    if not binding_contract:
        # WUv2-2 行为:无 binding_contract → 不工作
        return empty_set(empty_reason="no_binding_contract")
    # WUv2-5 新增:有 binding_contract → 按 binding.target_slots 在当前 schema 派生
    target_options = _derive_target_options_from_binding(binding_contract, case_view)
    ...
```

WUv2-5 完成后,只有声明了 `binding_contract` 的 pattern 才能让被阻断的 primitive 工作。这确保:
1. 老 library 中的 singleton/pattern 没有 binding_contract → 永远不再误触发 SELECT_REPLACE 等
2. 新 library 中由 WUv2-5 admission 产出含 binding_contract 的 pattern → 可在严格 applicability 检验后做 SELECT_REPLACE

#### 已知未兜尾巴(由 WUv2-5 兜底,不在 WUv2-2 扩边界)

WUv2-2 已验证切断两条 seed 字面泄漏路径:RoleRefV2 target_sql 字面值、`repair_program` dependency 字面值。残留问题不是 seed 字面泄漏,而是 `hint_instantiation` LLM 在保留 primitive 上仍可自由解释 primitive 语义,例如把 `MOVE_CONDITION` / `DROP_SELECT_SLOT` / `INSERT_BRIDGE` / `DROP_SIDE` / `SELECT_ENFORCE_DISTINCT` 扩写成 alias prefix、column rename 等新动作。

具体 case:q292。WUv2-2 后 `REPLACE_SELECT_SLOT` 已被 `wuv2_2_seed_target_binding_disabled_pending_binding_contract` 阻断,但 `MOVE_CONDITION` 仍是保留 primitive。`hint_instantiation` 把 raw action "Move the bound predicate..." 自由解释为 `"Prefix the column 'element' in the WHERE clause with the table alias 'atom'"`,随后 rewrite 按 alias-prefix 执行导致 regress。

责任归属:WUv2-5 在 admission/accumulate 引入 `applicability_contract.must_hold_before_rewrite` 与 `negative_guards` 后,q292 类 case 应在 runtime 第 2 层 applicability 检验阶段 no_action,例如命中 `schema_has_only_one_resolvable_column` 这类结构化 negative guard,根本不进入 hint 阶段。

硬约束:WUv2-2 阶段绝对禁止在 `hint_instantiation` prompt 加"禁止输出 alias"、"`MOVE_CONDITION` 不允许引入 X"一类规则;绝对禁止加 hint 输出关键词黑名单 / hint 后处理 fallback;绝对禁止用代码端 hint 内容拦截。这些都属于 case-by-case 反模式。WUv2-2 只记录 `hint_audit`,不做决策。

---

### 阶段 B:Singleton 严格化 + retrieval 重建(机制层,2.5d)

#### **WUv2-3** — Singleton 严格化(不走 pattern 级泛化)

**目的**:让 singleton 不再借 LLM Q+S 通道做跨 case 泛化触发,要么 exact/near-exact 触发,要么不触发。配合 WUv2-2,让 sing-1023 / sing-351 / sing-1366 / sing-973 等过度泛化的 singleton 失效。

**现状**:

- `runtime.py:2327` `_evaluate_pattern_pre_condition` 对 singleton 和 pattern 一视同仁
- `runtime.py:2337` 通过 pre-condition 后 `variant_required_match = True; required_misses = []`(放开结构 gate)
- 实测:singleton 借 LLM Q+S 通道触发的 case,误触发率极高:
  - card_games sing-351: 34/34 全错
  - student_club sing-1366: 2/2 全错
  - formula_1 sing-973: 1/1 错
  - thrombosis sing-1172 + 衍生 pattern 1172-1257: 16/18 错

**改动**:

1. **改 `runtime.py:2327`**:
   ```python
   recognition_contract = _pattern_recognition_contract(group)
   if passed and recognition_contract:
       pre_condition_match, pre_condition_matched, pre_condition_blockers = _evaluate_pattern_pre_condition(...)
       if pre_condition_blockers:
           passed = False
           reasons.extend(pre_condition_blockers)
       else:
           reasons.append("pre_condition_matched")
           if group.group_type == GroupType.PATTERN:
               # PATTERN 走原泛化路径
               variant_required_match = True
               generalized_canonical_gate_passed = True
               required_misses = []
           else:
               # SINGLETON: pre_condition 通过仅是 "形态相似",
               # 必须叠加 singleton_canonical_exact_check 才能触发
               # 不放开结构 gate
               pass
   ```
2. **改 `runtime.py:2034 _singleton_canonical_exact_check`**:
   - 现在已经做 source canonical shape 比对
   - 加入 strict mode flag:singleton 路径**必须**通过 exact check
3. **新增 `singleton_to_pattern_promotion_gate`**:
   - singleton 在 admission 阶段被吸入新 pattern(`_try_extend_existing_pattern`)是唯一的"升级为 pattern"路径
   - singleton 自身永远不会"自动升 pattern"
   - 这是设计决策:singleton 由单 case 派生,泛化责任在 admission 阶段
4. **撤销 v1 §2.3 设计假设**(代码注释更新):
   - 之前注释"singleton 与 pattern 共用机制,描述粒度自然决定阈值"
   - 改为"singleton 走 exact 路径,pattern 走泛化路径"

**验收**:

- card_games 34/34 sing-351 触发数下降到 ≤ 2(只在 exact 形态匹配时触发)
- student_club q1382 / formula_1 q988 / toxicology q255/q292 触发数下降到 0
- 但 toxicology 7 helped(走 pat-206-253)不应受影响(那是 pattern 路径)
- thrombosis pattern-1172-1257 仍触发(那是 pattern 不是 singleton);需 WUv2-4 处理
- 11 库整体净 Δ 在 WUv2-2 之后 → -2~0,本 WU 后 → 0~+5

**回滚**:`git revert <WUv2-3 commit>`。回滚后 singleton 恢复 pattern 级泛化路径。

**工时**:1d

**依赖**:WUv2-2 完成(WUv2-2 后 ActionCompiler 已用 role_family 派生 target,singleton exact 路径才有合理的 target 派生)

**风险**:

- toxicology 早期 sing-206 触发 q249 helped(在 pat 形成前)可能被严格化挡掉。缓解:验收时观察 q249 timeline,如被挡需考虑 "singleton with N≥2 触发历史" 例外
- 触发率显著下降,可能让"曾经凑巧改对"的 case 不再改对。缓解:同 WUv2-2 风险

---

#### **WUv2-4** — Retrieval bucket 改结构化 repair card

**目的**:撤销 `_repair_insight_interface_key` LLM 自由文本作为 retrieval bucket 主键,改用代码端结构化 repair card 派生的四元组哈希作为 bucket key。让同源 case 自动汇合,解决类 I 没聚类。

**现状**:

- `pattern_formation.py:129 _repair_insight_interface_key`:取 LLM 在 error_instance_extractor 阶段写的 `interface_key / repair_interface / target_preference`
- `pattern_formation.py:348 _retrieval_keys_for_card`:把 interface_key 作为 bucket 主键之一
- 实测影响:
  - codebase q581/q582: interface_key 措辞不同 → 不同 bucket → 永远不在 admission 中相遇
  - financial q141/q180/q193: 三种动词组合分散
  - EF2 q1038/q1085 / q1068/q1093 / q1119-q1127 / q1024-q1144: 同样问题
  - 类 I 损失估计 ~30+ case

**改动**:

1. **新增 `analysis/repair_card_normalizer.py`**:
   ```python
   def derive_repair_card(error_instance: ErrorInstanceV2, schema_view: LocalSchemaView) -> Dict[str, Any]:
       """Code-derived structured repair card (no LLM)."""
       return {
           "db_id": ...,
           "case_id": ...,
           "effect_axis": _derive_effect_axis(error_instance),
           "source_answer_unit": _derive_answer_unit(error_instance.pred_sql, schema_view),
           "target_answer_unit": _derive_answer_unit(error_instance.gold_sql, schema_view),
           "operation_family": _derive_operation_family(error_instance.structure_delta),
           "changed_locus": list(error_instance.structure_delta.changed_loci),
           "preserve_invariants": _derive_preserve_invariants(error_instance.structure_delta),
       }
   ```
2. **`_derive_answer_unit`**:
   ```python
   def _derive_answer_unit(sql: str, schema_view: LocalSchemaView) -> Dict[str, Any]:
       """Output kind/role from SELECT projection."""
       ast = parse_sql(sql)
       select_items = ast.select_items
       if any(_is_count_star(item) for item in select_items):
           return {"kind": "row_count", ...}
       if any(_is_count_distinct(item) for item in select_items):
           return {"kind": "distinct_entity_count", "entity_role": _select_role_family(item, schema_view), ...}
       # ... 其他形态
   ```
3. **`_derive_operation_family`** 沿用 structure_delta_v2 已有派生
4. **改 `pattern_formation.py:348 _retrieval_keys_for_card`**:
   ```python
   def _retrieval_keys_for_card(card: Dict[str, Any]) -> Set[Tuple[str, str]]:
       keys: Set[Tuple[str, str]] = set()
       db_id = str(card.get("db_id") or "")
       # 新主键: 结构化四元组
       source_kind = card.get("source_answer_unit", {}).get("kind", "")
       target_kind = card.get("target_answer_unit", {}).get("kind", "")
       ops_signature = "|".join(sorted(card.get("operation_family") or []))
       primary_key = f"answer_unit_op:{source_kind}->{target_kind}|{ops_signature}"
       keys.add((db_id, primary_key))
       # 保留 effect_axis|shape_family 作为粗筛轴
       for axis in card.get("delta_axes") or []:
           keys.add((db_id, f"axis:{axis}"))
       # 撤销 interface_key:{interface}
       return keys
   ```
5. **撤 `pattern_formation.py:129 _repair_insight_interface_key`**:函数保留供 audit 用,但不再被 `_retrieval_keys_for_card` 调用
6. **改 `accumulate.py`**:wrong case 入库时调 `derive_repair_card`,把 card 存在 `formation_signals.repair_card`
7. **改 `pattern_formation.py::_pair_candidates_from_index`**:索引由 `(db_id, primary_key)` 维护

**验收**:

- 重跑 codebase,(q581, q582) 出现在同一 `shared_insight_judge` 调用中
- 重跑 financial,(q141, q180) / (q141, q193) 至少一对出现在 admission 中
- 重跑 EF2,(q1119, q1120) / (q1126, q1127) 至少一对出现在 shared_insight 中
- pattern_admission_judge 调用数显著上升(同源 case 汇聚后 pair 增多)
- 11 库形成的 pattern 总数从 ~25 上升到 ~35
- 11 库整体净 Δ 从 (WUv2-3 后)0~+5 提升至 +5~+10

**回滚**:`git revert <WUv2-4 commit>`。回滚后 `_retrieval_keys_for_card` 恢复用 interface_key。注意 library 中已用新 key 索引的 group 在回滚后可能找不到旧 bucket,需重 build 索引(已有 lazy rebuild 机制)。

**工时**:1.5d

**依赖**:无(但与 WUv2-5 强相关,建议连做)

**风险**:

- 结构化 answer_unit 派生用 SQL parser,某些复杂 case(嵌套子查询、CTE)可能派生失败。缓解:派生失败时降级到 effect_axis 单维度 bucket(损失一些精度但不出错)
- 新 bucket key 可能过窄,把"同源但实例化路径不同"的 case 分开。缓解:`operation_family` 用集合,覆盖多种 op;`source/target_kind` 用粗类别(row_count / distinct_entity_count / value 等,不到具体表)

---

### 阶段 C:Pattern 三 contract + Regression negative feedback(架构层,3d)

#### **WUv2-5** — Pattern 三 contract 拆分 + Runtime 五层判断 + Regression negative feedback

**目的**:把 v1 单一 `pattern_recognition_contract` 拆成三 contract(recognition/applicability/binding),admission_judge 输出从自由文本变成受词表约束的结构化字段;runtime 拆五层判断,任何一层失败 no_action;regression 案例进入 negative_feedback 模式给已触发 group 加 negative_guard。

**这是 v2 最大的 WU,工程量 3d,可能需要拆 3 个 sub-commit**。

**现状**:

- `data_structures.py InstantiationProgram.pattern_recognition_contract`:单一 contract,4 字段全自由文本
- `runtime.py:2327` 五层判断中目前只有 recognition + branch matching + binder dry-run,缺 applicability
- `run_single_db_e2e.py:2716` `local_gate_wrong = baseline_correct is not True` → regression 跳过 accumulate
- 实测影响:
  - thrombosis pat-1172-1257 触发 16 / regress 13(完全无 applicability 检验,完全无 negative feedback)
  - 类 IV 22 例 regress 全部不进 accumulate

**改动 a:Pattern 三 contract 数据结构**

1. **新增 `data_structures_v2.py`**:
   ```python
   class GroundedAnchor(BaseModel):
       kind: Literal["column_role", "path_role", "relation_role", "aggregate_kind", "operation_family"]
       value: str
       role_family: Optional[str] = None
       table_hint: Optional[str] = None

   class RecognitionContract(BaseModel):
       question_precondition: str
       sql_precondition: str
       grounded_anchors: List[GroundedAnchor]  # 必须 ≥ 2 个

   class ApplicabilityContract(BaseModel):
       required_answer_unit_change: str        # 枚举值
       must_hold_before_rewrite: List[str]     # 谓词词表名
       negative_guards: List[str]              # 谓词词表名

   class BindingContract(BaseModel):
       source_slots: List[Dict[str, Any]]      # role_family + kind
       target_slots: List[Dict[str, Any]]
       allowed_operations: List[str]           # ActionPrimitive 集合

   class PatternRecognitionContractV2(BaseModel):
       recognition: RecognitionContract
       applicability: ApplicabilityContract
       binding: BindingContract
   ```
2. **改 `InstantiationProgram.pattern_recognition_contract`** 字段类型:从旧 `Dict` 改为 `Optional[PatternRecognitionContractV2]`(向后兼容:旧 library 加载时用 model_validator 自动迁移到新结构)

**改动 b:Admission_judge prompt 重写**

3. **改 `llm/prompts/pattern_admission_judge.py`**:
   - 删除自由文本 4 字段(`pre_question_signature` etc.)
   - 改为输出三 contract JSON 结构
   - prompt 末尾提供 `APPLICABILITY_PREDICATES` 词表(~30 个谓词)+ `ROLE_FAMILY_DICTIONARY`(从 schema_role_annotator 派生)+ `ACTION_PRIMITIVES`
   - 强制 grounded_anchors ≥ 2(否则 `admit_pattern=false`)
   - `applicability.must_hold_before_rewrite` 必须从词表选(否则 `admit_pattern=false`)
4. **加 `pattern_admission_judge_postprocessor`**:LLM 输出后,代码端校验:
   - grounded_anchors 中每个 anchor 必须真实存在于 admitted member 的 schema/SQL(代码端可验证)
   - applicability 谓词必须在词表内
   - 校验失败 → `admit_pattern=false, reject_reason=structural_contract_validation_failed`

**改动 c:Runtime 五层判断**

5. **改 `runtime.py:2327` 主 gate**:
   ```python
   # Layer 1: recognition (LLM Q+S, 已有)
   recognition_audit, recognition_passed, recognition_blockers = _evaluate_recognition_contract(group, case_view)
   if not recognition_passed:
       passed = False; reasons.extend(recognition_blockers); return ...

   # Layer 2: applicability (代码端谓词检验, 新增)
   applicability_audit, applicability_passed, applicability_blockers = _evaluate_applicability_contract(group, case_view)
   if not applicability_passed:
       passed = False; reasons.extend(applicability_blockers); return ...

   # Layer 3: binding (代码端 role_family 派生, 配合 WUv2-2)
   binding_audit, binding_passed, binding_blockers = _evaluate_binding_contract(group, case_view)
   if not binding_passed:
       passed = False; reasons.extend(binding_blockers); return ...

   # Layer 4: compile (ActionCompiler, 已有)
   ...

   # Layer 5: rewrite (LLM, 已有)
   ...
   ```
6. **新增 `_evaluate_recognition_contract`**:
   - 先做 grounded_anchors 短路:无任一 anchor 在新 case 命中直接 false
   - 否则调 LLM Q+S 通道(沿用 v1 `_pre_condition_channel_call`)
7. **新增 `_evaluate_applicability_contract`**:
   - 纯代码端,根据 case_view 检验 must_hold 全部 hold + negative_guards 全部 not hold
   - 谓词词表实现在 `analysis/applicability_predicates.py`
8. **新增 `_evaluate_binding_contract`**:
   - 在 case_view.local_schema_view 中查找 binding.source_slots / target_slots role_family 对应列
   - 无候选 → no_action(类 III 行为)

**改动 d:Regression negative feedback**

9. **改 `run_single_db_e2e.py:2716` gate**:
   ```python
   should_update = (baseline_correct is not True) or (
       final_correct is False and baseline_correct is True
   )
   if should_update:
       if baseline_correct is True and final_correct is False:
           # REGRESSION feedback mode
           update_request["accumulate_mode"] = "negative_feedback"
           update_request["matched_group_ids"] = trigger.matched_group_ids
           update_request["regression_artifact"] = {
               "s0_sql": s0_sql,
               "s1_sql": s1_sql,
               "gold_sql": gold_sql,
               "s0_vs_gold": run_execution_comparison(s0_sql, gold_sql, db_path),
               "s1_vs_gold": run_execution_comparison(s1_sql, gold_sql, db_path),
           }
       v2_library, eea_update = update_from_selected_sql(..., update_request=update_request)
   ```
10. **改 `accumulate_v2.py::accumulate_wrong_case`**:
    - 支持 `mode="negative_feedback"`:
      - 不形成新 singleton
      - 对每个 matched_group_id,**代码端**派生当前 case 的 applicability 谓词集合
      - 把"在该 case 上 hold 的、与 group.applicability.must_hold_before_rewrite 不冲突的谓词"作为 `negative_guards` 增量加入 group
      - 例 thrombosis q1267:谓词派生出 `where_predicates_constrain_unique_per_row=true`,该谓词不在 pat-1172-1257 的 must_hold 中 → 加入 negative_guards
      - 后续 q1278 触发同 pattern 时,applicability 层检验 `where_predicates_constrain_unique_per_row=true` 命中 negative_guards → no_action
11. **加 negative_guard 收敛限制**:
    - 同 group 累积超过 N 个 negative_guard 后,标记 `pattern_overgeneralized` 并降级为 audit-only
    - 这是终极保护:某 pattern 反复加 guard 说明它本身就是错的形态

**改动 e:Online evolve 配套**

12. **改 `evolution.py::evolve_library_with_replay`**:
    - 接收 negative_feedback 更新时,跳过 admission(没有新 singleton 产生)
    - 直接更新 matched_group 的 applicability_contract.negative_guards

**改动 f:_member_case_views_for_group 扩展**

13. **改 `evolution.py::_member_case_views_for_group`**:
    - WU12 已实现在 online local_evolve 时跑 self_recall
    - v2 扩展:同时跑 applicability self-check(每个 seed case 的代码端谓词集合应满足 group.applicability.must_hold_before_rewrite)
    - 这是 admission 端 LLM 写的谓词是否真的在 seed 上 hold 的代码端验证

**验收**:

- thrombosis pat-1172-1257 在 admission 时被强制输出 applicability.must_hold_before_rewrite,例如 `["current_select_uses_count_star", "join_path_includes_one_to_many"]`
- 重跑 thrombosis,q1267 第一次触发 pat-1172-1257 时:
  - recognition 通过
  - applicability 当前通过(`current_select_uses_count_star=true`, `join_path_includes_one_to_many=true`)
  - 但 selector(WUv2-1)选 S0,final_correct=True 不退化
  - 即使 selector 没救住(假设 q1267 仍 regress),触发 negative_feedback,给 pat-1172-1257 加 `where_predicates_constrain_unique_per_row` 到 negative_guards
- q1278 起,触发 pat-1172-1257 时 applicability 层检测 negative_guard 命中 → no_action
- thrombosis 13 regress 下降到 ≤ 1(只丢首次 regress 教训)
- card_games / debit_card / student_club / formula_1 同机制,各库 regress 下降到 ≤ 1
- 11 库整体净 Δ 从 (WUv2-4 后)+5~+10 提升至 +12~+18
- EF2 pat-1035-1045 因 grounded_anchors 不足(LLM 无法从"entity attribute column" 提取 ≥ 2 个 anchor),admission 阶段被拒,不形成此 pattern → 减少类 II 误形成

**回滚**:`git revert <WUv2-5 commit>`。注意 IR 大改,回滚需要 library 转换。改动按以下 3 个 sub-commit 拆:
- sub-commit a: 数据结构 + admission_judge prompt(可独立回滚)
- sub-commit b: runtime 五层判断
- sub-commit c: regression negative feedback

**工时**:3d(数据结构 1d + admission 改造 0.5d + runtime 五层 0.5d + negative feedback 1d)

**依赖**:WUv2-2 完成(binding_contract 依赖 role_family 派生 ActionCompiler)

**风险**:

- admission_judge LLM 在新 prompt 下可能大量返回 `admit_pattern=false`(因为强制 grounded_anchors + applicability 词表约束)。缓解:验收时观察 pattern 形成数,如比 v1 显著少(如 < 50%),需放宽 grounded_anchors 数量从 ≥ 2 到 ≥ 1
- applicability 谓词词表初始 ~30 个可能覆盖不全。缓解:留 LLM 一个 `applicability_predicates_extension_request` 输出字段,记录"我想用但词表没有的谓词",作为词表扩展依据
- negative_guards 可能过度积累导致 pattern 完全失效。缓解:加上限保护(改动 11)

---

### 阶段 D:配套优化(可选,1d)

#### **WUv2-6** — Schema role annotator 启动时预热

**目的**:WUv2-4 + WUv2-5 重度依赖 schema_role_annotator 的 role_family 覆盖率。当前是 lazy-write(每个 case 触发新 missing 列时才调 LLM),启动时主动预热可让前期 case 直接受益。

**现状**:

- `io/local_schema.py:202 annotate_schema_roles(view, db_id=db_id)` 在 build LocalSchemaView 时调用
- 但只对当前 case 涉及的 missing 列调 LLM
- 实测:EF2 39 次 LLM 调用,均为 case 触发的 incremental annotation(`workspace/schema_roles/european_football_2.json` 累积 84 列)
- 启动时第一批 case 看不到完整角色标注

**改动**:

1. **新增 `cli/preheat_schema_roles.py`**:
   ```python
   def preheat(db_id: str, db_path: str):
       """对 db 全部表 + 全部列做一次 schema_role_annotator 标注, 写入 cache."""
       schema = build_full_schema_view(db_id, db_path)
       view = annotate_schema_roles(schema, db_id=db_id, force_full=True)
       print(f"{db_id}: annotated {len(view.semantic_hints)} columns")
   ```
2. **改 `analysis/schema_role_annotator.py::annotate_schema_roles`** 加 `force_full` 参数:
   - 当 force_full=True,把 schema 中所有 (table, column) 都视为 missing(即使有 cache 也重 annotate)
   - 否则保持 lazy 行为(默认)
3. **改 `run_multidb_validation_v2.py` 启动钩子**:
   - 运行前对所有 manifest 中的 db 跑一次 preheat(如果 cache 不存在或 stale)
   - 这是 11 库 batch 跑前的一次性 ~5-10 min 投入

**验收**:

- preheat 后,11 库的 `workspace/schema_roles/{db}.json` 覆盖率 ≥ 95%(即 95% schema 列有 role_family)
- EF2 第一批 case(step 1-20)的 retrieval card 中 role_family 字段不再大量为空
- runtime 中 `pred.contains_column_role=unknown` 信号占比 < 10%

**回滚**:`git revert <WUv2-6 commit>`。cache 已 cumulative,回滚后 lazy 路径仍工作,只是冷启动慢。

**工时**:0.5d

**依赖**:无(独立优化)

---

## 4. 阶段间核查(每阶段必跑)

每个 WU 完成后强制 6 项检查再进下一 WU:

1. **测试基线**:`pytest method/EEA/rulebook/tests/` 失败集 ⊆ v1 时确认的 19 项 pre-existing
2. **11 库总净 Δ**:`r{n} ≥ r{n-1}`(不退化),目标随 WU 推进逐步提升
3. **类 IV regress 数**:r{n} regress 数 ≤ r{n-1}(每个 WU 应减 regress,不增加)
4. **selector 准确率**(WUv2-1 后):`selector_choose_s1_correctly + selector_keep_s0_correctly` / 总 selector 调用 ≥ 60%
5. **schema_role_annotator 覆盖率**(WUv2-6 后):`pred.contains_column_role=unknown` 占比 ≤ 10%
6. **总耗时**:不超过 v1 实测基线的 120%(目前 v1 11 库约 9 小时)

---

## 5. 验收边界总表

按 case_audit + db_pattern_discussion 中代表性 pattern,每个 WU 阶段结束应能识别:

| Pattern(来自人工标注)| v1 r1 现状 | WUv2 全部完成预期 |
|---|---|---|
| toxicology pat-206-249-253 RoleGraph 双列变单列(q206/249/253/268/277/285/302/307)| 7 触发 helped | 7 触发 helped(保留)|
| toxicology pat-220-304 等被 self_recall 卡的 pattern | usable=False 0 触发 | applicability 重写后 ≥ 1 触发 |
| codebase pat editor→Owner(q581/q582)| 没形成 | WUv2-4 后形成,WUv2-5 后触发 ≥ 1 |
| codebase pat postHistory(q602/q631/q632/q635/q639/q640/q652)| 碎片化 | WUv2-4 后聚合 ≥ 5 member,WUv2-5 后触发 ≥ 3 |
| financial pat ID 输出 contract(q141/q180/q193)| 没形成 | WUv2-4 后形成 |
| EF2 pat Player 唯一粒度(q1040/q1052/q1064/q1087)| 1 helped(q1087) | WUv2-4 + WUv2-5 后形成完整 pattern,触发 ≥ 2 |
| EF2 pat 排序漏指标(q1038/q1085)| 没形成 | WUv2-4 后形成 |
| EF2 pat AVG→SUM/COUNT(q1068/q1093)| 没形成 | WUv2-4 后形成 |
| thrombosis pat 1172-1257 患者计数粒度漂移 | 16 触发 13 regress | WUv2-5 后第一次 regress 加 negative_guard,后续 ≤ 1 regress |
| card_games / student_club / formula_1 singleton 误触发系列 | 大量 regress | WUv2-3 后 singleton 严格化,regress 显著降 |

跨 pattern 总体:
- **11 库 r1 (v1 baseline): -16**
- **WUv2-1 (selector) 后: -8 ~ -12**
- **WUv2-2 (compiler 保守阻断,helped 数下降是预期) 后: +3 ~ +5**(22 regress 转 saved-s0,EF2 q1087 等少量 helped 丢失)
- **WUv2-3 (singleton 严格) 后: +5 ~ +7**(WUv2-2 已挡掉大部分 singleton 误触发,WUv2-3 进一步收紧)
- **WUv2-4 (retrieval 结构化) 后: +7 ~ +10**(类 I 没聚类的 case 形成 pattern,但 WUv2-5 前 pattern 还不能触发被阻断的 primitive,所以增益有限)
- **WUv2-5 (三 contract + negative feedback + 解除阻断) 后: +12 ~ +18**(binding_contract 引入后,WUv2-2 阻断的 primitive 在有效 binding 下重新工作,恢复并扩大 helped;negative_guard 阻止后续 regress)
- **WUv2-6 (annotator 预热) 后: +13 ~ +20(锦上添花)**

**关键转折**:WUv2-2 → WUv2-5 之间存在"helped 数下降"的过渡期。这是设计上接受的:宁可保守 no_action 也不要 LLM 自由派生 target 导致的 regress。WUv2-5 是恢复 helped 的合法路径。

#### WUv2-5 验收硬指标(2026-05-11 由 WUv2-2 收尾时锁定)

- q292 / 同类 case(保留 primitive + hint LLM 自由解释为 alias-prefix / column-rename / 其他非该 primitive 语义)在 `r_v2_e` 中必须 0 例 regress。
- `r_v2_e` 中 `hint_instantiation` 实际改变 primitive 语义的越界占总触发数比例 ≤ 5%,用 `per_case_log.eea_runtime.hint_audit.hint_introduces_non_primitive_action` 统计。
- 上述两条达不到时,不允许标记 `stage-v2-c-complete`。

---

## 6. 风险与回滚

### 6.1 关键风险

| 风险 | 触发信号 | 应对 |
|---|---|---|
| WUv2-1 selector 自己也选错 | selector_wrong > selector_choose_s1_correctly | 降级:把 selector 改成"只在 EEA confidence ≥ 高位时选 S1,否则保 S0" |
| WUv2-2 ActionCompiler 在新 schema 找不到 role_family 列 | toxicology 7 helped 触发数下降 | 加 fallback:role_family 找不到时,用 evidence.original_table/column 作为参考(audit 用法,不强制 binding) |
| WUv2-3 singleton 严格化挡太严 | 11 库 helped 数下降 | 放宽 singleton_canonical_exact_check 阈值;允许 "exact 形态 + role_family 一致" 时触发 |
| WUv2-4 结构化 answer_unit 派生失败 | 大量 case 的 repair_card 缺字段 | 派生失败时降级到 effect_axis 单维度 bucket |
| WUv2-5 admission 大量 admit=false | pattern 形成数 < v1 50% | 放宽 grounded_anchors 从 ≥ 2 到 ≥ 1;applicability 谓词词表扩展 |
| applicability 词表覆盖不全 | LLM 在 admission 大量请求扩展词表 | 收集请求,迭代扩展词表(每两周 review) |
| negative_guards 过度积累让 pattern 失效 | 同 pattern negative_guards ≥ 10 | 触发 audit-only 降级保护(WUv2-5 改动 11 已设计) |

### 6.2 回滚机制

沿用 v1 §6.2:不在代码中保留 env/config flag 双轨,git revert 颗粒回滚。

**WU 起点 tag 约定**:
- 改造开始前:`git tag pre-v2-refactor`
- 阶段 A 完成(WUv2-1 + WUv2-2):`git tag stage-v2-a-complete`
- 阶段 B 完成(WUv2-3 + WUv2-4):`git tag stage-v2-b-complete`
- 阶段 C 完成(WUv2-5):`git tag stage-v2-c-complete`
- 阶段 D 完成(WUv2-6):`git tag stage-v2-d-complete`

**library state 兼容性**:
- WUv2-2 改 RoleRef IR schema,旧 library 加载时用 model_validator 自动迁移
- WUv2-5 改 PatternRecognitionContract IR schema,同样自动迁移
- 转换脚本 `cli/migrate_library_v1_to_v2.py` 双向支持(便于回滚后转旧)

### 6.3 完整回滚路径

最坏情况:WUv2-5 完成后 11 库整体 Δ 显著下降:
1. `git tag rollback-checkpoint-v2-stage-c` 标记当前状态
2. `git reset --hard stage-v2-b-complete` 回阶段 B
3. 重新评估 WUv2-5 设计(三 contract + negative feedback 中哪个子改动是主因)

---

## 7. 工时与时间表

| WU | 工时 | 累计 | 依赖 | 阶段 |
|---|---|---|---|---|
| WUv2-1 | 0.25d | 0.25d | - | A 止血 |
| WUv2-2 | **1d** | 1.25d | - | A 止血 |
| WUv2-3 | 1d | 2.25d | WUv2-2 | B 机制 |
| WUv2-4 | 1.5d | 3.75d | - | B 机制 |
| WUv2-5 | 3d | 6.75d | WUv2-2 + WUv2-4 | C 架构 |
| WUv2-6 | 0.5d | 7.25d | - | D 优化 |

总计 **7.25 人天**(v1 总计 12.5d,v2 通过聚焦 5 改造省时约 42%;WUv2-2 修订后由 1.5d 降为 1d,因为撤销了"派生 + guard"路径)。

阶段化交付:

- **阶段 A(D1-D1.25,WUv2-1 + WUv2-2)**:止血,首次端到端验证(r_v2_a),预期 11 库整体 -16 → **+3~+5**(WUv2-2 阻断 22 regress - 少量 helped 损失)
- **阶段 B(D2.25-D3.75,WUv2-3 + WUv2-4)**:机制层重建,二次验证(r_v2_b),预期 +3 → **+7~+10**(singleton 严格 + retrieval 重建)
- **阶段 C(D4.75-D6.75,WUv2-5)**:架构层重建,**解除 WUv2-2 阻断**(用 binding_contract 合法重建 target binding)+ negative feedback,三次验证(r_v2_c),预期 +7 → **+12~+18**
- **阶段 D(D7.25,WUv2-6)**:配套优化,终次验证(r_v2_d),预期 +12 → +13~+20

---

## 8. 关键设计决策记录(决策依据)

### 8.1 为什么是 5 个改造而不是再加 WU14/15/16

v1 r24 暴露 3 个根因后,加 WU11/12/13 三个补丁,虽各自落地正确,但 11 库 r1 仍 -16。原因(详见 `full11_analysis_r1.md §6`):

- WU11 admission self_check:LLM 自报 estimated_recall 与实际 runtime 脱节(thrombosis pat 自报 1.0 仍 13 regress)
- WU12 online self_recall:落地正确但是单边屏蔽(formula_1 sing-973 自指 1.0 通过仍造灾)
- WU13 prefilter:控制成本不解决 false-positive

继续加 WU14/15/16 这种增量补丁救不了根本问题。**必须从架构层重建**。

### 8.2 为什么撤掉 confidence 阈值方案

读 `workspace/pre_condition_cache.json` 1000 条 entry 实测:

- matches=true: 458 条,平均 confidence=0.970,中位数=0.95
- matches=false: 542 条,平均 confidence=0.947,中位数=0.95
- matches=true 且 confidence<0.5: 1 条

LLM 在 yes/no 判断上几乎不给中等 confidence。**这不是 prompt 问题,是 LLM 在二值判断任务上的固有行为**。加阈值无效。

正确做法:**用代码端结构化约束**(grounded_anchors / applicability_predicates / role_family binding),不依赖 LLM 自报 confidence。

### 8.3 为什么不再用"this case really needs repair?"反问

`full11_analysis_r1.md §4.5.B 方向 15` 曾建议"pattern 触发后做 repair_direction 领域可逆性反问"。但这种 LLM 反问:

1. **违反 answer-blind 硬边界**(plan v1 §0.2 与 §8.1):runtime 不应做对错判断
2. **无法验证**:LLM 反问输出是另一段自由文本判断,与 recognition LLM 同样的不稳定问题

替代方案:**applicability_contract 代码端检验**。把"什么时候 repair 适用"显式写成代码端可机械判定的谓词(WUv2-5)。LLM 在 admission 时从词表选谓词,runtime 时只做谓词代码端检验,不做自由判断。

### 8.4 为什么 schema literal anchors → grounded anchors

`full11_analysis_r1.md §4.4.B 方向 9` 曾建议"admission 强制 pre_sql_signature 含 ≥ 2 schema-specific 列名/表名"。

实测反例:thrombosis pat-1172-1257 已含 "COUNT(*) Patient-Laboratory" 字面术语,字面命中很高 → 但 repair_direction 在领域上不普适。**强制字面会把方向引向 schema 字面过拟合**。

正确表述:**grounded_anchors**。anchor 可以是 column_role / path_role / relation_role 等代码端可派生的角色标签,**不一定是字面列名**。这样既覆盖 toxicology 类(字面 atom_id 强 anchor),也覆盖 codebase 类(role-based anchor,如 `editor_ref_role`)。

### 8.5 为什么 question_view 纯化不再列为方向

核对 commit 历史:

```text
667a940 Purify admission question evidence view
eb1271e Refactor pattern admission evidence boundaries  ← question/sql/shared 三 view 分栏已在此
```

本轮 11 库 run 基线包含此改动,**question_view 已物理分栏**。

剩余问题不是"没纯化",而是 error_instance_extractor 在 case_card 阶段 LLM 仍看全字段(因为 case_card 是给 LLM 看的入口),且 runtime 端**过度信任 LLM 写出的 signature 文本本身**(LLM 写出 SQL 词汇仍能塞进 question_signature)。WUv2-5 三 contract 拆分 + grounded_anchors 结构化约束从根本上解决。

### 8.6 为什么 v2 保留 v1 的 WU5-WU8 清理

v1 WU5-WU8 撤销了:
- 14 phenomenon 闭词(`core/vocabulary.py BIAS_RECOGNITION_SIGNAL_VOCABULARY`)
- `_column_role` 英文 token 启发式
- `_bucket_count` 桶化
- `_is_substantive_hard_signal` 黑名单

这些撤销是对的(详见 v1 §1.1)。v2 不回退。

**v2 解决 v1 撤销后留下的真空**:用代码端结构化(repair_card / grounded_anchors / applicability_predicates / role_family)替代旧 case-specific 启发式。

### 8.7 为什么 WUv2-2 不在新 case 上用 role_family 派生 target(2026-05-11 修订)

第一次执行 WUv2-2 时的越界探针:

- 撤掉 seed 字面 `cards.name` 后,compiler 仍能通过 `column_role="primary name"` 在当前 schema 找到 `cards.name`
- q366 evidence "rule refers to format",S0 正确,但 compiler 产 hint "把 l.format 换成 T1.name" → 仍然 regress
- 问题从"字面泄漏"变成"role 泄漏"
- 补的 answer-focus guard 是看到错误后加启发式拦截 — 这是 v1 反思中**要避免的反模式**

**根因**:仅靠 role profile 在新 schema 派生 target_columns,**是把 seed 的"要改输出"决定泄漏到新 case**。

正确做法:**WUv2-2 完全不再派生 target_columns**。"在新 case 上合法派生 target"的能力由 WUv2-5 用 binding_contract 引入:

- WUv2-5 admission 时,LLM 在每个 admitted pattern 上输出 `binding_contract.target_slots`(含 role_family,**不含字面列名**)
- WUv2-5 admission 同时强制输出 `applicability_contract.must_hold_before_rewrite` 谓词,代码端可机械检验
- runtime 时,只有同时满足 (applicability_contract 全部 hold) + (binding_contract 在当前 schema 找到唯一 target column) 的情况,才允许走 SELECT_REPLACE 等改变 answer unit 的 primitive

这样保证:**不存在"在没有 WUv2-5 binding_contract 的情况下,把 seed target role 泄漏到新 case"的路径**。WUv2-2 与 WUv2-5 配合后,seed target binding 只在严格契约下复用,不会泛化滑动。

### 8.8 为什么 v2 保留 WU0 final_freeze skip

WU0 节省 1h freeze 时间,代价是失去 cross-pattern replay 安全网。v2 用 negative feedback(WUv2-5)替代该安全网:

- regression 立即被 negative_guard 阻止后续触发(比 freeze 检测更快)
- DeepEye selector(WUv2-1)作为运营层兜底

freeze skip 保留,后续 v3 再考虑恢复。

---

## 9. 与现有文档的关系

- `current_pipeline.md`:
  - v2 完成后需要更新 §3(5 层 gate → 5 层判断)、§7(trigger_contract → 三 contract)、新增 §X(repair_card retrieval)
  - 标注 §11 问题清单状态(v2 解 / 部分解 / 仍存)
- `experiment_log.md`:
  - 每个 WUv2-{N} 完成时追加 entry,含验收结果 + 11 库整体 Δ 变化
- `full11_analysis_r1.md`:
  - v2 完成后追加 §8 "WUv2 改造后 r2 实测对照",对比 r1 v1 与 r2 v2 的 11 库结果
- `CLAUDE.md`:
  - 更新 "两条硬边界" 段:answer-blind 硬约束的实现现在分布在 recognition / applicability / binding 三层
  - 更新 "三层记忆" 段:singleton 严格化后,singleton 路径与 pattern 路径行为差异显著

---

## 10. 维护策略

### 10.1 改造开始前

```bash
git checkout -b eea-mechanism-rebuild-v2 eea-repair-interface-v1
git tag pre-v2-refactor
```

在 `experiment_log.md` 追加一条 entry 标记 v2 计划开始执行(含起点 commit + 11 库 r1 基线数据)。

### 10.2 每完成一个 WU

1. **commit 原子化**:单个 WU 一个 commit;commit message 含验收结果 + 11 库整体 Δ
2. **更新本文档**:在对应 WUv2-{N} 段末尾追加 `**完成 commit**: <hash>` 一行
3. **更新 experiment_log.md**:一条 entry,含改动概要 + 验收数据 + 与上 WU 的差异
4. **如验收未达预期**:决定是修复还是 revert,在 log 中记录原因
5. **更新 full11_analysis_r1.md §8**(v2 进展专章,追加方式)

### 10.3 每完成一个阶段(A/B/C/D)

1. 跑 §4 6 项强制检查
2. 全部通过:`git tag stage-v2-{a|b|c|d}-complete`
3. 把本阶段 WU commit squash 合入 `main`(可选,看后续策略)

### 10.4 整个 v2 计划完成后

1. 更新 `current_pipeline.md`(详见 §9)
2. 更新 `CLAUDE.md` 中涉及的设计描述
3. `git tag v2-refactor-complete` 标记完成
4. 在 `experiment_log.md` 写一篇 "v2 改造结案":11 库 r1 v1 → r2 v2 的端到端对比

---

## 11. 与 v1 的差异摘要(快速参考)

| 维度 | v1 | v2 |
|---|---|---|
| 改造单元数 | WU0-WU13 共 14 个 | WUv2-1 to WUv2-6 共 6 个(其中 WUv2-5 是大单元) |
| 工时 | 12.5d | 7.25d |
| 核心方法 | LLM 自由文本贯穿(retrieval / admission / runtime 三处 LLM 协作)| 代码端结构化接口(repair_card / grounded_anchors / applicability_predicates / role_family binding)+ LLM 在每个接口受限职能 |
| pattern contract | 单一 pattern_recognition_contract(4 字段自由文本)| 三 contract(recognition + applicability + binding,每个含代码端可检字段)|
| runtime 判断 | 单一 LLM Q+S 通道 | 五层判断(recognition LLM,applicability 代码,binding 代码,compile,rewrite)|
| singleton 触发 | 与 pattern 共用,描述粒度自然区分 | 严格化,只走 exact/near-exact;泛化责任在 admission 升 pattern |
| canonical_op IR | 含 seed gold table/column/expression 字面 | 仅 role_family / path_role,字面进 evidence audit 字段 |
| seed target binding 在新 case 的使用 | ActionCompiler 用 seed table/column 字面直接生成 target | **WUv2-2**:完全阻断(对应 primitive 0 candidates → no_action);**WUv2-5**:仅在 admission 输出有效 binding_contract + applicability_contract 验证通过时,在新 schema 派生 target |
| regression 反馈 | s0_correct=True 跳过 accumulate | regression 进入 negative_feedback 加 negative_guard |
| 运营层安全网 | 无(DeepEye direct_accept_s1 默认)| 有(DeepEye selector 默认开启)|

---

## 12. 验收与终结

v2 计划终结条件:

1. **量化指标**:11 库整体净 Δ ≥ +10
2. **质量指标**:
   - 类 IV regress 总数 ≤ 5(对比 v1 r1 22 例)
   - codebase / financial 形成 ≥ 2 pattern(对比 v1 r1 codebase 4 个错位 / financial 0)
   - 人工标注 6 个 EF2 group 中至少 4 个有 pattern 形成
3. **稳定性**:连续 2 轮 r2 跑结果差异 ≤ 5%
4. **可维护性**:applicability 谓词词表稳定(连续 1 轮 r2 后无新增词表请求)

达成上述条件后,`git tag v2-refactor-complete`,在 main 上 squash merge,关闭 `eea-mechanism-rebuild-v2` 分支。

后续 v3 路线图(不在本计划范围):

- 恢复 final_freeze 的 cross-pattern replay 安全网(配合 negative_guards 做双重保护)
- ContrastiveRepairEffect 的 source_state runtime 投影(v1 §0.3 已列)
- 跨 DB pattern 共享(如 ID 输出 contract 在 financial 和 EF2 都适用)
