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
