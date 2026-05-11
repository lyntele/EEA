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
