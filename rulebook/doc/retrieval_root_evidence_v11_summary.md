# Retrieval Root Evidence v11 Summary

Skip-rewrite 11-db run on current HEAD. Rewrite was not run in this pass.

## Totals

- Manual patterns total: 35
- Manual patterns fully covered by an actual PURE pattern: 2/35
- Manual patterns touched by an actual PURE pattern: 8/35
- Manual patterns touched by an actual MIXED pattern: 10/35
- Candidate-layer fully co-candidate: 13/35
- Candidate-layer mixed candidates: 53

## Per DB

| db | cases | actual patterns | PURE | MIXED | UNMAPPED | manual patterns |
|---|---:|---:|---:|---:|---:|---:|
| card_games | 191 | 6 | 2 | 2 | 2 | 4 |
| codebase_community | 186 | 3 | 1 | 1 | 1 | 4 |
| formula_1 | 174 | 9 | 2 | 3 | 4 | 7 |
| thrombosis_prediction | 163 | 3 | 0 | 0 | 3 | 3 |
| student_club | 158 | 2 | 1 | 0 | 1 | 2 |
| toxicology | 145 | 2 | 1 | 0 | 1 | 4 |
| european_football_2 | 129 | 1 | 0 | 0 | 1 | 3 |
| superhero | 129 | 2 | 1 | 0 | 1 | 1 |
| financial | 106 | 0 | 0 | 0 | 0 | 3 |
| california_schools | 89 | 2 | 0 | 0 | 2 | 2 |
| debit_card_specializing | 64 | 0 | 0 | 0 | 0 | 2 |

## Actual Patterns

### card_games
- `grp-pat-card_games-341-437-d7ac0fb1` MIXED cases=['341', '343', '344', '387', '388', '389', '425', '437'] manual=['card_legalities_uuid_to_card_grain', 'card_rulings_uuid_then_answer_grain']
- `grp-pat-card_games-354-458-efd6561e` UNMAPPED cases=['354', '458'] manual=[]
- `grp-pat-card_games-355-358-de6de35c` UNMAPPED cases=['355', '358', '364', '453', '456'] manual=[]
- `grp-pat-card_games-355-508-d8b5f3a0` PURE cases=['355', '358', '364', '373', '391', '392', '411', '418', '440', '442', '444', '453', '455', '456', '470', '482', '483', '508'] manual=['card_rulings_uuid_then_answer_grain']
- `grp-pat-card_games-387-443-68f1cfac` PURE cases=['387', '388', '428', '430', '431', '432', '433', '441', '442', '443', '447'] manual=['card_set_translation_setcode_bridge']
- `grp-pat-card_games-473-530-12dc2a80` MIXED cases=['473', '494', '530'] manual=['card_named_card_anchor_to_set_layer', 'card_rulings_uuid_then_answer_grain']

### codebase_community
- `grp-pat-codebase_community-603-696-e36d11a9` PURE cases=['603', '631', '642', '696'] manual=['code_user_post_relation_via_posthistory']
- `grp-pat-codebase_community-616-681-de894bbe` MIXED cases=['616', '617', '646', '681', '709', '710'] manual=['code_comment_created_on_comments_creationdate', 'code_comment_score_filter_on_posts_score']
- `grp-pat-codebase_community-672-700-ca1b2f87` UNMAPPED cases=['672', '700'] manual=[]

### formula_1
- `grp-pat-formula_1-855-903-1f153d0c` MIXED cases=['849', '855', '891', '893', '896', '902', '903', '905', '921', '948', '949', '950'] manual=['f1_circuit_info_url', 'f1_driver_standings_path']
- `grp-pat-formula_1-850-854-a1adfe9f` UNMAPPED cases=['850', '851', '853', '854', '856', '857', '868'] manual=[]
- `grp-pat-formula_1-850-860-fbbff538` UNMAPPED cases=['850', '851', '853', '856', '857', '860', '868'] manual=[]
- `grp-pat-formula_1-888-989-6d67f555` UNMAPPED cases=['888', '989', '993'] manual=[]
- `grp-pat-formula_1-889-1006-bcd11d9c` MIXED cases=['889', '1006'] manual=['f1_display_semantics_direct_field_slot', 'f1_time_text_parse_numeric']
- `grp-pat-formula_1-891-952-815dc6e1` MIXED cases=['891', '893', '902', '929', '950', '952'] manual=['f1_driver_standings_path', 'f1_ranked_k_results_rank']
- `grp-pat-formula_1-893-966-72525060` PURE cases=['893', '896', '905', '948', '966', '995'] manual=['f1_driver_standings_path']
- `grp-pat-formula_1-949-984-1a9e7997` UNMAPPED cases=['949', '984'] manual=[]
- `grp-pat-formula_1-970-973-489d09c5` PURE cases=['970', '973', '985'] manual=['f1_pitstops_raw_detail_grain']

### thrombosis_prediction
- `grp-pat-thrombosis_prediction-1155-1159-6c2af783` UNMAPPED cases=['1155', '1159', '1172', '1214', '1220', '1225'] manual=[]
- `grp-pat-thrombosis_prediction-1159-1209-923d0173` UNMAPPED cases=['1159', '1209', '1232'] manual=[]
- `grp-pat-thrombosis_prediction-1186-1187-ed2e45a3` UNMAPPED cases=['1186', '1187'] manual=[]

### student_club
- `grp-pat-student_club-1366-1451-b9092129` PURE cases=['1366', '1451'] manual=['stu_entity_question_output_primary_key']
- `grp-pat-student_club-1447-1465-24ae0337` UNMAPPED cases=['1447', '1465'] manual=[]

### toxicology
- `grp-pat-toxicology-206-249-b5991530` PURE cases=['206', '249', '253', '268', '277', '302', '307'] manual=['tox_bond_pair_to_connected_atom_single_column']
- `grp-pat-toxicology-264-296-8a350c0b` UNMAPPED cases=['264', '267', '296'] manual=[]

### european_football_2
- `grp-pat-european_football_2-1035-1045-32928125` UNMAPPED cases=['1035', '1045', '1053', '1065', '1066', '1067', '1071', '1099', '1129', '1130', '1141'] manual=[]

### superhero
- `grp-pat-superhero-726-728-fa8a0823` PURE cases=['726', '728'] manual=['sup_rank_output_format']
- `grp-pat-superhero-771-797-335fe2c0` UNMAPPED cases=['771', '797'] manual=[]

### financial
- none

### california_schools
- `grp-pat-california_schools-31-85-8226bf83` UNMAPPED cases=['31', '85'] manual=[]
- `grp-pat-california_schools-53-72-50504ba8` UNMAPPED cases=['53', '72'] manual=[]

### debit_card_specializing
- none
