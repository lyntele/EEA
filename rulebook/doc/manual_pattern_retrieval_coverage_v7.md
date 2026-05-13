# Manual Pattern Retrieval Coverage v7

## B2 Revised Gate

| check | v6 | v7 | status |
|---|---:|---:|---|
| posthistory max co-cases | 4/7 | 7/7 | PASS |
| posthistory candidates with >=5 manual cases | 0 | 1 | PASS |
| toxA max co-cases | 4/10 | 7/10 | PASS |
| actual pure patterns | 3 | 2 | PASS (>=2) |
| actual mixed patterns | 1 | 0 | PASS (<=2) |

Notes:

- `actual pure/mixed patterns` are computed from `library.json` patterns, not from admission candidates.
- Codebase posthistory reached complete candidate coverage, but no codebase pattern was admitted in the final v7 library.
- Toxicology bond_pair stayed as a 7/8 pure pattern and did not shrink below the stop threshold.

## Summary

| metric | value |
|---|---:|
| databases | 2 |
| candidates | 47 |
| complete candidates | 11 |
| mixed candidates | 7 |
| manual patterns | 8 |
| patterns fully co-candidate | 5 |
| no full co-candidate | 3 |
| no pair co-candidate | 1 |

## Phase 1 Gate

| check | expected | actual | status |
|---|---|---:|---|
| complete candidates | >= 5 | 11 | PASS |
| mixed candidates | <= 1 | 7 | FAIL |
| no pair co-candidate | <= 1 | 1 | PASS |
| toxicology bond_pair complete | complete | no (6/8) | FAIL |
| codebase posthistory complete | complete | yes | PASS |
| admission_judge calls | >= 5 | 47 | PASS |

## toxicology

| metric | value |
|---|---:|
| candidates | 24 |
| complete candidates | 3 |
| mixed candidates | 4 |
| patterns fully co-candidate | 2 / 4 |
| no full co-candidate | 2 |
| no pair co-candidate | 0 |

| pattern | full | max co-cases | pair coverage | fragments |
|---|---|---:|---:|---:|
| `tox_bond_condition_to_molecule_scope` | no | 7/10 | 26/45 | 8 |
| `tox_bond_pair_to_connected_atom_single_column` | no | 6/8 | 17/28 | 3 |
| `tox_connected_bidirectional_count_over_atom_id_only` | yes | 2/2 | 1/1 | 2 |
| `tox_carcinogenic_label_numerator_only` | yes | 2/2 | 1/1 | 1 |

### Complete Candidates

- cases=['239', '257', '285', '286', '308'] patterns=['tox_connected_bidirectional_count_over_atom_id_only']
- cases=['201', '239', '298', '317'] patterns=['tox_carcinogenic_label_numerator_only']
- cases=['201', '218', '219', '239', '251', '263', '286', '308', '315', '317', '330'] patterns=['tox_connected_bidirectional_count_over_atom_id_only']

### Mixed Candidates

- cases=['201', '239'] touched=['tox_bond_condition_to_molecule_scope', 'tox_connected_bidirectional_count_over_atom_id_only']
- cases=['239', '257', '285', '286', '308'] touched=['tox_bond_pair_to_connected_atom_single_column', 'tox_connected_bidirectional_count_over_atom_id_only']
- cases=['201', '239', '298', '317'] touched=['tox_bond_condition_to_molecule_scope', 'tox_carcinogenic_label_numerator_only', 'tox_connected_bidirectional_count_over_atom_id_only']
- cases=['201', '218', '219', '239', '251', '263', '286', '308', '315', '317', '330'] touched=['tox_bond_condition_to_molecule_scope', 'tox_carcinogenic_label_numerator_only', 'tox_connected_bidirectional_count_over_atom_id_only']

## codebase_community

| metric | value |
|---|---:|
| candidates | 23 |
| complete candidates | 8 |
| mixed candidates | 3 |
| patterns fully co-candidate | 3 / 4 |
| no full co-candidate | 1 |
| no pair co-candidate | 1 |

| pattern | full | max co-cases | pair coverage | fragments |
|---|---|---:|---:|---:|
| `code_editor_to_owner_user` | yes | 2/2 | 1/1 | 3 |
| `code_user_post_relation_via_posthistory` | yes | 7/7 | 21/21 | 7 |
| `code_comment_created_on_comments_creationdate` | yes | 2/2 | 1/1 | 4 |
| `code_comment_score_filter_on_posts_score` | no | 1/2 | 0/1 | 0 |

### Complete Candidates

- cases=['581', '582', '594'] patterns=['code_editor_to_owner_user']
- cases=['616', '617'] patterns=['code_comment_created_on_comments_creationdate']
- cases=['616', '617', '646'] patterns=['code_comment_created_on_comments_creationdate']
- cases=['594', '602', '630', '631', '632', '635', '637', '639', '640', '652'] patterns=['code_user_post_relation_via_posthistory']
- cases=['581', '582', '594', '610', '630', '632', '635', '637', '652', '679'] patterns=['code_editor_to_owner_user']
- cases=['581', '582', '594', '610', '630', '637', '679', '694'] patterns=['code_editor_to_owner_user']
- cases=['614', '616', '617', '662', '683', '709'] patterns=['code_comment_created_on_comments_creationdate']
- cases=['614', '616', '617', '662', '710'] patterns=['code_comment_created_on_comments_creationdate']

### Mixed Candidates

- cases=['581', '582', '594', '610', '630', '632', '635', '637', '652', '679'] touched=['code_editor_to_owner_user', 'code_user_post_relation_via_posthistory']
- cases=['614', '616', '617', '662', '683', '709'] touched=['code_comment_created_on_comments_creationdate', 'code_comment_score_filter_on_posts_score']
- cases=['614', '616', '617', '662', '710'] touched=['code_comment_created_on_comments_creationdate', 'code_comment_score_filter_on_posts_score']
