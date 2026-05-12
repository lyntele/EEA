# P0b case_signal_view 现状 audit

本报告只读当前实现，不新增信号抽取逻辑。输入 SQL 使用 r1 `selection.rewrite_only_selected_sql`，缺失时回退到 `result.s0_sql`。

## Anchor Summary

| qid | db | role | pred_sql_source | select_arity | distinct | count_star | count_distinct | join_count | predicate_count | grain |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---|
| q1172 | thrombosis_prediction | seed | selection.rewrite_only_selected_sql | 1 | False | True | False | 1 | 3 | scalar_aggregate |
| q1267 | thrombosis_prediction | regress_selected_s1 | selection.rewrite_only_selected_sql | 1 | False | False | True | 2 | 2 | scalar_aggregate |
| q1418 | student_club | seed | selection.rewrite_only_selected_sql | 1 | False | False | False | 0 | 1 | row_result |
| q268 | toxicology | helped_selected_s1 | selection.rewrite_only_selected_sql | 1 | True | False | False | 1 | 1 | row_result |
| q277 | toxicology | helped_selected_s1 | selection.rewrite_only_selected_sql | 1 | True | False | False | 1 | 1 | row_result |

## Observed Coverage

- `case_signal_view.pred_sql_view` 已经包含 `select_arity`、`output_shape_current.has_distinct`、`aggregate_profile.has_count_star`、`aggregate_profile.has_count_distinct`、`join_graph`、`predicate_profile.predicate_count/literal_count`、`group_order_profile`。
- 因此 P0b 需要的 distinct / aggregate function / count-star / join-count / predicate-count 这类 SQL 结构观测并不是完全缺失；它们已经在现有 `case_signal_view` 内部存在。
- q1172 与 q1267 已可由现有字段机械区分：q1172 是 `has_count_star=true / has_count_distinct=false / join_count=1`，q1267 的 r1 selected SQL 是 `has_count_star=false / has_count_distinct=true / join_count=2`。
- 注意：`output_shape_current.has_distinct` 不覆盖 `COUNT(DISTINCT ...)`，q1267 这里仍为 `false`；aggregate-level DISTINCT 必须读 `aggregate_profile.has_count_distinct`。
- 但是 `runtime_signature` 目前只带 `output_shape_current`、`tables_used`、schema resolvability 等摘要，不包含完整 `aggregate_profile` 和 `predicate_profile`；也没有一个去字面化的 flat fact set 可直接作为 hard gate。

## Gaps For P0b

- 缺的不是 AST 解析能力，而是从 `case_signal_view.pred_sql_view` 到 runtime hard-gate facts 的统一投影层。
- 不能直接把 `select_items`、`predicate_profile.predicates`、`join_graph.on_clause` 当 gate，因为这些字段携带表名、列名、alias、字面值。
- 需要扩展 `case_signal_view` 或其 runtime projection，生成统一、去字面化、可复算的 facts，例如 aggregate/count/distinct/select-arity/join-count/predicate-count/group-order 等结构事实。
- P0b 的 LLM 只能从 seed cases 的这些既有 facts 里选择共享 facts；不能发明 check_type，也不能把原始 SQL substring 作为 signal。

## Raw Summary JSON

```json
[
  {
    "qid": "1172",
    "db_id": "thrombosis_prediction",
    "role": "seed",
    "pred_sql_source": "selection.rewrite_only_selected_sql",
    "select_arity": 1,
    "select_items": [
      "COUNT(*)"
    ],
    "grain": "scalar_aggregate",
    "shape_has_distinct": false,
    "shape_has_aggregate": true,
    "tables_used": [
      "Laboratory",
      "Patient"
    ],
    "join_count": 1,
    "predicate_count": 3,
    "literal_count": 2,
    "comparison_operators": [
      "=",
      ">="
    ],
    "has_count": true,
    "has_count_star": true,
    "has_count_distinct": false,
    "has_sum": false,
    "has_avg": false,
    "group_by_count": 0,
    "order_by_count": 0
  },
  {
    "qid": "1267",
    "db_id": "thrombosis_prediction",
    "role": "regress_selected_s1",
    "pred_sql_source": "selection.rewrite_only_selected_sql",
    "select_arity": 1,
    "select_items": [
      "COUNT(DISTINCT Patient.ID)"
    ],
    "grain": "scalar_aggregate",
    "shape_has_distinct": false,
    "shape_has_aggregate": true,
    "tables_used": [
      "Examination",
      "Laboratory",
      "Patient"
    ],
    "join_count": 2,
    "predicate_count": 2,
    "literal_count": 5,
    "comparison_operators": [
      "=",
      "IN"
    ],
    "has_count": true,
    "has_count_star": false,
    "has_count_distinct": true,
    "has_sum": false,
    "has_avg": false,
    "group_by_count": 0,
    "order_by_count": 0
  },
  {
    "qid": "1418",
    "db_id": "student_club",
    "role": "seed",
    "pred_sql_source": "selection.rewrite_only_selected_sql",
    "select_arity": 1,
    "select_items": [
      "type"
    ],
    "grain": "row_result",
    "shape_has_distinct": false,
    "shape_has_aggregate": false,
    "tables_used": [
      "event"
    ],
    "join_count": 0,
    "predicate_count": 1,
    "literal_count": 1,
    "comparison_operators": [
      "="
    ],
    "has_count": false,
    "has_count_star": false,
    "has_count_distinct": false,
    "has_sum": false,
    "has_avg": false,
    "group_by_count": 0,
    "order_by_count": 0
  },
  {
    "qid": "268",
    "db_id": "toxicology",
    "role": "helped_selected_s1",
    "pred_sql_source": "selection.rewrite_only_selected_sql",
    "select_arity": 1,
    "select_items": [
      "a1.element"
    ],
    "grain": "row_result",
    "shape_has_distinct": true,
    "shape_has_aggregate": false,
    "tables_used": [
      "atom",
      "connected"
    ],
    "join_count": 1,
    "predicate_count": 1,
    "literal_count": 1,
    "comparison_operators": [
      "="
    ],
    "has_count": false,
    "has_count_star": false,
    "has_count_distinct": false,
    "has_sum": false,
    "has_avg": false,
    "group_by_count": 0,
    "order_by_count": 0
  },
  {
    "qid": "277",
    "db_id": "toxicology",
    "role": "helped_selected_s1",
    "pred_sql_source": "selection.rewrite_only_selected_sql",
    "select_arity": 1,
    "select_items": [
      "a1.element"
    ],
    "grain": "row_result",
    "shape_has_distinct": true,
    "shape_has_aggregate": false,
    "tables_used": [
      "atom",
      "connected"
    ],
    "join_count": 1,
    "predicate_count": 1,
    "literal_count": 1,
    "comparison_operators": [
      "="
    ],
    "has_count": false,
    "has_count_star": false,
    "has_count_distinct": false,
    "has_sum": false,
    "has_avg": false,
    "group_by_count": 0,
    "order_by_count": 0
  }
]
```
