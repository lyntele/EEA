# emergence_refactor_plan_v2 验收记录

> 维护: 每个 WU 完成后追加一节到此文档。原独立 probe 报告废弃合并。
> 与 plan v2 主文档配对: plan = "做什么", 本文档 = "验过没"。

## §1 WUv2-2 verification

### probe 1: 6-case quick verification

commit: 82575c0

原报告: 已废弃,内容下移。

# WUv2-2 quick probe(6 case)

Probe 入口: `cli/replay_runtime_trigger_v2.py`

起点提交:

- EEA: `e3ac0bc`
- DeepEye v1 run 输入: `full11_*_postsel_v1_qwen3coderflash_20260510_164346`

执行边界:

- 使用 v1 full11 run 的 `final_library.json`
- 使用对应 DeepEye `.state/work/qid_*/eea_runtime_request.json`
- 只运行 runtime trigger -> compiler -> hint audit
- 不运行 SQL rewrite

原始输出:

- `/tmp/wuv2_2_probe/card_games.json`
- `/tmp/wuv2_2_probe/thrombosis_prediction.json`
- `/tmp/wuv2_2_probe/debit_card_specializing.json`
- `/tmp/wuv2_2_probe/toxicology.json`

| qid | db | 假设 primitive | 假设结果 | 实测 primitive | 实测 status | empty_reason 含 WUv2-2 阻断 | hint_audit | 符合? |
|---:|---|---|---|---|---|---:|---|---|
| 366 | card_games | `SWITCH_CANONICAL_FIELD` | 被阻断 -> `no_action` | 无 | `no_match` | 0 | 无 | 部分符合 |
| 1267 | thrombosis_prediction | `SELECT_REPLACE_SLOT` | 被阻断 -> `no_action` | 无 | `no_match` | 0 | 无 | 部分符合 |
| 1474 | debit_card_specializing | `ADD_SELECT_SLOT` | 被阻断 -> `no_action` | 无 | `no_match` | 0 | 无 | 部分符合 |
| 249 | toxicology | `DROP_SELECT_SLOT` | 保留 -> `ready` | `DROP_SELECT_SLOT` | `ready` | 0 | `hint_introduces_non_primitive_action=false` | 符合 |
| 253 | toxicology | `DROP_SELECT_SLOT` | 保留 -> `ready` | `DROP_SELECT_SLOT` | `ready` | 0 | `hint_introduces_non_primitive_action=false` | 符合 |
| 292 | toxicology | `MOVE_CONDITION` | 保留 -> `ready` + hint 越界 audit | `MOVE_CONDITION` | `ready` | 1 | `hint_introduces_non_primitive_action=true` | 符合 |

说明:

- q366 / q1267 / q1474 没有进入 compiler，因此 `compiler.empty_reason_counts` 中没有 WUv2-2 阻断计数。
- 这三例不是按假设中的 `no_action` 结束，而是在 trigger 阶段提前 `no_match`。从安全方向看，错误 rewrite 没有发生；但从 WUv2-2 验收假设看，没有观测到 compiler 层阻断，所以只能标为“部分符合”。
- 这三例的 trigger top candidate hard reasons 中能看到 WUv2-2 seed-target binding disabled 痕迹，但它们没有成为 selected group，因此不计入 compiler empty reason。

## 不符合 case

严格意义上没有“反向不符合”case，即没有出现应保留的 primitive 被 WUv2-2 阻断，也没有出现应阻断的高风险 primitive 继续 ready。

但有 3 个“部分符合”case，偏离点都是“预期 no_action，实际 no_match”。

### q366 / card_games

- 实际触发 group_id: 无 selected group
- trigger top candidate: `grp-sing-card_games-364`
- 实测 primitive: 无
- s0: `SELECT l.format FROM cards c INNER JOIN legalities l ON c.uuid = l.uuid WHERE c.name = 'Benalish Knight'`
- s1: 无，未进入 rewrite
- compiler.empty_reason_counts 相关条目: 无，未进入 compiler
- 假设偏离原因: trigger 阶段已被 `runtime_usable_false` / `trigger_contract_missing_required_signals` / `singleton_canonical_exact_failed` 挡住，没有到达 `SWITCH_CANONICAL_FIELD` compiler 阻断点。

### q1267 / thrombosis_prediction

- 实际触发 group_id: 无 selected group
- trigger top candidate: `grp-sing-thrombosis_prediction-1185`
- 实测 primitive: 无
- s0: `SELECT COUNT(*) FROM Laboratory l INNER JOIN Examination e ON l.ID = e.ID WHERE (l.SM IN ('-', '+-', 'negative', '0')) AND e.Thrombosis = 0`
- s1: 无，未进入 rewrite
- compiler.empty_reason_counts 相关条目: 无，未进入 compiler
- 假设偏离原因: trigger 阶段已被 `runtime_usable_false` / `required_contract_signals_missed` / `singleton_canonical_exact_failed` 挡住，没有到达 `SELECT_REPLACE_SLOT` compiler 阻断点。

### q1474 / debit_card_specializing

- 实际触发 group_id: 无 selected group
- trigger top candidate: `grp-sing-debit_card_specializing-1533`
- 实测 primitive: 无
- s0: `SELECT c."CustomerID" FROM "customers" AS c INNER JOIN "yearmonth" AS y ON c."CustomerID" = y."CustomerID" WHERE c."Currency" = 'CZK' AND y."Date" BETWEEN '201101' AND '201112' ORDER BY y."Consumption"`
- s1: 无，未进入 rewrite
- compiler.empty_reason_counts 相关条目: 无，未进入 compiler
- 假设偏离原因: trigger 阶段已被 `channel_q_missed` / `channel_s_missed` / `required_contract_signals_missed` / `singleton_canonical_exact_failed` 挡住，没有到达 `ADD_SELECT_SLOT` compiler 阻断点。

## 结论

- 严格符合假设: 3/6
- 部分符合假设: 3/6
- 不符合假设: 0/6

按本轮准则，“6 case 中 ≥5 符合假设”才自动继续 WUv2-3。严格计数只有 3/6，因此当前停在 gate，等待用户确认是否继续 WUv2-3。

### probe 2: q988 rewrite verify

commit: 84fce61

## q988 rewrite verification

验证入口: `cli/replay_runtime_rewrite_v2.py`

原始输出: `/tmp/wuv2_3_rewrite_probe/formula_1_q988.json`

| 维度 | v1 r1 | WUv2-3 rewrite replay |
|---|---|---|
| matched group | `grp-sing-formula_1-973` | `grp-sing-formula_1-970` |
| primitive | `SELECT_REPLACE_SLOT` | `INSERT_BRIDGE` |
| selected SQL / S0 | `SELECT d.forename, d.surname FROM drivers d JOIN pitStops p ON d.driverId = p.driverId ... GROUP BY d.driverId, d.forename, d.surname ...` | 同左 |
| final SQL / S1 | `SELECT drivers.driverId FROM drivers JOIN pitStops ON drivers.driverId = pitStops.driverId ... GROUP BY drivers.driverId ...` | `SELECT d.forename, d.surname FROM drivers d JOIN pitStops p ON d.driverId = p.driverId ... GROUP BY d.driverId, d.forename, d.surname ...` |
| final_correct | `False` | `True` |
| WUv2-2 disabled reason count | 未记录 | `1` |
| hint audit | 未记录 | `hint_introduces_non_primitive_action=false` |

观察:

- WUv2-3 quick probe 中 q988 的 `INSERT_BRIDGE` ready 不是实际退化；真实 rewrite replay 里 LLM 没改动 SQL，S1 保持 S0，执行结果与 gold 等价。
- WUv2-2 已切断旧的 `SELECT_REPLACE_SLOT` 退化路径；本次 `wuv2_2_disabled_reason_count=1`。
- q988 仍说明 singleton exact 可以让 `INSERT_BRIDGE` 路径进入 rewrite，但这次没有造成 regress；后续如果要处理“ready 但不应改”的边界，应归入 WUv2-5 applicability/negative guard，而不是在 WUv2-3/WUv2-4 打补丁。

### 结论

- WUv2-2 设计验证通过。
- 关键观察: q988 `INSERT_BRIDGE` 路径 `rewrite_status=equivalent`, 未 regress。
- 遗留: hint 越界(q292)归 WUv2-5。

## §2 WUv2-3 verification

### probe: 8-case quick verification

commit: e9800e7

# WUv2-3 quick probe(8 case)

Probe 入口: `cli/replay_runtime_trigger_v2.py`

起点提交:

- WUv2-2 probe: `82575c0`
- WUv2-3 implementation: `b8c4e5b`

执行边界:

- 使用 v1 full11 run 的 `final_library.json`
- 使用对应 DeepEye `.state/work/qid_*/eea_runtime_request.json`
- 只运行 runtime trigger -> compiler -> hint audit
- 不运行 SQL rewrite

原始输出:

- WUv2-2: `/tmp/wuv2_2_probe/*.json`
- WUv2-3: `/tmp/wuv2_3_probe/*.json`

## 与 WUv2-2 probe 对比

| qid | db | 类型 | WUv2-2 probe | WUv2-3 probe | 假设 | 符合? |
|---:|---|---|---|---|---|---|
| 366 | card_games | WUv2-2 阻断代表例 | `no_match`, 无 primitive | `no_match`, 无 primitive | 不应 regress；如果走 singleton 应被 strict/exact 挡 | 符合 |
| 1267 | thrombosis_prediction | WUv2-2 阻断代表例 | `no_match`, 无 primitive | `no_match`, 无 primitive | 不应 regress；如果走 singleton 应被 strict/exact 挡 | 符合 |
| 1474 | debit_card_specializing | WUv2-2 阻断代表例 | `no_match`, 无 primitive | `no_match`, 无 primitive | 不应 regress；如果走 singleton 应被 strict/exact 挡 | 符合 |
| 249 | toxicology | pattern helped 路径 | `ready`, `DROP_SELECT_SLOT`, pattern | `ready`, `DROP_SELECT_SLOT`, pattern | pattern 路径不受 WUv2-3 影响 | 符合 |
| 253 | toxicology | pattern helped 路径 | `ready`, `DROP_SELECT_SLOT`, pattern | `ready`, `DROP_SELECT_SLOT`, pattern | pattern 路径不受 WUv2-3 影响 | 符合 |
| 292 | toxicology | hint 越界基线 | `ready`, `MOVE_CONDITION`, singleton, hint 越界 | `ready`, `MOVE_CONDITION`, singleton, hint 越界 | 视 sing-205 是否 exact 通过；如果 exact 通过则仍 ready | 部分符合 |
| 1052 | european_football_2 | singleton 误触发 guard | 未跑 | `no_match`, 无 primitive | sing-1023 不再借 pre-condition 泛化，exact 失败 -> `no_match` | 符合 |
| 988 | formula_1 | singleton 误触发 guard | 未跑 | `ready`, `INSERT_BRIDGE`, singleton | sing-973 不再触发 -> `no_match` 或 saved-s0 | 不符合 |

## WUv2-2 vs WUv2-3 拦截层对比表

| qid | WUv2-2 probe 拦截层 | WUv2-3 probe 拦截层 | 变化 |
|---:|---|---|---|
| 366 | trigger: `runtime_usable_false` / `trigger_contract_missing_required_signals` / `singleton_canonical_exact_failed` | trigger: 同类原因；新增 `singleton_strict_audit` 显示 `singleton_pre_condition_matched=false`, `singleton_canonical_exact_passed=false` | 行为不变，audit 增强 |
| 1267 | trigger: `runtime_usable_false` / `required_contract_signals_missed` / `singleton_canonical_exact_failed` | trigger: 同类原因；新增 `singleton_strict_audit` 显示 `singleton_pre_condition_matched=false`, `singleton_canonical_exact_passed=false` | 行为不变，audit 增强 |
| 1474 | trigger: `channel_q_missed` / `channel_s_missed` / `required_contract_signals_missed` / `singleton_canonical_exact_failed` | trigger: 同类原因；新增 `singleton_strict_audit` 显示 `singleton_pre_condition_matched=false`, `singleton_canonical_exact_passed=false` | 行为不变，audit 增强 |
| 249 | compiler ready: pattern `grp-pat-toxicology-206-253-93286776`, `DROP_SELECT_SLOT` | compiler ready: 同 pattern, `DROP_SELECT_SLOT` | 不变 |
| 253 | compiler ready: pattern `grp-pat-toxicology-206-253-93286776`, `DROP_SELECT_SLOT` | compiler ready: 同 pattern, `DROP_SELECT_SLOT` | 不变 |
| 292 | compiler ready: singleton `grp-sing-toxicology-205`, `MOVE_CONDITION` + 1 个 WUv2-2 阻断 + hint 越界 | compiler ready: 同 singleton, `MOVE_CONDITION`; `singleton_strict_audit` 显示 `singleton_pre_condition_matched=true`, `singleton_canonical_exact_passed=true` | 不变，因为 exact 通过 |
| 1052 | 未跑 | trigger: `runtime_usable_false` / `required_contract_signals_missed` / `singleton_canonical_exact_failed`; strict audit false/false | 新增验证通过 |
| 988 | 未跑 | compiler ready: singleton `grp-sing-formula_1-970`, `INSERT_BRIDGE`; strict audit true/true | WUv2-3 未挡住，因为 singleton canonical exact 通过 |

## 关键观察

1. WUv2-3 实现生效: 所有 singleton candidate 都带 `singleton_strict_audit.singleton_strict_mode=wuv2_3_pre_condition_audit_only`。
2. q249 / q253 的 pattern 路径完全不变，说明 WUv2-3 没有误伤 pattern 泛化路径。
3. q1052 被挡在 trigger 层，符合“singleton 不靠 pre-condition 泛化”的方向。
4. q292 没被挡住的直接原因不是 pre-condition 放宽，而是 `singleton_canonical_exact_passed=true`。因此 WUv2-3 对它没有新增拦截作用。
5. q988 仍 ready，直接原因同样是 `singleton_canonical_exact_passed=true`，并且 primitive 是 `INSERT_BRIDGE`，不是用户描述里的 sing-973/name->driverId 路径。这个 case 说明当前 exact check 对某些 singleton 仍然认为可迁移。

## 结论

- 8 case 中符合假设: 6
- 部分符合假设: 1
- 不符合假设: 1
- singleton 误触发数下降: q1052 从预期旧误触发目标变为 `no_match`；q988 未下降。

本轮已完成 WUv2-3 quick probe。按任务要求，此处停止，等待用户 review；未进入 WUv2-4。

### 结论

- WUv2-3 设计验证通过。
- 8/8 没出现“应保留被阻断 / 应阻断被通过”的反向不符合；q292/q988 的剩余问题归后续 applicability / negative guard 层处理。

## §3 WUv2-4 verification

### probe: 7-pair unit bucket convergence

commit: 2cf8696

# WUv2-4 单元级 probe(retrieval bucket key 派生验证)

不跑 online；只验证 `derive_repair_card` + `_retrieval_keys_for_card` 在已知 v1 同源案例上的 bucket 行为。

说明: legacy full11 `final_library.json` 没有 WUv2-4 新字段，本 probe 用 final library 的 group 结构补 operation family，并优先用对应 `.state/work/qid_*/eea_update_request.json` 的 S0/gold SQL 重新派生 answer unit。

## case 对 bucket 汇合验证

| db | case 对 | v1 interface_key(选其一) | best primary key(count) | used shared bucket(count) | 汇合? | 备注 |
|---|---|---|---|---|---|---|
| codebase_community | 581/582 | `replace editor reference with owner reference preserving join scope` | `answer_unit_op:single_column->single_column|JOIN|bridge_path|no_output_shape_change (2)` | `answer_unit_op:single_column->single_column|JOIN|bridge_path|no_output_shape_change (2)` | 是 | manual formal pattern: editor -> Owner |
| codebase_community | 616/617 | `move predicate from reference table to primary entity` | `answer_unit_op:single_column->single_column|WHERE|condition|no_output_shape_change (2)` | `answer_unit_op:single_column->single_column|WHERE|condition|no_output_shape_change (2)` | 是 | manual formal pattern: comment time |
| codebase_community | 709/710 | `move predicate from joined table to source table` | `answer_unit_op:row_count->aggregate_value|WHERE|condition|replace_or_reorder_output_slot (2)` | `answer_unit_op:row_count->aggregate_value|WHERE|condition|replace_or_reorder_output_slot (2)` | 是 | manual formal pattern: comment score |
| financial | 141/180/193 | `replace answer slot preserving aggregate scope` | `answer_unit_op:single_column->single_column|SELECT|replace_or_reorder_output_slot|slot (1)` | `axis:output_shape_delta (3)` | 是 | manual formal pattern: ID output; expectation is at least two merge |
| european_football_2 | 1119/1120/1121 | `replace bridge path preserving metric` | `answer_unit_op:aggregate_value->aggregate_value|JOIN|bridge_path|no_output_shape_change (2)` | `axis:predicate_scope_delta (3)` | 是 | manual same-source group; expectation is at least two merge |
| european_football_2 | 1068/1093 | `replace avg with sum over count preserving scope` | `answer_unit_op:aggregate_value->aggregate_value|SELECT|metric|replace_or_reorder_output_slot (2)` | `answer_unit_op:aggregate_value->aggregate_value|SELECT|metric|replace_or_reorder_output_slot (2)` | 是 | manual formal pattern: AVG -> count-like formula correction |
| european_football_2 | 1038/1085 | `add missing metric slot preserving group order` | `answer_unit_op:single_column->paired_columns|SELECT|add_output_slot|slot (2)` | `answer_unit_op:single_column->paired_columns|SELECT|add_output_slot|slot (2)` | 是 | manual formal pattern pair used to fill the incomplete EF2 row in the task |

## 总结

- 7 个 case 组中汇合 7/7 个。
- 其中 exact `answer_unit_op:*` 主键汇合 6/7 个；其余依赖 `axis:*` 粗筛轴进入后续 semantic/program 判断。
- 未汇合 case 组: 无。
- 判定: 符合 quick probe 标准。

### 结论

- 7/7 人工同源组汇合，其中 6/7 通过 exact `answer_unit_op:*` bucket，1/7 通过 `axis:*` 粗筛轴进入后续 semantic/program 判断。
- 真实 online 效果待 r_v2_e。

## §4 WUv2-5 verification (未完成)

待 WUv2-5 完成后补充。

## §5 WUv2-6 verification (未完成)

待 WUv2-6 完成后补充。

## §6 r_v2_e 11 库全量验收(终结条件)

待 WUv2-5 / WUv2-6 完成后执行。
