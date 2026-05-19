# E2E11 分析与改进计划

## 一、运行概况

批次：e2e11_qwen3coder_openrouter_20260518_141502
代码：eea-mechanism-rebuild-v2 分支，HEAD = 3e9d5e4

| DB | baseline | enhanced | Δ | improved | regressed |
|---|---:|---:|---:|---:|---:|
| toxicology | 95 | 100 | +5 | 9 | 4 |
| formula_1 | 120 | 123 | +3 | 3 | 0 |
| card_games | 126 | 122 | -4 | 0 | 4 |
| codebase_community | 137 | 136 | -1 | 0 | 1 |
| 其他 7 库 | 614 | 614 | 0 | 0 | 0 |
| **总计** | **992** | **995** | **+3** | 12 | 9 |

ready=52, 触发精度=12/52=23%。

## 二、两个主要问题

### 问题 A：各环节信号不足

系统的工作流是合理的（accumulate → formation → trigger → compile → rewrite），但各判断环节缺少精确信号，导致判断失常。

#### A1. Formation：gold_join_edges 提取不完整

**影响**：card_games pattern 360-438 混入了 q360（set_translations）和 q365（foreign_data），导致 4 regression。

**数据**：q360 的 gold SQL 是子查询结构 `SELECT ... WHERE id IN (SELECT id FROM cards ...)`，`_compact_retrieval_evidence` 只提取 explicit JOIN 的 edge，不处理子查询内的隐含关联 → `gold_join_edges = []`。

**源码位置**：`common/analysis/signal_summary.py` 中 `_compact_retrieval_evidence` 函数（从 error_instance 的 gold SQL 提取 join edges）。需要追查 `_table_diff` 和 `_normalized_join_edge` 的实现，确认子查询结构是否被解析。

#### A2. Trigger：branch required_signals 缺列级语义角色

**影响**：toxicology singleton-206 误触发 baseline-correct cases（q216/q248/q276），导致 4 regression。formula_1 误触发 q928 等。

**数据**：
- singleton-206 的 `required_signals = ['pred.output_arity=2']`——只要 S0 有 2 列就触发
- helped case q249：S0 输出 `a1.element, a2.element`（element 角色）→ gold 要单列 element ✅
- regressed case q216：S0 输出 `c.atom_id, c.atom_id2`（atom identifier 角色）→ gold 要双列 atom_id ❌
- 区分信号 `pred.output_slot_role=0:atomic element` 在 `build_current_case_signals` 中已生成（runtime.py:777-782），但未进入 branch required_signals

**源码位置**：`_branch_spec_required_signals`（pattern_formation.py:3891-3909）取成员 `pred.*` tags 交集。交集策略导致列级角色信号（在成员间名称可能不完全一致）消失。

#### A3. Trigger：route_evidence_fast_track 跳过语义检查

**影响**：codebase q702（单表 `COUNT(*) FROM posts`）被 postHistory pattern 误触发，导致 1 regression。

**数据**：q702 的 `join_count=0` 和 pattern 成员的 `join_count=1` 不同，但 `source_state_facts` 被 route_evidence_fast_track defer 了（runtime.py:2552-2557）。

**源码位置**：runtime.py:2547-2560。fast_track 对所有 source_fact_misses 统一 defer，不区分 load-bearing facts（join_count/join_tables）和 non-essential facts。

#### A4. Rewrite：schema context 缺目标表

**影响**：formula_1 q902 在 e2e3 中 S1=S0（rewrite LLM 拒绝改写），在 e2e11 中修复后成功 helped。

**已修复**：commit 47e0b96（EEA 侧 guard.required_schema_tables）+ DeepEye 侧 schema 扩展。
验证：q902 的 rewrite LLM 收到 driverStandings schema 后成功产出正确 S1。

### 问题 B：pair scoring 对 branch-level 差异过度拒绝

**影响**：7 个零变化库中，人工标注的强 pattern 成员被 retrieval 成功召回，但在 pair scoring 阶段被拒，无法形成 component → 无法进入 admission → 没有 pattern。

**数据（从 eea_retrieval_audit.json 逐库提取）**：

| 库 | 人工 pair | score | retrieval 结果 | pair 结果 | 拒绝原因 |
|---|---|---:|---|---|---|
| superhero | 728×726 | 0.535 | 召回，有 gold_edge+target_eq | rejected | `output_grain_conflict` → downgrade → `lowering_family_incompatible` |
| ef2 | 1087×1064 | 0.562 | 召回，有 gold_edge+target_eq+target_role | rejected | 同上 |
| financial | 193×141 | 0.217 | 召回，有 gold_edge+target_eq | rejected | `grain_branch_axis_downgrade`（route reasons 不足未 downgrade） |
| thrombosis | 1276×1261 | 0.275 | 召回 | rejected | `output_grain_conflict` hard_conflict |
| california | 87×23 | 0.517 | 召回，有 gold_edge+target_eq | rejected | `case_local_insight_conflict` |
| student_club | 1451×1407 | 0.128 | 召回 | rejected | `hard_conflict` |
| debit_card | 1527×1511 | — | **未召回** | — | retrieval 失败 |

**源码位置**：

1. `_hard_conflict`（pattern_formation.py:760-776）：`output_grain_conflict` 是 `_DIRECT_MERGE_ONLY_VETOES`，可被 downgrade
2. grain downgrade（pattern_formation.py:1233-1249）：有 route_strong_reasons 时 veto=None
3. `_shared_program_pair_compatibility`（pattern_formation.py:1085-1129）：**lowering_family 检查是独立的 hard check**（line 1091），不受 grain downgrade 影响

**根因**：grain downgrade 放过了 Step 2a（grain conflict），但 Step 2d（lowering_family_incompatible）是一个**独立的、不可 downgrade 的 hard check**。superhero 728×726 和 ef2 1087×1064 都是 grain downgrade 后被 lowering_family 再次拦住。

**设计矛盾**：branch 的设计初衷就是容纳"同一修复方向下的不同具体操作"。但 pair scoring 在 branch 构建之前就把 lowering_family 不同的 pair 拒了——branch 没有机会发挥作用。

## 三、改进计划

### 改动 1：Formation 信号补全——子查询 gold_join_edges 提取

**目标**：让子查询结构的 gold SQL 也能提取出 join edges。

**需要做的**：
1. 读 `signal_summary.py` 中 `_compact_retrieval_evidence` 的完整实现，确认 edge 提取逻辑
2. 读 `structure_delta_v2.py` 或 `structure_family.py` 中 gold SQL 的 AST 解析，确认是否支持子查询
3. 在 edge 提取中增加对 `WHERE col IN (SELECT ... FROM table)` 和 `WHERE EXISTS (SELECT ... FROM table WHERE ...)` 结构的处理

**验证**：用 card_games q360 和 q438 的 gold SQL，确认修复后 edge 非空。offline probe 不需要跑 e2e。

### 改动 2：Trigger 信号丰富——branch required_signals 从 repair target 反推

**目标**：让 branch 的 required_signals 包含列级语义角色，而不仅是粗粒度 `pred.*` tags 交集。

**需要做的**：
1. 读 `_branch_spec_required_signals`（pattern_formation.py:3891-3909）的完整逻辑
2. 从 branch 的 canonical_op 的 `target_output_roles` 或 `source_output_roles` 反推：如果 repair 是"替换某个 element 列"，那 branch 的 required_signal 应包含 `pred.contains_column_role=atomic element`
3. 这些 role 信号来自成员的 repair_program，是确定性的（不依赖 LLM）

**验证**：用 toxicology singleton-206 的 case card，确认新生成的 required_signals 能区分 q249（element 角色）和 q216（atom_id 角色）。offline probe。

### 改动 3：Pair scoring 放宽——lowering_family 差异作为 branch axis

**目标**：当有 route_strong_reasons 时，lowering_family 差异不 hard block，而是作为 branch axis。

**需要做的**：
1. 在 `_shared_program_pair_compatibility`（pattern_formation.py:1091）中，当 `grain_branch_axis_downgrade` 已生效时，`lowering_family_incompatible` 也 downgrade 为 blocker（不直接 return False）
2. 让 `synthesize_shared_program` 能处理 lowering_family 不同的成员——产出一个多 branch 的 program

**验证**：用 superhero 728×726 和 ef2 1087×1064，确认修改后 pair 从 rejected 变为 accepted（with branch axis blocker）。offline probe。

### 改动优先级

| 改动 | 影响范围 | 预期收益 | 实施难度 |
|---|---|---|---|
| **改动 3** | 7 个零变化库 | 解锁 formation → 从 0 pattern 到有 pattern | 小（改 1 个条件） |
| **改动 2** | toxicology + formula_1 触发精度 | 减少 regression（-9 → 可能 -5 以下） | 中（需要反推 role 逻辑） |
| **改动 1** | card_games 聚类纯度 | 减少 card_games regression | 中（需要理解 AST 解析） |

建议顺序：3 → 2 → 1。改动 3 最小代价最大收益（7 个库从 0 到有可能产出收益）。
