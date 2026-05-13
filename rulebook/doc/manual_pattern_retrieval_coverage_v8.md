# Manual Pattern Retrieval Coverage v8

## B3 Gate

| check | v7 | v8 | status |
|---|---:|---:|---|
| codebase actual pattern count | 0 | 0 | FAIL |
| codebase posthistory hit across actual patterns | 0/7 | 0/7 | FAIL |
| toxicology bond_pair pure pattern | 7/8 | 7/8 | PASS |
| toxicology toxA pure pattern | 5/10 | 8/10 | PASS |
| actual mixed pattern count | 0 | 0 | PASS |

Notes:

- B3 did not recover codebase admission into final `library.json`: codebase still has zero actual patterns.
- Candidate coverage also regressed for codebase posthistory from v7 `7/7` to v8 `5/7`, so the failure is not only final persistence.
- Toxicology stayed safe: bond_pair remained 7/8 pure, and toxA expanded to 8/10 pure.

## Summary

| metric | value |
|---|---:|
| databases | 2 |
| candidates | 45 |
| complete candidates | 10 |
| mixed candidates | 11 |
| manual patterns | 8 |
| patterns fully co-candidate | 4 |
| no full co-candidate | 4 |
| no pair co-candidate | 1 |

## Phase 1 Gate

| check | expected | actual | status |
|---|---|---:|---|
| complete candidates | >= 5 | 10 | PASS |
| mixed candidates | <= 1 | 11 | FAIL |
| no pair co-candidate | <= 1 | 1 | PASS |
| toxicology bond_pair complete | complete | no (6/8) | FAIL |
| codebase posthistory complete | complete | no (5/7) | FAIL |
| admission_judge calls | >= 5 | 45 | PASS |

## toxicology

| metric | value |
|---|---:|
| candidates | 22 |
| complete candidates | 2 |
| mixed candidates | 5 |
| patterns fully co-candidate | 1 / 4 |
| no full co-candidate | 3 |
| no pair co-candidate | 1 |

| pattern | full | max co-cases | pair coverage | fragments |
|---|---|---:|---:|---:|
| `tox_bond_condition_to_molecule_scope` | no | 6/10 | 19/45 | 8 |
| `tox_bond_pair_to_connected_atom_single_column` | no | 6/8 | 15/28 | 2 |
| `tox_connected_bidirectional_count_over_atom_id_only` | no | 1/2 | 0/1 | 0 |
| `tox_carcinogenic_label_numerator_only` | yes | 2/2 | 1/1 | 2 |

### Complete Candidates

- cases=['201', '219', '239', '251', '286', '298', '310', '315', '317'] patterns=['tox_carcinogenic_label_numerator_only']
- cases=['198', '219', '251', '263', '269', '298', '311', '315', '317', '330'] patterns=['tox_carcinogenic_label_numerator_only']

### Mixed Candidates

- cases=['201', '219', '239'] touched=['tox_bond_condition_to_molecule_scope', 'tox_connected_bidirectional_count_over_atom_id_only']
- cases=['206', '207', '249', '253', '254', '268', '277', '285'] touched=['tox_bond_condition_to_molecule_scope', 'tox_bond_pair_to_connected_atom_single_column']
- cases=['201', '219', '239', '251', '286', '298', '310', '315', '317'] touched=['tox_bond_condition_to_molecule_scope', 'tox_carcinogenic_label_numerator_only', 'tox_connected_bidirectional_count_over_atom_id_only']
- cases=['198', '219', '251', '263', '269', '298', '311', '315', '317', '330'] touched=['tox_bond_condition_to_molecule_scope', 'tox_carcinogenic_label_numerator_only']
- cases=['263', '269', '286', '308', '311', '330', '332'] touched=['tox_bond_condition_to_molecule_scope', 'tox_connected_bidirectional_count_over_atom_id_only']

## codebase_community

| metric | value |
|---|---:|
| candidates | 23 |
| complete candidates | 8 |
| mixed candidates | 6 |
| patterns fully co-candidate | 3 / 4 |
| no full co-candidate | 1 |
| no pair co-candidate | 0 |

| pattern | full | max co-cases | pair coverage | fragments |
|---|---|---:|---:|---:|
| `code_editor_to_owner_user` | yes | 2/2 | 1/1 | 4 |
| `code_user_post_relation_via_posthistory` | no | 5/7 | 20/21 | 10 |
| `code_comment_created_on_comments_creationdate` | yes | 2/2 | 1/1 | 3 |
| `code_comment_score_filter_on_posts_score` | yes | 2/2 | 1/1 | 1 |

### Complete Candidates

- cases=['581', '582', '594'] patterns=['code_editor_to_owner_user']
- cases=['616', '617'] patterns=['code_comment_created_on_comments_creationdate']
- cases=['581', '582', '584', '594', '595', '602', '610', '630', '632', '635', '637'] patterns=['code_editor_to_owner_user']
- cases=['581', '582', '602', '631', '632', '637', '639', '642', '646', '652'] patterns=['code_editor_to_owner_user']
- cases=['581', '582', '594', '610', '630', '631', '632', '635', '637', '638', '652', '679'] patterns=['code_editor_to_owner_user']
- cases=['582', '594', '610', '616', '617', '630', '637', '638', '646', '652', '679', '694'] patterns=['code_comment_created_on_comments_creationdate']
- cases=['603', '614', '616', '617', '662', '709'] patterns=['code_comment_created_on_comments_creationdate']
- cases=['616', '662', '709', '710'] patterns=['code_comment_score_filter_on_posts_score']

### Mixed Candidates

- cases=['581', '582', '584', '594', '595', '602', '610', '630', '632', '635', '637'] touched=['code_editor_to_owner_user', 'code_user_post_relation_via_posthistory']
- cases=['581', '582', '602', '631', '632', '637', '639', '642', '646', '652'] touched=['code_editor_to_owner_user', 'code_user_post_relation_via_posthistory']
- cases=['581', '582', '594', '610', '630', '631', '632', '635', '637', '638', '652', '679'] touched=['code_editor_to_owner_user', 'code_user_post_relation_via_posthistory']
- cases=['582', '594', '610', '616', '617', '630', '637', '638', '646', '652', '679', '694'] touched=['code_comment_created_on_comments_creationdate', 'code_editor_to_owner_user', 'code_user_post_relation_via_posthistory']
- cases=['603', '614', '616', '617', '662', '709'] touched=['code_comment_created_on_comments_creationdate', 'code_comment_score_filter_on_posts_score']
- cases=['616', '662', '709', '710'] touched=['code_comment_created_on_comments_creationdate', 'code_comment_score_filter_on_posts_score']
