# Structural Validation Errors v9

Source: `outputs/retrieval_root_evidence_v9/{toxicology,codebase_community}/family_reports`.

## Run summary

| db | total_cases | strict_contract_issues | ready | triggered | patterns | notes |
|---|---:|---:|---:|---:|---:|---|
| toxicology | 10 | 1 | 1 | 1 | 2 | case 268: action_missing_repair_program:act-268-drop-a2-element-253 |
| codebase_community | 13 | 0 | 0 | 0 | 1 |  |

## Error frequency

| error type | count |
|---|---:|
| `grounded_anchor_1_missing_descriptor` | 2 |
| `grounded_anchor_2_not_observed_in_support` | 2 |
| `grounded_anchor_0_not_observed_in_support` | 1 |
| `grounded_anchor_2_missing_descriptor` | 1 |
| `grounded_anchor_3_missing_descriptor` | 1 |

## Candidate blocker frequency

| blocker | count |
|---|---:|
| `structural_contract_validation_failed` | 4 |

## Per-candidate

- db=codebase_community report=local_evolve_after_qid_617.json cids=[616,617] recognition_payload_present=true errors=[`grounded_anchor_1_missing_descriptor`, `grounded_anchor_2_not_observed_in_support`]
- db=codebase_community report=local_evolve_after_qid_639.json cids=[631,632,635,639] recognition_payload_present=true errors=[`grounded_anchor_0_not_observed_in_support`, `grounded_anchor_1_missing_descriptor`]
- db=codebase_community report=local_evolve_after_qid_709.json cids=[616,617,709] recognition_payload_present=true errors=[`grounded_anchor_2_not_observed_in_support`]
- db=codebase_community report=local_evolve_after_qid_710.json cids=[616,617,709,710] recognition_payload_present=true errors=[`grounded_anchor_2_missing_descriptor`, `grounded_anchor_3_missing_descriptor`]

## Pattern snapshot

### toxicology
- `grp-pat-toxicology-198-263-3ebff987` cases=[198,207,263]
- `grp-pat-toxicology-206-249-b5991530` cases=[206,249,253]

### codebase_community
- `grp-pat-codebase_community-631-632-026c8df4` cases=[631,632,635]
