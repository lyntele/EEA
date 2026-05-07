对，这个例子既然是你临时编的，就**不应该进入当前修改方案**。我前一版把 `literal_value_delta / REPLACE_PREDICATE_LITERAL` 写进正式流程，会把系统设计推向“为了假想错误扩动作空间”，这不符合你现在的实验依据。

下面是去掉大小写假设后的重新整理版。核心变化是：

```text
不新增 literal_value_delta
不新增 REPLACE_PREDICATE_LITERAL
不把假想错误纳入当前 action space
继续以已有人工标注和真实实验暴露的问题为准
```

---

# EEA v2 最终版修改流程文档：基于真实人工标注与 r4 实验的 ContrastiveRepairEffect

## 0. 修改目标

当前系统已经有：

```text
RuntimeCaseView
CaseAudit
ErrorInstanceV2
CanonicalRepairIR
CanonicalRepairOp
RepairEffectSignature
ProgramEnvelope
CanonicalRepairProgram
ActionPrimitive
ActionCompiler
ProgramCoverage
Replay-gated evolution
```

r4 实验说明，当前机制已经能修通一条真实路径：

```text
206 singleton
  -> 307 runtime ready
  -> Action count = 1
  -> rewrite S0
  -> selector 选择 S1
  -> final correct
```

这说明 `ProgramEnvelope / bundle-level action / role-side output subset` 的方向是有效的。  

但是 r4 也说明，目前只打通了：

```text
joined role-side element pair -> single-side element output
```

没有打通：

```text
direct endpoint pair -> single-side output
scope / molecule-level relation reroute
predicate-scope / denominator repair
grain / aggregation-unit repair
canonical field switch
ranking / display output repair
```

所以最终目标不是为 `connected.atom_id / atom_id2` 写规则，而是把系统学习对象升级为：

```text
ContrastiveRepairEffect:
  axis
  source_state
  target_state
  delta
  role
  triggerability
  actionability
```

其中 `axis` 来自真实人工标注中反复出现的跨库修复轴，而不是来自假想错误。

---

# 1. 真实 effect axes

当前只使用人工标注和已有实验中出现过的 effect axes。

根据 `manual_group_signal_map.md`，人工 pattern / family 已经被映射到以下跨库抽象信号轴：`output_shape_delta`、`grain_delta`、`source_route_delta`、`predicate_scope_delta`、`aggregation_unit_delta`、`role_anchor_delta`、`temporal_scope_delta`、`proxy_slot_delta`、`storage_contract_delta`、`formula_delta`、`ranking_contract_delta`、`multi_output_contract_delta`。

因此当前正式 axes 设为：

```text
output_shape_delta
multi_output_contract_delta
grain_delta
aggregation_unit_delta
source_route_delta
predicate_scope_delta
role_anchor_delta
temporal_scope_delta
proxy_slot_delta
storage_contract_delta
formula_delta
ranking_contract_delta
```

不加入：

```text
literal_value_delta
value_contract_delta
REPLACE_PREDICATE_LITERAL
```

除非后续真实 case audit 中反复出现这类错误，并且能证明它有 runtime triggerability 和 actionability。

---

# 2. 旧流程实际怎么跑

## 2.1 Runtime

当前 runtime 流程不变：

```text
DeepEye 生成并选择 S0
  ↓
EEA 构造 RuntimeCaseView
  ↓
检索 LibraryStateV2 中的 memory objects
  ↓
trigger_contract gate
  ↓
ActionCompiler 生成 actions
  ↓
HintInstantiation / MemoryRewrite
  ↓
得到 S1
  ↓
DeepEye / selector 在 S0 和 S1 中选择 final
```

runtime 只看：

```text
question
evidence
S0
schema
candidate context
existing memory
```

不看 gold，不看官方正确性，不做 execution comparison。这一 answer-blind 边界应保持不变。

---

## 2.2 Offline update

当前 offline update 流程也保留：

```text
wrong case
  ↓
WrongCaseAuditor
  ↓
CaseAudit
  ↓
ErrorInstanceExtractor
  ↓
ErrorInstanceV2
  ↓
RepairProgramNormalizer
  ↓
CanonicalRepairIR / CanonicalRepairOp
  ↓
singleton GroupSummary
  ↓
local evolution / final evolution
  ↓
family / pattern
  ↓
ProgramCoverage + replay
  ↓
runtime_usable / formal pattern / offline-only family
```

新修改是在 `CanonicalRepairIR` 里增加更通用的 effect candidate 表示，并让 `SharedProgramSynthesizer` 和 runtime gate 优先围绕 effect 来工作。

---

# 3. 新增核心对象：ContrastiveRepairEffect

## 3.1 定义

建议在 `data_structures_v2.py` 中新增：

```python
class ContrastiveRepairEffect(BaseModel):
    effect_id: str
    axis: str

    source_state: Dict[str, Any] = Field(default_factory=dict)
    target_state: Dict[str, Any] = Field(default_factory=dict)
    delta: Dict[str, Any] = Field(default_factory=dict)

    role: str = "primary"
    # primary / dependency / accessory / noise

    triggerability: Dict[str, Any] = Field(default_factory=dict)
    # source_visible_in_runtime
    # target_bindable_from_schema_or_memory
    # gold_only_blocker

    actionability: Dict[str, Any] = Field(default_factory=dict)
    # primitive
    # arguments_bindable
    # branch_count
    # branch_selection_answer_blind

    evidence: Dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0
```

并扩展 `RepairEffectSignature`：

```python
class RepairEffectSignature(BaseModel):
    effect_candidates: List[ContrastiveRepairEffect] = Field(default_factory=list)

    # backward compatibility:
    output_effect: Dict[str, Any] = Field(default_factory=dict)
    relation_effect: Dict[str, Any] = Field(default_factory=dict)
    predicate_scope_effect: Dict[str, Any] = Field(default_factory=dict)
    grain_effect: Dict[str, Any] = Field(default_factory=dict)
    field_binding_effect: Dict[str, Any] = Field(default_factory=dict)
    ranking_effect: Dict[str, Any] = Field(default_factory=dict)
```

## 3.2 与旧实现区别

旧实现：

```text
RepairEffectSignature 是固定字段：
  output_effect
  relation_effect
  predicate_scope_effect
  grain_effect
  field_binding_effect
  ranking_effect
```

新实现：

```text
RepairEffectSignature.effect_candidates 是一个列表。
每个 effect candidate 都有 axis / source_state / target_state / delta。
```

这样可以表达：

```text
output shape change
source route change
predicate scope change
aggregation unit change
grain change
proxy / canonical slot switch
storage contract effect
formula effect
ranking contract effect
multi-output contract effect
```

但不会为了不存在的大小写错误扩展动作空间。

---

# 4. 每一步输入 / 输出 / 验收

## Step 1：WrongCaseAuditor

### 输入

```text
question
evidence
pred_sql
gold_sql
local_schema_view
execution_result
```

### 输出

```json
{
  "final_error_reason": "...",
  "minimal_fix": "...",
  "candidate_fix_sql": "...",
  "minimal_patch_ops": [
    {
      "clause": "...",
      "edit_kind": "...",
      "source_fragment": "...",
      "target_fragment": "...",
      "preserve_fragments": [],
      "primary": true,
      "effect_axis_hint": "..."
    }
  ],
  "secondary_differences": [],
  "error_locus_hint": "...",
  "confidence": "..."
}
```

### Prompt 修改

当前 `wrong_case_auditor.py` 已经明确它是 offline 节点，允许看 gold，目标是 minimal verifiable repair，不是抽象标签；也明确 `candidate_fix_sql` 不能声称已执行验证。

建议只补充：

```text
effect_axis_hint
primary / dependency / accessory / noise 区分
真实 effect axis 的正反例
```

不要加入 `change_literal` 相关要求。

### 验收

#### `206`

```text
primary:
  SELECT a1.element, a2.element -> DISTINCT a1.element
  effect_axis_hint = output_shape_delta

dependency:
  drop second atom join if unused

accessory:
  DISTINCT
```

#### `263`

```text
primary:
  source route / grain / aggregation unit changes
  effect_axis_hint in {source_route_delta, grain_delta, aggregation_unit_delta}
```

---

## Step 2：RoleGraph / AST Delta Extraction

### 输入

```text
pred_sql
gold_sql / candidate_fix_sql
schema
```

### 输出

```text
source_role_graph
target_role_graph
output refs
predicate refs
join equalities
output_shape_delta
relation_delta
predicate_scope_delta
grain_delta
```

### 和旧实现区别

旧实现已有 `RoleGraphNormalizer`，并且 r4 里 `206 -> 307` 已经成功利用 `direct_role_path / derived_role_path / role_side_group / side_key`。

新要求是：

```text
RoleGraph 不只服务 role-side pair。
它要为所有 effect axes 提供 source_state / target_state 的结构证据。
```

### 验收

#### `307`

保持：

```text
a1.element.derived_role_path = direct:connected.atom_id
a2.element.derived_role_path = direct:connected.atom_id2
```

#### `223`

要求：

```text
atom_id.direct_role_path = direct:connected.atom_id
atom_id2.direct_role_path = direct:connected.atom_id2
```

但不将这写成专门的 column-pair rule；它只是 `output_shape_delta / storage_contract_delta` 的 source_state evidence。

#### `263`

要求：

```text
atom.molecule_id = bond.molecule_id
```

只作为 target_state / target_invariant，不作为 current S0 的 source_antipattern。

---

## Step 3：ErrorInstanceExtractor

### 输入

```text
runtime_case_view
case_audit
execution_comparison
code_prepared
```

### 输出

保留旧字段：

```text
question_features
pred_sql_features
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

新增：

```json
{
  "possible_effect_axes": [
    {
      "axis": "output_shape_delta|source_route_delta|predicate_scope_delta|aggregation_unit_delta|grain_delta|proxy_slot_delta|storage_contract_delta|formula_delta|ranking_contract_delta|multi_output_contract_delta|temporal_scope_delta|role_anchor_delta",
      "source_state_summary": "...",
      "target_state_summary": "...",
      "delta_summary": "...",
      "primary_likelihood": "low|medium|high",
      "why": "..."
    }
  ]
}
```

### Prompt 修改

当前 `error_instance_extractor.py` 已经要求：

```text
LLM 输出 deep_bias / repair_goal / repair_skeleton / slots / repair_program / guardrails
code 会 canonicalize op_type 和 accessory status
```

也已经有 `source_antipattern_hypothesis`、`target_invariant_hypothesis`、`core_vs_accessory`、`uncertain_axes`。

新增重点是：

```text
possible_effect_axes
source_state_summary
target_state_summary
delta_summary
```

### 验收

#### 反例：target invariant 不得放进 source

如果 gold 中有：

```text
atom.molecule_id = bond.molecule_id
```

但 pred 中没有，LLM 不得写：

```text
visible_in_pred_sql = atom.molecule_id = bond.molecule_id
```

必须写入：

```text
target_invariant_hypothesis
```

---

## Step 4：ContrastiveRepairEffect Discovery

### 输入

```text
CaseAudit
source_role_graph
target_role_graph
AST delta
execution_comparison
ErrorInstanceExtractor hypotheses
```

### 输出

```json
{
  "effect_candidates": [
    {
      "axis": "...",
      "source_state": {},
      "target_state": {},
      "delta": {},
      "role": "primary|dependency|accessory|noise",
      "triggerability": {},
      "actionability": {},
      "evidence": {},
      "confidence": 1.0
    }
  ]
}
```

### 主要 axis 发现规则

#### output_shape_delta

```text
SELECT arity / output roles / output refs changed
target is subset / expansion / replacement
```

#### source_route_delta

```text
fact table / bridge table / join route changed
```

#### predicate_scope_delta

```text
predicate moved between WHERE / CASE / HAVING / subquery
or predicate applies to different branch/side/scope
```

#### grain_delta / aggregation_unit_delta

```text
COUNT anchor / DISTINCT / GROUP BY / row-vs-entity-vs-record grain changed
```

#### proxy_slot_delta

```text
source column and target column are semantically adjacent / same role family / canonical slot switch
```

#### storage_contract_delta

```text
gold follows physical storage representation rather than natural semantic interpretation
```

#### formula_delta

```text
numeric expression / denominator / explicit formula / text-threshold comparison changes
```

#### temporal_scope_delta

```text
date/time field source or time comparison policy changes
```

#### ranking_contract_delta

```text
rank / top-k / order metric / tie policy changes
```

#### multi_output_contract_delta

```text
multi-question / multi-slot output added or trimmed
```

### 验收

#### `206`

```text
axis = output_shape_delta
delta.kind = output_subset
role = primary
actionability.primitive = DROP_SIDE or DROP_SELECT_SLOT
```

#### `263`

```text
axis includes source_route_delta / grain_delta / aggregation_unit_delta
source_state = local endpoint-bound route
target_state = molecule-scope route
```

No uppercase/literal test.

---

## Step 5：Effect Role Classification

### 输入

```text
effect_candidates
minimal_patch_ops
repair_program steps
execution_delta
```

### 输出

每个 effect：

```text
primary
dependency
accessory
noise
```

### 和旧实现区别

旧实现容易把多个 edit 都当 required ops。r3 里 `307` 因 `SELECT edit + JOIN cleanup` 被拆成多个 action 而失败；r4 通过 bundle-level action 修通。

新实现把：

```text
primary effect 决定 ProgramEnvelope
dependency 放入 cleanup_edits
accessory 放入 optional policy
noise 不参与 synthesis
```

### 验收

`206`：

```text
output subset = primary
drop second atom join = dependency cleanup
DISTINCT = accessory
```

---

## Step 6：CanonicalRepairIR / ProgramEnvelope 写入

### 输入

```text
CanonicalRepairOp
effect_candidates
role classification
```

### 输出

```text
CanonicalRepairIR.repair_effect_signature.effect_candidates
CanonicalRepairProgram.program_envelope
```

### ProgramEnvelope

```json
{
  "source_antipatterns": [
    {"from": "effect.source_state"}
  ],
  "target_effects": [
    {"from": "effect.target_state"}
  ],
  "action_envelope": {
    "axis": "...",
    "delta": "...",
    "primary_primitive": "..."
  },
  "lowering_branches": [],
  "branch_selection_contract": {},
  "required_role_slots": [],
  "negative_guards": []
}
```

### 与旧实现区别

r4 已经有 `ProgramEnvelope`，但要从固定 effect 字段转为由 `effect_candidates` 主导。

---

## Step 7：SharedProgramSynthesizer

### 输入

```text
多个 singleton 的 CanonicalRepairIR
其中包含 effect_candidates
```

### 输出

```text
program-backed family
offline-only family
formal pattern candidate
```

### 新对齐规则

cases 可合成，如果：

```text
axis 相同
source_state abstraction 兼容
target_state abstraction 兼容
delta.kind 兼容
actionability primitive 兼容
```

不要求：

```text
same CanonicalRepairOp
same SQL patch
same exact instantiation function
same concrete column
```

### 与旧实现区别

当前 `shared_program_synthesizer_v2.py` 已有 `_effect_bucket_key`、`_ops_by_effect_bucket`、`_invariant_envelope_bucket` 等逻辑。

新要求是：

```text
effect_candidates 成为 synthesis 的主要对齐源。
```

### 验收

#### `206 / 307`

保持 runtime family：

```text
runtime_usable = true
runtime_binding_coverage = 1.0
```

r4 final freeze 中 `grp-fam-toxicology-206-307-71209fcf` 已是 runtime family。

#### `201 / 263`

应形成 effect family：

```text
axis = source_route_delta + grain_delta / aggregation_unit_delta
```

如果 runtime 不能使用，应明确：

```text
offline_family
reason = branch_not_runtime_bindable / target_state_not_bindable / primitive_missing
```

---

## Step 8：ProgramCoverage / Replay

### 输入

```text
ProgramEnvelope
member RuntimeCaseViews
ActionCompiler candidate enumeration
C0 -> C1 replay rows
```

### 输出

```text
static_program_coverage
runtime_binding_coverage
member_candidate_coverage
replay_improvement
regression
promotion decision
```

### 与旧实现区别

当前 `ProgramCoverage` 已有 static/runtime coverage 字段。

新要求：

```text
runtime family / formal pattern gate 优先看 runtime_binding_coverage
static_program_coverage 只说明结构上看起来可编译
```

### 验收

`201-263` 当前：

```text
static_program_coverage = 1.0
runtime_binding_coverage = 0.0
runtime_usable = false
```

保持阻断是对的，但 failure reason 要具体到 effect/branch，而不是笼统 compile failure。

---

# 6. Runtime 新流程

## Step 9：Runtime Retrieval

### 输入

```text
RuntimeCaseView
LibraryStateV2
```

### 输出

```text
candidate memory objects
```

### 规则

不用 weighted similarity。

使用离散 index：

```text
db_id
effect axis
source_state rough signal
schema availability
memory status
```

只负责召回，不负责最终准入。

---

## Step 10：Effect Applicability Gate

### 输入

```text
candidate ProgramEnvelope
current RuntimeCaseView
current S0 role graph
schema
```

### 输出

```text
pass / reject with reason
```

### Gate 三分

```text
1. source_state visible in current S0
2. target_state bindable from schema / memory
3. lowering branch answer-blind and unique
4. negative guards clean
5. ActionCompiler candidates exist
```

### 与旧实现区别

旧实现有时将 target relation/path 当成 source required signal。新实现禁止。

### 验收

#### `263`

不再失败于：

```text
source_antipattern_missing_output_path_roles:
  atom.molecule_id=bond.molecule_id:output:atom.element
```

如果失败，应是：

```text
target_state_not_bindable
branch_selection_ambiguous
runtime_binding_no_candidate
```

#### `223`

不再只失败于：

```text
source_antipattern_missing_output_path_roles
```

应先识别：

```text
source_state = direct multi-side endpoint output
```

然后如果失败，给出：

```text
target_side_unbound
branch_ambiguous
no_prior_effect_support
```

---

## Step 11：ActionCompiler

### 输入

```json
{
  "runtime_case_view": {},
  "memory_objects": [],
  "effect_contract": {
    "axis": "...",
    "source_state": {},
    "target_state": {},
    "delta": {}
  },
  "candidate_sets": []
}
```

### 输出

```text
Action[]
schema_diagnostics
```

### effect-to-primitive mapping

```text
output_shape_delta
  -> ADD_SELECT_SLOT / REPLACE_SELECT_SLOT / DROP_SELECT_SLOT / DROP_SIDE

multi_output_contract_delta
  -> ADD_SELECT_SLOT / DROP_SELECT_SLOT

source_route_delta
  -> REROUTE_FACT / INSERT_BRIDGE

predicate_scope_delta
  -> MOVE_CONDITION

aggregation_unit_delta
  -> CHANGE_GRAIN

grain_delta
  -> CHANGE_GRAIN

proxy_slot_delta
  -> SWITCH_CANONICAL_FIELD / REPLACE_SELECT_SLOT

temporal_scope_delta
  -> SWITCH_CANONICAL_FIELD / MATERIALIZE_RANKING_OUTPUT

ranking_contract_delta
  -> MATERIALIZE_RANKING_OUTPUT

storage_contract_delta
  -> DROP_SIDE / CHANGE_GRAIN / REROUTE_FACT depending on source_state

formula_delta
  -> MOVE_CONDITION / CHANGE_GRAIN / SWITCH_CANONICAL_FIELD depending on delta
```

### 不新增

```text
REPLACE_PREDICATE_LITERAL
literal_value_delta
value_contract_delta
```

当前没有真实案例依据，不进入本轮。

### 与旧实现区别

当前 ActionCompiler prompt 已经是 strict selector：只能选 code-enumerated candidates，不能 invent。

新增：

```text
effect_contract input
```

候选必须实现 effect_contract，否则不能选。

---

## Step 12：HintInstantiation

当前 `hint_instantiation.py` 已经是 readability helper，不再处理 source-case template。

保留，只加反例：

```text
If action says DROP a2.element, do not rewrite hint as “add DISTINCT” unless DISTINCT appears in actions.
```

---

## Step 13：MemoryRewrite

当前 `memory_rewrite.py` 已经是 bounded autonomy：actions required、不能补缺失参数、不能自行加 WHERE/GROUP/LIMIT/DISTINCT，realization trace 必填。

保留。

新增 cleanup 规则：

```text
cleanup_edits are optional unless marked required=true.
If cleanup cannot be applied safely, leave it unrealized.
```

不新增 `REPLACE_PREDICATE_LITERAL` template。

---

# 7. Prompt 修改总表

## 7.1 WrongCaseAuditor

### 当前优点

offline/gold-visible、minimal repair、candidate_fix_sql 不自称验证，规则清楚。

### 修改

加入：

```text
effect_axis_hint
primary/dependency/accessory/noise examples
```

去掉任何大小写 literal 示例。

### 正例

```text
pred SELECT x, y
gold SELECT x
→ output_shape_delta, primary

pred joins table T only because dropped output y used it
gold no longer uses T
→ dependency cleanup
```

### 反例

```text
Do not mark cleanup join drop as primary if the main repair is output shape.
```

---

## 7.2 ErrorInstanceExtractor

### 当前优点

已经写明 code/LLM ownership，LLM 不最终决定 canonical op；有 `source_antipattern_hypothesis`、`target_invariant_hypothesis`、`core_vs_accessory`、`uncertain_axes`。

### 修改

加入：

```text
possible_effect_axes
source_state_summary
target_state_summary
delta_summary
primary_likelihood
```

### 反例

```text
If gold adds a relation not present in pred, do not say it is visible in pred_sql.
Put it in target_invariant_hypothesis.
```

---

## 7.3 CompatibilityJudge

当前已经 advisory-only。

修改为 effect-based advisory：

```json
{
  "shared_effect_axis_hypothesis": "...",
  "aligned_source_state": [],
  "aligned_target_state": [],
  "delta_compatibility": "compatible|partial|conflict",
  "branch_variation_axes": [],
  "runtime_binding_blockers": [],
  "recommended_code_checks": []
}
```

---

## 7.4 ActionCompiler

当前是 strict selector，不发明 action。

新增输入：

```text
effect_contract
```

正反例：

```text
Effect: output subset / drop side.
Candidate drops correct side -> select.
Candidate drops keep side -> reject.

Effect: route reroute.
Candidate only changes SELECT -> reject unless route already correct.
```

---

## 7.5 MemoryRewrite

当前 prompt 已经好。

只补 cleanup 规则，不补 literal primitive。

---

# 8. 新旧实现差异表

| 环节              | 当前旧实现                                           | 新实现                                                          |
| --------------- | ----------------------------------------------- | ------------------------------------------------------------ |
| 学习对象            | CanonicalRepairOp + fixed RepairEffectSignature | ContrastiveRepairEffect candidates                           |
| effect 表达       | output_effect / relation_effect 等固定字段           | axis + source_state + target_state + delta                   |
| LLM 抽象          | deep_bias / repair_goal / repair_program        | possible_effect_axes + semantic hypothesis                   |
| code 抽取         | SQL delta / role graph / op                     | SQL delta / role graph / effect candidates                   |
| family 聚合       | op bucket / effect bucket / invariant bucket    | compatible effect candidates                                 |
| runtime trigger | trigger_contract signals                        | source_state visible + target_state bindable + branch unique |
| target relation | 可能误作 source signal                              | 只作 target_state bindability                                  |
| direct pair     | 容易缺 output_path_roles                           | output_shape/storage effect direct branch                    |
| scope relation  | 受 ratio/scalar/grain 表面约束                       | source_route/grain effect + branch contract                  |
| literal 大小写     | 不考虑                                             | 不纳入当前正式流程                                                    |
| prompt          | LLM 兼做较多结构判断                                    | LLM 产 hypothesis，code/replay 裁决                              |

---

# 9. 验收计划

## Phase 1：保持 r4 成果

```text
307 继续 improved
matched grp-sing-toxicology-206
action_count=1
selector chooses S1
no regression
```

## Phase 2：223

新要求：

```text
direct multi-side endpoint output 被识别为 source_state
不再只失败于 source_antipattern_missing_output_path_roles
如果失败，给出：
  target_side_unbound
  branch_ambiguous
  no_prior_effect_support
```

## Phase 3：263 / 306

新要求：

```text
target relation 不再作为 source required path
failure reason 改为：
  target_state_not_bindable
  branch_selection_ambiguous
  runtime_binding_no_candidate
  primitive_missing
```

## Phase 4：跨库验收

使用 `doc/testCase.md` 里的 formal pattern、family-only、误并防回归用例。人工标注只做验收，不进入代码逻辑。

---

# 10. 最终 Codex 指令摘要

```text
Do not redesign EEA v2.
Do not add toxicology-specific rules.
Do not add column-pair rules.
Do not add literal/case rules.
Do not use manual pattern labels in code.
Do not introduce weighted case similarity.

Implement ContrastiveRepairEffect:

1. Add ContrastiveRepairEffect schema:
   axis, source_state, target_state, delta, role,
   triggerability, actionability, evidence, confidence.

2. Extend RepairEffectSignature with effect_candidates list,
   preserving old fixed fields for backward compatibility.

3. Modify RepairProgramNormalizer:
   generate effect_candidates from pred_sql vs gold_sql:
   output_shape_delta
   multi_output_contract_delta
   grain_delta
   aggregation_unit_delta
   source_route_delta
   predicate_scope_delta
   role_anchor_delta
   temporal_scope_delta
   proxy_slot_delta
   storage_contract_delta
   formula_delta
   ranking_contract_delta

4. Classify each effect:
   primary / dependency / accessory / noise.

5. Modify SharedProgramSynthesizer:
   align cases by compatible effect_candidates,
   not only CanonicalRepairOp bucket.

6. Modify runtime gate:
   source_state visible in current S0;
   target_state schema/memory-bindable;
   branch selection answer-blind and unique;
   ActionCompiler candidates exist.

7. Modify ActionCompiler prompt/input:
   add effect_contract.
   selected candidate must realize effect_contract.

8. Modify prompts:
   WrongCaseAuditor: add effect_axis_hint and primary/dependency/accessory examples.
   ErrorInstanceExtractor: add possible_effect_axes.
   CompatibilityJudge: make effect-based advisory only.
   MemoryRewrite: clarify cleanup_edits optional/required.
   HintInstantiation: readability only.

9. Preserve r4 success on 206 -> 307.
```

---

# 11. 最终一句话

当前版本已经证明 `ProgramEnvelope / effect-bundle` 能带来真实收益。下一步不是扩某个结构规则，也不是加入假想大小写规则，而是把每个错例统一表示成：

```text
ContrastiveRepairEffect:
  source_state -> target_state
  under a real repair axis
  with primary/dependency/accessory role
  and runtime triggerability/actionability
```

这样系统才能从人工标注中的跨库共性学习，而不是被当前 toxicology 的某个局部结构牵着走。
