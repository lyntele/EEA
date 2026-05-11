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
