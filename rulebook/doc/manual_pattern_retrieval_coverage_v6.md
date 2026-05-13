# Manual Pattern Retrieval Coverage v6

## Summary

| metric | value |
|---|---:|
| databases | 2 |
| candidates | 44 |
| complete candidates | 6 |
| mixed candidates | 6 |
| manual patterns | 8 |
| patterns fully co-candidate | 3 |
| no full co-candidate | 5 |
| no pair co-candidate | 2 |

## Phase 1 Gate

| check | expected | actual | status |
|---|---|---:|---|
| complete candidates | >= 5 | 6 | PASS |
| mixed candidates | <= 1 | 6 | FAIL |
| no pair co-candidate | <= 1 | 2 | FAIL |
| toxicology bond_pair complete | complete | no (6/8) | FAIL |
| codebase posthistory complete | complete | no (4/7) | FAIL |
| admission_judge calls | >= 5 | 44 | PASS |

## toxicology

| metric | value |
|---|---:|
| candidates | 20 |
| complete candidates | 2 |
| mixed candidates | 5 |
| patterns fully co-candidate | 1 / 4 |
| no full co-candidate | 3 |
| no pair co-candidate | 1 |

| pattern | full | max co-cases | pair coverage | fragments |
|---|---|---:|---:|---:|
| `tox_bond_condition_to_molecule_scope` | no | 4/10 | 9/45 | 5 |
| `tox_bond_pair_to_connected_atom_single_column` | no | 6/8 | 15/28 | 2 |
| `tox_connected_bidirectional_count_over_atom_id_only` | yes | 2/2 | 1/1 | 2 |
| `tox_carcinogenic_label_numerator_only` | no | 1/2 | 0/1 | 0 |

### Complete Candidates

- cases=['201', '218', '219', '239', '286', '308'] patterns=['tox_connected_bidirectional_count_over_atom_id_only']
- cases=['201', '218', '239', '286', '308', '317', '332'] patterns=['tox_connected_bidirectional_count_over_atom_id_only']

### Mixed Candidates

- cases=['201', '219', '239'] touched=['tox_bond_condition_to_molecule_scope', 'tox_connected_bidirectional_count_over_atom_id_only']
- cases=['206', '207', '249', '253', '268', '277', '285'] touched=['tox_bond_condition_to_molecule_scope', 'tox_bond_pair_to_connected_atom_single_column']
- cases=['201', '239', '286'] touched=['tox_bond_condition_to_molecule_scope', 'tox_connected_bidirectional_count_over_atom_id_only']
- cases=['201', '218', '219', '239', '286', '308'] touched=['tox_bond_condition_to_molecule_scope', 'tox_connected_bidirectional_count_over_atom_id_only']
- cases=['201', '218', '239', '286', '308', '317', '332'] touched=['tox_bond_condition_to_molecule_scope', 'tox_carcinogenic_label_numerator_only', 'tox_connected_bidirectional_count_over_atom_id_only']

## codebase_community

| metric | value |
|---|---:|
| candidates | 24 |
| complete candidates | 4 |
| mixed candidates | 1 |
| patterns fully co-candidate | 2 / 4 |
| no full co-candidate | 2 |
| no pair co-candidate | 1 |

| pattern | full | max co-cases | pair coverage | fragments |
|---|---|---:|---:|---:|
| `code_editor_to_owner_user` | yes | 2/2 | 1/1 | 2 |
| `code_user_post_relation_via_posthistory` | no | 4/7 | 6/21 | 5 |
| `code_comment_created_on_comments_creationdate` | yes | 2/2 | 1/1 | 3 |
| `code_comment_score_filter_on_posts_score` | no | 1/2 | 0/1 | 0 |

### Complete Candidates

- cases=['581', '582', '594'] patterns=['code_editor_to_owner_user']
- cases=['616', '617'] patterns=['code_comment_created_on_comments_creationdate']
- cases=['616', '617', '646'] patterns=['code_comment_created_on_comments_creationdate']
- cases=['581', '582', '594', '610', '616', '617', '630', '637', '652', '679', '694'] patterns=['code_editor_to_owner_user', 'code_comment_created_on_comments_creationdate']

### Mixed Candidates

- cases=['581', '582', '594', '610', '616', '617', '630', '637', '652', '679', '694'] touched=['code_comment_created_on_comments_creationdate', 'code_editor_to_owner_user', 'code_user_post_relation_via_posthistory']
