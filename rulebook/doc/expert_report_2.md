对，不能针对几个 toxicology case 写规则。机制层面的改动应该是：**不增加人工 pattern 特例，而是让系统从任意 case 的 `S0 -> fix/gold` 中自动抽出更高层的“修复效果 / repair effect”，再把这些 effect 编译成已有 ActionPrimitive。**

换句话说，下一步不是写：

```text
if toxicology and connected.atom_id2 then drop side
```

而是写一套通用机制：

```text
SQL role graph + pred/fix delta
  -> repair effect signature
  -> program envelope
  -> branch selection contract
  -> action bundle lowering
  -> runtime binding coverage
```

这才是框架级泛化改动。

---

# 1. 先明确：当前不是缺动作空间，而是缺“动作组合的效果归一”

你们当前已经有两层动作空间：

```text
offline: CanonicalRepairOp
runtime: ActionPrimitive
```

`current_implementation_overview.md` 也明确说明，当前 action compiler 已经从 matched memory 编译 bounded actions，且已有 6 个实现较完整的 primitives，另有 4 个 placeholder：`CHANGE_GRAIN / MOVE_CONDITION / SWITCH_CANONICAL_FIELD / MATERIALIZE_RANKING_OUTPUT`。

所以泛化性改动不是“新增几个 pattern 动作”，而是解决这个问题：

> 一个真实修复经常由多个底层 SQL edit 共同实现同一个 repair effect；当前系统把它们拆成多个 required ops，导致 singleton budget、compiler coverage、conflict gate 全部变严。

例如这次 `307` 中，正确 memory `206` 已被检索，variant signal 命中，binder dry-run 成功，但被：

```text
compiler_dry_run_missing_required_ops:206:canonical:1
compiler_dry_run_action_budget_exceeded:2>1
```

挡住。
这说明失败点不是召回，而是**一个修复效果被拆成了多个 runtime action，无法作为一个可执行 envelope 使用**。

因此第一条机制改动应该是：

```text
CanonicalRepairOp 之上增加 RepairEffect / ProgramEnvelope。
```

---

# 2. 机制改动一：增加 `RepairEffectSignature`

现在 `CanonicalRepairOp.op_type` 表达的是：

```text
SELECT_DROP_SLOT
JOIN_ADD_BRIDGE
WHERE_REPLACE_CONDITION
```

它是 SQL 区域动作。
但泛化需要表达的是：

```text
这个动作组合最终改变了什么 answer contract / relation scope / grain / predicate scope。
```

建议为每个 case 的 `CanonicalRepairIR` 增加一层：

```json
{
  "repair_effect_signature": {
    "output_effect": {},
    "relation_effect": {},
    "predicate_scope_effect": {},
    "grain_effect": {},
    "field_binding_effect": {},
    "ranking_effect": {}
  }
}
```

它不是人工 pattern，而是由 `pred_sql -> fix/gold_sql` 的 AST delta 和 role graph 自动抽出。

## 2.1 `output_effect`

用于所有 SELECT 输出变化：

```json
{
  "kind": "output_subset | output_expand | output_replace | output_reorder",
  "source_arity": 2,
  "target_arity": 1,
  "target_is_subset_of_source": true,
  "kept_output_refs": [],
  "dropped_output_refs": [],
  "same_attribute_multi_role_output": true,
  "same_relation_multi_side_output": true
}
```

这可以泛化到任何库：

```text
两个 role-side 输出塌成一个
多余 display column 删除
补 identifier column
替换 canonical display field
```

不是 toxicology 特例。

## 2.2 `relation_effect`

用于 JOIN / route / bridge 变化：

```json
{
  "kind": "add_relation | remove_relation | reroute_relation | change_scope_relation",
  "source_relation_paths": [],
  "target_relation_paths": [],
  "added_relation_equalities": [],
  "removed_relation_equalities": [],
  "target_scope_key": null
}
```

这可以表达：

```text
side-table 接入
parent route
fact table reroute
local path -> scope relation
```

不需要写具体数据库规则。

## 2.3 `predicate_scope_effect`

用于 WHERE / CASE / HAVING / numerator / denominator：

```json
{
  "kind": "predicate_add | predicate_drop | predicate_replace | predicate_move",
  "predicate_signature": {},
  "source_scope": "WHERE",
  "target_scope": "CASE_NUMERATOR",
  "denominator_preserved": true
}
```

这对应 `MOVE_CONDITION`，不是某个特例。

## 2.4 `grain_effect`

用于 count anchor / distinct / row/entity/pair grain：

```json
{
  "kind": "grain_change | count_anchor_change | distinct_anchor_change",
  "source_grain": "...",
  "target_grain": "...",
  "source_anchor": "...",
  "target_anchor": "..."
}
```

这对应 `CHANGE_GRAIN`。

---

# 3. 机制改动二：`CanonicalRepairProgram` 增加 `ProgramEnvelope`

`RepairEffectSignature` 是单个 case 的 evidence。
多个 case 合成 family/pattern 时，需要的是 `ProgramEnvelope`。

建议在 `CanonicalRepairProgram` 里加：

```json
{
  "program_envelope": {
    "source_antipatterns": [],
    "target_effects": [],
    "required_role_slots": [],
    "allowed_canonical_ops": [],
    "allowed_action_primitives": [],
    "lowering_branches": [],
    "branch_selection_contract": {},
    "negative_guards": [],
    "unresolved_axes": []
  }
}
```

核心是：**program envelope 聚合同一个 repair effect，而不是要求 `CanonicalRepairOp.op_type` 完全一致。**

例如：

```text
case A: SELECT_DROP_SLOT + JOIN_DROP_TABLE
case B: SELECT_REPLACE_SLOT
case C: DROP_SIDE
```

如果它们的 `output_effect` 都是：

```text
source multi-side output -> target subset output
```

那它们可以属于同一个 envelope。

这不是“修复路径一致”，也不是“实例化函数一致”，而是：

```text
repair effect compatible
+ runtime branch 可由 S0/schema/question answer-blind 选择
```

---

# 4. 机制改动三：SharedProgramSynthesizer 从 “op bucket first” 改成 “effect envelope first”

当前 `shared_program_synthesizer_v2.py` 已经不是旧式相似度聚类，它会合成 shared canonical program；但主路径仍以 lowering family / op bucket 为核心，例如 `select_drop / join_bridge / where_side_edit`。

框架级改法：

```text
不要只问：成员是否共享同一个 CanonicalRepairOp bucket？
要先问：成员是否共享同一个 RepairEffectSignature？
```

建议 synthesis 顺序改为：

```text
Step 1. 尝试 exact op-family synthesis
  - 当前已有逻辑保留

Step 2. 若失败，尝试 effect-envelope synthesis
  - output_effect 对齐
  - relation_effect 对齐
  - predicate_scope_effect 对齐
  - grain_effect 对齐

Step 3. 生成 ProgramEnvelope
  - 如果 lowering branch 可 answer-blind 选择，进入 runtime gate
  - 如果 branch 需要 gold 才能选，保留 offline family
```

这解决的是机制问题，不是特例问题。

---

# 5. 机制改动四：runtime ActionCompiler 不再按“单个 op”计数，而按“action envelope”计数

这次 `307` 的核心失败是：

```text
compiler_dry_run_action_budget_exceeded:2>1
```

因为一个真实修复被拆成：

```text
SELECT edit
JOIN cleanup
```

但从 runtime 角度，它应是一个 bounded action envelope：

```text
output role-side collapse
```

所以 action budget 应该改成：

```text
预算按 envelope 计，不按底层 SQL edit step 计。
```

一个 envelope 可以含多个 bounded edits：

```json
{
  "primitive": "DROP_SIDE",
  "effect_kind": "output_subset",
  "bounded_edits": [
    {"clause": "SELECT", "edit": "drop_output_side"},
    {"clause": "JOIN", "edit": "drop_join_if_unused"}
  ],
  "counts_as_action": 1
}
```

这里不需要新增 toxicology 专用 primitive。
可以继续使用现有 `DROP_SIDE`，但把它从“单个 side drop”升级成：

```text
role-side envelope lowerer
```

这类机制适用于任意双 role-side / multi-role-side schema。

---

# 6. 机制改动五：role graph 要分两层：direct role 和 derived role

这次 `223` 暴露的问题是：

```text
direct pair:
  connected.atom_id, connected.atom_id2

joined pair:
  connected.atom_id -> atom.element
  connected.atom_id2 -> atom.element
```

当前系统对 direct pair 和 joined pair 的 role graph 处理没有统一，导致 `223` 仍然报：

```text
source_antipattern_missing_output_path_roles
source_antipattern_missing_role_side_pair_shape
```



框架级改动是：

```text
RoleGraphNormalizer 输出 direct_role_path 和 derived_role_path 两层。
```

例如：

```json
{
  "output_ref": {
    "expr": "a1.element",
    "base_column": "atom.element",
    "direct_role_path": "connected.atom_id",
    "derived_role_path": "connected.atom_id -> atom.atom_id -> atom.element",
    "role_side_group": "connected.{atom_id,atom_id2}",
    "side_key": "atom_id"
  }
}
```

对 direct pair：

```json
{
  "output_ref": {
    "expr": "connected.atom_id",
    "base_column": "connected.atom_id",
    "direct_role_path": "connected.atom_id",
    "derived_role_path": null,
    "role_side_group": "connected.{atom_id,atom_id2}",
    "side_key": "atom_id"
  }
}
```

这样 program envelope 可以说：

```text
multi-side output over same role_side_group
```

而不是必须要求完整 joined atom path。

这不是 toxicology 规则；任何双 FK / symmetric role / paired endpoint schema 都适用。

---

# 7. 机制改动六：source_antipattern 不应要求 target path 出现在当前 S0

当前多个 case 的 gate reason 里出现：

```text
source_antipattern_missing_output_path_roles:
  join_path:atom.molecule_id=bond.molecule_id:output:atom.element
```

这其实很危险：`atom.molecule_id=bond.molecule_id` 是 fix/gold 里的 target relation，不一定存在于当前错误 `S0`。

对 runtime 来说，source anti-pattern 应该检查：

```text
当前 S0 是否暴露了错误形态。
```

target invariant 应该检查：

```text
schema 是否支持目标 relation；
compiler 能否 lower 到目标 relation。
```

不能把 target relation path 当作 source anti-pattern required signal。

所以机制上要拆：

```text
source_antipattern_contract:
  must be visible in current S0

target_invariant_contract:
  must be schema-bindable, not necessarily present in current S0

lowering_contract:
  can add/reroute from source to target
```

这会改善 `263 / 306` 这类 scope family 的误杀。`263` runtime response 中就出现了要求当前 S0 具有 `atom.molecule_id=bond.molecule_id:output:atom.element` 的 source-path 检查，这本质上把 gold/fix target path 错当成了 source anti-pattern。

---

# 8. 机制改动七：branch selection contract，而不是同一实例化函数

为了保持泛化性，不应要求同一 family 中所有 case 共用 exact instantiation function。

正确机制是：

```text
ProgramEnvelope 可以有多个 lowering_branches；
但每个 branch 必须有 answer-blind precondition。
```

例如：

```json
{
  "lowering_branches": [
    {
      "branch_id": "drop_extra_output_side",
      "precondition": {
        "current_sql_has": ["multi_side_output"],
        "target_effect": "output_subset"
      },
      "actions": ["DROP_SIDE"]
    },
    {
      "branch_id": "reroute_to_target_relation",
      "precondition": {
        "current_sql_has": ["source_local_path"],
        "schema_has": ["target_relation_equality"]
      },
      "actions": ["REROUTE_FACT", "INSERT_BRIDGE"]
    },
    {
      "branch_id": "move_predicate_scope",
      "precondition": {
        "current_sql_has": ["predicate_in_where", "ratio_or_conditional_aggregate"]
      },
      "actions": ["MOVE_CONDITION"]
    }
  ]
}
```

如果当前 case 同时满足多个 branch，且无法用 contract 决定唯一 branch：

```text
runtime reject: branch_ambiguous
```

如果只能用 gold 决定 branch：

```text
offline family only
```

这避免了：

```text
exact instantiation function 一致
```

也避免了：

```text
case similarity 直接触发
```

---

# 9. 机制改动八：实现 placeholder primitives，但以 effect 类型驱动

你们的 `ActionPrimitive` 里已经有 4 个 placeholder。
框架级改动不是写特例，而是补齐 effect 到 primitive 的通用 lowering：

| RepairEffect                           | ActionPrimitive                  |
| -------------------------------------- | -------------------------------- |
| `output_subset / output_side_collapse` | `DROP_SELECT_SLOT` / `DROP_SIDE` |
| `relation_scope_change`                | `REROUTE_FACT` / `INSERT_BRIDGE` |
| `predicate_scope_move`                 | `MOVE_CONDITION`                 |
| `grain_anchor_change`                  | `CHANGE_GRAIN`                   |
| `canonical_field_switch`               | `SWITCH_CANONICAL_FIELD`         |
| `ranking_output_materialization`       | `MATERIALIZE_RANKING_OUTPUT`     |

其中后四个目前没有完整实现，所以大量非 SELECT/JOIN 局部修复无法生效。

这也是机制问题，不是特例问题。

---

# 10. 机制改动九：promotion gate 用 runtime binding coverage，而不是 static op support

当前 `program_coverage_v2.py` 已经有 static coverage 和 runtime binding coverage 两层；runtime binding coverage 会真实调用 `enumerate_candidates` 看每个 member 能不能生成 compiler candidates。

下一步应该把 promotion / runtime family 的主要准入改为：

```text
static_program_supported
AND runtime_binding_coverage sufficient
AND replay no regression
```

而不是只看：

```text
canonical op 是否有 lowering family
```

这次 `201-263` family 的问题就是：static 可能看起来成了，但 runtime binding coverage 为 0，最终 `runtime_usable=false`。`ANALYSIS.md` 也指出它 static coverage = 1.0 但 runtime binding coverage = 0.0。

所以要把这两个 coverage 在报告和 gate 中彻底分开。

---

# 11. 机制改动十：manual annotations 只做验收，不参与机制

你强调得对。系统里不能出现：

```text
Pattern C
toxicology case 206
connected.atom_id2 特判
```

机制层应只出现：

```text
multi_side_output
target_output_subset
role_side_group
predicate_scope_move
grain_anchor_change
relation_scope_change
runtime_binding_coverage
```

人工标注只用于：

```text
1. 评估系统自动学出的 envelope 是否覆盖这些人工经验；
2. 检查系统有没有把人工认为不该 promotion 的 family 错升；
3. 设计通用 primitive 是否覆盖足够的 repair effect 类型。
```

不能进入代码逻辑、prompt 条件、trigger contract、discovery label。

---

# 12. 当前这次 run 对机制改动的映射

| 暴露现象                                                               | 机制级原因                                                                       | 泛化改动                                                                           |
| ------------------------------------------------------------------ | --------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `307` 检索到 `206`，variant match，binder success，但 `2>1` action budget | 一个 repair effect 被拆成 SELECT edit + JOIN cleanup                             | action envelope 计数；`DROP_SIDE` 支持 bounded edit bundle                          |
| `307` missing required op                                          | `JOIN_DROP_TABLE` 这类 dependency 未被合并进 envelope                              | dependency-to-envelope lowering，不作为独立 required op 阻断                           |
| `223` direct pair 不能触发                                             | role graph 只认 joined output path，不认 direct role-side pair                   | RoleGraph 输出 direct_role_path + derived_role_path                              |
| `263/306` 被 scalar aggregate / output grain 挡住                     | family overfit source manifestation，target relation 被误作 source anti-pattern | source contract / target invariant / lowering contract 三分                      |
| run-end 有 `206-307` runtime family，但无未来机会                          | family activation 太晚；singleton action 无法早用                                  | singleton 若 envelope binding unique 可早用；local evolve 后 replay-gated activation |
| summary 构建崩溃                                                       | 旧 module import 未清理                                                         | 修 summary import，不影响 runtime 但影响诊断                                             |

---

# 13. 最终建议的框架改动清单

可以给 code agent 的机制级任务是：

```text
1. Add RepairEffectSignature to CanonicalRepairOp / CanonicalRepairIR.
   Do not add manual pattern labels.

2. Add ProgramEnvelope to CanonicalRepairProgram:
   source_antipatterns
   target_invariants
   action_envelope
   lowering_branches
   branch_selection_contract
   required_role_slots
   negative_guards

3. Rewrite synthesis:
   common op bucket synthesis remains;
   add effect-envelope synthesis fallback.

4. Rewrite runtime applicability:
   source_antipattern must be visible in S0;
   target_invariant must be schema-bindable;
   lowering must produce bounded actions.

5. Change action budget:
   count one ProgramEnvelope action bundle as one action,
   even if it contains SELECT edit + cleanup JOIN edit.

6. Extend RoleGraph:
   direct_role_path
   derived_role_path
   role_side_group
   side_key

7. Implement placeholder primitives:
   MOVE_CONDITION
   CHANGE_GRAIN
   SWITCH_CANONICAL_FIELD
   MATERIALIZE_RANKING_OUTPUT

8. Use runtime_binding_coverage as promotion/runtime-family gate.

9. Before conflicting_action_contracts:
   canonicalize and merge action envelopes.

10. Keep manual annotations as validation only.
```

---

# 14. 一句话总结

机制和框架层面的泛化性改动应该是：

> **从“按 CanonicalRepairOp 单步匹配”升级为“按 RepairEffect / ProgramEnvelope 匹配”；从“每个 SQL edit 算一个 action”升级为“一个可绑定 repair effect 算一个 bounded action bundle”；从“source SQL 必须长得像历史 case”升级为“source anti-pattern 可见、target invariant 可绑定、lowering branch answer-blind 可选”。**

这样可以解释并修复这次 `307 / 223 / 263 / 306` 的失败，但不会写任何 toxicology 专用规则，也不会依赖人工 pattern。
