# EEA 涌现化改造执行计划

> 维护：本文档是基于对 `current_pipeline.md` 的端到端实现盘点 + 与人工标注数据集（`/method/EEA/doc/db_pattern_discussion.md` + `case_audit.md`）的对照结果整合的具体改造方案。
>
> 目标：把当前依赖 case-specific 预定义规则的实现，改造为"前置特征驱动的相似性预测 + 多 case 涌现"的架构。
>
> 主分支：`eea-repair-interface-v1`，起点 commit：`323a43b`。
>
> 关联文档：
> - `current_pipeline.md` — 当前实现端到端讲解
> - `experiment_log.md` — 历次改动决策日志

---

## 0. 背景与目标

### 0.1 现状的根本问题

诊断 git 历史（近 50 个 commit）+ 端到端代码盘点后发现：

1. **触发链路完全无语义**：runtime gate 5 层全部基于结构信号（14 phenomenon 闭词 jaccard / `pred.X=Y` 字符串集子集匹配）；问题文本、SQL 字面字段、`repair_insight_signature` 等语义资产**完全不进 trigger 决策**。
2. **结构信号生成自身是 case-specific 拟合**：`_column_role` 英文 token 启发式、14 phenomenon 闭词命名、`_core_pred_contract_signals` 用 locus 决定 required 字段、`_is_substantive_hard_signal` 黑名单丢否定信号 —— 都是从过往案例反推命名/规则。
3. **pattern 比 singleton 触发反更严**：14 闭词阈值 + trigger_contract 严格子集 + branch ambiguity；实测 r19 中 pattern 路径被 singleton 抢占。
4. **runtime 与 accumulate 的语言边界混乱**：之前提案用 `misread / misalignment` 这类**事后审计语言**做 runtime 触发条件，要求 LLM 在没有 gold 的情况下做对错判断 —— 概念错误。
5. **层叠机制不撤旧层**：从 trigger_contract 严格子集 → lightweight pattern recognition → branch matching → bias_recognition → root+branch evolution → pattern extension，5 层并存且各自打补丁，不同案例反复加 case-specific 规则。

### 0.2 改造目标

把架构对齐到："**从历史错误中提取共性 → 用相似性预测新 case 是否重复犯错 → 给方向 + branch 实例化**"。

具体到机制：
- **触发是相似性预测，不是对错判断**：runtime answer-blind，只能对比 (question, pred_sql) 与 pattern 学到的前置特征是否相似
- **错误判断在 accumulate 阶段完成**：那时看 gold 才能判错，把"在什么前置条件下犯错"压缩进 pre-condition 字段
- **pattern + branch 架构保留**：pattern 表达共性识别（用 LLM 涌现描述），branch 承担实例化路径多样性
- **family 取消（已在历史决策中确认）**：不可稳定实例化的 case 留 singleton，不进 pattern

### 0.3 不在范围内的事项

- 重新引入 family 层（已取消，决策不变）
- "最小实例化函数"口径的 pattern（历史已确认抽取太难）
- 跨 DB 验证（先不做）
- toxicology 全 145 case 验证（先做 focus18，稳定后扩）
- ContrastiveRepairEffect 的 source_state runtime 投影（独立大改造，不在本计划）
- 删除 Locus / OpFamily / TargetFamily / OutputContract / ActionPrimitive 等结构事实 Enum（这些是 SQL 编辑坐标系，保留）
- 删除 `effect_axis` 12 闭值（这是抽象修复轴大类，每个不到根因粒度，保留作为 retrieval bucket）

---

## 1. 设计原则

### 1.1 涌现 vs 预定义边界

| 类型 | 是否允许 | 理由 |
|---|---|---|
| **结构事实坐标 Enum**（Locus / OpFamily / TargetFamily / ActionPrimitive）| ✅ 保留 | SQL 编辑坐标系，不是规则拟合 |
| **抽象修复轴大类**（effect_axis 12 值）| ✅ 保留 | 范畴层级，不到具体根因 |
| **case-by-case 命名识别词**（14 phenomenon 闭词）| ❌ 撤除 | 命名暗示用意，从案例反推 |
| **case-specific 规则补丁**（motif 互斥表 / source-route 关键词命中）| ❌ 撤除（已部分完成 @323a43b） | 案例拟合 |
| **英文命名启发式**（`_column_role`）| ❌ 撤除 | 命名约定脆弱，跨语言 schema 失效 |
| **跨 case 涌现描述**（pattern.pre_question_signature 等自然语言）| ✅ 是新机制 | 多 case admission 时 LLM 涌现，不限词表 |

### 1.2 Runtime 边界

- runtime 只看 case 已携带的特征：question 文本 / pred_sql 文本 / schema_view / role_graph
- runtime 不做对错判断，只做"特征是否落在 pattern 描述的区域"
- 错误判断在 accumulate 阶段完成（gold-aware 写入 pre-condition 字段）

### 1.3 Pattern 字段语义清晰化

| 字段 | 性质 | runtime 是否使用 |
|---|---|---|
| `pre_question_signature` | 前置条件：什么类型的 question 容易触发此错 | ✅ 通道 Q |
| `pre_sql_signature` | 前置条件：什么形态的 pred_sql 容易触发此错 | ✅ 通道 S |
| `observed_failure_summary` | 事后审计：在以上前置条件下，pred 与 gold 的差异是什么 | ❌（仅 audit）|
| `repair_direction` | 修复方向（自然语言）| ✅（实例化阶段输入）|
| `branch.required_signals`（结构特征）| 当前 schema 下选哪条修复路径 | ✅ branch matching |

---

## 2. 关键概念定义

### 2.1 前置特征（pre-condition signature）

Pattern/Singleton 在 accumulate 时记录、admission 时多 case 涌现的特征描述。**不带"错"的语义**，只描述"这种 question + 这种 pred_sql 在历史上出现过失败"。

例（toxicology q206 系列 pattern）：
- `pre_question_signature`: `"asks for chemical elements participating in a typed bond"`
- `pre_sql_signature`: `"projects two columns playing the same role on the connected relation simultaneously (双 alias on connected)"`

### 2.2 2 通道触发

```
通道 Q：current case.question 是否符合 pattern.pre_question_signature
通道 S：current case.pred_sql + schema 是否符合 pattern.pre_sql_signature
两条都中 → pattern 触发 → 进入 branch matching
```

每条用一次小 LLM 调用判断，cache key 分别为 `(pattern_id, question_hash)` 与 `(pattern_id, pred_sql_hash, schema_hash)`。

### 2.3 Singleton 与 Pattern 共用机制，区别只在描述粒度

|  | Singleton（单 case 派生）| Pattern（多 case 涌现）|
|---|---|---|
| `pre_question_signature` | 具体（含 case 题型细节）| 抽象（多 case 共性）|
| `pre_sql_signature` | 具体（含 case SQL 形态细节）| 抽象（多 case 共性）|
| 触发命中率 | 自然低（描述具体）| 自然高（描述抽象）|

不需要手动调阈值；"宽松/严格"由描述粒度自然决定。

### 2.4 Schema 语义角色（替代 `_column_role`）

撤掉英文 token 启发式后必须建立的能力：每个 LocalSchemaView 的列携带 `role_family` 标注。

派生路径优先级：
1. 用户/数据库提供的 hint（`LocalSchemaView.semantic_hints` 已有此字段，已支持但未用）
2. accumulate 时一次性 LLM 标注（per-table 一次调用，cache schema-level）
3. 兜底为 `unknown`

不再回退到 `_column_role` 启发式。

---

## 3. 工作单元清单（执行顺序）

### 颗粒度与 commit 约定

**每个 WU 对应一个独立 commit**，即使 WU 较大也不拆分（拆分会破坏"原子回滚"语义）。

Commit 命名：`WU{N}: {short subject}`，body 包含：
- 改动文件清单
- 验收结果（focus18 数 / pytest / 关键诊断）
- 与上一 commit 的差异点

回滚由 git 直接管理（详见 §6.2），不在代码中保留双轨 fallback / env flag。每 WU 内部可以拆 commits 用于增量调试，但向 main 合入时要 squash 成单个原子 commit。

### 阶段 A：基础设施与依赖（必须先做）

#### **WU0** — Final freeze 跳过开关 + 诊断 dump 工具

**目的**：让后续 r20+ 验证不被 1 小时 final freeze 卡住；同时建立可对照的"现状证据"基线。

**改动**：
1. `learning/evolution.py::final_evolve_and_freeze` 增加 `skip_replay_freeze: bool = False` 参数
   - skip 时直接返回上一步 `last_library` + 空 freeze audit `{"skipped": true}`
2. `cli/run_online_e2e_validation_v2.py` + `cli/run_multidb_validation_v2.py` 增加 `--skip-final-freeze`
3. `common/config.toml` 加 `[evolution] skip_final_freeze = true`
4. 输出 `compact_evolution_report.final_freeze_skipped: bool`

**新增 dump 工具**：`cli/audit_pattern_signatures.py`
- 输入：`--library_json` 或 `--work_root`
- 输出：每个 pattern 的 (case_ids / current bias_recognition_contract / current trigger_contract.required_signals / repair_insight_signature / formation_signals.pattern_admission)
- 用于阶段 B 改造前的现状基线

**验收**：
- r20 focus18 + `--skip-final-freeze` 总耗时 ≤ 35 min（基线 ~1.5h）
- `final_library.json` 中 patterns 数量 == incremental 阶段最后一步
- `audit_pattern_signatures.py` 能从 r19 final library 中导出全部 5 个 pattern 的 signatures

**回滚**：默认 false 不传即不改；不需要 git 回滚（新增功能不破坏旧行为）

**工时**：0.5d

**依赖**：无

**完成状态**：已实现。默认跳过 final freeze；`run_online_e2e_validation.py`
读取 `[evolution] skip_final_freeze=true`，并保留显式 `--skip-final-freeze`。
新增 `cli/audit_pattern_signatures.py` 可导出 pattern 签名摘要。

---

#### **WU1** — Schema 语义角色标注能力

**目的**：撤掉 `_column_role` 英文启发式（最底层 case-specific 规则）后，必须有替代的列角色标注路径。

**现状**：
- `analysis/role_graph_normalizer.py:32-48 _column_role` — 英文 token 启发式
- `analysis/role_graph_normalizer.py:51-62 _schema_column_role` — 优先 hint 兜底启发式
- `core/data_structures.py LocalSchemaView.semantic_hints` 字段已存在
- `io/db_schema_access.py` 加载 schema 时**未生成** semantic_hints（默认为空）

**改动**：
1. **新增 `analysis/schema_role_annotator.py`**：
   - 入口：`annotate_schema_roles(local_schema_view: LocalSchemaView, db_id: str) -> LocalSchemaView`
   - 实现：
     - 优先从持久化缓存读：`{cache_root}/schema_roles/{db_id}.json`
     - 缓存未命中时调用一次 LLM：输入 schema DDL + sample rows（如有），输出每列的 `role_family`（owner_ref / editor_ref / temporal / measure / category / identifier / ... 自由命名，不限闭集）
     - 写回缓存
2. **改 `io/db_schema_access.py`**：build LocalSchemaView 后调 annotator
3. **新增 prompt** `llm/prompts/schema_role_annotator.py`：
   - 输入：`{db_id, tables: [{name, columns: [{name, dtype, sample_values, fk_to}]}]}`
   - 输出：`{tables: [{name, columns: [{name, role_family, rationale}]}]}`
   - 强调：role_family 用自然短语，不限词表；同 db 内一致性

**验收**：
- toxicology / financial / codebase_community 三个库的 schema 跑一次 annotator，产出 `schema_roles/{db_id}.json`
- 抽样验证：q581 的 `posts.LastEditorUserId` 的 role_family 应不是 "identifier"，而是更具体的 "editor_ref" / "edit_history_ref" 类描述（具体词由 LLM 涌现）
- 抽样验证：q1418 的 `event.type` 应不是 "label"，而是 "event_type_marker" 之类
- annotator 调用幂等：同 schema 多次跑结果一致
- 无 LLM 时降级：返回 `LocalSchemaView` 但 hint 全空，不抛错

**回滚**：`git revert <WU1 commit>` —— 此 WU 是新增功能，回滚后 LocalSchemaView 不带 hint，下游 `_schema_column_role` 仍走 `_column_role` 启发式（WU6 才删除）

**工时**：1d

**依赖**：无

**完成状态**：已实现。新增 `common/analysis/schema_role_annotator.py`
与 `common/llm/prompts/schema_role_annotator.py`；`build_local_schema_view`
构造后会用缓存/LLM 给 `LocalSchemaView.semantic_hints` 补充自由命名的
`role_family`。同时撤掉 `db_schema_access.py` 中原有英文列名 `_guess_role_family`
启发式，LLM/缓存不可用时仅保留列说明，`role_family=None`。

---

### 阶段 B：抽取与演化的涌现化改造

#### **WU2** — accumulate 阶段：error_instance_extractor 加 4 个字段

**目的**：让 LLM 在看 gold 时（accumulate 唯一可看 gold 的位置）写下 pre-condition 与修复方向。

**现状**：
- `llm/prompts/error_instance_extractor.py` 已输出 `repair_insight_signature` (interface_key / source_misread / target_preference / repair_interface / ...)，但这是事后审计语言
- 该字段只在 audit + pair scoring 用，未进 trigger

**改动**：
1. **改 prompt** `llm/prompts/error_instance_extractor.py`：
   - 新增输出 4 字段：
     ```json
     "pre_question_signature_local": "what kind of question this case represents (free natural language, ≤200 chars)",
     "pre_sql_signature_local": "what shape/form pred_sql currently exhibits, described using schema role families (free natural language, ≤200 chars, NO case_id/table-name/column-name literals)",
     "observed_failure_local": "what differs between pred and gold given the above two pre-conditions (≤200 chars)",
     "repair_direction_local": "which direction the repair takes (≤200 chars, refer to schema role families when describing column targets)"
     ```
   - 强调：
     - `pre_question_signature` 与 `pre_sql_signature` 是"前置条件"，不带"错"的语义
     - `pre_sql_signature` 描述列时用 `LocalSchemaView.semantic_hints` 的 `role_family`，不用具体列名（跨 case/schema 工作）
     - `observed_failure` 是事后审计，runtime 不读
     - `repair_direction` 是自然语言指令，由 ActionCompiler/branch 后续实例化
   - 软化已识别问题：R6 "MUST be locus=SELECT" → "strongly suggest"；R1 "must agree" → "tend to align"

2. **改 schema** `core/data_structures.py::ErrorInstanceV2`：增加 4 字段
3. **改 `learning/accumulate.py::error_instance_to_singleton`**：把 4 字段写入 `formation_signals.pre_condition_local` + `trigger_contract.pre_condition`（新子结构）
4. **改 `analysis/signal_summary.py::build_trigger_contract`**：把 pre_condition 字段附加到 trigger_contract 输出

**验收**：
- 重跑 r19 focus18 第 1 个 case（如 q206），`error_instance.pre_question_signature_local` 含"asks for chemical elements" 类描述
- `pre_sql_signature_local` 不出现 "atom_id2" / "a2.element" 这种字面列名（用 role_family 替代）
- 重跑 5 个 case，4 字段长度均 ≤ 200 字符
- LLM trace 显示 prompt 大小未增 > 30%
- pytest 失败集 ⊆ 19 项 pre-existing

**回滚**：`git revert <WU2 commit>`。注意：WU2 后入库的 case 会带新字段，revert 后这些字段会变成"未消费"状态（仍写在 formation_signals 里但下游不读），不影响系统运行。LLM 不输出新字段时（旧 prompt cache）schema 用 `Optional` 接收为空 — 这是兼容性而非回滚开关。

**工时**：1d

**依赖**：WU1 完成（pre_sql_signature 用 role_family，需 hint）

---

#### **WU3** — admission 阶段：pattern_admission_judge 涌现版

**目的**：让 LLM 在多 case 上写出共性的 pre-condition + repair_direction，构成 pattern 的识别契约。

**现状**：
- `llm/prompts/pattern_admission_judge.py` 输出含 `bias_recognition_contract`（recognition_signals 限于 14 闭词 + jaccard 阈值）
- `pattern_formation.py::_pattern_case_card` 提供给 LLM 的输入字段已含丰富 code-derived signals

**改动**：
1. **改 prompt** `llm/prompts/pattern_admission_judge.py`：
   - case_cards 输入加入每个 case 的 `pre_question_signature_local` / `pre_sql_signature_local` / `observed_failure_local` / `repair_direction_local`
   - 输出增加 4 字段（替换 `bias_recognition_contract`）：
     ```json
     "pre_question_signature": "abstracted from members' pre_question_signature_local; describes the question type that triggers this failure",
     "pre_sql_signature": "abstracted from members' pre_sql_signature_local; uses role_family vocabulary",
     "observed_failure_summary": "common failure observed across members (audit only, not used at runtime)",
     "repair_direction": "common repair direction across members (input to action compiler at runtime)"
     ```
   - 强调：
     - 4 字段都是自然语言，不限词表
     - pre_*_signature 必须比成员描述更抽象（多 case 共性）
     - `repair_direction` 是 actionable 的（可由 branch + binder 实例化），不是空泛提醒；如不能 actionable 则 admit_pattern=false
     - 删除 `bias_recognition_contract` block（连同闭词词表注入）
2. **改 schema** `core/data_structures.py::InstantiationProgram`：
   - `bias_recognition_contract` 字段保留向后兼容但不再写入新值
   - 新增 `pattern_recognition_contract` 子结构（含上面 4 字段）
3. **改 `pattern_formation.py`**：
   - `_pattern_case_card` 输出 case 的 pre-condition 字段
   - admission 通过后把 4 字段写入 `pattern.instantiation_program.pattern_recognition_contract`
   - 删除 `_validated_bias_recognition_contract_payload` / `_attach_validated_bias_recognition_contract`（不再需要闭词验证）

**验收**：
- 用 r19 final library 的 5 个 pattern，跑 admission_judge 一次（输入历史 case_cards）：
  - 每个 pattern 输出 `pre_question_signature` 长度 ≤ 200 字符且不含具体 case_id / table-name
  - `repair_direction` 含 actionable 动词（drop / replace / collapse / aggregate / reroute / preserve / ...）
- `audit_pattern_signatures.py` 输出每个 pattern 的 4 字段，可人工 review
- toxicology pattern 2（q206 系列 8 case）的 `pre_sql_signature` 含 "two columns playing same role on connected relation" 这种基于 role_family 的描述
- pytest 失败集 ⊆ 19 项 pre-existing

**回滚**：`git revert <WU3 commit>`。WU3 期间 runtime 仍走旧 5 层 gate（WU4 才切换），所以 admission 输出新字段不影响线上行为；revert 后旧 admission prompt 与 bias_recognition_contract 自动恢复。**不双写**：双写会污染 library 数据。

**工时**：1.5d

**依赖**：WU2 完成（admission 输入需 case 级 pre-condition 字段）

---

### 阶段 C：Runtime 触发改造

#### **WU4** — runtime 2 通道 LLM 触发

**目的**：用 pre-condition 相似性预测替代 14 闭词 jaccard + 严格子集匹配。

**现状**：
- `runtime.py::_gate_group` 5 层 gate，line 2156-2614
- `runtime.py::_evaluate_bias_recognition` line 1418-1465（14 闭词 jaccard）
- `runtime.py::compute_bias_recognition_signals` line 512-562（生成 14 闭词）

**改动**：
1. **新增 `runtime.py::_evaluate_pattern_pre_condition`**：
   ```python
   def _evaluate_pattern_pre_condition(
       *, group: GroupSummary, case_view: RuntimeCaseView,
   ) -> Tuple[Dict[str, Any], bool, List[str]]:
       """Two-channel similarity check using LLM."""
       contract = _pattern_recognition_contract(group)
       if not contract:
           return {}, False, ["missing_pattern_recognition_contract"]
       q_match = _channel_q_llm_call(  # cache key: (pattern_id, question_hash)
           question=case_view.question,
           pre_question_signature=contract["pre_question_signature"],
       )
       s_match = _channel_s_llm_call(  # cache key: (pattern_id, pred_sql_hash, schema_hash)
           pred_sql=case_view.pred_manifestation.top1_sql,
           schema_view=case_view.local_schema_view,
           pre_sql_signature=contract["pre_sql_signature"],
       )
       audit = {
           "channel_q": q_match,
           "channel_s": s_match,
           "schema_version": "pre-condition-v1",
       }
       blockers = []
       if not q_match["matches"]:
           blockers.append(f"channel_q_missed:{q_match.get('reason','')[:80]}")
       if not s_match["matches"]:
           blockers.append(f"channel_s_missed:{s_match.get('reason','')[:80]}")
       passed = q_match["matches"] and s_match["matches"]
       return audit, passed, blockers
   ```

2. **新增 prompt** `llm/prompts/pattern_pre_condition_match.py`：
   - 通道 Q prompt：
     ```
     question: "{question}"
     pattern.pre_question_signature: "{signature}"
     Does the question fall into the type described? Output strict JSON.
     ```
   - 通道 S prompt：
     ```
     pred_sql: "{pred_sql}"
     relevant_schema (with role_family hints): {schema_excerpt}
     pattern.pre_sql_signature: "{signature}"
     Does the SQL exhibit the described shape? Output strict JSON.
     ```

3. **改 `_gate_group`** (line 2156)：
   - 删除 Layer 2 `_evaluate_bias_recognition` 调用 + 后续 `variant_required_match=True` 跳过逻辑（line 2258-2277）
   - 替换为：
     ```
     if group.group_type in (PATTERN, SINGLETON) and pattern_recognition_contract exists:
         audit, pre_passed, blockers = _evaluate_pattern_pre_condition(group, case_view)
         if not pre_passed:
             passed = False
             reasons.extend(blockers)
     ```
   - Singleton 与 Pattern 共用，描述粒度自然决定阈值
   - 删除 Layer 3-5（required_signals / variant gates / soft path / generalized canonical gate），代码 line 2285-2398
   - 保留 Layer 1（hard gates）+ Layer 7（branch matching）+ binder dry-run

4. **保留并加固 retrieval 粗筛**（避免对所有 group 调 LLM）：
   - `retrieve_groups` (line 4088) 加 SQL 原子事实（select_arity / has_aggregate / table 重叠）粗筛，限制候选 ≤ N（默认 5 per case）
   - 对召回的候选才调 _evaluate_pattern_pre_condition

5. **缓存** `runtime/pre_condition_cache.py`：
   - LRU（max 1000 entries），磁盘 spill 到 `{work_dir}/pre_condition_cache.json`
   - 命中率写入 trace

**验收**：
- focus18 r20 触发率 ≥ r19 的 8/18（不退化）；目标 ≥ 9/18
- r20 中 pattern 路径占比 > singleton 路径占比（满足"pattern 比 singleton 触发宽松"）
- 单 case 平均 LLM 调用数 ≤ 10（粗筛 5 候选 × 2 通道 = 10）
- 缓存命中率 ≥ 50%（同 case 多次跑、同 question_hash 跨 case）
- 删除 14 闭词与 5 层 gate 后 pytest 失败集与基线 19 项不引入新增（修复反而可能修好原有的几项）

**回滚**：`git revert <WU4 commit>` 即可恢复旧 5 层 gate。**不在代码中保留双轨**——保留 fallback 会让后续 WU5-8 的清理无法落地（旧代码持续被引用）。如需对照实验，开 worktree / 新 branch 跑旧 commit。

**工时**：2d

**依赖**：WU3 完成（runtime 读 pattern_recognition_contract）

---

### 阶段 D：清理冗余机制（撤旧层）

#### **WU5** — 撤除 14 phenomenon 闭词与 bias_recognition gate

**目的**：核心清理。WU4 已建立替代机制，可安全撤除。

**改动**：
1. **删 `core/vocabulary.py:209-229`** `BIAS_RECOGNITION_SIGNAL_VOCABULARY` 整 set
2. **删 `runtime.py:512-562`** `compute_bias_recognition_signals` 整函数
3. **删 `runtime.py:1401-1465`** `_bias_recognition_contract` / `_case_bias_recognition_signals` / `_evaluate_bias_recognition`
4. **改 `runtime.py:287`** 不再注入 `case_view.bias_recognition_signals`
5. **删 `runtime.py:1414/1430/1435`** 闭词过滤逻辑
6. **删 `pattern_formation.py::_validated_bias_recognition_contract_payload`**（WU3 已弃用，此处物理删除）
7. **改 `llm/prompts/pattern_admission_judge.py`** 删除 `BIAS_RECOGNITION_SIGNAL_VOCABULARY` 引用与 bias_recognition_contract block（WU3 完成时已改）
8. **保留兼容**：`InstantiationProgram.bias_recognition_contract` 字段保留，加载旧 library 不报错

**验收**：
- `grep -rn BIAS_RECOGNITION_SIGNAL_VOCABULARY common/` 0 命中
- `grep -rn compute_bias_recognition_signals common/` 0 命中
- focus18 r21 触发数 ≥ r20 的水平
- pytest 失败集 ⊆ 19 项 pre-existing

**回滚**：`git revert <WU5 commit>` 即恢复全部被删函数与 vocabulary。物理删除而不留 `_deprecated_` 路径，因为下游 WU6-8 的清理会引用这些函数；保留 deprecated 路径会形成残留依赖。

**工时**：0.5d

**依赖**：WU4 完成且 r20 验证通过

---

#### **WU6** — 撤除 `_column_role` 启发式 / 桶化 / 黑名单 / 强制规则

**目的**：清理底层 case-specific 启发式。

**改动**：

1. **撤 `_column_role`**（最关键）：
   - `analysis/role_graph_normalizer.py:32-48` 删除函数
   - `analysis/role_graph_normalizer.py:51-62 _schema_column_role` 改为：
     ```python
     def _schema_column_role(*, table, column, schema_view) -> str:
         if not table or not column or schema_view is None:
             return "unknown"
         for hint in schema_view.semantic_hints:
             if hint.table.lower() == table.lower() and hint.column.lower() == column.lower():
                 return hint.role_family or "unknown"
         return "unknown"
     ```
   - 同步 `runtime/action_compiler.py:70 _column_role_from_name` 删除（重复定义）
   - `_get_column_role`（action_compiler.py:89）改为只查 schema_view.semantic_hints

2. **撤 `_bucket_count`**：
   - `analysis/signal_summary.py:472-480 _bucket_count` 改为返回原始 int（保留函数名兼容）
   - 下游 `_pred_current_summary` 字段改为 `int` 类型
   - 改 `_pred_contract_signals_from_summary`：`pred.join_count_bucket={value}` 改为 `pred.join_count={int}`
   - **注意**：这是字符串 token 改变，导致 trigger_contract.required_signals 字符串与历史 library 不兼容。本 WU 后历史 library 必须重新 build_trigger_contract。

3. **撤 `_is_substantive_hard_signal` 黑名单**：
   - `runtime.py:2107-2149` 改为只过滤 `program.*` 前缀（保留），其它信号全部进入比对
   - 包括允许 `pred.has_aggregate=False` / `pred.contains_column_role=other` / `pred.contains_relation_role=root_table` 进 trigger_contract

4. **软化 prompt 强制规则**：
   - `llm/prompts/error_instance_extractor.py:259-268 (R1)`：`must agree` → `tend to align`
   - `llm/prompts/error_instance_extractor.py:298-320 (R6)`：`MUST be` → `strongly suggest`（保留 R6 内容作为 hint）
   - WU2 prompt 重写时一并完成

5. **删除已知 case-specific 段**：
   - `pattern_formation.py::_bias_signals_for_group` 中 action_payload `if any(token in ... for token in ("reroute","source_route","join_route","bridge"))` 段（已确认是 case-specific 补丁）

**验收**：
- `grep "_column_role\b" common/ runtime/` 仅剩 `_schema_column_role`（不含递归）
- toxicology 库的 schema 经 WU1 annotator 后，role_graph_normalizer 派生的 `pred.contains_column_role={role}` 信号有具体 role（不是 `unknown` 占主导）
- 中文/拼音 schema 的库（如内部数据库）不再退化（之前会全部落 `other`）
- focus18 r22 触发数 ≥ r21
- pytest 失败集 ⊆ 19 项 pre-existing
- 历史 library 重新 build_trigger_contract 后所有 pattern 仍可加载（schema_version 兼容性）

**回滚**：`git revert <WU6 commit>` 恢复 `_column_role` 启发式 + 桶化 + 黑名单 + 强制规则。WU6 改动跨越多个文件，commit 应明确包含全部改动以保证 revert 原子性。

**工时**：1d

**依赖**：WU1（schema 标注必须就绪，否则 column_role 全部为 `unknown`）；WU5 完成（避免清理冲突）

---

#### **WU7** — 撤除 `trigger_contract` 严格子集匹配（pattern 上）

**目的**：pattern 触发不再依赖 trigger_contract.required_signals 严格子集（WU4 已改用 pre-condition）；trigger_contract 在 pattern 上降级为 audit。

**现状**：
- WU4 已删除 `_gate_group` 中的 required_signals / variant gates / soft path
- 但 trigger_contract.required_signals 字段仍在 admission/promotion 写入，runtime 加载时会过滤

**改动**：
1. **改 `trigger_contract.py::is_contract_runtime_executable`**（line 123）：
   - pattern 上不再要求 required_signals / variant_required_signal_sets 非空
   - 只要求 `_has_program(contract)` 为真
2. **改 `trigger_contract.py::ensure_materialized_trigger_contract`**：
   - pattern 不进入 legacy_signature → required_signals 实例化路径
3. **改 `signal_summary.py::build_trigger_contract`**：
   - pattern 模式下 `required_signals` / `variant_required_signal_sets` 仍可填（仅用作 audit），但加 `audit_only=True` 标记
4. **保留 singleton 严格匹配**：
   - singleton 的 `_singleton_canonical_exact_check`（runtime.py:2034）不变 —— singleton 仍用 trigger_contract 做精确比对
   - 但 singleton 也加 pre-condition 通道（WU4 已加），即 singleton 满足两条：(A) pre-condition 通道 + (B) singleton_canonical_exact_check

**验收**：
- pattern 触发不再因 `required_contract_signals_missed` 失败（gate audit 中此 reason 在 r22 出现 0 次）
- singleton 触发率不下降（保留严格匹配）
- pytest 失败集 ⊆ 19 项 pre-existing

**回滚**：`git revert <WU7 commit>`。WU7 是 trigger_contract 模块改动，回滚后 pattern 重新走 required_signals 严格匹配（但因 WU4 已删 _gate_group 中的引用，需配合 WU4 一起 revert 才完整恢复）。

**工时**：0.5d

**依赖**：WU4 完成

---

#### **WU8** — 撤除 case-specific token 命中段

**目的**：清除 pattern_formation 中残留的 case-specific 字符串命中规则。

**改动**：
1. **删 `pattern_formation.py::_bias_signal_from_runtime_signal`** 整函数（手维 mapping dict）
2. **删 `pattern_formation.py::_bias_signals_for_group`** 整函数（含 action_payload token 命中段）
3. **改调用方**：
   - `_try_extend_existing_pattern` 改为用 WU4 的 `_evaluate_pattern_pre_condition` 做扩入判断（不再 jaccard）
4. **保留** `_pair_supports_root_membership` 与 `score_pair`（这些不是 case-specific，是结构兼容性评分）

**验收**：
- `grep "_bias_signal_from_runtime_signal\|_bias_signals_for_group" common/` 0 命中
- 在 r19 库基础上，`_try_extend_existing_pattern` 对 q302 singleton 仍能成功扩入 RoleGraph pattern（用 pre-condition 通道判断）
- focus18 r23 不退化

**回滚**：`git revert <WU8 commit>` 恢复 mapping dict + token 命中段。

**工时**：0.5d

**依赖**：WU4 完成

---

### 阶段 E：演化机制对接

#### **WU9** — pattern_formation 内部对齐

**目的**：把 pattern admission / extension / dedup / promotion 全链路对齐到新的 pre-condition 体系。

**改动**：
1. **`_try_extend_existing_pattern`**（pattern_formation.py:2660+）：
   - 用 `_evaluate_pattern_pre_condition` 替代 jaccard overlap
   - 通过条件：通道 Q + 通道 S 都中 + score_pair ≥ 阈值
2. **`_merge_overlapping_same_root_patterns`**（evolution.py 中）：
   - 合并条件改为：两 pattern 的 `pre_question_signature + pre_sql_signature + repair_direction` 在 LLM 判断下"覆盖关系"或"等价"
   - 增加一次 LLM 调用 `pattern_equivalence_judge`（输入两 pattern 的 4 字段，输出 equivalent / subsumed / disjoint）
3. **promotion replay 加 self-recall 验证**：
   - `promotion.py::_apply_branch_runtime_decision` 增加：用 pattern 自己的 pre_*_signature 在自己的 support_cases 上跑 _evaluate_pattern_pre_condition，self_recall ≥ 0.8 才升 runtime_usable
   - 否则 pattern 进 audit-only 状态（已有 promotion_state 字段）
4. **dynamic vocabulary（轻量）**：
   - admission 时 LLM 在 `pre_*_signature` 中使用的关键短语收集到 `library.signature_phrase_catalog`（dict[phrase → first_seen_pattern_id, support_pattern_count]）
   - 仅作 audit / 后续诊断用，不参与 trigger 判断
   - 这是为后续观察"哪些短语在涌现"准备的，不立即发挥作用

**验收**：
- r19 库的 5 个 pattern 跑 self-recall：每个 pattern 的 pre-condition 在自己的 support_cases 上至少 80% 命中
- r24 focus18 中至少 1 个 pattern 因 self-recall 不达标进入 audit-only 状态（说明 gate 在工作）
- `library.signature_phrase_catalog` 在 r24 之后 size ≥ 10
- focus18 r24 触发数 ≥ r23

**回滚**：`git revert <WU9 commit>`。self-recall 阈值（0.8）作为常量定义在代码中，调整需要新 commit；不在线上做参数化（避免阈值漂移导致行为不稳定）。

**工时**：1.5d

**依赖**：WU3 + WU4 + WU8 完成

---

## 4. 阶段间核查（每阶段必跑）

每个 WU 完成后强制 5 项检查再进下一 WU：

1. **测试基线**：`pytest method/EEA/rulebook/tests/` 失败集 ⊆ 当前 19 项 pre-existing
2. **focus18 触发率**：r{n} ≥ r{n-1}（不退化），目标随 WU 推进逐步提升至 9/18+
3. **pattern 路径占比**：r24 末 pattern 路径触发数 > singleton 路径触发数（满足设计原则）
4. **r19 历史 library 兼容**：r19 final_library.json 加载后 5 pattern 全部可识别历史 cases（self-recall ≥ 0.8）
5. **总耗时**：focus18 + skip-final-freeze ≤ 35 min（基线 1.5h，WU0 后稳定）

---

## 5. 验收边界总表

按 case_audit + db_pattern_discussion 中代表性 pattern，每个 WU 阶段结束应能识别：

| Pattern（来自人工标注）| 期望 r24 行为 |
|---|---|
| toxicology pattern 1（bond → 分子级共现，q198/201/207/263/269/306/326/328/335/338）| 全部 10 case 触发 ≥ 8 个 |
| toxicology pattern 2（双列 → 单列，q206/249/253/268/277/285/302/307）| 全部 8 case 触发 ≥ 7 个（r19 已达 7/8）|
| codebase_community pattern 1（editor → owner，q581/582）| 2 case 全部触发（依赖 WU1 schema 角色标注质量）|
| student_club pattern 1（event category → budget category，q1418/1422）| 2 case 全部触发 |
| debit_card pattern 1（年度汇总，q1472/1475）| 2 case 全部触发 |
| european_football_2 pattern 2（player 唯一粒度，q1040/1052/1064/1087）| 4 case 触发 ≥ 3 个；branch 选择正确（计数题→COUNT(DISTINCT)；排行题→GROUP BY）|
| thrombosis pattern 3（laboratory 记录粒度，q1205/1213/1217）| 3 case 全部触发 |

跨 pattern 总体：
- **focus18 r24 final_correct ≥ 10/18**（基线 8/18，目标 +2）
- **0 regression**（不出新 no_match）
- **pattern_extension_count > 0**（在线扩入机制工作）

---

## 6. 风险与回滚

### 6.1 关键风险

| 风险 | 触发信号 | 应对 |
|---|---|---|
| schema 角色标注质量差 | WU1 后 `pred.contains_column_role={role}` 中 `unknown` 占比 > 50% | 加大 LLM annotator prompt 中 sample row 数量；引入用户手动 hint 入口 |
| 2 通道 LLM 调用成本失控 | r24 单 case 平均 LLM 调用 > 15 次 | 加强 retrieval 粗筛（候选 N 从 5 → 3）；提高缓存命中率 |
| WU4 后 pattern 触发率不升反降 | r20 触发数 < r19 的 8/18 | 检查通道 Q/S 的 prompt 措辞；考虑加入 question 关键短语作为辅助通道（不强制）|
| WU6 删 _column_role 后 schema hint 不全导致大量 unknown | r22 触发数显著下降 | 暂停删除，先把 WU1 annotator 跑遍所有库 |
| 反直觉 pattern（如 editor→owner）触发不准 | WU4 r20 codebase_community pattern 1 触发 < 2/2 | 在 admission 输出加 `counter_intuitive: bool`；通道 Q prompt 加 "judge similarity, not correctness" |

### 6.2 回滚机制（git 颗粒回滚）

不在代码中保留 env / config flag 双轨。理由：
- env flag 双轨会让后续 WU 清理无法落地（旧路径持续被引用）
- 测试维度爆炸（每个开关一组 case）
- 新人不易判断哪条路径在线上生效
- git 已经提供原子化回滚

回滚操作分三种粒度：

**单 WU 回滚**：
```bash
git revert <WU{n} commit>           # 单个 WU 出问题
```
适用于：WU 完成后 r{n} 验证退化但只与该 WU 相关；revert 后系统自动回到上一 WU 完成态。

**多 WU 回滚（依赖链一并撤）**：
```bash
git revert <WU{n} commit>..<WU{m} commit>   # 撤销一段连续 commits
```
适用于：发现某早期 WU 设计错误，依赖其的后续 WU 也需要撤。常见情况：WU4（runtime 切换）出错，WU5-9 依赖 WU4，需一并撤。

**紧急完整回滚**：
```bash
git reset --hard <WU0 之前的 commit>          # 回到改造起点
```
适用于：连续多次验证退化、设计需重大调整。要求 WU0 之前打 tag 标记起点。

**对照实验**：用 worktree 跑旧 commit
```bash
git worktree add ../EEA-baseline <baseline_commit>
cd ../EEA-baseline && python method/EEA/rulebook/cli/run_online_e2e_validation_v2.py ...
```

### 6.3 完整回滚路径

最坏情况：r24 触发数显著下降且诊断无解 →
1. `git tag rollback-checkpoint-r24` 标记当前状态便于事后分析
2. `git reset --hard pre-WU0-baseline`（前提：开始前打过 tag）
3. 重新评估设计

WU 起点 tag 约定：
- 改造开始前：`git tag pre-emergence-refactor`
- 阶段 A 完成：`git tag stage-a-complete`
- 阶段 B 完成：`git tag stage-b-complete`
- 以此类推到 stage-e

### 6.4 跨阶段验证保护

每个阶段（A/B/C/D/E）完成时：
1. 跑 §4 阶段间核查 5 项
2. 通过则 `git tag stage-{x}-complete`
3. 在该 tag 后入主分支 `main`（如有）
4. 后续 WU 在 `eea-repair-interface-v1` 上继续

如某 WU 验证通过但下一阶段发现回归：
- 优先 revert 单 commit
- 不要 reset 到 stage tag（会丢失中间已通过的 WU）

---

## 7. 工时与时间表

| WU | 工时 | 累计 | 依赖 |
|---|---|---|---|
| WU0 | 0.5d | 0.5d | - |
| WU1 | 1d | 1.5d | - |
| WU2 | 1d | 2.5d | WU1 |
| WU3 | 1.5d | 4d | WU2 |
| WU4 | 2d | 6d | WU3 |
| WU5 | 0.5d | 6.5d | WU4 |
| WU6 | 1d | 7.5d | WU1, WU5 |
| WU7 | 0.5d | 8d | WU4 |
| WU8 | 0.5d | 8.5d | WU4 |
| WU9 | 1.5d | 10d | WU3, WU4, WU8 |

总计 **10 人天**。

阶段化交付：
- **阶段 A（D1-D1.5，WU0+WU1）**：基础设施就绪，可跑 r20 验证脚手架
- **阶段 B（D2.5-D4，WU2+WU3）**：抽取与演化的涌现化字段就绪，但 runtime 仍走旧路径
- **阶段 C（D4-D6，WU4）**：runtime 切换到 2 通道，**首次端到端验证**（r20）
- **阶段 D（D6.5-D8.5，WU5+WU6+WU7+WU8）**：清理旧机制
- **阶段 E（D8.5-D10，WU9）**：演化对接收口

---

## 8. 关键设计决策记录（决策依据）

### 8.1 为什么是 2 通道而不是 3 通道

之前提案的"通道 3 (misalignment)"是事后审计语言（misread / 不对齐），要求 runtime 在 answer-blind 下做对错判断 —— 概念错误。修正为：
- **触发 = 相似性预测**（看 case 特征是否落进 pattern 描述区域）
- **错误判断在 accumulate 完成**（gold-aware 写 pre-condition）
- **runtime 只匹配，不评判**

通道 Q + 通道 S 已编码足够信息：当 question 类型 + pred_sql 形态都符合时，"这种组合下历史犯错"已在 pre-condition 中固化，runtime 不需要再判断 misalignment。

### 8.2 为什么 singleton 与 pattern 共用机制

- 之前 singleton 走严格子集（`pred.X=Y` 集合包含），pattern 走 jaccard + 严格子集双轨 —— 两套机制
- 修正：两者共用 2 通道 LLM 触发，区别仅在描述粒度
- 单 case singleton 的 pre-condition 描述具体 → 命中率自然低
- 多 case pattern 的 pre-condition 描述抽象 → 命中率自然高
- 不需要手动调阈值

### 8.3 为什么 family 不重新引入

历史决策已确认：family 收益不稳定。本计划坚持：
- 不可稳定实例化的 case 留 singleton
- 不可稳定 pattern 化的多 case 留 singleton 集合（不强行归类）
- repair_direction 必须 actionable（branch + binder 可实例化），否则 admission 拒绝 → 不变成 family

### 8.4 为什么保留 `effect_axis` 12 闭值

- 已验证：在 retrieval bucket 与 pair scoring 阶段使用，不到根因粒度，是抽象坐标
- 不进 runtime trigger（runtime 走 pre-condition LLM 通道）
- 不影响涌现机制

### 8.5 为什么撤 `_column_role` 必须配套 WU1

`_column_role` 启发式撤掉后，`pred.contains_column_role={role}` 派生信号会全部落 `unknown`，role_graph 拓扑信息塌缩。必须有替代角色标注路径（WU1 LLM annotator + cache）。

---

## 9. 与现有文档的关系

- `current_pipeline.md` §11 列出的 20 个问题在本计划中的对应：
  - 问题 1.1 / 1.2（_column_role 启发式与重复）→ WU6
  - 问题 2.1（14 phenomenon 闭词）→ WU5
  - 问题 2.2（trigger_contract 单 case 投影）→ WU7
  - 问题 2.3（question 5 类 enum）→ WU2 + WU4（pre_question_signature 取代）
  - 问题 2.4（_core_pred_contract_signals locus 硬规则）→ WU6 软化 R1
  - 问题 2.5（_is_substantive 黑名单）→ WU6
  - 问题 2.6（_bucket_count 桶化）→ WU6
  - 问题 3.1（ContrastiveRepairEffect 不进 runtime）→ 不在本计划范围（独立大改）
  - 问题 3.2（repair_insight_signature 不进 trigger）→ WU2 + WU4 间接解决（pre-condition 接通）
  - 问题 3.3（R1 / R6 强制规则）→ WU6
  - 问题 4.1（SELECT_ENFORCE_DISTINCT 隐式）→ WU5 撤 14 闭词后需要替代触发条件，由 pre_sql_signature 自然描述
  - 问题 4.2（memory_mentioned_options 中文启发式）→ 暂不动，留作后续
  - 问题 5.1（三套信号体系不一致）→ WU4 + WU7 后简化为：pre-condition 通道（语义层）+ 原子事实（结构层）
  - 问题 5.2（final freeze 性价比）→ WU0
  - 问题 5.3（lightweight 双轨）→ WU9 self-recall 收紧
  - 问题 5.4（_QUESTION_CORE_TAGS 闭集）→ WU2 + WU4 间接解决
  - 问题 5.5（hint_instantiation 双轨）→ 不在本计划范围
  - 问题 5.6（dynamic vocabulary 缺失）→ WU9 轻量 catalog
  - 问题 5.7（信号冗余）→ WU4 后 pre-condition 通道吸收
  - 问题 5.8（信号字符串化）→ WU6 撤桶化部分缓解

- `experiment_log.md` 历次决策的兼容：
  - "family 已禁用" — 本计划坚持
  - "branch-level runtime trigger" — 本计划保留 branch matching
  - "rewrite_contract + LLM 不再补 SQL" — 本计划不影响 rewrite 阶段
  - "online local-evolve 用 lightweight 路径" — WU9 加 self-recall gate 收紧

---

## 10. 维护策略

### 10.1 改造开始前

```bash
git tag pre-emergence-refactor      # 起点 tag，紧急回滚目标
```

在 `experiment_log.md` 追加一条 entry，标记本计划开始执行。

### 10.2 每完成一个 WU

1. **commit 原子化**：单个 WU 一个 commit（即使涉及多个文件）；commit message 含验收结果与 focus18 数据
2. **更新本文档**：在对应 WU 段末尾追加 `**完成 commit**: <hash>` 一行
3. **更新 experiment_log.md**：一条 entry，含改动概要 + 验收数据 + 与上 WU 的差异
4. **如验收未达预期**：决定是修复还是 revert，无论选哪条都要在 log 中记录原因

### 10.3 每完成一个阶段（A/B/C/D/E）

1. 跑 §4 的 5 项强制检查
2. 全部通过：`git tag stage-{a|b|c|d|e}-complete`
3. 更新本文档对应阶段段头标注 tag

### 10.4 整个计划完成后

1. 更新 `current_pipeline.md` 反映新机制（重写 §3 的 5 层 gate 段、§7.7 trigger_contract 段、新增 §X 2 通道触发节）
2. 标注 §11 问题清单状态（已解 / 部分解 / 仍存）
3. 在 `CLAUDE.md` 中更新涉及的设计描述（特别是"两条硬边界"以下关于 trigger 与 bias_recognition 的段）
4. `git tag emergence-refactor-complete` 标记完成
