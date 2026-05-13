# Manual Pattern Retrieval Coverage v6

## Summary

| metric | value |
|---|---:|
| databases | 11 |
| candidates | 285 |
| complete candidates | 28 |
| mixed candidates | 53 |
| manual patterns | 35 |
| patterns fully co-candidate | 13 |
| no full co-candidate | 22 |
| no pair co-candidate | 7 |

## Phase 1 Gate

| check | expected | actual | status |
|---|---|---:|---|
| complete candidates | >= 5 | 28 | PASS |
| mixed candidates | <= 1 | 53 | FAIL |
| no pair co-candidate | <= 1 | 7 | FAIL |
| toxicology bond_pair complete | complete | no (6/8) | FAIL |
| codebase posthistory complete | complete | no (6/7) | FAIL |
| admission_judge calls | >= 5 | 285 | PASS |

## card_games

| metric | value |
|---|---:|
| candidates | 50 |
| complete candidates | 0 |
| mixed candidates | 12 |
| patterns fully co-candidate | 0 / 4 |
| no full co-candidate | 4 |
| no pair co-candidate | 0 |

| pattern | full | max co-cases | pair coverage | fragments |
|---|---|---:|---:|---:|
| `card_legalities_uuid_to_card_grain` | no | 3/4 | 3/6 | 7 |
| `card_named_card_anchor_to_set_layer` | no | 2/5 | 1/10 | 3 |
| `card_set_translation_setcode_bridge` | no | 4/5 | 6/10 | 6 |
| `card_rulings_uuid_then_answer_grain` | no | 4/5 | 6/10 | 4 |

### Mixed Candidates

- cases=['341', '343', '344', '355', '361', '363', '387', '388', '389'] touched=['card_legalities_uuid_to_card_grain', 'card_rulings_uuid_then_answer_grain']
- cases=['341', '343', '344', '360', '388', '389', '400'] touched=['card_legalities_uuid_to_card_grain', 'card_rulings_uuid_then_answer_grain', 'card_set_translation_setcode_bridge']
- cases=['344', '349', '354', '361', '363', '389', '392', '408', '413'] touched=['card_legalities_uuid_to_card_grain', 'card_rulings_uuid_then_answer_grain']
- cases=['341', '343', '344', '358', '373', '387', '388', '389', '392', '400', '411', '418', '425'] touched=['card_legalities_uuid_to_card_grain', 'card_rulings_uuid_then_answer_grain', 'card_set_translation_setcode_bridge']
- cases=['341', '343', '344', '387', '388', '389', '425', '437'] touched=['card_legalities_uuid_to_card_grain', 'card_rulings_uuid_then_answer_grain']
- cases=['355', '358', '364', '373', '391', '392', '400', '411', '418', '440'] touched=['card_rulings_uuid_then_answer_grain', 'card_set_translation_setcode_bridge']
- cases=['341', '343', '344', '355', '358', '360', '373', '387', '388', '389', '391', '392', '400', '407', '411', '418', '425', '428', '430', '431', '432', '433', '441', '442'] touched=['card_legalities_uuid_to_card_grain', 'card_rulings_uuid_then_answer_grain', 'card_set_translation_setcode_bridge']
- cases=['354', '361', '363', '413', '463'] touched=['card_legalities_uuid_to_card_grain', 'card_named_card_anchor_to_set_layer', 'card_rulings_uuid_then_answer_grain']
- cases=['469', '473', '494'] touched=['card_named_card_anchor_to_set_layer', 'card_rulings_uuid_then_answer_grain']
- cases=['354', '361', '363', '413', '458', '463', '499'] touched=['card_legalities_uuid_to_card_grain', 'card_named_card_anchor_to_set_layer', 'card_rulings_uuid_then_answer_grain', 'card_set_translation_setcode_bridge']
- cases=['360', '365', '387', '389', '400', '425', '519'] touched=['card_rulings_uuid_then_answer_grain', 'card_set_translation_setcode_bridge']
- cases=['469', '473', '494', '530'] touched=['card_named_card_anchor_to_set_layer', 'card_rulings_uuid_then_answer_grain']

## codebase_community

| metric | value |
|---|---:|
| candidates | 29 |
| complete candidates | 4 |
| mixed candidates | 4 |
| patterns fully co-candidate | 2 / 4 |
| no full co-candidate | 2 |
| no pair co-candidate | 1 |

| pattern | full | max co-cases | pair coverage | fragments |
|---|---|---:|---:|---:|
| `code_editor_to_owner_user` | yes | 2/2 | 1/1 | 1 |
| `code_user_post_relation_via_posthistory` | no | 6/7 | 17/21 | 9 |
| `code_comment_created_on_comments_creationdate` | yes | 2/2 | 1/1 | 3 |
| `code_comment_score_filter_on_posts_score` | no | 0/2 | 0/1 | 0 |

### Complete Candidates

- cases=['616', '617'] patterns=['code_comment_created_on_comments_creationdate']
- cases=['581', '582', '594', '595', '602', '630'] patterns=['code_editor_to_owner_user']
- cases=['616', '617', '646'] patterns=['code_comment_created_on_comments_creationdate']
- cases=['616', '617', '646', '681'] patterns=['code_comment_created_on_comments_creationdate']

### Mixed Candidates

- cases=['581', '582', '594', '595', '602', '630'] touched=['code_editor_to_owner_user', 'code_user_post_relation_via_posthistory']
- cases=['582', '594', '595', '602', '630', '631', '632', '635', '637'] touched=['code_editor_to_owner_user', 'code_user_post_relation_via_posthistory']
- cases=['581', '594', '630', '631', '632', '635', '637', '638', '679'] touched=['code_editor_to_owner_user', 'code_user_post_relation_via_posthistory']
- cases=['594', '610', '617', '630', '637', '638', '652', '679', '682', '693', '694'] touched=['code_comment_created_on_comments_creationdate', 'code_user_post_relation_via_posthistory']

## formula_1

| metric | value |
|---|---:|
| candidates | 38 |
| complete candidates | 8 |
| mixed candidates | 15 |
| patterns fully co-candidate | 3 / 7 |
| no full co-candidate | 4 |
| no pair co-candidate | 1 |

| pattern | full | max co-cases | pair coverage | fragments |
|---|---|---:|---:|---:|
| `f1_circuit_info_url` | yes | 3/3 | 3/3 | 8 |
| `f1_driver_standings_path` | no | 5/7 | 14/21 | 10 |
| `f1_ranked_k_results_rank` | no | 2/4 | 2/6 | 4 |
| `f1_pitstops_raw_detail_grain` | yes | 2/2 | 1/1 | 1 |
| `f1_time_text_parse_numeric` | no | 1/2 | 0/1 | 0 |
| `f1_multi_question_attached_output_slot` | yes | 2/2 | 1/1 | 4 |
| `f1_display_semantics_direct_field_slot` | no | 2/3 | 1/3 | 3 |

### Complete Candidates

- cases=['866', '894'] patterns=['f1_multi_question_attached_output_slot']
- cases=['866', '894', '922'] patterns=['f1_multi_question_attached_output_slot']
- cases=['866', '894', '922', '928'] patterns=['f1_multi_question_attached_output_slot']
- cases=['849', '850', '851', '853', '855', '856', '860', '873', '921', '936'] patterns=['f1_circuit_info_url']
- cases=['866', '894', '922', '928', '958'] patterns=['f1_multi_question_attached_output_slot']
- cases=['970', '973'] patterns=['f1_pitstops_raw_detail_grain']
- cases=['849', '855', '889', '921', '986', '1000'] patterns=['f1_circuit_info_url']
- cases=['849', '851', '855', '873', '908', '921', '936', '958', '1012'] patterns=['f1_circuit_info_url']

### Mixed Candidates

- cases=['849', '850', '853', '855', '856', '860', '873'] touched=['f1_circuit_info_url', 'f1_display_semantics_direct_field_slot']
- cases=['853', '855', '856', '889'] touched=['f1_circuit_info_url', 'f1_display_semantics_direct_field_slot']
- cases=['849', '855', '891', '893', '896', '902'] touched=['f1_circuit_info_url', 'f1_driver_standings_path']
- cases=['855', '889', '891', '893', '896', '902', '903'] touched=['f1_circuit_info_url', 'f1_display_semantics_direct_field_slot', 'f1_driver_standings_path']
- cases=['866', '894', '922', '928'] touched=['f1_multi_question_attached_output_slot', 'f1_ranked_k_results_rank']
- cases=['849', '850', '851', '853', '855', '856', '860', '873', '921', '936'] touched=['f1_circuit_info_url', 'f1_display_semantics_direct_field_slot']
- cases=['928', '930', '936', '937'] touched=['f1_display_semantics_direct_field_slot', 'f1_ranked_k_results_rank']
- cases=['928', '936', '937', '944'] touched=['f1_display_semantics_direct_field_slot', 'f1_ranked_k_results_rank']
- cases=['849', '855', '891', '893', '929', '948', '949', '950'] touched=['f1_circuit_info_url', 'f1_driver_standings_path']
- cases=['891', '893', '902', '929', '950', '952'] touched=['f1_driver_standings_path', 'f1_ranked_k_results_rank']
- cases=['873', '902', '929', '936', '950', '952', '956'] touched=['f1_display_semantics_direct_field_slot', 'f1_driver_standings_path', 'f1_ranked_k_results_rank']
- cases=['866', '894', '922', '928', '958'] touched=['f1_multi_question_attached_output_slot', 'f1_ranked_k_results_rank']
- cases=['849', '855', '889', '921', '986', '1000'] touched=['f1_circuit_info_url', 'f1_display_semantics_direct_field_slot']
- cases=['889', '1006'] touched=['f1_display_semantics_direct_field_slot', 'f1_time_text_parse_numeric']
- cases=['849', '851', '855', '873', '908', '921', '936', '958', '1012'] touched=['f1_circuit_info_url', 'f1_display_semantics_direct_field_slot']

## thrombosis_prediction

| metric | value |
|---|---:|
| candidates | 50 |
| complete candidates | 5 |
| mixed candidates | 4 |
| patterns fully co-candidate | 1 / 3 |
| no full co-candidate | 2 |
| no pair co-candidate | 1 |

| pattern | full | max co-cases | pair coverage | fragments |
|---|---|---:|---:|---:|
| `thr_abnormal_fg_unparenthesized_precedence` | yes | 2/2 | 1/1 | 5 |
| `thr_text_lab_raw_threshold_compare` | no | 2/4 | 1/6 | 2 |
| `thr_laboratory_record_level_status` | no | 1/3 | 0/3 | 0 |

### Complete Candidates

- cases=['1167', '1174', '1233', '1247', '1248'] patterns=['thr_abnormal_fg_unparenthesized_precedence']
- cases=['1167', '1168', '1174', '1211', '1233', '1242', '1243', '1245', '1247', '1248', '1249', '1250', '1254', '1259'] patterns=['thr_abnormal_fg_unparenthesized_precedence']
- cases=['1167', '1174', '1175', '1189', '1219', '1233', '1243', '1245', '1247', '1248', '1254', '1259', '1264'] patterns=['thr_abnormal_fg_unparenthesized_precedence']
- cases=['1167', '1172', '1174', '1175', '1189', '1211', '1219', '1233', '1243', '1245', '1247', '1248', '1254', '1257', '1259', '1260', '1264', '1265', '1269', '1271'] patterns=['thr_abnormal_fg_unparenthesized_precedence']
- cases=['1159', '1167', '1172', '1199', '1225', '1244', '1247', '1248', '1257', '1261', '1271', '1279', '1305'] patterns=['thr_abnormal_fg_unparenthesized_precedence']

### Mixed Candidates

- cases=['1167', '1211', '1248', '1249'] touched=['thr_abnormal_fg_unparenthesized_precedence', 'thr_text_lab_raw_threshold_compare']
- cases=['1167', '1168', '1174', '1211', '1233', '1242', '1243', '1245', '1247', '1248', '1249', '1250', '1254', '1259'] touched=['thr_abnormal_fg_unparenthesized_precedence', 'thr_text_lab_raw_threshold_compare']
- cases=['1174', '1223', '1248', '1249', '1254', '1259', '1265', '1271', '1274', '1275'] touched=['thr_abnormal_fg_unparenthesized_precedence', 'thr_text_lab_raw_threshold_compare']
- cases=['1159', '1167', '1172', '1199', '1225', '1244', '1247', '1248', '1257', '1261', '1271', '1279', '1305'] touched=['thr_abnormal_fg_unparenthesized_precedence', 'thr_text_lab_raw_threshold_compare']

## student_club

| metric | value |
|---|---:|
| candidates | 17 |
| complete candidates | 2 |
| mixed candidates | 2 |
| patterns fully co-candidate | 1 / 2 |
| no full co-candidate | 1 |
| no pair co-candidate | 0 |

| pattern | full | max co-cases | pair coverage | fragments |
|---|---|---:|---:|---:|
| `stu_event_category_to_budget_category` | yes | 2/2 | 1/1 | 2 |
| `stu_entity_question_output_primary_key` | no | 2/3 | 2/3 | 2 |

### Complete Candidates

- cases=['1366', '1385', '1387', '1404', '1418', '1422'] patterns=['stu_event_category_to_budget_category']
- cases=['1385', '1417', '1418', '1422', '1433'] patterns=['stu_event_category_to_budget_category']

### Mixed Candidates

- cases=['1366', '1387', '1404', '1418'] touched=['stu_entity_question_output_primary_key', 'stu_event_category_to_budget_category']
- cases=['1366', '1385', '1387', '1404', '1418', '1422'] touched=['stu_entity_question_output_primary_key', 'stu_event_category_to_budget_category']

## toxicology

| metric | value |
|---|---:|
| candidates | 33 |
| complete candidates | 2 |
| mixed candidates | 4 |
| patterns fully co-candidate | 2 / 4 |
| no full co-candidate | 2 |
| no pair co-candidate | 0 |

| pattern | full | max co-cases | pair coverage | fragments |
|---|---|---:|---:|---:|
| `tox_bond_condition_to_molecule_scope` | no | 8/10 | 34/45 | 12 |
| `tox_bond_pair_to_connected_atom_single_column` | no | 6/8 | 16/28 | 5 |
| `tox_connected_bidirectional_count_over_atom_id_only` | yes | 2/2 | 1/1 | 1 |
| `tox_carcinogenic_label_numerator_only` | yes | 2/2 | 1/1 | 1 |

### Complete Candidates

- cases=['201', '206', '218', '219', '239', '247', '253', '257', '271', '285', '286', '308'] patterns=['tox_connected_bidirectional_count_over_atom_id_only']
- cases=['201', '218', '219', '239', '251', '286', '298', '310', '317'] patterns=['tox_carcinogenic_label_numerator_only']

### Mixed Candidates

- cases=['201', '219', '239'] touched=['tox_bond_condition_to_molecule_scope', 'tox_connected_bidirectional_count_over_atom_id_only']
- cases=['201', '206', '249', '253', '268', '277', '281'] touched=['tox_bond_condition_to_molecule_scope', 'tox_bond_pair_to_connected_atom_single_column']
- cases=['201', '206', '218', '219', '239', '247', '253', '257', '271', '285', '286', '308'] touched=['tox_bond_condition_to_molecule_scope', 'tox_bond_pair_to_connected_atom_single_column', 'tox_connected_bidirectional_count_over_atom_id_only']
- cases=['201', '218', '219', '239', '251', '286', '298', '310', '317'] touched=['tox_bond_condition_to_molecule_scope', 'tox_carcinogenic_label_numerator_only', 'tox_connected_bidirectional_count_over_atom_id_only']

## european_football_2

| metric | value |
|---|---:|
| candidates | 18 |
| complete candidates | 2 |
| mixed candidates | 4 |
| patterns fully co-candidate | 1 / 3 |
| no full co-candidate | 2 |
| no pair co-candidate | 1 |

| pattern | full | max co-cases | pair coverage | fragments |
|---|---|---:|---:|---:|
| `ef2_restore_order_metric_to_select` | no | 1/2 | 0/1 | 0 |
| `ef2_player_attributes_unique_player_grain` | no | 3/4 | 3/6 | 4 |
| `ef2_avg_attribute_sum_count_row_grain` | yes | 2/2 | 1/1 | 2 |

### Complete Candidates

- cases=['1023', '1031', '1040', '1052', '1068', '1087', '1093'] patterns=['ef2_avg_attribute_sum_count_row_grain']
- cases=['1023', '1040', '1052', '1068', '1093', '1094'] patterns=['ef2_avg_attribute_sum_count_row_grain']

### Mixed Candidates

- cases=['1023', '1040', '1068'] touched=['ef2_avg_attribute_sum_count_row_grain', 'ef2_player_attributes_unique_player_grain']
- cases=['1023', '1040', '1045', '1052', '1054', '1068', '1087'] touched=['ef2_avg_attribute_sum_count_row_grain', 'ef2_player_attributes_unique_player_grain']
- cases=['1023', '1031', '1040', '1052', '1068', '1087', '1093'] touched=['ef2_avg_attribute_sum_count_row_grain', 'ef2_player_attributes_unique_player_grain']
- cases=['1023', '1040', '1052', '1068', '1093', '1094'] touched=['ef2_avg_attribute_sum_count_row_grain', 'ef2_player_attributes_unique_player_grain']

## superhero

| metric | value |
|---|---:|
| candidates | 9 |
| complete candidates | 1 |
| mixed candidates | 0 |
| patterns fully co-candidate | 1 / 1 |
| no full co-candidate | 0 |
| no pair co-candidate | 0 |

| pattern | full | max co-cases | pair coverage | fragments |
|---|---|---:|---:|---:|
| `sup_rank_output_format` | yes | 2/2 | 1/1 | 1 |

### Complete Candidates

- cases=['726', '728'] patterns=['sup_rank_output_format']

## financial

| metric | value |
|---|---:|
| candidates | 17 |
| complete candidates | 0 |
| mixed candidates | 5 |
| patterns fully co-candidate | 0 / 3 |
| no full co-candidate | 3 |
| no pair co-candidate | 1 |

| pattern | full | max co-cases | pair coverage | fragments |
|---|---|---:|---:|---:|
| `fin_id_output_contract` | no | 2/3 | 1/3 | 1 |
| `fin_static_config_vs_executed_flow` | no | 1/2 | 0/1 | 0 |
| `fin_count_grain_not_early_distinct` | no | 2/3 | 1/3 | 1 |

### Mixed Candidates

- cases=['130', '141', '142'] touched=['fin_id_output_contract', 'fin_static_config_vs_executed_flow']
- cases=['102', '129', '135', '141', '142', '145'] touched=['fin_count_grain_not_early_distinct', 'fin_id_output_contract', 'fin_static_config_vs_executed_flow']
- cases=['102', '129', '130', '135', '141', '142', '145', '149', '172', '177', '182'] touched=['fin_count_grain_not_early_distinct', 'fin_id_output_contract', 'fin_static_config_vs_executed_flow']
- cases=['94', '107', '110', '130', '141', '142', '144', '152', '177', '179', '186'] touched=['fin_id_output_contract', 'fin_static_config_vs_executed_flow']
- cases=['110', '142', '144', '177', '179', '180', '186', '193'] touched=['fin_id_output_contract', 'fin_static_config_vs_executed_flow']

## california_schools

| metric | value |
|---|---:|
| candidates | 15 |
| complete candidates | 2 |
| mixed candidates | 3 |
| patterns fully co-candidate | 1 / 2 |
| no full co-candidate | 1 |
| no pair co-candidate | 1 |

| pattern | full | max co-cases | pair coverage | fragments |
|---|---|---:|---:|---:|
| `cal_low_grade_frpm_low_grade` | yes | 2/2 | 1/1 | 2 |
| `cal_canonical_school_side_slot` | no | 1/3 | 0/3 | 0 |

### Complete Candidates

- cases=['23', '25', '26', '28', '33', '74', '81'] patterns=['cal_low_grade_frpm_low_grade']
- cases=['74', '81', '83'] patterns=['cal_low_grade_frpm_low_grade']

### Mixed Candidates

- cases=['17', '23', '25', '33', '72', '74'] touched=['cal_canonical_school_side_slot', 'cal_low_grade_frpm_low_grade']
- cases=['23', '25', '26', '28', '33', '74', '81'] touched=['cal_canonical_school_side_slot', 'cal_low_grade_frpm_low_grade']
- cases=['24', '81', '87'] touched=['cal_canonical_school_side_slot', 'cal_low_grade_frpm_low_grade']

## debit_card_specializing

| metric | value |
|---|---:|
| candidates | 9 |
| complete candidates | 2 |
| mixed candidates | 0 |
| patterns fully co-candidate | 1 / 2 |
| no full co-candidate | 1 |
| no pair co-candidate | 1 |

| pattern | full | max co-cases | pair coverage | fragments |
|---|---|---:|---:|---:|
| `deb_customer_year_consumption_grain` | yes | 2/2 | 1/1 | 2 |
| `deb_price_as_aggregatable_amount` | no | 1/2 | 0/1 | 0 |

### Complete Candidates

- cases=['1472', '1475'] patterns=['deb_customer_year_consumption_grain']
- cases=['1472', '1475', '1481'] patterns=['deb_customer_year_consumption_grain']
