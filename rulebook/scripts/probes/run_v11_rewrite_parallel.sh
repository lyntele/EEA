#!/usr/bin/env bash
set -uo pipefail

cd /data/liuyining/ace4sql || exit 1

WORK_BASE=/data/liuyining/ace4sql/method/deepeye/DeepEye-SQL/workspace/rulebook_runs/r_v2_e_p0b_v5_direct_20260512_191050
OUT_BASE=/data/liuyining/ace4sql/method/EEA/rulebook/outputs/retrieval_root_evidence_v11_rewrite
GROUND_TRUTH=/data/liuyining/ace4sql/method/EEA/rulebook/scripts/probes/manual_pattern_ground_truth.json
LOG_DIR=$OUT_BASE/logs
mkdir -p "$LOG_DIR"

# Same order as v11 skip-rewrite validation: largest databases first.
DBS=(
  card_games
  codebase_community
  formula_1
  thrombosis_prediction
  student_club
  toxicology
  european_football_2
  superhero
  financial
  california_schools
  debit_card_specializing
)

run_one() {
  local db=$1
  local work_root="$WORK_BASE/r_v2_e_p0b_v5_direct_${db}_20260512_191050/.state/work"
  local out_dir="$OUT_BASE/$db"
  local log="$LOG_DIR/${db}.log"

  if [ ! -d "$work_root" ]; then
    echo "[$db] SKIP: work_root not found at $work_root" | tee -a "$log"
    return 1
  fi

  echo "[$db] start $(date -Iseconds)" | tee -a "$log"
  PYTHONPATH=/data/liuyining/ace4sql python3 method/EEA/rulebook/cli/run_online_e2e_validation.py \
    --db_id "$db" \
    --work_root "$work_root" \
    --output_dir "$out_dir" \
    --manual_groups_json "$GROUND_TRUTH" \
    --skip-final-freeze \
    --strict_contract_policy skip_accumulate \
    >> "$log" 2>&1
  local rc=$?
  echo "[$db] done $(date -Iseconds) rc=$rc" | tee -a "$log"
  return "$rc"
}

export -f run_one
export WORK_BASE OUT_BASE GROUND_TRUTH LOG_DIR

printf '%s\n' "${DBS[@]}" | xargs -P 6 -I {} bash -c 'run_one "$@"' _ {}
