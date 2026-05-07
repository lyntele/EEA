# EEA v2 当前实现阅读后的修改建议报告

## 0. 总体判断

我重新读了 `rulebook/doc/current_implementation_overview.md` 和当前 `rulebook/common/*_v2.py` 主体实现后，结论和我前一版相比需要更精确：

> 当前 EEA v2 的问题不是“没有动作空间”，也不是“需要回退到 repair-path clustering”。
> 当前系统已经有两层动作空间、canonical repair IR、shared program synthesis、compiler coverage、runtime trigger、replay-gated promotion。真正的问题是：**这些层之间的语义桥还不够强，导致历史 repair evidence 没有稳定变成 runtime-usable canonical program。**

当前总览文档已经明确了完整 v2/post-selection 流程：DeepEye 先生成、修订、选择 `S0`；EEA runtime 只看 `question / evidence / S0 / schema / candidate context / memory`，不看 gold；命中 memory 后实例化 action 和 hint；DeepEye rewrite `S0` 得到 `S1`；case 结束后，错例才进入 offline update、singleton、local evolution 和 final replay-gated evolution。

所以修改方向不应是：

```text
重写架构
引入 case similarity 权重
按人工 pattern 写规则
回退到 repair_signature 聚类
把人工标注导入 discovery / runtime
```

而应是：

```text
沿当前 v2 实现补强：

1. CanonicalRepairIR 的 operation_signature 信息密度
2. CanonicalRepairProgram 的 program_envelope
3. SharedProgramSynthesizer 的 invariant/envelope synthesis fallback
4. ActionCompiler 对 10 个 ActionPrimitive 的完整 lowering
5. Runtime trigger contract 与 synthesized_program 的一致性
6. ProgramCoverage 中 static coverage 与 runtime binding coverage 的拆分
7. Prompt 从“让 LLM 判断系统状态”改成“让 LLM 只产出有限证据 / 有界选择”
```

---

# 1. 当前实现主线对齐

## 1.1 Runtime 路径

`common/runtime_v2.py` 是当前 answer-blind runtime 主入口，职责包括：

```text
build RuntimeCaseView
build current_signals
traverse LibraryStateV2 memory objects
apply trigger contracts
run ActionCompiler
build rewrite guard / response
```

总览文档也明确指出 runtime 不看 gold，gold / execution feedback 只用于 update/evolution。
`runtime_v2.py` 的文件说明也强调：runtime chain 是 “answer-blind library retrieval → Compiler → Rewrite”，并且 runtime 函数绝不接收 `gold_sql`。

这一点是正确的，不应改变。

---

## 1.2 Update / Accumulate 路径

`common/accumulate_v2.py` 会把 wrong case 通过：

```text
run_error_instance_pipeline
  -> ErrorInstanceV2
  -> attach_canonical_repair_ir
  -> singleton_program_from_ir
  -> coverage_for_singleton_program
  -> build_trigger_contract
  -> ensure_materialized_trigger_contract
  -> singleton GroupSummary
```

也就是说，现在错例不是只被压成自然语言经验，而是已经产生 `CanonicalRepairIR` 和 singleton-level `CanonicalRepairProgram`。

所以，不需要“新建 IR 层”。真正要改的是：**当前 IR 里的 evidence 是否足够支撑跨 case anti-unification，以及后续 compiler 是否能消费这些 evidence。**

---

## 1.3 两层动作空间已经存在

你补充的是准确的。

### 离线层：`CanonicalRepairOp`

`CanonicalRepairOp` 定义在 `data_structures_v2.py`，字段包括：

```text
op_id
op_type
locus
role_refs
arguments
invariants
source_step_ids
supporting_case_ids
confidence
```

它属于 `CanonicalRepairIR.program_ops`，用于记录从单个 audited case 中抽出的 canonical repair operation。

当前 `shared_program_synthesizer_v2.py` 支持的 canonical op 包括：

```text
SELECT_ADD_SLOT
ADD_SELECT_SLOT
SELECT_OUTPUT_PATCH
SELECT_REPLACE_SLOT
REPLACE_SELECT_SLOT
SELECT_DROP_SLOT
DROP_SELECT_SLOT
JOIN_ADD_BRIDGE
JOIN_ADD_TABLE
BRIDGE_ADD_TABLE
WHERE_DROP_CONDITION
WHERE_REPLACE_CONDITION
```

并将其映射到 lowering family：

```text
select_output_patch
select_add
select_replace
select_drop
join_bridge
where_side_edit
```



### 在线层：`ActionPrimitive`

`vocabulary_v2.py` 中的 runtime `ActionPrimitive` 有 10 个：

```text
ADD_SELECT_SLOT
REPLACE_SELECT_SLOT
DROP_SELECT_SLOT
REROUTE_FACT
INSERT_BRIDGE
CHANGE_GRAIN
MOVE_CONDITION
DROP_SIDE
SWITCH_CANONICAL_FIELD
MATERIALIZE_RANKING_OUTPUT
```

其中 `action_compiler_v2.py` 明确说明目前完整实现的是 6 个：

```text
ADD_SELECT_SLOT
REPLACE_SELECT_SLOT
DROP_SELECT_SLOT
DROP_SIDE
INSERT_BRIDGE
REROUTE_FACT
```

另外 4 个仍是 placeholder：

```text
CHANGE_GRAIN
MOVE_CONDITION
SWITCH_CANONICAL_FIELD
MATERIALIZE_RANKING_OUTPUT
```

 

这说明当前系统已经有 action inventory；接下来的重点是**补齐离线 canonical op 到在线 primitive 的 lowering 桥**。

---

# 2. 当前问题诊断

## 2.1 `CanonicalRepairOp` 仍偏 SQL 区域动作，缺少 program envelope

当前离线 op 的命名是：

```text
SELECT_DROP_SLOT
JOIN_ADD_BRIDGE
WHERE_REPLACE_CONDITION
```

这很适合底层 edit DSL，但它只说明“这个 case 在哪个 SQL 区域做了什么结构动作”。它还没有充分说明：

```text
这个动作背后的 repair interface 是什么？
source anti-pattern 是什么？
target invariant 是什么？
当前 case 下如何选择 lowering branch？
```

例如，同样是 `SELECT_DROP_SLOT`，可能对应：

```text
输出多了一列
pair output collapse
benchmark output subset
错误 display slot
```

这些不能只靠 `select_drop` 区分。

再例如，同样是 `JOIN_ADD_BRIDGE`，可能对应：

```text
补 side table
补 parent route
补 role anchor path
从 local endpoint route 到 higher-scope relation
```

所以当前需要的不是新动作，而是给 `CanonicalRepairProgram` 加一层：

```text
program_envelope
```

用于解释 `CanonicalRepairOp` 组合背后的可复用 repair interface。

---

## 2.2 `SharedProgramSynthesizer` 仍偏 common op bucket

`shared_program_synthesizer_v2.py` 现在会从 singleton evidence 合成 shared canonical repair program，并且已经使用 role deltas 和 invariants 做 anti-unification。它不是旧式表面聚类。

但当前主路径仍是：

```text
按 lowering_family + locus 分 bucket
找 common_bucket_keys
再比较 slot_signature / role_delta / common_invariants
```

这对 `SELECT_DROP_SLOT` 这种同 op family 的 pattern 很有用；但对“同 target invariant，不同 concrete op”的经验不够。

典型情况是：

```text
case A: JOIN_ADD_BRIDGE
case B: REROUTE_FACT
case C: CHANGE_GRAIN
case D: SELECT_OUTPUT_PATCH
```

它们可能共享同一个 repair interface：

```text
source path 停在局部 relation；
target 需要通过 higher-scope relation / target equality relation 展开。
```

但当前 common bucket synthesis 很容易失败。

因此需要第二条 synthesis path：

```text
common op bucket synthesis
  -> fail
invariant / envelope synthesis fallback
```

这个 fallback 仍然完全来自 code-derived `CanonicalRepairIR`，不是人工 pattern。

---

## 2.3 `RepairProgramNormalizer` 已经抽了很多证据，但没有被充分利用

`repair_program_normalizer_v2.py` 已经做了非常重要的事情：

```text
把 SELECT_REPLACE + arity decrease + target subset 规范化为 SELECT_DROP_SLOT
抽 source/target output refs
抽 target_relation_equality / target_added_relation_equality
抽 DISTINCT / ORDER / LIMIT / predicate accessory policy
```

并且它的 docstring 明确说：executable op 必须来自 audited case 的 extracted `repair_program`；role graphs、SQL deltas、invariants 是 anti-unification evidence，不能被转成预定义错误类型。

这正是正确方向。

现在需要加强的是：把这些 evidence 结构化进 `operation_signature`，让 synthesizer 和 compiler 都能消费。

---

## 2.4 `ActionCompiler` 的 4 个 placeholder 会系统性挡住高价值经验

当前完整实现的 6 个 primitive 更偏：

```text
SELECT add/replace/drop
DROP_SIDE
INSERT_BRIDGE
REROUTE_FACT
```

但人工标注和 toxicology strong pattern 暴露的很多修复需要：

```text
MOVE_CONDITION
CHANGE_GRAIN
SWITCH_CANONICAL_FIELD
MATERIALIZE_RANKING_OUTPUT
```

这 4 个当前仍是 placeholder。

它们不是边缘能力：

```text
MOVE_CONDITION:
  WHERE predicate -> CASE numerator / denominator scope

CHANGE_GRAIN:
  count anchor / distinct anchor / entity-vs-row-vs-pair grain

SWITCH_CANONICAL_FIELD:
  canonical slot / display slot / field convention

MATERIALIZE_RANKING_OUTPUT:
  rank / metric / top-k / ordering contract
```

如果不实现它们，系统会天然偏向只修 SELECT/JOIN 局部问题，而大量人工标注中的 family/formal pattern 无法 runtime 生效。

---

## 2.5 `ProgramCoverage` 目前需要显式拆 static 与 runtime binding

`program_coverage_v2.py` 中有两层：

```text
validate_program:
  静态检查 program.ops / lowering family / shape direction / invariants

validate_runtime_bindings:
  真实调用 enumerate_candidates，检查每个 member 是否能生成 compiler candidates
```

第二层才是真正接近 runtime 可用性的验证。

当前如果报告里只给 `compile_coverage`，容易混淆：

```text
static coverage 过了，但 runtime binding 失败；
或者 static blockers 低，但 candidate enumeration 失败。
```

需要拆成：

```text
static_program_coverage
runtime_binding_coverage
member_candidate_coverage
```

---

## 2.6 Family formation 已经 program-first，但 pair score 仍需降级为 audit/retrieval

`family_formation_v2.py` 当前已经调用 `synthesize_shared_program([left, right])`，并把 `program_compatible` 作为 accepted 的核心条件。

这很好。

但代码中仍存在：

```text
question_overlap
manifest_overlap
structural_compat
slot_overlap
signal_axes_overlap
shape_compat
legacy_compat
score >= 0.58
```

这些 score 不应再被解释成“case similarity 决定 family”。应明确：

```text
pair score 只用于 neighbor retrieval / audit ranking；
accepted family 必须 program-backed 或 offline-only shared-intent；
runtime usable 必须 replay / compiler gate。
```

---

## 2.7 Runtime contract 已经修了一部分，但还没和 program envelope 完全绑定

`trigger_contract_v2.py` 已经实现：

```text
is_contract_runtime_executable
sanitize_trigger_contract
ensure_materialized_trigger_contract
materialize_contract_from_legacy_signature
```

并且已经防止 `pred.output_grain=pair_rows` 这类 source-side pair signal 被错误放入 output-decrease negative signals。

这是正确方向。

下一步应该让 `trigger_contract.action_contract` 直接消费：

```text
program_envelope.source_antipatterns
program_envelope.required_role_slots
program_envelope.branch_selection_contract
program_envelope.negative_guards
```

而不是只依赖 op lowering family / representative source signals。

---

# 3. 具体修改建议

## 3.1 `data_structures_v2.py`：给 `CanonicalRepairProgram` 加 `program_envelope`

建议新增：

```python
class ProgramEnvelope(BaseModel):
    schema_version: str = "program-envelope-v0"

    source_antipatterns: List[Dict[str, Any]] = Field(default_factory=list)
    target_invariants: List[Dict[str, Any]] = Field(default_factory=list)

    action_envelope: Dict[str, Any] = Field(default_factory=dict)
    lowering_branches: List[Dict[str, Any]] = Field(default_factory=list)
    branch_selection_contract: Dict[str, Any] = Field(default_factory=dict)

    required_role_slots: List[Dict[str, Any]] = Field(default_factory=list)
    negative_guards: List[Dict[str, Any]] = Field(default_factory=list)

    unresolved_variation_axes: List[str] = Field(default_factory=list)
```

并在 `CanonicalRepairProgram` 里加：

```python
program_envelope: Optional[ProgramEnvelope] = None
```

### 作用

`ops` 仍然是 canonical edit ops；`program_envelope` 说明这些 ops 背后的共享 repair interface。

例如不写：

```text
toxicology Pattern C
```

而写：

```json
{
  "source_antipatterns": [
    {
      "kind": "multi_role_output",
      "conditions": [
        "same_relation_multiple_role_sides",
        "same_attribute_projected_from_multiple_role_paths"
      ]
    }
  ],
  "target_invariants": [
    {
      "kind": "target_output_subset_of_source"
    }
  ],
  "action_envelope": {
    "allowed_primitives": ["DROP_SELECT_SLOT", "DROP_SIDE"],
    "must_preserve_scopes": ["WHERE"]
  }
}
```

这是 generic 的，不含人工 pattern。

---

## 3.2 `repair_program_normalizer_v2.py`：扩展 `operation_signature`

当前 `_operation_signature` 应从 output role delta 扩成四类 delta。

### A. output path delta

```json
"output_path_delta": {
  "source_output_path_roles": [],
  "target_output_path_roles": [],
  "kept_output_path_roles": [],
  "dropped_output_path_roles": [],
  "target_output_subset_of_source": true,
  "same_table_multi_role_output": true,
  "same_attribute_multi_role_output": true
}
```

### B. relation delta

```json
"relation_delta": {
  "source_relation_equalities": [],
  "target_relation_equalities": [],
  "added_relation_equalities": [],
  "removed_relation_equalities": [],
  "target_relation_role_equalities": []
}
```

当前 `_relation_invariants` 已经能抽 relation equality，应该结构化到这里。

### C. predicate scope delta

```json
"predicate_scope_delta": {
  "moved_predicates": [],
  "source_scope": "WHERE",
  "target_scope": "CASE|HAVING|WHERE|NONE",
  "predicate_signature": {}
}
```

### D. grain delta

```json
"grain_delta": {
  "source_grain": null,
  "target_grain": null,
  "source_count_anchor": null,
  "target_count_anchor": null,
  "aggregate_kernel_changed": false
}
```

这些字段不需要人工标注，来自 pred/gold AST、role graph、delta signature。

---

## 3.3 `shared_program_synthesizer_v2.py`：增加 invariant-based synthesis fallback

当前 `synthesize` 保留，但新增：

```python
def _synthesize_by_invariant_envelope(groups):
    ...
```

触发条件：

```text
common op bucket synthesis 失败
但成员之间存在 shared target invariant / shared source anti-pattern
```

候选 shared evidence 包括：

```text
target_output_subset_of_source_outputs
same_attribute_multi_role_output
target_added_relation_equality
target_relation_role_equality
predicate_scope_delta
grain_delta
```

输出：

```text
CanonicalRepairProgram
  ops = branch-level generalized ops
  program_envelope = synthesized envelope
  unresolved_variation_axes = branch / grain / predicate / relation axes
```

### 关键 gate

如果 lowering branch 可以由当前 runtime-visible case 决定：

```text
branch_selection_contract.answer_blind = true
```

才允许进入 runtime family / pattern gate。

如果 branch selection 需要 gold：

```text
runtime_usable=false
offline-only family
```

---

## 3.4 `action_compiler_v2.py`：补齐四个 placeholder

### 第一优先级：`MOVE_CONDITION`

输入 evidence：

```text
WHERE_DROP_CONDITION
WHERE_REPLACE_CONDITION
predicate_scope_delta
```

输出：

```json
{
  "primitive": "MOVE_CONDITION",
  "arguments": {
    "predicate_ref": "...",
    "from_scope": "WHERE",
    "to_scope": "CASE_NUMERATOR",
    "target_aggregate_expr": "...",
    "preserve_denominator_scope": true
  }
}
```

用途：条件聚合、比例、分子/分母、WHERE -> CASE。

---

### 第二优先级：`CHANGE_GRAIN`

输入 evidence：

```text
grain_delta
output_shape_delta
aggregation profile
```

输出：

```json
{
  "primitive": "CHANGE_GRAIN",
  "arguments": {
    "source_grain": "...",
    "target_grain": "...",
    "source_anchor": "...",
    "target_anchor": "...",
    "aggregate_rewrite": "COUNT_TO_COUNT_DISTINCT | COUNT_DISTINCT_TO_COUNT | GROUP_KEY_CHANGE"
  }
}
```

用途：count anchor、entity/row/pair grain、aggregation unit。

---

### 第三优先级：`SWITCH_CANONICAL_FIELD`

输入 evidence：

```text
SELECT_REPLACE_SLOT
target_output_refs
column role hints
display / canonical slot delta
```

输出：

```json
{
  "primitive": "SWITCH_CANONICAL_FIELD",
  "arguments": {
    "current_expr": "...",
    "target_expr": "...",
    "preserve_join_path": true
  }
}
```

用途：canonical slot、display slot、field convention。

---

### 第四优先级：`MATERIALIZE_RANKING_OUTPUT`

输入 evidence：

```text
target ORDER BY / LIMIT accessory policy
output_shape_delta
rank / metric output contract
```

输出：

```json
{
  "primitive": "MATERIALIZE_RANKING_OUTPUT",
  "arguments": {
    "ranking_expr": "...",
    "metric_expr": "...",
    "window_fn": "RANK|ROW_NUMBER",
    "tie_policy": "preserve_ties|single_top"
  }
}
```

---

## 3.5 `program_coverage_v2.py`：拆分 coverage

建议 `ProgramCoverage` 增加：

```python
static_program_coverage: float
runtime_binding_coverage: float
member_candidate_coverage: float
static_blockers: List[str]
runtime_binding_blockers: Dict[str, str]
```

如果暂时不改 schema，也至少在 report 中拆：

```json
{
  "static_coverage": {
    "coverage": 1.0,
    "blockers": []
  },
  "runtime_binding_coverage": {
    "coverage": 0.5,
    "failed_members": ["206"],
    "failure_reasons": {
      "206": "role_slot_unbound:target_output_subset_slot_binding_unresolved"
    }
  }
}
```

promotion gate 应优先看 runtime binding coverage，而不是 static lowering family coverage。

---

## 3.6 `runtime_v2.py`：trigger 变成 applicability-first

当前 runtime 已经有 `build_current_case_signals` 和 binder dry-run。建议把 gate 语义写成三层：

```text
1. Contract executable:
   trigger_contract 是否非空、runtime-visible、含 program

2. Applicability:
   当前 S0 是否满足 program_envelope.source_antipatterns
   required_role_slots 是否可绑定
   negative_guards 是否未命中

3. Compiler dry-run:
   enumerate_candidates 是否有候选
   candidate op ids 是否覆盖 required ops
   action_count 是否 <= budget
```

最终：

```text
ready = executable_contract && applicability_pass && binder_dry_run_pass
```

这仍然是纯合同 / 硬门，不引入 case similarity 权重。

---

## 3.7 `family_formation_v2.py`：pair score 降级为候选召回 / audit

建议将 `PairScore.accepted` 的语义改名或至少在 report 中分开：

```text
pair_score_candidate = true/false
program_compatible = true/false
family_accepted = true/false
acceptance_basis = program_backed | offline_only | rejected
```

并明确：

```text
score >= threshold 不能独立形成 runtime family；
representative 不能进入 hard trigger；
runtime-facing fields 必须来自 synthesized_program / program_envelope。
```

---

## 3.8 `runtime conflict`：先 canonical merge，再判 conflict

对于多个 memory 同时通过 gate：

```text
surface actions
  -> canonicalize action envelope
  -> merge compatible envelopes
  -> reject true conflicts only
```

兼容条件：

```text
same primitive family
same source role slots
same target invariant
same keep/drop role side
same preserve scopes
```

真冲突：

```text
opposite keep/drop side
one collapse pair, one requires pair
one broaden scope, one preserve local scope
```

这可以解决 toxicology report 中 `253 / 277 / 285` 相关候选接近但被 `conflicting_action_contracts` 拦掉的问题。

---

# 4. Prompt 评价与修改建议

当前 prompt 体系在总览中列为：

```text
wrong_case_auditor.py
error_instance_extractor.py
action_compiler.py
memory_rewrite.py
hint_instantiation.py
compatibility_judge.py
```



我的总体评价是：

> 这些 prompt 的安全边界写得很认真，尤其是 answer-blind、strict JSON、不能发明 schema、不能使用 gold 等规则。
> 但它们也有一个共同问题：**有些 prompt 仍然让 LLM 判断系统级状态或抽取过多结构化程序信息，而这些应该尽量由代码 / compiler / replay 裁决。**

下面逐个说。

---

## 4.1 `wrong_case_auditor.py`

### 当前优点

这个 prompt 的目标清晰：offline 节点，允许看 gold，要求找最小可验证修复，不做抽象标签；要求 `minimal_fix` 不是整条 rewrite；要求 `error_locus_hint` 从枚举中选；还加入了 R-equivalence 规则，避免在执行结果等价时把主错误判到 predicate / aggregation。

这是好的。

### 当前问题

#### 问题 A：`validated_sql` 要求过强

Prompt 要求：

```text
If possible, emit validated_sql — it must execute and match gold.
```

但 LLM 本身不能执行 SQL。它只能提出 candidate SQL。真正验证应由 code side 做。

如果 prompt 让 LLM 以为自己必须“确认执行匹配”，可能导致两类坏行为：

```text
1. LLM 过度自信输出 validated_sql
2. LLM 为了保证匹配而写成大范围 gold-like rewrite
```

#### 问题 B：minimal_fix 没有结构化 patch ops

它输出：

```text
final_error_reason
minimal_fix
validated_sql
error_locus_hint
confidence
```

但对后续 `CanonicalRepairIR` 来说，更有用的是：

```text
changed_clause
source_expr
target_expr
preserve_clause
primary_vs_secondary
```

现在这些都藏在自然语言 `minimal_fix` 里，给 extractor 增加负担。

### 建议修改

把 `validated_sql` 改名为：

```text
candidate_fix_sql
```

并明确：

```text
You cannot verify execution yourself. Emit candidate_fix_sql only if it is the direct application of minimal_fix. Code will validate it.
```

增加结构化字段：

```json
{
  "minimal_patch_ops": [
    {
      "clause": "SELECT|JOIN|WHERE|GROUP_BY|ORDER_BY|AGGREGATION",
      "edit_kind": "add|drop|replace|move|reroute",
      "source_fragment": "...",
      "target_fragment": "...",
      "preserve_fragments": [],
      "primary": true
    }
  ],
  "secondary_differences": []
}
```

这样后续 extractor 不必完全从自然语言里二次解释。

---

## 4.2 `error_instance_extractor.py`

### 当前优点

这个 prompt 已经非常完整。它明确：

```text
code 预处理负责 structure_flags / legacy_signature / candidate tags
LLM 负责 deep_bias / repair_goal / decisive tags / repair_skeleton / slots / repair_program / guardrails / hint
repair_program 是 runtime actions 的 source of truth
不能出现 gold / benchmark 字样
dependency step 必须来自 audited diff
```

并且它有一套 R1-R7 coherence rules，特别是 R7：每个 runtime-executable edit 必须出现在 `repair_program`，不能让 compiler 后续自己加 generic repairs。

这些规则是必要的。

### 当前问题

#### 问题 A：一个 prompt 任务过多

它同时让 LLM 做：

```text
decisive tag 判定
deep_bias
repair_goal
repair_skeleton
instantiation_slots
branch_rules
guardrails
repair_program
rewrite_hint_proto
risk_level
```

这会带来字段间不一致。尤其是：

```text
repair_skeleton
repair_program
instantiation_slots
rewrite_hint_proto
```

任何一个方向错，后面 singleton / trigger / compiler 都会被污染。

#### 问题 B：LLM 被要求输出 executable repair_program，但很多 executable evidence 更适合代码抽

例如：

```text
SELECT_DROP_SLOT
JOIN_ADD_BRIDGE
WHERE_REPLACE_CONDITION
target_output_subset_of_source
relation equality
```

这些已经能从 pred/gold AST diff 和 role graph 自动抽。LLM 输出 repair_program 可能和 code delta 冲突。

#### 问题 C：op vocabulary 与当前 supported canonical ops 不完全一致

Prompt 中举例包括：

```text
WHERE_ADD_CONDITION
SET_SELECT_DISTINCT
```

但当前 `COMPILER_SUPPORTED_CANONICAL_OPS` 里没有 `WHERE_ADD_CONDITION`，`SET_SELECT_DISTINCT` 更像 accessory policy。

这会让 LLM 产出后续 synthesizer / compiler 不支持的 step。

### 建议修改

不要彻底拆成多轮 LLM，但应把 prompt 输出分成：

```text
code-owned
llm-hypothesis
compiler-facing
```

具体改法：

#### 1. LLM 不直接决定 canonical op

保留 `repair_program`，但改成：

```text
repair_program_hypothesis
```

真正写入 `CanonicalRepairIR.program_ops` 的 op 由 `repair_program_normalizer_v2.py` 结合 SQL delta 决定。

Prompt 应明确：

```text
Do not force SQL-delta-derived op labels. Describe the repair step; code will canonicalize op_type.
```

#### 2. 新增字段

```json
{
  "source_antipattern_hypothesis": {
    "description": "...",
    "visible_in_pred_sql": ["..."]
  },
  "target_invariant_hypothesis": {
    "description": "...",
    "visible_in_validated_or_gold_sql": ["..."]
  },
  "core_vs_accessory": {
    "core_steps": [],
    "accessory_steps": []
  },
  "uncertain_axes": []
}
```

这会帮助 normalizer / synthesizer，但不会让 LLM 成为最终裁决。

#### 3. 限制 op examples 到当前支持集合

Prompt 中的 op examples 应更新为：

```text
SELECT_ADD_SLOT
SELECT_REPLACE_SLOT
SELECT_DROP_SLOT
SELECT_OUTPUT_PATCH
JOIN_ADD_BRIDGE
JOIN_ADD_TABLE
BRIDGE_ADD_TABLE
WHERE_DROP_CONDITION
WHERE_REPLACE_CONDITION
```

如果要允许新 op，例如 `CONDITION_SCOPE_MOVE` / `GRAIN_CHANGE`，应先在 canonical op vocabulary 和 compiler lowering 中声明。

---

## 4.3 `action_compiler.py`

### 当前优点

这个 prompt 的方向是对的：ActionCompiler 是 strict selector，不允许 LLM 自由发明 target column/table；candidate sets 由代码枚举；LLM 只选择；no out-of-set actions；schema recall miss 不作为 memory demotion。

这正是 EEA runtime 应该采用的模式。

### 当前问题

#### 问题 A：prompt 太长，LLM 容易忽略细节

它包含了大量选择规则、multi-slot candidate 规则、direction 规则、memory_alignment 规则、canonical_contract 规则。所有都对，但放在一个 prompt 中，模型很可能只执行部分。

#### 问题 B：让 LLM 判断的东西仍然偏多

例如：

```text
Direction matters...
If candidates include memory_alignment...
If candidates include canonical_contract...
```

这些很多可以由代码预先转成 candidate-level 字段：

```text
candidate.compatibility = exact|partial|conflict
candidate.direction_consistent = true/false
candidate.canonical_binding_score = ...
candidate.reject_reasons = [...]
```

LLM 不应该自己读复杂字段判断方向，尤其是 `DROP_SIDE` 这种错一边就 regression 的动作。

#### 问题 C：deterministic-only candidate 不应走 LLM

如果某 primitive 的 candidate 唯一、canonical binding 唯一、negative guard clean，那么最好 code 直接 deterministic select，LLM 只处理需要语义选择的情况。

### 建议修改

#### 1. 将 prompt 缩短为 selector matrix

传给 LLM 的 candidate 应该已经由代码标注：

```json
{
  "candidate_id": "...",
  "primitive": "...",
  "compatibility": "exact|partial|conflict",
  "binding_status": "unique|ambiguous|unbound",
  "direction_check": "pass|fail",
  "memory_alignment": "pass|fail",
  "candidate_summary": "...",
  "reject_reasons": []
}
```

Prompt 只要求：

```text
Select candidates with compatibility=exact and no reject_reasons.
If more than one exact candidate exists, choose the one with narrower edit scope.
If no exact candidate exists, emit no action.
```

#### 2. 对 deterministic-only 不调用 LLM

在 `run_action_compiler` 前增加：

```text
if all required canonical ops have exactly one exact candidate:
    emit deterministic_unique action
else:
    call LLM selector
```

#### 3. prompt 中保留硬规则，但减少语义推断

不要让 LLM 解释 `canonical_contract`；让代码把解释压成：

```text
candidate_contract_status
```

---

## 4.4 `memory_rewrite.py`

### 当前优点

这个 prompt 非常强调 bounded autonomy：

```text
每个 action 是 required edit
不能 silent ignore
只能执行 action 和 explicit repair_program steps
不能自行发明 DISTINCT / WHERE / GROUP / literal rewrites
必须填 realization trace
不能看 gold
```

这是正确的。

### 当前问题

#### 问题 A：自然语言 hint 仍然可能扩张动作

Prompt 说：

```text
natural_language_hint can be used to disambiguate action arguments when structured actions are under-specified
```

这有风险。因为如果 action 本身 under-specified，不应该由 raw hint 补；应该回到 compiler 失败，而不是让 rewrite LLM 从 hint 补参。

#### 问题 B：action required edit 可能强迫错误 action

如果 compiler 选错，rewrite prompt 会要求所有 action 必须 realize。这是正确的归因方式，但可能导致 regression。这里关键是 compiler gate，而不是 rewrite prompt 本身。

#### 问题 C：重复编号 E

Prompt 有两个 E：

```text
E. Natural-language repair hint
E. Output
```

小问题，但建议修掉，降低 prompt 噪声。

### 建议修改

#### 1. natural_language_hint 降级

改成：

```text
The natural_language_hint is explanatory only. It must not supply missing arguments. If structured action arguments are insufficient, mark the action unrealized.
```

#### 2. 如果 required action 无法 realize

明确：

```text
If a required action cannot be realized, return the original SQL unchanged, set realized=false for that action, and explain why.
```

这样 guard / replay 更容易归因。

#### 3. 增加 action patch template

对每个 primitive 给一个极短 mapping：

```text
DROP_SELECT_SLOT -> remove listed SELECT expressions only
DROP_SIDE -> remove projection and join side only if arguments specify side
INSERT_BRIDGE -> add listed bridge table/join only
MOVE_CONDITION -> move listed predicate from source scope to target scope
```

不要让 LLM自己泛化 primitive 语义。

---

## 4.5 `hint_instantiation.py`

### 当前优点

它承认了一个真实问题：raw `rewrite_hint_proto` 来自历史 case，可能绑定历史 SQL 形态；因此需要把 raw hint 实例化到当前 case。它要求不改变 repair direction，不引用 gold，且若 raw hint 与 actions 冲突，则 `applicable=false`。

### 当前问题

#### 问题 A：这个节点可能已经不该是核心路径

如果 ActionCompiler 已经生成了具体 action，hint instantiation 不应该再成为影响 rewrite 的核心来源。否则历史 hint 仍可能污染当前 action。

#### 问题 B：raw hint 的 direction preservation 会阻碍 canonical program

如果 raw hint 是 source-specific，例如“drop `a2.element`”，但 canonical action 已经是“drop role-side B”，hint instantiation 可能因为当前 SQL alias 不同而误判 not applicable。

#### 问题 C：它把“hint 是否适用”交给 LLM，但这个应该由 compiler action 是否可绑定决定

只要 action 已经通过 compiler，hint 可以由代码渲染，不需要再让 LLM判断 raw_hint 是否适用。

### 建议修改

#### 1. 将 hint_instantiation 降级为 fallback / human-readable

默认不再使用 raw_hint 作为 rewrite 的主要指导。

改成：

```text
primary hint = code-rendered action brief
secondary hint = instantiated raw_hint if applicable
```

#### 2. 代码生成 action brief

例如：

```text
DROP_SELECT_SLOT:
  Drop SELECT expression `{from_expr}`.

DROP_SIDE:
  Remove the join/output side `{drop_side}` while preserving `{keep_side}`.

MOVE_CONDITION:
  Move predicate `{predicate}` from WHERE into the numerator CASE expression.
```

LLM hint instantiation 只在 action brief 为空或需要润色时调用。

#### 3. 如果保留 prompt

要求：

```text
Do not decide applicability. Applicability is already decided by ActionCompiler.
Only rewrite raw_hint for readability; if raw_hint conflicts with actions, return empty hint.
```

---

## 4.6 `compatibility_judge.py`

### 当前优点

它要求 family formation / promotion 不可只靠表面相似，必须检查 repair skeleton、semantic intent、instantiation shape、guardrail conflict，并支持 leave-one-out。

### 当前问题

这个 prompt 已经与当前 code-first / compiler-first 方向有点冲突。

它输出：

```text
eligible
shared_minimal_repair_interface
shared_instantiation_program
```

这些字段不应该由 LLM 最终判断。当前真正应该决定这些的是：

```text
SharedProgramSynthesizer
CompilerCoverageValidator
Replay
```

如果这个 prompt 的结果参与 gate，就会重新引入“LLM 觉得像不像”的问题。

### 建议修改

#### 1. compatibility_judge 只作为 audit explainer

不要让它输出最终 gate bool。或者保留 bool，但明确：

```text
advisory_only = true
```

#### 2. 输出改成 blockers / variation axes

建议 schema：

```json
{
  "semantic_alignment_notes": "...",
  "possible_shared_interface": "...",
  "variation_axes": [],
  "suspected_blockers": [],
  "recommended_code_checks": [
    "check_common_target_invariant",
    "check_role_slot_alignment",
    "check_compiler_coverage"
  ]
}
```

#### 3. 不要说“same instantiation rule”作为必要条件

因为你的人工作品里很多收益来自：

```text
shared repair interface / target invariant
但 lowering branch 不同
```

应改成：

```text
same program envelope or answer-blind branch selection
```

---

# 5. Prompt 体系的总体修改原则

当前 prompt 共同问题可以概括为：

```text
LLM 仍然承担了太多“系统裁决”职责。
```

应改成：

```text
LLM 只做：
  - offline natural-language evidence interpretation
  - bounded hypothesis generation
  - candidate selection over code-enumerated options
  - SQL rewrite realization under explicit actions

Code / compiler / replay 做：
  - canonical op derivation
  - program synthesis
  - trigger gate
  - runtime applicability
  - promotion decision
  - regression attribution
```

更具体：

```text
WrongCaseAuditor:
  产 audit evidence，不声明已验证执行。

ErrorInstanceExtractor:
  产 semantic hypotheses / slots / guardrails，不做最终 canonical op 裁决。

ActionCompiler:
  只选 code-enumerated candidates；deterministic unique 不走 LLM。

MemoryRewrite:
  只执行 structured actions；hint 不能补缺失参数。

HintInstantiation:
  降级为 readability helper，不参与 applicability。

CompatibilityJudge:
  降级为 audit explainer，不参与 promotion gate。
```

---

# 6. 建议的 Codex 任务文档摘要

可以这样发给 Codex：

```text
Read current v2 implementation. Do not redesign the architecture.

Current system already has:
- RuntimeCaseView
- ErrorInstanceV2
- CanonicalRepairIR / CanonicalRepairOp
- CanonicalRepairProgram
- ActionPrimitive
- SharedProgramSynthesizer
- ProgramCoverage
- replay-gated promotion

This task is to strengthen the bridge among these layers.

Implement:

1. Extend CanonicalRepairOp.operation_signature with:
   - output_path_delta
   - relation_delta
   - predicate_scope_delta
   - grain_delta

2. Add CanonicalRepairProgram.program_envelope:
   - source_antipatterns
   - target_invariants
   - action_envelope
   - lowering_branches
   - branch_selection_contract
   - required_role_slots
   - negative_guards

3. Add invariant/envelope synthesis fallback in SharedProgramSynthesizer:
   - target_output_subset_of_source_outputs
   - target_added_relation_equality
   - target_relation_role_equality
   - predicate_scope_delta
   - grain_delta

4. Complete placeholder ActionPrimitive lowerers:
   - MOVE_CONDITION
   - CHANGE_GRAIN
   - SWITCH_CANONICAL_FIELD
   - MATERIALIZE_RANKING_OUTPUT

5. Split ProgramCoverage reporting:
   - static_program_coverage
   - runtime_binding_coverage

6. Before conflicting_action_contracts:
   - canonicalize and merge compatible action envelopes

7. Prompt changes:
   - WrongCaseAuditor emits candidate_fix_sql, not self-validated SQL
   - ErrorInstanceExtractor emits hypotheses and core/accessory distinction, not final canonical op authority
   - ActionCompiler prompt becomes simpler selector over pre-scored candidates
   - MemoryRewrite cannot use natural_language_hint to fill missing arguments
   - HintInstantiation becomes optional readability helper
   - CompatibilityJudge becomes advisory audit, not promotion gate

Hard constraints:
- No manual pattern rules.
- No toxicology-specific code.
- No case-id rules.
- No weighted case similarity.
- Runtime stays answer-blind.
```

---

# 7. 最终收束

当前 EEA v2 已经具备正确骨架，甚至很多我前面抽象建议的对象你们已经实现了：

```text
CanonicalRepairIR
CanonicalRepairOp
CanonicalRepairProgram
ActionPrimitive
ProgramCoverage
trigger_contract materialization
replay-gated promotion
```

现在效果不理想，不是因为这些方向错，而是因为：

```text
1. CanonicalRepairOp 的 operation_signature 还不足以跨 case 表达 repair interface；
2. SharedProgramSynthesizer 仍偏 common op bucket，缺 invariant/envelope fallback；
3. ActionCompiler 只有 6/10 primitive 完整实现；
4. Runtime trigger contract 还没有完全绑定 program envelope；
5. Prompt 仍让 LLM 承担过多系统裁决，而不是让 code/compiler/replay 裁决。
```

因此下一版的核心不是“重新想 taxonomy”，而是把当前 v2 的中间桥打通：

```text
historical S0/gold evidence
  -> richer CanonicalRepairIR
  -> program_envelope
  -> answer-blind trigger/applicability contract
  -> ActionPrimitive lowering
  -> runtime binding coverage
  -> replay-gated admission
```

这条链打通后，当前系统会保留新架构的安全性、answer-blind、低 regression、可 replay，同时恢复旧 repair-path 方法在强重复错误上的收益。
