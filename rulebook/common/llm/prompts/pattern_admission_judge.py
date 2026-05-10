"""Pattern admission judge prompt.

This judge runs after conservative candidate construction and pair-level
semantic checks. It is intentionally offline-only: it may admit a pattern
candidate, but it must not invent executable actions or runtime trigger rules.
"""

from __future__ import annotations

from method.EEA.rulebook.common.core.vocabulary import BIAS_RECOGNITION_SIGNAL_VOCABULARY


PATTERN_ADMISSION_JUDGE_PROMPT = """\
Task:
Decide whether a retrieved group of audited failed cases exposes one stable
benchmark/model bias that should become an offline formal-pattern candidate.

This is conservative group-level admission, not pair merging.

Boundary:
- You are NOT writing SQL.
- You are NOT creating a rewrite hint.
- You are NOT inventing compiler arguments.
- You are NOT promoting the candidate to runtime.
- First decide the root pattern: whether the cases share the same source
  misconception / target preference / repair effect. Then assign admitted
  members to finite branches. Do not decide root membership from SQL edit path
  alone.
- You may admit a pattern only when the cases share a stable source->target
  root bias AND each admitted case can either be assigned to an answer-blind
  branch or explicitly marked as not runtime-usable/offline-only.
- The candidate may contain different extracted core repair packages. Treat
  those differences as branch/accessory evidence first. Reject only when the
  difference changes the source misconception, target preference, answer unit,
  metric/formula, or non-branchable scope contract.
- Pair context is auxiliary audit evidence. Use case_cards as the primary
  evidence for stable bias and repair interface. Do not admit a pattern only
  because representative pairs look compatible, and do not reject solely
  because one representative pair is partial when accepted_case_ids can isolate
  a clean subset.
- Branches are allowed only when each branch can be selected from the current
  question/pred/schema/memory signals, not from gold SQL or final answers.
- Finite dependency/action branches may be admitted only when they remain inside
  the same root bias. If a branch changes target preference, answer unit,
  ranking metric, formula, predicate metric, or non-branchable aggregation
  contract, reject or exclude it.
- If a retrieved component contains a clean stable subset plus unrelated cases,
  admit the clean subset and list the unrelated cases in excluded_case_ids.
  Do not reject the whole component when accepted_case_ids can isolate one
  reusable pattern of size at least two.
- If only a loose family resemblance exists, reject formal pattern admission.

Definitions:
- stable_bias_key: a short source->target contrast shared by the admitted cases,
  e.g. "drop extra paired output side while preserving filter scope".
- primary_repair_interface: the abstract action interface compiler must later
  instantiate, e.g. "select one canonical answer unit from a paired output".
- branch_axis: a finite unresolved variation such as "which output side is kept"
  or "whether a dependency such as DISTINCT is needed". Branches must be
  selected by local signals and compiler coverage, not by hand-coded case IDs.
- core_program_signature: the extracted SQL-edit package for one member. It is
  branch evidence, not root-pattern identity. A different core signature can
  remain in the same pattern only if it is a finite lowering branch under the
  same source misconception and target preference.
- dependency branch: a supporting edit inside the same answer-unit contract.
  Examples include duplicate removal, route cleanup, or preserving equivalent
  predicate scope while applying the same core output repair. A dependency is
  NOT branchable when it introduces a different target preference such as a
  different ranking metric, formula, aggregation grain, predicate metric, or
  answer unit.
- negative guard: a condition showing when this pattern must not absorb a case.
- accepted_case_ids: the subset, if any, that truly shares this pattern. Exclude
  members that only match a broad axis but require a different repair program.
- membership_by_case: every candidate case should be accounted for as
  accepted_root, rejected_root, branch_unassigned, or offline_only. Do not omit a
  case silently just because its card was sampled or its branch is not yet
  runtime-safe.
- When excluding a case from a slicer_hypothesis, explain the concrete conflict
  in source_misread, target_preference, answer unit, or scope contract. Do not
  exclude only because it needs an additional join, predicate move, DISTINCT, or
  route cleanup if that extra edit is a finite branch of the same stable bias.
- loose generic interfaces such as "add missing name slot", "fix selected
  column", or "adjust join" are not enough. The admitted cases must share the
  same source misread and target preference, not merely the same SQL surface
  edit family.

Positive examples:
- Several cases output both sides of a relationship while the target keeps one
  answer unit. Some cases need a dependency such as duplicate removal and some
  do not. Admit if the core bias is the same and the dependency is a finite
  branch extracted from member repair programs; require compiler/replay checks
  before runtime use if branch selection is not yet proven.
- Several cases use a proxy/display slot while the target consistently uses a
  canonical answer slot. Admit if the source slot role, target slot role, and
  preserve invariants are shared even when concrete table/column names differ.
- Several cases miss a materialized ranking output under the same ranking
  contract. Admit if metric binding and output role are stable.

Negative examples:
- Same broad output_shape_delta but one case adds a missing metric column while
  another drops an extra paired output side. Reject or exclude one side: they do
  not share one repair interface.
- Same database and relation-like SQL, but one case asks for local attributes
  and another asks for a bidirectional pair count. Reject: shared schema surface
  is not a shared repair program.
- One case adds an entity/name output required by a multi-part question while
  another changes a ranking metric or answer unit. Reject even if both touch
  SELECT: the source misread and target preference are different.
- One case only completes an output identity while another also changes the
  semantic predicate/ranking contract. Reject if that extra contract is not a
  cleanup branch of the same stable bias.
- Same join shape but one repair changes the answer unit, another changes route
  scope, and another changes aggregation grain. Reject formal pattern; this is
  at most a family.
- Broad source/target insight such as "proxy to canonical", "fix selected
  column", "row to entity", or "reroute to correct source" is not enough when
  concrete answer-unit or action interface differs.
- Do not admit because of case ids, manual labels, exact table names, exact
  column names, or database-specific vocabulary.

Return strict JSON only:
{{
  "admit_pattern": true,
  "accepted_case_ids": ["..."],
  "excluded_case_ids": ["..."],
  "stable_bias_key": "short normalized phrase",
  "primary_repair_interface": "answer-blind repair interface compiler must instantiate later",
  "branch_axes": [
    {{
      "name": "finite branch axis",
      "why_needed": "why members vary",
      "selection_signal": "current-case signal that can select branch without gold"
    }}
  ],
  "branch_specs": [
    {{
      "branch_id": "short branch id",
      "case_ids": ["..."],
      "required_interface_delta": "what differs inside the same stable bias"
    }}
  ],
  "membership_by_case": [
    {{
      "case_id": "...",
      "status": "accepted_root | rejected_root | branch_unassigned | offline_only",
      "reason": "why this case belongs, conflicts, or cannot yet be branched"
    }}
  ],
  "negative_guards": ["conditions that must prevent trigger/absorption"],
  "bias_recognition_contract": {{
    "bias_motif": "short phenomenon-level motif, no concrete table/column names",
    "answer_shape_hint": "scalar | subset | preserved_aggregate_unit | route_scope | other",
    "recognition_signals": ["3-5 signals from the closed vocabulary below"],
    "anti_signals": ["optional signals from the same vocabulary"],
    "min_signal_overlap": 0.6
  }},
  "required_code_checks": ["compiler/replay checks still required before runtime use"],
  "reject_reason": "",
  "rationale": "one concise sentence"
}}

If not admitting, return the same object shape with:
{{
  "admit_pattern": false,
  "accepted_case_ids": [],
  "excluded_case_ids": ["all or relevant ids"],
  "stable_bias_key": "",
  "primary_repair_interface": "",
  "branch_axes": [],
  "branch_specs": [],
  "membership_by_case": [],
  "negative_guards": [],
  "bias_recognition_contract": {{}},
  "required_code_checks": [],
  "reject_reason": "why this is not one stable pattern",
  "rationale": "one concise sentence"
}}

Data:

case_cards:
{case_cards_json}

pair_semantic_decisions (relation counts plus representative pairs only):
{pair_semantic_decisions_json}

component_summary:
{component_summary_json}

Bias recognition contract:
- If admit_pattern is true, also fill bias_recognition_contract.
- It is only for lightweight runtime recognition: "does current S0 show this
  same bias?", not "how to rewrite it".
- Select 3-5 phenomenon-level recognition_signals from this closed vocabulary:
{bias_recognition_vocabulary_json}
- Do not use concrete table names, column names, aliases, SQL literals, case ids,
  or database-specific terms in bias_motif or answer_shape_hint.
- anti_signals are exclusion cues: if any anti signal is true, runtime must not
  recognize this pattern.
- Choose anti_signals only when their presence means the current case exposes a
  different or opposing bias_motif. Do not use a signal as anti merely because
  it marks a branch/accessory detail inside the same bias.
"""


def build_pattern_admission_judge_prompt(
    *,
    case_cards_json: str,
    pair_semantic_decisions_json: str,
    component_summary_json: str,
) -> str:
    return PATTERN_ADMISSION_JUDGE_PROMPT.format(
        case_cards_json=case_cards_json,
        pair_semantic_decisions_json=pair_semantic_decisions_json,
        component_summary_json=component_summary_json,
        bias_recognition_vocabulary_json=(
            "["
            + ", ".join(
                f'"{item}"'
                for item in sorted(BIAS_RECOGNITION_SIGNAL_VOCABULARY)
            )
            + "]"
        ),
    )


__all__ = ["PATTERN_ADMISSION_JUDGE_PROMPT", "build_pattern_admission_judge_prompt"]
