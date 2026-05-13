# Structural Validation Errors v10

Source: `outputs/retrieval_root_evidence_v10/codebase_community/family_reports`.

## Run summary

| db | total_cases | strict_contract_issues | ready | triggered | patterns | notes |
|---|---:|---:|---:|---:|---:|---|
| codebase_community | 13 | 0 | 1 | 1 | 4 |  |

## Error frequency

| error type | count |
|---|---:|
| `recognition_grounded_anchors_well_formed_lt_2:1` | 2 |
| `grounded_anchor_1_not_observed_in_support` | 1 |
| `grounded_anchor_0_not_observed_in_support` | 1 |

## Candidate blocker frequency

| blocker | count |
|---|---:|
| `structural_contract_validation_failed` | 2 |

## Per-candidate

- db=codebase_community report=local_evolve_after_qid_617.json cids=[616,617] recognition_payload_present=true errors=[`grounded_anchor_1_not_observed_in_support`, `recognition_grounded_anchors_well_formed_lt_2:1`]
- db=codebase_community report=local_evolve_after_qid_709.json cids=[616,617,709] recognition_payload_present=true errors=[`grounded_anchor_0_not_observed_in_support`, `recognition_grounded_anchors_well_formed_lt_2:1`]

## Pattern snapshot

### codebase_community
- `grp-pat-codebase_community-602-652-338284ca` cases=[602,631,652]
- `grp-pat-codebase_community-616-710-5e22a456` cases=[616,617,709,710]
- `grp-pat-codebase_community-631-632-026c8df4` cases=[631,632]
- `grp-pat-codebase_community-632-635-558b0f65` cases=[632,635]
