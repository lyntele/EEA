                                                       
● EEA Pattern 识别层改造 — 详细实施计划                                                         
                                                              
  设计原则                                                                                      
                                               
  pattern.bias_recognition_contract  →  轻识别（"是不是这种错"）                                
  branch.required_signals + binder    →  严实例化（"这题怎么改"）                               
                                                                                                
  不退化已有 7/18 的 singleton 收益是硬约束。每个工作单元都有"破坏即回退"的边界。               
                                                                                                
  ---                                                                                           
  WU1：工程基础（无业务依赖，先做）                              
                                                                                                
  WU1a — DeepEye 字段透传                      
                                                                                                
  目的：让 EEA 已经在写的诊断字段真正落到 jsonl，否则后面 WU2/3 的所有验证都瞎。                
                                                                 
  改动：                                                                                        
  - 文件：/data/liuyining/ace4sql/method/deepeye/DeepEye-SQL/rulebook_experiments/eea_contract_a
  dapter.py:656-686                                                                             
  - add_row() 字段集合追加：                   
  "rewrite_enabled_reason": row.get("rewrite_enabled_reason"),                                  
  "trigger_blocker_counts": row.get("trigger_blocker_counts") or {},                            
  "top_candidate_reasons": row.get("top_candidate_reasons") or [],                              
  "selected_branch_ids": row.get("selected_branch_ids") or {},                                  
  "memory_selection_audit": row.get("memory_selection_audit") or {},                            
  "compiler_empty_reason_counts": row.get("compiler_empty_reason_counts") or {},
  "replay_trigger_diagnostics": row.get("replay_trigger_diagnostics") or {},                    
                                                              
  验收边界：                                                                                    
  - 任意 r6 跑后，eea_promotion_replay_rows.jsonl 第一行的 keys 集合包含上述 7 个新字段
  - 对 r5/142851 数据回填检查（用现有数据做静态校验），新字段应能从 EEA 内部已存的              
  replay_trigger_diagnostics 字段读出非空内容                 
                                                                                                
  ---                                                         
  WU1b — closure 收紧（防止后续工作里 16-case 怪反扑）                                          
                                                                                                
  目的：阻止 _pair_supports_root_membership 把 toxicology 不同 misconception 的 case 闭包成单
  root pattern。                                                                                
                                                              
  改动：                                                                                        
  - 文件：rulebook/common/learning/pattern_formation.py:2423-2440
  - 接受集从 4 种降到 2 种：                                                                    
  return str(pair.semantic_relation or "") in {
      "compatible",                                                                             
      # 删除 "partial" / "direct_merge_veto" / "core_program_signature_conflict"                
  }                                                                                             
  # 如果保留 "partial"，必须叠加额外条件：                                                      
  # pair.broad_retrieval_reasons 必须包含至少一项强证据：                                       
  #   "shared_primary_repair_locus" 或                                                          
  #   "shared_root_effect_axis_with_same_target_invariant_family"                               
                                                                                                
  验收边界：                                                                                    
  - 重跑 final_evolve（用 r5 已有 18 singletons 作输入）                                        
  - 期待结果：不再出现 cases ≥ 12 的 pattern；当前的 16-case grp-pat-198-338 应消失或拆成 ≥ 3   
  个独立 root patterns                                                                          
  - toxicology pattern 1（bond-endpoint）成员（206/249/253/268/277/285/302/307）应聚成一个      
  root；pattern 2/3 各自独立                                                              
                                                                                                
  回退条件：如果收紧后 final patterns 数 ≤ 2（即过度收敛），回到 4-relation
  集合并改为加权评分（compatible=1.0, partial=0.5, ...）。                                      
                                                              
  ---                                                                                           
  WU2：pattern bias_recognition_contract（核心机制 1）        
                                                                                                
  WU2a — 数据结构                              
                                                                                                
  改动：                                                      
  - 文件：rulebook/common/core/data_structures.py                
  - 新增 Pydantic 模型：                                                                        
  class BiasRecognitionContract(BaseModel):
      schema_version: Literal["bias-recognition-v1"]                                            
      bias_motif: str                       # 来自 admission，e.g. "duplicate_role_side_output"
      answer_shape_hint: str                # e.g. "scalar" / "subset" /                        
  "preserved_aggregate_unit"                                                                    
      recognition_signals: List[str]        # 3-5 个，全部来自                                  
  BIAS_RECOGNITION_SIGNAL_VOCABULARY                                                            
      anti_signals: List[str] = []          # 反向排除（满足任一即不识别）                      
      min_signal_overlap: float = 0.6                                     
  - 在 InstantiationProgram 加可选字段 bias_recognition_contract:                               
  Optional[BiasRecognitionContract]                               
                                                                                                
  WU2b — vocabulary 与 builder                                
                                                                                                
  改动：                                                      
  - 文件：rulebook/common/core/vocabulary.py                                                    
  - 新增白名单常量：                                          
  BIAS_RECOGNITION_SIGNAL_VOCABULARY = {                         
      # 输出 shape 偏差类                                                                       
      "has_pair_role_side_output",        # SELECT 输出双端点
      "same_relation_two_role_sides",     # 两端点同表                                          
      "select_arity_ge_2",                                                                      
      "no_distinct_on_pair_output",                                                             
      "select_role_dtype_homogeneous",                                                          
      # 聚合单位类                                                                              
      "has_aggregate_in_select",                                 
      "answer_unit_count_distinct",                                                             
      "answer_unit_count_plain",                                                                
      "answer_unit_scalar_aggregate",                            
      # source-route 类                                                                         
      "has_join_chain_via_bridge_table",  # 经 connected/bridge 走 atom-bond
      "has_direct_relation_join",                                                               
      "has_predicate_outside_aggregate_scope",                                                  
      # 其他                                                                                    
      "has_group_by",                                                                           
      "has_order_by_limit",                                                                     
  }                                                                                             
  - 文件：rulebook/common/runtime/runtime.py 在 build_current_case_signals 附近
  - 加 compute_bias_recognition_signals(case_view) -> Dict[str, bool]：把白名单里每个信号       
  builder 跑一遍。多数 builder 应能复用现有 signal_summary                                      
  里的逻辑（pred.has_pair_role_side_output 等已经存在）。                                       
  - case_view 加字段 bias_recognition_signals: Dict[str, bool]                                  
                                                                                                
  WU2c — admission_judge prompt 改造                                                            
                                                                                                
  改动：                                                                                        
  - 文件：rulebook/common/llm/prompts/pattern_admission_judge.py                                
  - prompt 末尾追加段：                                                                         
  ## 识别条件抽取 (bias_recognition_contract)                    
                                                                                                
  对 admit 的 root pattern，列出 3-5 个**现象级**信号让 runtime 在新 SQL
  上识别"这是同一种错"。要求：                                                                  
                                                              
  1. 信号必须从下列白名单选 (BIAS_RECOGNITION_SIGNAL_VOCABULARY): [枚举]                        
  2. 不允许提具体表/列名                                                                        
  3. 信号要能"轻"匹配——3-5 个之中命中 60% 即认为识别成功                                        
  4. 选反向排除信号 (anti_signals) 防止误识别                                                   
                                                                                                
  输出 JSON:                                                                                    
  {                                                                                             
    "bias_recognition_contract": {                                                              
      "bias_motif": "...",                                       
      "answer_shape_hint": "...",                                
      "recognition_signals": ["...", "..."],                                                    
      "anti_signals": ["..."],              
      "min_signal_overlap": 0.6                                                                 
    }                                                                                           
  }                                                              
                                                                                                
  WU2d — admission 输出落地                                   
                                                                 
  改动：                                       
  - 文件：rulebook/common/learning/pattern_formation.py
  - 在 _call_pattern_admission_judge 之后加：                                                   
  brc = response.get("bias_recognition_contract") or {}
  if brc:                                                                                       
      # 校验信号词都在白名单里                                
      sigs = [s for s in (brc.get("recognition_signals") or [])                                 
              if s in BIAS_RECOGNITION_SIGNAL_VOCABULARY]                                       
      anti = [s for s in (brc.get("anti_signals") or [])                                        
              if s in BIAS_RECOGNITION_SIGNAL_VOCABULARY]                                       
      if 3 <= len(sigs) <= 6:                                                                   
          response["bias_recognition_contract_validated"] = {    
              "bias_motif": brc.get("bias_motif", ""),                                          
              "answer_shape_hint": brc.get("answer_shape_hint", ""),
              "recognition_signals": sigs,                                                      
              "anti_signals": anti,                           
              "min_signal_overlap": float(brc.get("min_signal_overlap", 0.6)),                  
          }                                                                                     
  - 在 _build_pattern_candidate 里把 validated contract 写到     
  candidate_pattern.instantiation_program.bias_recognition_contract                             
                                                                                                
  验收边界（WU2 整体）：                                         
  - r6 final library 至少 6/9 patterns 有非空 bias_recognition_contract                         
  - 所有 contract 内 recognition_signals 全部命中白名单                                         
  - 不存在具体表名/列名串入 contract（pre-commit 检查）          
  - toxicology bond-endpoint pattern 的 bias_motif 应在 {"duplicate_role_side_output",          
  "pair_endpoint_over_projection"} 之列                                                         
  - toxicology source-route pattern 的 bias_motif 应在 {"wrong_join_route",                     
  "indirect_via_bridge_table"} 之列                                                             
                                                                                                
  回退条件：如果 LLM 拒绝产 contract（response 没有该字段超 80%），降级用代码侧的 fallback：从
  group 内 singleton 共有的 trigger_contract.required_signals 投票出 top-3 signal 作为          
  recognition_signals。                                       
                                                                                                
  ---                                                         
  WU3：trigger 两段化（核心机制 2）                              
                                                                                                
  WU3a — _gate_group 改造（段 1 轻识别）
                                                                                                
  改动：                                                      
  - 文件：rulebook/common/runtime/runtime.py 内 _gate_group（约 :1900 起）                      
  - 在 PATTERN 候选的 trigger_contract 严格检查之前插入：                                       
  if (group.group_type == GroupType.PATTERN                      
      and group.instantiation_program.bias_recognition_contract is not None):                   
      brc = group.instantiation_program.bias_recognition_contract                               
      case_signals = case_view.bias_recognition_signals or {}                                   
                                                                                                
      # anti_signals 任一命中 → hard fail                        
      anti_hits = [s for s in brc.anti_signals if case_signals.get(s)]                          
      if anti_hits:                                                   
          audit.hard_gate_reasons.append(f"bias_anti_signal_hit:{','.join(anti_hits)}")         
          return audit                                                                 
                                                                                                
      # recognition_signals 命中率 < min_signal_overlap → hard fail
      hits = sum(1 for s in brc.recognition_signals if case_signals.get(s))                     
      total = len(brc.recognition_signals)                                 
      overlap = hits / max(total, 1)                                                            
      audit.bias_recognition = {                              
          "matched_signals": hits, "total": total, "overlap": overlap,                          
          "bias_motif": brc.bias_motif,                                                         
      }                                                          
      if overlap < brc.min_signal_overlap:                                                      
          audit.hard_gate_reasons.append(                     
              f"bias_recognition_signals_missed:{hits}/{total}@{overlap:.2f}"                   
          )                                                                                     
          return audit                                           
                                                                                                
      audit.bias_recognized = True                            
      # 跳过原 trigger_contract.required_signals 严格匹配                                       
      # 直接进入 _select_runtime_branch 段 2             
      return _select_branch_for_pattern(group, case_view, audit)                                
  - 原有的"singleton + 没 bias_recognition_contract 的 pattern"路径不变                         
                                                                                                
  WU3b — pattern_recognized_branch_unbindable 新状态                                            
                                                                                                
  改动：                                                                                        
  - 文件：rulebook/common/runtime/runtime.py 在 _select_branch_for_pattern 内                   
  - 当 bias_recognized=True 但所有 branches 都 binder dry-run 失败时：                          
  if audit.bias_recognized and not branch_match_audit.has_bindable_branch:
      audit.gate_passed = False                                                                 
      audit.hard_gate_reasons.append("pattern_recognized_branch_unbindable")                    
      audit.diagnostic_only = True   # 标记：识别成功但仅作审计                                 
  - _select_compatible_groups 看 diagnostic_only=True 的候选不进 selection_pool，但写入 audit   
                                                                                                
  WU3c — runtime_audit_summary 加段 1/2 计数                                                    
                                                                                                
  改动：                                                                                        
  - 文件：rulebook/common/runtime/runtime.py 在 audit summary 处加：                            
  "stage_1_bias_recognized_count": ...,                                                         
  "stage_1_bias_signals_missed_count": ...,                      
  "stage_2_branch_ready_count": ...,                                                            
  "stage_2_branch_unbindable_count": ...,                                                       
                                                                                                
  验收边界（WU3 整体）：                                                                        
  - r6 trigger blocker 计数中 bias_recognition_signals_missed 必须出现且 ≥ 30% 占比（替代部分原 
  required_contract_signals_missed）                                                            
  - r6 至少 2 个 case 的 trigger 出现 bias_recognized=True（即 pattern 被识别），无论最后是     
  ready 还是 branch_unbindable                                                                  
  - pattern_recognized_branch_unbindable 计数应 ≥                                               
  1（说明识别成功但实例化失败的有价值数据被记下来了）            
  - r6 enhanced_correct ≥ 7（不破坏 r5 singleton 路径收益）—— 硬约束                            
                                                                    
  回退条件：                                                                                    
  - 如果 r6 enhanced_correct < 7：立刻把 WU3a 的段 1 改动加 feature flag                        
  EEA_PATTERN_TWO_STAGE_TRIGGER=False 关掉                                                      
  - 如果 r6 patterns 触发了但全错（regression ≥ 2）：把 min_signal_overlap 默认值从 0.6 提到 0.8
                                                                                                
  ---                                                                                           
  WU4：trigger_contract 字段同步（清理悬空对象）                 
                                                                                                
  改动：                                                      
  - 文件：rulebook/common/learning/pattern_formation.py 在 _build_pattern_candidate 之后        
  - 新增 _sync_trigger_contract_from_envelope_and_admission(candidate)：                        
  def _sync_trigger_contract_from_envelope_and_admission(group):        
      ip = group.instantiation_program                                                          
      tc = ip.trigger_contract or TriggerContract()                                             
      envelope = ip.synthesized_program.program_envelope                                        
                                                                                                
      # 1. runtime_branches: 从 envelope 同步过来                                               
      tc.runtime_branches = list(envelope.runtime_branches or [])                               
                                                                                                
      # 2. required_signals: 从 envelope.runtime_branches 各 branch 的                          
      #    required_signals 求并集（pattern 顶层接受任一 branch 的入口信号）                    
      tc.required_signals = sorted({                                                            
          s for b in tc.runtime_branches for s in (b.get("required_signals") or [])             
      })                                                                           
                                                                                                
      # 3. action_contract.locus / op_family: 从 program 的主 op 派生
      main_op = ...  # 第一个 non-dependency op                                                 
      tc.action_contract.locus = main_op.locus                
      tc.action_contract.op_family = main_op.op_family                                          
                                                                                                
      # 4. canonical_discriminants: 用 admission.bias_recognition_contract.recognition_signals  
      brc = ip.bias_recognition_contract                                                        
      if brc:                                                    
          tc.canonical_discriminants = list(brc.recognition_signals)                            
                                                                    
      ip.trigger_contract = tc                                                                  
                                                                                                
  验收边界：                                                     
  - r6 final library 任意 pattern：trigger_contract.required_signals 不为 None                  
  - trigger_contract.runtime_branches 长度 == program_envelope.runtime_branches 长度            
  - trigger_contract.action_contract.locus 不为 None                                
                                                                                                
  ---                                                                                           
  WU5：accessory action 进 compiler 主链（branch 实例化能力）                                   
                                                                                                
  WU5a — DROP_SELECT_SLOT 自动 bind cleanup_edits                
                                                                                                
  目的：直接修 r5 q302 类 "rewrite 删了列没删 a2 JOIN" 问题。                                   
                                                                                                
  改动：                                                                                        
  - 文件：rulebook/common/runtime/action_compiler.py 内 enumerate 函数，在 DROP_SELECT_SLOT
  primitive 产出 candidate 时：                                                                 
  def _bind_cleanup_edits_for_drop_select(candidate, case_view):
      drop_alias = _alias_of(candidate.arguments.get("from_expr"))                              
      if not drop_alias:                                                                        
          return                                                                                
      # 在当前 SQL 的 FROM/JOIN 里找 alias 失依的 JOIN block                                    
      remaining_refs = _alias_references_after_drop(case_view.pred_sql,                         
                                                    drop_alias,                                 
                                                    drop_select_only=True)                      
      if not remaining_refs:                                                                    
          # alias 失依 → 加进 cleanup_edits                                                     
          join_block = _join_block_for_alias(case_view.pred_sql, drop_alias)                    
          candidate.cleanup_edits = list(candidate.cleanup_edits or []) + [                     
              {"kind": "drop_join_block", "alias": drop_alias, "text": join_block}              
          ]                                                                                     
  - 文件：rulebook/common/llm/nodes.py _rewrite_contract_prompt_payload 把 cleanup_edits 写进   
  contract                                                                                   
  - 文件：rulebook/common/llm/nodes.py _enforce_rewrite_contract_absence_checks 把 cleanup_edits
   中的 JOIN 文本加进 absence checks                          
                                                                                                
  验收边界：                                                  
  - q302 重 run rewrite 不再含 JOIN atom a2 ON c.atom_id2 = a2.atom_id                          
  - promotion replay 的 branch_runtime_replay_duplicate_rows blocker 计数从 r5 的 5/9 patterns  
  降到 ≤ 2/9                                                                                  
                                                                                                
  WU5b — SELECT_ENFORCE_DISTINCT accessory                    
                                                                                                
  改动：                                                      
  - 文件：rulebook/common/core/vocabulary.py 加 primitive SELECT_ENFORCE_DISTINCT               
  - 文件：rulebook/common/runtime/action_compiler.py:                                           
  # 在 DROP_SELECT_SLOT bind 后追加：                            
  if (candidate.primitive == "DROP_SELECT_SLOT"                                                 
      and case_view.bias_recognition_signals.get("has_pair_role_side_output")                   
      and not case_view.bias_recognition_signals.get("source_has_distinct")):                   
      candidate.cleanup_edits.append({"kind": "enforce_distinct"})                              
  - rewrite_contract 把 enforce_distinct 表达成 "rewritten SELECT must contain DISTINCT"        
                                                                                                
  验收边界：                                                                                    
  - r6 q302 rewrite 含 SELECT DISTINCT a1.element                                               
  - 任何 has_pair_role_side_output=True 且 source 无 DISTINCT 的 case，rewrite 都自动加 DISTINCT
                                                                                                
  WU5c — answer_unit_preserve invariant（仅当 r6 数据需要）                                     
                                                                                                
  触发条件：r6 在 q335/q338/q306 上看到 bias_recognized=True 但 rewrite 改坏方向。              
                                                                                                
  改动：                                                                                        
  - 文件：rulebook/common/core/data_structures.py 加 PreserveConstraint("answer_unit")
  - 在 admission 输出 source-route 类 pattern 时，自动给所有 branch 加这条 invariant            
  - rewrite_contract 显式禁止改 SELECT 的 COUNT / 聚合主体                          
  - required_absence_checks 加上："S0 中的 COUNT(...) 主体 expr 必须保留在 rewrite_sql 中"      
                                                                                                
  验收边界：                                                                                    
  - q335 rewrite 不再把 COUNT(DISTINCT m.molecule_id) 改成 COUNT(b.bond_id)                     
  - 如果 route 修对：selector_choose_s1 +1                                                      
  - 如果 route 改不对：rewrite fail-closed 回 S0（不再 keep_s0 而 selector pick S0）
                                                                                                
  ---                                                                                           
  WU6：r6 端到端验证                                                                            
                                                                                                
  6a — 跑通完整 run                                              
                                                                                                
  python method/EEA/rulebook/cli/run_online_e2e_validation.py \
    --db_id toxicology \                                                                        
    --case_ids 198,201,206,207,249,253,263,268,269,277,285,302,306,307,326,328,335,338 \        
    --work_root <deepeye/.state/work> \                                                         
    --output_dir <toxicology_focus18_postsel_v1_qwen3coderflash_20260509_r6> \                  
    --family_runtime_policy replay_gated \                                                      
    --promotion_interval 1 \                                                                    
    --promotion_min_support 2 \                                                                 
    --strict_contract_policy continue \                                                         
    --save_library_snapshots                                                                    
                                                                 
  6b — 量化验收 checklist                                                                       
                                                              
  ┌────────────────────────────────────────┬──────────────────────┬────────┬────────────────┐   
  │                  项目                  │     r5 baseline      │  r6    │   必要/期望    │
  │                                        │                      │  目标  │                │   
  ├────────────────────────────────────────┼──────────────────────┼────────┼────────────────┤
  │ enhanced_correct                       │ 7/18                 │ ≥ 7    │ 必要（不退化） │
  ├────────────────────────────────────────┼──────────────────────┼────────┼────────────────┤   
  │ enhanced_correct                       │ 7/18                 │ ≥ 9    │ 期望           │
  ├────────────────────────────────────────┼──────────────────────┼────────┼────────────────┤   
  │ regressed_qids                         │ 0                    │ 0      │ 必要           │   
  ├────────────────────────────────────────┼──────────────────────┼────────┼────────────────┤
  │ bias_recognition_signals_missed        │ N/A                  │ ≥ 30%  │ 必要           │   
  │ blocker                                │                      │ 占比   │                │
  ├────────────────────────────────────────┼──────────────────────┼────────┼────────────────┤   
  │ trigger 上 bias_recognized=True 的     │ 0                    │ ≥ 4    │ 必要           │
  │ case                                   │                      │        │                │   
  ├────────────────────────────────────────┼──────────────────────┼────────┼────────────────┤
  │ trigger 上 selected 出现 pattern_id    │ 0                    │ ≥ 1    │ 必要           │   
  ├────────────────────────────────────────┼──────────────────────┼────────┼────────────────┤
  │ pattern_recognized_branch_unbindable   │ 0                    │ ≥ 1    │ 必要           │   
  │ count                                  │                      │        │                │
  ├────────────────────────────────────────┼──────────────────────┼────────┼────────────────┤   
  │ final patterns 含 cases ≥ 12           │ 1（grp-pat-198-338） │ 0      │ 必要           │
  ├────────────────────────────────────────┼──────────────────────┼────────┼────────────────┤
  │ 9 patterns 中                          │ 0                    │ ≥ 6    │ 必要           │
  │ bias_recognition_contract 非空         │                      │        │                │   
  ├────────────────────────────────────────┼──────────────────────┼────────┼────────────────┤
  │ q302 rewrite 含 DISTINCT 且无 a2 JOIN  │ ✗                    │ ✓      │ 必要           │   
  ├────────────────────────────────────────┼──────────────────────┼────────┼────────────────┤   
  │ q335 rewrite 不改 COUNT 主体（如做了   │ ✗                    │ ✓      │ 期望           │
  │ WU5c）                                 │                      │        │                │   
  ├────────────────────────────────────────┼──────────────────────┼────────┼────────────────┤
  │ branch_runtime_replay_duplicate_rows   │ 5/9 patterns         │ ≤ 2/9  │ 必要           │   
  │ 占比                                   │                      │        │                │   
  ├────────────────────────────────────────┼──────────────────────┼────────┼────────────────┤
  │ jsonl 含新诊断字段                     │ ✗                    │ ✓      │ 必要           │   
  └────────────────────────────────────────┴──────────────────────┴────────┴────────────────┘   
                                                                 
  6c — 失败回退矩阵                                                                             
                                                              
  ┌─────────────────────────────────────────┬───────────────────────────────────────────────┐   
  │               r6 实际表现               │                   立即处置                    │
  ├─────────────────────────────────────────┼───────────────────────────────────────────────┤   
  │ enhanced_correct < 7                    │ 关 WU3 段 1 feature flag，保留                │
  │                                         │ WU2/WU4/WU5/WU1，再跑 r6.1                    │
  ├─────────────────────────────────────────┼───────────────────────────────────────────────┤   
  │ 段 1 通了但 patterns 全错 (regression ≥ │ min_signal_overlap 0.6→0.8，再跑 r6.1         │   
  │  2)                                     │                                               │   
  ├─────────────────────────────────────────┼───────────────────────────────────────────────┤   
  │ pattern 触发但 selector 全 keep_s0      │ 优先做 WU5c，再跑 r6.2                        │
  ├─────────────────────────────────────────┼───────────────────────────────────────────────┤   
  │ branch_runtime_replay_duplicate_rows 仍 │ 检查 WU5a 的 alias 失依判断逻辑（看 q302 的   │
  │  ≥ 4/9                                  │ cleanup_edits 是否真生成）                    │   
  ├─────────────────────────────────────────┼───────────────────────────────────────────────┤
  │ bias_recognition_contract 6/9 不达标    │ 看 WU2c LLM trace 是否产了字段；若没产，启用  │   
  │                                         │ fallback 投票 builder                         │   
  ├─────────────────────────────────────────┼───────────────────────────────────────────────┤
  │ 新出现 cases≥12 巨型 root               │ WU1b 没生效，重检                             │   
  │                                         │ _pair_supports_root_membership 实际改动       │   
  └─────────────────────────────────────────┴───────────────────────────────────────────────┘
                                                                                                
  ---                                                         
  实施顺序图                                                     
                                               
  Day 1 (WU1: 工程基础)
    WU1a (DeepEye 字段透传, 0.5h)                                                               
    WU1b (closure 收紧, 1h)                                                                     
    WU1 联跑 final_evolve 验证 → 看 patterns 是否拆开                                           
                                                                                                
  Day 2-3 (WU2: bias 契约)                                       
    WU2a 数据结构 (1h)                                                                          
    WU2b vocabulary + builder (4h)                                                              
    WU2c prompt 改造 (3h)                                        
    WU2d 落地解析 + fallback (2h)                                                               
    WU2 联跑 final_evolve → 看 contract 是否产出                                                
                                                                                                
  Day 4 (WU3: trigger 两段化)                                                                   
    WU3a/b/c (4-5h)                                              
    在线 probe q277/q302 → 看 bias_recognized 标志                                              
    完整 r6 跑（先跑这一段，看是否破坏 baseline）                                               
                                                                                                
  Day 5 (WU4: trigger_contract 同步)                                                            
    WU4 (2h)                                                                                    
    快速重跑 → 看 trigger_contract.runtime_branches 同步                                        
                                                                                                
  Day 6 (WU5: accessory action)                                  
    WU5a (3h) → 跑 r6.1 验证 q302 / duplicate_rows blocker                                      
    WU5b (1h) → 跑 r6.2 验证 DISTINCT                                                           
    WU5c 待 r6 数据决定 (4h if needed)                                                          
                                                                                                
  Day 7 (WU6: 完整 r6 + 验收)                                                                   
    完整 r6 跑 + 验收 checklist + 数据归档到 experiment_log                                     
                                                                                                
  硬约束总结                                                     
                                                                                                
  1. 不退化 r5 的 7/18 singleton 收益：WU3 段 1 必须有 feature flag 一键关闭                    
  2. 每个 WU 完成后必须独立验证：不允许多个 WU 一起跑后才验证    
  3. r6 jsonl 上诊断字段必须可见：WU1a 不做完，后面所有验证都是猜                               
  4. closure 收紧（WU1b）必须先于 WU2/3：否则 bias_recognition_contract 抽到 16-case 大 root    
  上会同时混不同 misconception 的信号                                                           
                                                                                                
  一句话                                                                                        
                                                                 
  整个计划核心是：先建工程地基（WU1）→ 给 pattern 注入"轻识别"机制（WU2）→ 把 trigger           
  改成"轻识别+严实例化"两段（WU3）→ 同步遗留字段（WU4）→ 补 branch 实例化的 accessory action 
  能力（WU5）→ 端到端 r6 验证（WU6）。验收以 trigger 上 bias_recognized=True 出现 ≥4 次和 r6    
  不退化为硬基线，以 enhanced_correct ≥ 9 和 pattern 真正参与 trigger 为期望突破。