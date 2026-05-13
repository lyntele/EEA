# Retrieval Root Evidence 修补计划 (codex 执行)

本计划面向 `rulebook/common/learning/pattern_formation.py` 的 retrieval / pair
recall 缺陷。所有事实在以下文档与代码中已直接复核完毕（包括我此前未读、用户要求
我补读的部分）：

- audit doc:
  - `rulebook/doc/pair_recall_signal_audit.md`
  - `rulebook/doc/manual_pattern_retrieval_coverage.md`
  - `rulebook/doc/runtime_gate_breakdown.md`
  - `rulebook/doc/pattern_clustering_signal_audit.md`
- 实现文档：`rulebook/doc/current_implementation_overview.md`（完整通读；存在与代
  码不一致处见 §0.A）
- 源码段（全部直读完整函数体）：
  - `common/learning/pattern_formation.py` 行 85-95 (`_signal_payload`)、333-376
    (`_evolution_card` / `_retrieval_keys_for_card`)、388-442
    (`_build_retrieval_index` / `_candidate_pair_keys`)、570-614
    (`_broad_retrieval_reasons`)、840-947 (`_program_core_signature`)、1018-1051
    (`_shared_program_pair_compatibility`)、1054-1090 (`_pair_score_cache_key`)、
    1093-1207 (`score_pair`)、1666-1723 (`_coherent_components` /
    `_component_program_coherent`)、2389-2589 (`_call_insight_pattern_slicer` /
    `_call_pattern_admission_judge`)、3461-3595 (`_pair_supports_root_membership`
    / `_root_membership_closure`)、3970-4014 (`_build_pattern_candidate`)、
    4122-4427 (`_build_pattern_admission_candidates`)、4810-4900 (report
    structure)
  - `common/analysis/signal_summary.py` 行 1-280 (compaction helpers)、342-373
    (`_pred_current_summary`)、376-398 (`_delta_summary`)、401-442
    (`build_formation_signals`)
  - `common/analysis/repair_program_normalizer.py` 行 1480-1532 (CanonicalRepairIR
    构造，确认 `source_role_graph` / `target_role_graph` 是 full dict，未压缩)
  - `common/analysis/role_graph_normalizer.py` 行 214-263 (`_relation_roles`)、
    645-668 (role_graph 返回结构)
  - `common/learning/accumulate.py` 行 280-355 (`error_instance_to_singleton` 中
    formation_signals 构造顺序)
  - `tests/test_canonical_program.py` 行 947-963 (现有 `build_formation_signals`
    测试不带 `error_instance`，新增 helper 必须容忍 `None`)
- 4 库 47 case 原始 dump（人工 8 个 pattern 全覆盖）:
  `rulebook/workspace/probes/pattern_clustering_signal_audit/<db>/q<id>.json`
- ground truth: `rulebook/scripts/probes/manual_pattern_ground_truth.json`（35
  formal pattern）

不修改 `runtime/`、`llm/prompts/`、`accumulate.py` 的失败 streak 逻辑、admission
prompt、numeric pair score 权重、execution-only pred tags 过滤、experience_families
策略、card_games promotion 阈值。

## 0. 关键事实摘要（每条都已直读源码）

### 0.A `current_implementation_overview.md` 与代码的不一致

L480-486 描述 `insight_pattern_slicer.py` 与 `_call_insight_pattern_slicer` 被从
`pattern_formation.py` 调用。**实测：**

- `_call_insight_pattern_slicer`（`pattern_formation.py:2389`）只有定义，无任何
  call site
- `_slicer_candidate_case_sets`（`pattern_formation.py:4030`）只有定义，无任何
  call site
- `_build_pattern_admission_candidates:4168` 调用 `_call_pattern_admission_judge`
  时硬编 `slicer_candidate=None`
- 报告字段 `pattern_candidate_generation_policy`（`pattern_formation.py:4832-4835`）
  自我承认 `"insight slicer is disabled"`，`insight_slicer_candidates=[]` 与
  `component_splits=[]` 永远为空

实现总览文档对 slicer/coherent_components 状态描述与代码不符。本计划以代码为准。

### 0.B 现有 retrieval / pair 通路

1. `_retrieval_keys_for_card`（`pattern_formation.py:354-376`）只生成两类 bucket:
   - `(db_id, "answer_unit_op:<source_kind>-><target_kind>|<ops_signature>")`
   - `(db_id, "axis:<delta_axis>")`
2. `_evolution_card`（`pattern_formation.py:333-351`）当前字段：`group_id /
   version / case_ids / db_id / repair_card / effect_core / delta_axes /
   shape_key / lowering_families / repair_insight_interface`。**无任何 role_graph
   衍生字段**。
3. `_pair_score_cache_key`（`pattern_formation.py:1084-1090`）仅 hash
   `_evolution_card` 序列化。所以新增 `_evolution_card` 字段会自动驱动 cache 失效
   （只需保证新字段是 JSON-serialisable）。
4. `_broad_retrieval_reasons`（`pattern_formation.py:570-614`）emit 5 个 reason:
   `shared_effect_axis / shared_output_shape_delta / shared_action_lowering_family
   / shared_slot_kind / shared_repair_insight_interface`。**永不 emit**
   `shared_primary_repair_locus`、
   `shared_root_effect_axis_with_same_target_invariant_family`。
5. `_pair_supports_root_membership`（`pattern_formation.py:3461-3487`）`compatible`
   pair 直接通过；`partial` pair 要求 `shared_primary_repair_locus` 或
   `shared_root_effect_axis_with_same_target_invariant_family` —— 即 §0.B.4 提到
   的两个 reason。**因此 `partial → root` 通路 100% 死代码**。
6. `_shared_program_pair_compatibility`（`pattern_formation.py:1018-1051`）
   `compatible=True` 要求：`synthesize_shared_program(...)` 返回 program、
   `compile_coverage >= 1.0`、`mean_action_count <= 3.0`、无 blockers、
   `effect_signature_count > 0`。`score_pair`（`pattern_formation.py:1144-1163`）
   还在 `compatible` 上再叠加 `_program_core_signature(left) ==
   _program_core_signature(right)`；不等则改 `compatible=False` 并加
   `core_program_signature_conflict` blocker。
7. `_root_membership_closure`（`pattern_formation.py:3490-3595`）在 admission_judge
   返回后机械扩展 accepted members：依赖
   `_pair_supports_root_membership(pair, left=group, right=seed)`。**所以激活
   `partial → root` 通路对它也有效，会带来 admission 后的机械纳入扩展**。
8. `_build_pattern_admission_candidates:4329-4374` 已有 **branched admission**：
   若 `core_signature_branch_coverage.has_core_signature_conflict=True` 且 branch_specs
   覆盖所有 admitted cases 且每个 member 都有 effect 证据，仍 admit 为 branched
   pattern。**问题不在 admission，是 pair 级 `core_signature 相等` 检查把这类
   pair 卡在 candidate 形成之前**。

### 0.C 源数据可用性（CanonicalRepairIR 不压缩前）

- `error_instance.canonical_repair_ir.source_role_graph` /
  `target_role_graph`：完整 Dict[str, Any]（`repair_program_normalizer.py:1516-1520`），
  含 `alias_path_roles / table_relation_roles / output_refs / predicate_refs /
  equality_relations` 全部字段（`role_graph_normalizer.py:645-668`）。
- `error_instance.canonical_repair_ir.target_invariants`：List[str]，含
  `target_relation_equality=...`、`target_added_relation_equality=...`、
  `target_output_arity=...`、`target_output_roles=...` 等。
- `compact_canonical_repair_ir_for_memory`（`signal_summary.py:194-219`）只
  保留 `{source/target}_role_graph.output_shape`，所以 retrieval evidence 必须在
  IR 压缩**之前**抽取并独立写入 `formation_signals["retrieval_evidence"]`。

### 0.D 时序

`accumulate.error_instance_to_singleton`（`accumulate.py:280-355`）实际顺序：

```
build_formation_signals(case_signal_view, delta_signature, error_instance)
   # 此时 error_instance.canonical_repair_ir 还没附上
   ↓
formation_signals = {...}   # 不含 retrieval_evidence
   ↓
attach_canonical_repair_ir(error_instance, ..., formation_signals=formation_signals)
   # 这一步给 error_instance.canonical_repair_ir 赋值
   ↓
formation_signals["canonical_repair_ir"] = compact_canonical_repair_ir_for_memory(
    error_instance.canonical_repair_ir
)   # IR 压缩，丢掉 role_graph 大部分内容
   ↓
formation_signals["repair_insight_signature"] = ...
formation_signals["synthesized_program"] = ...
formation_signals["program_coverage"] = ...
formation_signals["repair_card"] = derive_repair_card(error_instance, ...)
```

所以新 helper `_compact_retrieval_evidence(error_instance)` 必须在 `accumulate.py`
的 `attach_canonical_repair_ir` 之后、`compact_canonical_repair_ir_for_memory`
之前调用，从 `error_instance.canonical_repair_ir` 的全量
`source_role_graph` / `target_role_graph` / `target_invariants` 取数。

### 0.E 量化目标（已用 dump 实测，见 §5 表 5.A）

5 个新 retrieval key 在 4 库 47 case dump 上对 8 个人工 pattern 的 pair 覆盖：

| pattern | cases | 当前 pair_covered | 新 key 后 pair_covered | 全 case 共享 key 数 |
|---|---:|---:|---:|---:|
| codebase editor_to_owner_user | 2 | 0/1 | **1/1** | 1 |
| codebase user_post_via_posthistory | 7 | 0/21 | **21/21** | 1 |
| formula_1 circuit_info_url | 3 | 0/3 | **3/3** | 3 |
| formula_1 driver_standings_path | 7 | 0/21 | **21/21** | 5 |
| toxicology bond_pair_to_connected_single | 8 | 0/28 | **28/28** | 3 |
| toxicology bond_condition_to_molecule_scope | 10 | 0/45 | 33/45 | 0 (8/10 共享 1 key) |
| card_games legalities | 4 | 0/6 | 3/6 | 0 (3/4 共享 3 key) |
| card_games named_card_anchor_to_set | 5 | 0/10 | 2/10 → 5/10* | 0 (5/5 共享 predicate_role) |

*：named_card 的 `predicate_role:primary name` 在 5/5 case 中都出现（4 case
predicate_refs 含 `cards.name`，第 5 case q463 的 `cards.name` 出现在 pred_predicate_refs），
但需要把 pred ∪ gold 一起做 predicate_role；§3.5 已按这个规则。

## 1. 范围与原则

- 只改 `rulebook/common/learning/pattern_formation.py`、
  `rulebook/common/analysis/signal_summary.py`、
  `rulebook/common/learning/accumulate.py` 三个文件，加一个一次性 read-only probe
  脚本，按需加一个最小回归断言。不动 runtime/llm/prompt 或 promotion。
- 所有新字段、新 key、新 reason 从既有 `error_instance.canonical_repair_ir.
  {source_role_graph, target_role_graph, target_invariants}` 与
  `repair_skeleton.structural.locus` 取数，**不引入 db-specific 静态词表**。
- 不删除现有 retrieval key、broad reason、numeric score 权重。
- 不修改 `compact_canonical_repair_ir_for_memory` 的输出 schema（避免 backward
  compat 风险），retrieval evidence 单独写到 `formation_signals["retrieval_evidence"]`。

## 2. 修复目标层次

| 优先级 | Step | 修复对象 |
|---|---|---|
| P0（必做） | Step 1 | 把 retrieval evidence wire 进 `formation_signals` |
| P0（必做） | Step 2 | `_retrieval_keys_for_card` 增加 5 类 root key |
| P0（必做） | Step 3 | `_broad_retrieval_reasons` 增加 7 个 reason（含 §0.B.4 的死代码两个） |
| P0（必做） | Step 6 | `score_pair` 分级处理 core_signature 不等的 root-aligned pair |
| P1（按 5.B 验收后决定） | Step 4 | `_shape_broad_overlap` 收紧 |
| P1（按 5.B 验收后决定） | Step 5 | 激活 `_coherent_components` 作 component pre-split |

理由：P0 4 步覆盖根因（retrieval 维度薄 + 死代码 + 核心 signature 强相等）。P1 两
步处理"过宽"与"过大 component"，但当前 candidate 数量普遍不大（11 库 admission
candidate 总数 151，平均 ≤20/库），强行加 P1 可能回退现有 5 个 2-case 成功 case。

## 3. 实施步骤

### Step 1 — 加 `_compact_retrieval_evidence`，wire 进 `formation_signals`

**文件**: `rulebook/common/analysis/signal_summary.py`（新增 helper） +
`rulebook/common/learning/accumulate.py`（新增一次调用）

#### 3.1.1 新 helper（在 `signal_summary.py` 中，放在
`compact_canonical_repair_ir_for_memory` 之后，`build_formation_signals` 之前）

```python
def _compact_retrieval_evidence(error_instance: Optional[ErrorInstanceV2]) -> Dict[str, Any]:
    """Project full role_graph + target_invariants into compact retrieval-side fields.

    Read directly from `error_instance.canonical_repair_ir.{source,target}_role_graph`
    and `.target_invariants`.  Output is JSON-serialisable; pair-score cache hashes
    it as part of the evolution_card.
    """
    if error_instance is None:
        return {"schema_version": "retrieval-evidence-v0"}
    ir = _payload(getattr(error_instance, "canonical_repair_ir", None))
    if not ir:
        return {"schema_version": "retrieval-evidence-v0"}
    source_rg = _payload(ir.get("source_role_graph"))
    target_rg = _payload(ir.get("target_role_graph"))
    target_invariants = list(ir.get("target_invariants") or [])
    return {
        "schema_version": "retrieval-evidence-v0",
        "gold_join_edges": _norm_join_edges(target_rg),
        "pred_join_edges": _norm_join_edges(source_rg),
        "gold_only_tables": _table_diff(target_rg, source_rg),
        "pred_only_tables": _table_diff(source_rg, target_rg),
        "target_output_role": _normalized_output_role(target_rg),
        "source_output_role": _normalized_output_role(source_rg),
        "target_relation_equalities": _target_relation_equalities(target_rg, target_invariants),
        "predicate_column_roles": _predicate_roles(source_rg, target_rg),
        "primary_repair_locus": _primary_repair_locus(error_instance),
    }
```

#### 3.1.2 子归一化函数（同文件，全部 grounding 在 §0.C 数据）

```python
_DIRTY_TOKEN_CHARS = ("(", ")", "=", " ")


def _is_clean_table_name(value: str) -> bool:
    if not value:
        return False
    if "unknown" in value.lower():
        return False
    return not any(ch in value for ch in _DIRTY_TOKEN_CHARS)


def _normalize_join_edge(raw: str) -> Optional[str]:
    """Strip `join_path:` prefix, lower-case, alphabetise both sides of `=`.

    Examples:
      "join_path:bond.molecule_id=atom.molecule_id" -> "atom.molecule_id=bond.molecule_id"
      "join_path:A=B:output:X" -> drop (not a pure join_path; handled by caller)
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text.startswith("join_path:"):
        return None
    payload = text[len("join_path:"):]
    if ":output:" in payload:
        return None  # output-side path role, not a pure join edge
    if "=" not in payload:
        return None
    left, right = payload.split("=", 1)
    left = left.strip().lower()
    right = right.strip().lower()
    if not left or not right:
        return None
    return f"{left}={right}" if left < right else f"{right}={left}"


def _norm_join_edges(role_graph: Dict[str, Any]) -> List[str]:
    apr = role_graph.get("alias_path_roles") or {}
    edges: set[str] = set()
    for values in apr.values():
        if not isinstance(values, list):
            continue
        for item in values:
            edge = _normalize_join_edge(item)
            if edge:
                edges.add(edge)
    return sorted(edges)


def _table_diff(have_rg: Dict[str, Any], other_rg: Dict[str, Any]) -> List[str]:
    have_keys = {
        key.strip().lower()
        for key in (have_rg.get("table_relation_roles") or {}).keys()
        if isinstance(key, str) and _is_clean_table_name(key)
    }
    other_keys = {
        key.strip().lower()
        for key in (other_rg.get("table_relation_roles") or {}).keys()
        if isinstance(key, str) and _is_clean_table_name(key)
    }
    return sorted(have_keys - other_keys)


def _normalized_output_role(role_graph: Dict[str, Any]) -> str:
    refs = role_graph.get("output_refs") or []
    if not refs:
        return ""
    first = _payload(refs[0])
    role = str(first.get("column_role") or "").strip().lower()
    if not role or role == "unknown":
        return ""
    return role


def _target_relation_equalities(
    target_rg: Dict[str, Any],
    target_invariants: List[Any],
) -> List[str]:
    out: set[str] = set()
    # Primary source: pre-canonicalised equality_relations[*].canonical_key
    for relation in target_rg.get("equality_relations") or []:
        payload = _payload(relation)
        key = str(payload.get("canonical_key") or "").strip().lower()
        if not key or "=" not in key:
            continue
        left, right = key.split("=", 1)
        if not left or not right:
            continue
        out.add(f"{left}={right}" if left < right else f"{right}={left}")
    # Fallback: target_invariants strings prefixed with target_(added_)relation_equality=
    for inv in target_invariants or []:
        text = str(inv or "").strip()
        for prefix in ("target_relation_equality=", "target_added_relation_equality="):
            if text.startswith(prefix):
                payload = text[len(prefix):].strip().lower()
                if "=" not in payload:
                    continue
                left, right = payload.split("=", 1)
                if not left or not right:
                    continue
                out.add(f"{left}={right}" if left < right else f"{right}={left}")
                break
    return sorted(out)


def _predicate_roles(source_rg: Dict[str, Any], target_rg: Dict[str, Any]) -> List[str]:
    roles: set[str] = set()
    for rg in (source_rg, target_rg):
        for ref in rg.get("predicate_refs") or []:
            payload = _payload(ref)
            role = str(payload.get("column_role") or "").strip().lower()
            if role and role != "unknown":
                roles.add(role)
    return sorted(roles)


def _primary_repair_locus(error_instance: Optional[ErrorInstanceV2]) -> str:
    if error_instance is None:
        return ""
    skeleton = getattr(error_instance, "repair_skeleton", None)
    structural = getattr(skeleton, "structural", None) if skeleton is not None else None
    locus = getattr(structural, "locus", None) if structural is not None else None
    locus_value = getattr(locus, "value", locus) if locus is not None else ""
    return str(locus_value or "").strip().lower()
```

#### 3.1.3 在 `accumulate.error_instance_to_singleton` 中调用

`accumulate.py` 当前结构（§0.D 已复核）：第 305 行 `attach_canonical_repair_ir`
之后、第 311 行 `compact_canonical_repair_ir_for_memory` 调用之前 / 之后均可——
推荐放在 311 之后，与 `repair_card` 同层（第 337 行附近）。

```python
# in error_instance_to_singleton, after the existing block that writes
# canonical_repair_ir / repair_insight_signature / synthesized_program /
# program_coverage / repair_card into formation_signals
formation_signals["retrieval_evidence"] = _compact_retrieval_evidence(error_instance)
```

需要把 `_compact_retrieval_evidence` 从 `signal_summary.py` 导入。

#### 3.1.4 `build_formation_signals` 也调一次（兼容老测试）

`tests/test_canonical_program.py:955` 用 `build_formation_signals(case_signal_view
=case_view.case_signal_view)` 不带 `error_instance` 跑通。`_compact_retrieval_evidence(None)`
返回 `{"schema_version": ...}` 不会破坏现有 assert。同时在 `build_formation_signals`
的 return dict 里加：

```python
"retrieval_evidence": _compact_retrieval_evidence(error_instance),
```

让 `error_instance=None` 时返回最小 schema 即可；线上 `error_instance` 已附 IR
的路径在 `accumulate` 里覆盖更完整的内容（见 §3.1.3）。

### Step 2 — `_retrieval_keys_for_card` 增 5 类 root key

**文件**: `rulebook/common/learning/pattern_formation.py`

#### 3.2.1 `_evolution_card` 增 `retrieval_evidence` 字段

`pattern_formation.py:333-351`，在 return dict 末尾加：

```python
"retrieval_evidence": dict((_signal_payload(group).get("retrieval_evidence") or {})),
```

这一字段会自动进 `_pair_score_cache_key`（§0.B.3），等价于"加新维度后 cache 自动失
效"，无需另外清 cache。

#### 3.2.2 `_retrieval_keys_for_card` 加 5 类 key

`pattern_formation.py:354-376` 保留两类既有 key，**追加**：

```python
evidence = _model_dump(card.get("retrieval_evidence") or {})

for edge in evidence.get("gold_join_edges") or []:
    if edge:
        keys.add((db_id, f"gold_edge:{edge}"))

role = str(evidence.get("target_output_role") or "").strip()
if role:
    keys.add((db_id, f"target_role:{role}"))

for eq in evidence.get("target_relation_equalities") or []:
    if eq:
        keys.add((db_id, f"target_eq:{eq}"))

for table in evidence.get("gold_only_tables") or []:
    if table:
        keys.add((db_id, f"gold_only_table:{table}"))

for predicate_role in evidence.get("predicate_column_roles") or []:
    if predicate_role:
        keys.add((db_id, f"predicate_role:{predicate_role}"))
```

`_retrieval_key_reason`（`pattern_formation.py:383-385`）按 `:` 前缀分类，新前缀自
动归类，不需要改。

`_retrieval_audit`（同文件 `:456` 起）的 reason buckets 会自动新增 5 类，已存
在的 audit 报告字段 schema 不受影响（dict 自然扩展）。

### Step 3 — `_broad_retrieval_reasons` 发出 7 个新 reason

**文件**: `rulebook/common/learning/pattern_formation.py:570-614`

修改函数签名（向后兼容；现有调用点 `pattern_formation.py:1131-1141` 只传位置参数，
后跟 keyword-only 参数）：

```python
def _broad_retrieval_reasons(
    left: GroupSummary,
    right: GroupSummary,
    *,
    signal_axes_overlap: float,
    shape_compat: float,
    legacy_compat: float,
    slot_overlap: float,
    question_overlap: float,
    manifest_overlap: float,
    structural_compat: float,
) -> Tuple[str, ...]:
    reasons: Set[str] = set()
    # ... existing 5 reasons unchanged ...

    left_ev = _signal_payload(left).get("retrieval_evidence") or {}
    right_ev = _signal_payload(right).get("retrieval_evidence") or {}

    left_role = str(left_ev.get("target_output_role") or "").strip()
    right_role = str(right_ev.get("target_output_role") or "").strip()
    if left_role and left_role == right_role:
        reasons.add("shared_target_role")

    if set(left_ev.get("gold_join_edges") or []) & set(right_ev.get("gold_join_edges") or []):
        reasons.add("shared_target_route")

    if set(left_ev.get("gold_only_tables") or []) & set(right_ev.get("gold_only_tables") or []):
        reasons.add("shared_gold_only_table")

    if set(left_ev.get("target_relation_equalities") or []) & set(
        right_ev.get("target_relation_equalities") or []
    ):
        reasons.add("shared_target_invariant_family")

    if set(left_ev.get("predicate_column_roles") or []) & set(
        right_ev.get("predicate_column_roles") or []
    ):
        reasons.add("shared_predicate_anchor")

    # Activate the two dead-code reasons referenced by
    # _pair_supports_root_membership (`pattern_formation.py:3483-3487`).
    left_locus = str(left_ev.get("primary_repair_locus") or "")
    right_locus = str(right_ev.get("primary_repair_locus") or "")
    if left_locus and left_locus == right_locus:
        reasons.add("shared_primary_repair_locus")

    if _signal_axes(left) & _signal_axes(right) and (
        set(left_ev.get("target_relation_equalities") or [])
        & set(right_ev.get("target_relation_equalities") or [])
    ):
        reasons.add("shared_root_effect_axis_with_same_target_invariant_family")

    _ = (
        signal_axes_overlap, shape_compat, legacy_compat,
        question_overlap, manifest_overlap, structural_compat, slot_overlap,
    )
    return tuple(sorted(reasons))
```

#### Step 3 的双重效果（已直读源码确认）

1. `_pair_supports_root_membership`（`pattern_formation.py:3461-3487`）的 `partial
   → root` 升级条件依赖 `shared_primary_repair_locus` 与
   `shared_root_effect_axis_with_same_target_invariant_family`。Step 3 emit 这两
   个 reason 后此通路立刻激活。
2. `_root_membership_closure`（`pattern_formation.py:3490-3595`）在 admission 之
   后机械纳入 root-compatible members：调用 `_pair_supports_root_membership(pair,
   left=group, right=seed)`。Step 3 同时提高这一步的纳入率。

注意：现有 `_pair_supports_root_membership` 只承认 `compatible` 与 `partial`；本
计划不再扩展它的 strong_reasons 集合（保持代码本身现有的 2 个条件名称即可，因为
broad reason 端会发出这两个名字）。如果后续观察到大量 pair 卡在 `partial` 状态
且仅靠 Step 3 不够，再单独评估是否扩展 strong_reasons。

### Step 6 — `score_pair` 分级处理 root-aligned 但 core_signature 异构 pair

**文件**: `rulebook/common/learning/pattern_formation.py:1144-1163`

现状（`score_pair` 内）：

```python
if veto is None and broad_retrieval_reasons:
    (program_compatible, program_blockers, ...) = _shared_program_pair_compatibility(left, right)
    if program_compatible and shared_program_basis != "effect":
        program_compatible = False
        program_blockers = (*program_blockers, "missing_effect_backed_shared_program")
    if program_compatible and _program_core_signature(left) != _program_core_signature(right):
        program_compatible = False
        program_blockers = (*program_blockers, "core_program_signature_conflict")
elif veto is None:
    program_blockers = ("no_broad_retrieval_signal",)
```

**改法**：

```python
if veto is None and broad_retrieval_reasons:
    (program_compatible, program_blockers, ...) = _shared_program_pair_compatibility(left, right)
    if program_compatible and shared_program_basis != "effect":
        program_compatible = False
        program_blockers = (*program_blockers, "missing_effect_backed_shared_program")
    if program_compatible and _program_core_signature(left) != _program_core_signature(right):
        # Root-aligned but core_signature differs → keep candidate alive as a
        # branch-axis pair; downstream admission already supports branched patterns
        # at pattern_formation.py:4329-4374 when branch_specs cover all members and
        # every member carries effect evidence.
        root_reasons = {
            "shared_target_role",
            "shared_target_route",
            "shared_gold_only_table",
            "shared_target_invariant_family",
            "shared_primary_repair_locus",
            "shared_root_effect_axis_with_same_target_invariant_family",
        }
        if root_reasons & set(broad_retrieval_reasons):
            program_blockers = (*program_blockers, "branch_axis_pair_core_signature_differs")
            # program_compatible stays True → branchable_for_pattern=True
        else:
            program_compatible = False
            program_blockers = (*program_blockers, "core_program_signature_conflict")
elif veto is None:
    program_blockers = ("no_broad_retrieval_signal",)
```

#### Step 6 的下游影响（已直读代码确认）

1. `branchable_for_pattern = bool(broad_retrieval_reasons) and semantic_relation
   in {"compatible", "partial"}`（`pattern_formation.py:1176-1179`）。
   `_pair_semantic_relation`（`pattern_formation.py:740-761`）由 `accepted`
   决定；保持 `program_compatible=True` 则 `accepted=True`、`semantic_relation
   ="compatible"`、edge 进入 candidate 集。
2. `_pair_supports_root_membership` 接收 `compatible` pair 直接通过
   （`pattern_formation.py:3479-3480`），不依赖额外 strong_reasons。
3. 进入 component 后，`_call_pattern_admission_judge` 的 prompt 已携带每个 case
   的 program core 与 branch evidence；admission_response 若把 `branch_specs`
   覆盖所有 admitted cases 且每个 member 有 effect 证据，
   `_build_pattern_admission_candidates:4344-4360` 仍 admit 为 branched pattern。
4. `_taxonomy_from_blockers`（`pattern_formation.py:985-1015`）当前对
   `branch_axis_pair_core_signature_differs` 无显式分类；它会落入"不匹配既有 label"
   且 `program_compatible=True` 时 not labeled。需要在 `_taxonomy_from_blockers`
   中新增一行：

   ```python
   if "branch_axis_pair_core_signature_differs" in blocker_text:
       labels.add("branch_axis_pair")
   ```

   仅作 audit 分类用，不改决策。

## 4. P1 步骤（按 5.B 验收数据决定是否做）

### Step 4 — `_shape_broad_overlap` 收紧（仅在 mixed candidate 仍多时）

**文件**: `rulebook/common/learning/pattern_formation.py:554-567`

仅在 §5.B 验收 mixed candidate > 3 时执行：

```python
def _shape_broad_overlap(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    if not left or not right:
        return False
    op_match = bool(left.get("operation")) and left.get("operation") == right.get("operation")
    grain_match = (
        (left.get("current_grain") and left.get("current_grain") == right.get("current_grain"))
        or (left.get("target_grain") and left.get("target_grain") == right.get("target_grain"))
    )
    return bool(op_match and grain_match)
```

注：旧版"任一字段或 role 子集"会发 `shared_output_shape_delta`；新版要求
operation match AND grain match。这是当前 5 个完整 2-case candidate（student_club
budget / formula pitstops / toxicology bidirect / codebase comments x2）都满足
operation+grain 双匹配的情况，但需要 5.B 数据复核确认不回退。

### Step 5 — 激活 `_coherent_components` 做 component pre-split（仅在大 component 多时）

**文件**: `rulebook/common/learning/pattern_formation.py`

仅在 §5.B 验收 mean component size > 5 时执行：

在 `_build_pattern_admission_candidates:4148-4150` 处加：

```python
seen_case_sets: Set[Tuple[str, ...]] = set()
component_splits_audit: List[Dict[str, Any]] = []
for component in uf.components():
    if len(component) < 2:
        continue
    # Pre-split only large components by all-pairs root coherence.
    if len(component) >= 5:
        coherent = _coherent_components(component, pair_scores, by_id)
        component_splits_audit.append({
            "component_group_ids": sorted(component),
            "split_group_ids": [sorted(sub) for sub in coherent],
        })
        for sub in coherent:
            if len(sub) >= 2:
                _process_component(sub, ...)
        continue
    _process_component(component, ...)
# 把 component_splits_audit 写回 report 4880 行的 `component_splits`
```

需要把当前 4151-4426 行的 component 处理体抽成 `_process_component(component_ids,
pair_scores, by_id, seen_case_sets, reports, patterns)` 局部函数。

这一步是死代码激活；做之前先用 5.B 数据评估收益。

## 5. 验收 gate

### 5.A 单元验证 retrieval key（Step 1+2 完成后立刻跑）

新增 `rulebook/scripts/probes/retrieval_key_pair_coverage.py`（**read-only，
不写线上路径**）。骨架：

```python
"""Read-only validator for new retrieval keys against manual ground truth."""
from method.EEA.rulebook.common.analysis.signal_summary import _compact_retrieval_evidence
# ... load workspace/probes/pattern_clustering_signal_audit/<db>/q<id>.json,
#    reconstruct an `ErrorInstanceV2`-like object (or use the saved
#    canonical_repair_ir directly), call _compact_retrieval_evidence(...),
#    then compute the 5 retrieval keys exactly as _retrieval_keys_for_card does
#    (db_id, "gold_edge:<edge>") etc., and report per-manual-pattern pair coverage.
#
# Compare against rulebook/scripts/probes/manual_pattern_ground_truth.json.
```

**通过条件**（来自 §0.E 实测，不达标停在 Step 1+2 排查）:

| pattern | cases | pair coverage 目标(≥) |
|---|---:|---:|
| codebase editor_to_owner_user | 2 | 1/1 |
| codebase user_post_via_posthistory | 7 | 18/21 |
| formula_1 circuit_info_url | 3 | 3/3 |
| formula_1 driver_standings_path | 7 | 18/21 |
| toxicology bond_pair_to_connected_single | 8 | 28/28 |
| toxicology bond_condition_to_molecule_scope | 10 | 30/45 |
| card_games legalities | 4 | 3/6 |
| card_games named_card_anchor_to_set | 5 | 4/10 |

### 5.B 全 11 库 admission coverage（Step 1-3+6 完成后跑）

重跑 `rulebook/cli/run_multidb_validation.py --stage trigger`（manifest:
`rulebook/doc/multidb_quick_validation_manifest.json`），重新生成
`r_v2_e_p0b_v6_*` 工作目录，并复用 Agent2 的脚本（在
`workspace/probes/pattern_clustering_signal_audit/` 或新建一份 dedicated 脚本）
重新统计 admission candidate 对 `manual_pattern_ground_truth.json` 35 个 pattern
的覆盖。

**通过条件**：

- `complete candidates` ≥ 15（基线 5）
- `mixed candidates` ≤ 4（基线 11）
- `no full co-candidate` ≤ 14（基线 30）
- `no pair co-candidate` ≤ 10（基线 22）

若 mixed candidate > 4 → 启动 Step 4。若 mean component size > 5 → 启动 Step 5。

### 5.C 单元/回归

- `pytest method/EEA/rulebook/tests/test_canonical_program.py` 全绿。
- 新增最小 assertion：从 `tests/test_canonical_program.py:955` 附近，调用
  `build_formation_signals(case_signal_view=..., error_instance=<已固化 1 个>)`，
  断言 `signals["retrieval_evidence"]` 至少包含 `gold_join_edges`、
  `target_output_role`、`target_relation_equalities`、`gold_only_tables`、
  `predicate_column_roles` 五个 key，且 `schema_version` 为
  `"retrieval-evidence-v0"`。
- 不修改 `compact_canonical_repair_ir_for_memory` 的输出 schema，避免触动其他
  consumer。

### 5.D 端到端 11 库验证（Step 1-3+6 全绿 → 跑 stage=all）

```
python method/EEA/rulebook/cli/run_multidb_validation.py --stage all
```

**通过条件**：

- net change > 当前 `r_v2_e_p0b_v5` 的 +4（6 helped / 2 regressed）
- 任何库不出现 helped - regressed 反向减少 ≥ 2

**结果记入** `rulebook/doc/experiment_log.md` 新 entry：
`E-20260513-RR (retrieval root evidence)`。

## 6. 顺序与回退

| 顺序 | Step | gate | 失败回退 |
|---:|---|---|---|
| 1 | Step 1 (helper + wiring) | 5.C 现有测试通过 + 新最小 assertion 通过 | revert single commit |
| 2 | Step 2 (5 retrieval keys) | 5.A 8 pattern 全部达标 | revert; 不进 Step 3 |
| 3 | Step 3 (7 broad reasons) | 5.A 重新跑通；admission 报告无 KeyError | revert |
| 4 | Step 6 (branch-axis pair) | 5.C 通过；admission report 无新增异常 | revert |
| 5 | 5.B 全 11 库 admission | 5.B 4 条全部达标 | 调查并定位失败 pattern；可选触发 Step 4/5 |
| 6 | 5.D 全 11 库 e2e | 5.D 通过 | 调查回退库 |

每个 Step 单独 commit，commit message 含本计划 Step 编号与对应改动文件。

## 7. 明确不做（与上一版相同，重申）

- 不改 `runtime/runtime.py` 的 Q/S channel LLM judge、source_state_facts 硬 gate、
  branch dry-run、binder dry-run。
- 不改 admission prompt（`rulebook/common/llm/prompts/pattern_admission_judge.py`）。
- 不动 admission 后的 signature self-check、structural contract、effect-backed
  shared program 检查。
- 不引入 db-specific 静态词表（posthistory、driverStandings、legalities、
  connected、circuits 等不应出现在 patterns / 代码里）。
- 不删除既有 `answer_unit_op:` / `axis:` retrieval key 与 5 个 broad reason。
- 不调 numeric `score = 0.40/0.25/0.20/0.15`。
- 不动 card_games 的 promotion / runtime_usable_false 阻断（独立问题；retrieval
  修复后再单独评估）。
- 不改 `compact_canonical_repair_ir_for_memory` 的输出 schema（新数据放在
  独立的 `formation_signals["retrieval_evidence"]` 字段）。
- 不动 experience_families 策略（仍 forced empty）。

## 8. 风险与缓解

| 风险 | 表现 | 缓解 |
|---|---|---|
| `target_invariants` 在某些 case 退化为 `unknown` | q514 / q463 / q581 | `equality_relations.canonical_key` 作主源；`gold_join_edges` / `predicate_column_roles` 作 fallback；`target_output_role` 为 `""` 时不发 key |
| `alias_path_roles` 在子查询/无 JOIN case 为空 | q514 (CARD legalities) | `predicate_column_roles` 兜底；§5.A 表已包含 q514 的目标（3/6 而非 100%），不强求 |
| `pair_score` cache 命中变化导致老 library 误用 | 重跑 evolve 后 cache 失效 | `_evolution_card` 加新字段 → `_pair_score_cache_key` 自动失效；不需要手动清 cache |
| `_root_membership_closure` 因 partial 通路激活后过多扩展 | mixed candidate 上升 | 通过 5.B mixed candidate ≤ 4 监控；超阈值就启动 Step 4 |
| Step 6 让"形态完全不同"的 pair 进入 component | 假候选增多 | `program_compatible` 仍要求 effect-backed + compile_coverage ≥ 1.0 + mean_action ≤ 3.0；不会让无 program 关系的 pair 通过 |
| 现有 cache 命中导致改动不立刻生效 | 新 reason 不出现 | `_PAIR_SCORE_CACHE` / `_PATTERN_ADMISSION_CACHE` / `_INSIGHT_PATTERN_SLICER_CACHE` 都是 module-level dict，重启进程清空；codex 重跑 multidb_validation 时自动清 |

## 9. 文件改动清单

```
新增（codex 写）:
  rulebook/scripts/probes/retrieval_key_pair_coverage.py        (5.A 单元 probe)

修改（codex 写）:
  rulebook/common/analysis/signal_summary.py                    (Step 1 helper)
  rulebook/common/learning/accumulate.py                        (Step 1 调用点)
  rulebook/common/learning/pattern_formation.py                 (Step 2 / 3 / 6)
  rulebook/tests/test_canonical_program.py                      (5.C 最小 assertion)

修改（5.B 触发后 codex 才写）:
  rulebook/common/learning/pattern_formation.py                 (Step 4 / 5，仅
                                                                 在 mixed candidate
                                                                 > 4 或 mean
                                                                 component > 5 时)

记录（codex 在每 Step 完成后追加）:
  rulebook/doc/experiment_log.md
```

不应触碰：

```
rulebook/common/runtime/                                        (所有 runtime 代码)
rulebook/common/llm/prompts/                                    (所有 prompt 文件)
rulebook/common/core/data_structures.py                         (Pydantic schema)
rulebook/common/core/data_structures_v2.py                      (PatternRecognitionContractV2)
rulebook/common/learning/evolution.py / promotion.py            (replay / promotion)
rulebook/common/learning/shared_program_synthesizer.py          (shared program)
rulebook/common/analysis/repair_program_normalizer.py           (IR 构造)
rulebook/common/analysis/role_graph_normalizer.py               (role_graph 构造)
rulebook/cli/                                                   (CLI 业务逻辑)
```

## 10. 验证日志格式（每 Step 完成后追加到 `experiment_log.md`）

```
### E-20260513-RR<step>: <step 名>
- 改动文件: <列表>
- 验证 gate: <5.A / 5.B / 5.C / 5.D 的对应一项>
- 实测数字: <填具体>
- 决定: pass / fail，下一步 <Step n+1 / 修复 / 回退>
- 关联 commit hash: <填具体>
```
