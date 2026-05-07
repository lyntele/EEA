"""Vocabulary & threshold constants for the Minimal Repair Interface v2 stack.

This module is the single source of truth for:
- repair_skeleton 四元组枚举（LOCUS / OP_FAMILY / TARGET_FAMILY / OUTPUT_CONTRACT）
- tag vocabulary 类型层（封闭）。具体值是 per-case slot fillings，不进类型枚举
- ActionPrimitive 10 个（Action Compiler 支持的动作基元）
- 阈值常量：分"先验可直接定"和"TBD 校准"两类
- 5 个 codex 风险 patch 相关常量

Design principles:
- 类型层封闭，**具体值开放**——避免跨库 vocabulary 爆炸
- 阈值分两类，TBD 的标注清楚、不藏
- 所有枚举用 str Enum，方便 JSON 序列化
"""

from __future__ import annotations

from enum import Enum


# =============================================================================
# Repair Skeleton 四元组枚举
# 见 doc/plan.md §7.4。跨 DB 共享，单库 scoped 使用
# =============================================================================


class Locus(str, Enum):
    """修复发生在 SQL 的哪个语法位点。"""

    SELECT = "SELECT"
    JOIN = "JOIN"
    WHERE = "WHERE"
    GROUP_BY = "GROUP_BY"
    ORDER_BY = "ORDER_BY"
    FACT_ROUTE = "FACT_ROUTE"  # 主事实表层切换（trans vs order vs account.frequency）
    GRAIN = "GRAIN"            # 计数/分组粒度（entity vs row vs pair）
    SCOPE = "SCOPE"            # 条件作用域（WHERE vs CASE vs HAVING）
    AGGREGATION = "AGGREGATION"
    FORMAT = "FORMAT"          # 输出展示（rank/date/time 等 display slot）
    PREDICATE = "PREDICATE"
    BRIDGE = "BRIDGE"          # 中间表/桥接（setCode / legalities.uuid / rulings.uuid）


class OpFamily(str, Enum):
    """修复动作的大类。与 Locus 正交——同一 Locus 可接多种 OpFamily。"""

    ADD = "add"
    REPLACE = "replace"
    DROP = "drop"
    REROUTE = "reroute"          # 主体换层
    COLLAPSE = "collapse"        # 合并到更窄粒度（例如去 pair 化）
    MOVE = "move"                # 条件/槽位迁移（WHERE -> CASE）
    BRIDGE = "bridge"            # 插入桥接表
    MATERIALIZE = "materialize"  # 补 ranking metric / display slot
    RECOUNT = "recount"          # 计数锚点下沉（client_id -> account_id）
    BROADEN = "broaden"          # scope 放宽
    NARROW = "narrow"            # scope 收窄


class TargetFamily(str, Enum):
    """修复动作作用的对象类型。"""

    SLOT = "slot"                # select / output slot
    FACT_TABLE = "fact_table"
    BRIDGE_PATH = "bridge_path"
    GRAIN_KEY = "grain_key"
    CONDITION = "condition"
    DISPLAY_FIELD = "display_field"
    METRIC = "metric"
    SHAPE = "shape"              # pair / single / arity


class OutputContract(str, Enum):
    """Legacy exact SELECT arity labels kept for backward compatibility.

    New code should use ``OutputShapeDelta`` on repair skeletons for
    pred/target arity, delta magnitude, and direction. This enum must not be
    used as the primary pattern boundary for generic N-to-M output changes.
    """

    UNCHANGED = "UNCHANGED"
    ONE_TO_ONE = "1TO1"
    TWO_TO_ONE = "2TO1"
    ONE_TO_TWO = "1TO2"
    ROW_TO_ENTITY = "ROW_TO_ENTITY"
    ENTITY_TO_ROW = "ENTITY_TO_ROW"
    PAIR_TO_SET = "PAIR_TO_SET"
    SET_TO_PAIR = "SET_TO_PAIR"


# =============================================================================
# Tag vocabulary 类型层（封闭）
# 具体值（表名/列名/粒度 specific composite）留给 instantiation slots 填，不进此处
# =============================================================================


class QuestionTargetRole(str, Enum):
    """题面目标实体在当前库 schema 里扮演的抽象角色。"""

    PRIMARY_ENTITY = "primary_entity"        # 题面主语实体
    RELATED_ENTITY = "related_entity"        # 关联实体
    CONTAINER_ENTITY = "container_entity"    # 容器/集合级
    ATTRIBUTE_ENTITY = "attribute_entity"    # 属性/快照记录层


class AnswerSlotType(str, Enum):
    """题面要求的答案槽位类型。"""

    ID = "id"
    NAME = "name"
    URL = "url"
    DATE = "date"
    TIME = "time"
    RANK = "rank"
    CATEGORY = "category"
    COUNT = "count"
    RATIO = "ratio"
    STATUS = "status"
    LOCATION = "location"
    METRIC = "metric"
    LABEL = "label"
    CODE = "code"


class GrainType(str, Enum):
    """答案的粒度抽象。"""

    ENTITY = "entity"          # 唯一实体
    ROW = "row"                # 原始行
    PAIR = "pair"              # 双端 pair
    COMPOSITE = "composite"    # 组合粒度（时间段 x 实体等）


class OpType(str, Enum):
    """题面要求的运算类型。"""

    LOOKUP = "lookup"
    COUNT = "count"
    DISTINCT_COUNT = "distinct_count"
    SUM = "sum"
    AVG = "avg"
    RATIO = "ratio"
    RANK = "rank"
    TOPK = "topk"
    SAMPLE = "sample"
    EXTREMUM = "extremum"
    CONDITIONAL_AGGREGATE = "conditional_aggregate"


class RouteCue(str, Enum):
    """题面里指向主体层路由的线索类型。"""

    CONFIG = "config"                  # 静态配置（frequency / order）
    EXECUTED_FLOW = "executed_flow"    # 已发生流水（trans）
    CANONICAL_SLOT = "canonical_slot"  # canonical school-side / canonical field
    ROLE_ANCHOR = "role_anchor"        # OWNER/DISPONENT 类角色锚点
    STORAGE_BIAS = "storage_bias"      # benchmark 存储偏好（connected 单列展开）


class PredManifestationType(str, Enum):
    """pred_sql 当前的抽象症状类型（不含 DB-specific 表名）。"""

    STOPPED_AT_NATURAL_LAYER = "stopped_at_natural_layer"
    USED_PROXY_FIELD = "used_proxy_field"
    USED_DISTINCT_EARLY = "used_distinct_early"
    MISSING_BRIDGE = "missing_bridge"
    MISROUTE_FACT = "misroute_fact"
    TEXT_TIME_SORT = "text_time_sort"
    PAIR_OUTPUT = "pair_output"
    MISSING_RANK_SLOT = "missing_rank_slot"
    MISSING_SECOND_SLOT = "missing_second_slot"
    SELECT_NAME_ONLY = "select_name_only"
    SELECT_SINGLE_ID_ONLY = "select_single_id_only"
    AGG_WITHOUT_ROLE_ANCHOR = "agg_without_role_anchor"
    ROW_GRAIN_NOT_ENTITY = "row_grain_not_entity"
    COUNT_DISTINCT_ENTITY = "count_distinct_entity"
    COUNT_RAW_ROWS = "count_raw_rows"
    JOIN_BOTH_SIDES = "join_both_sides"
    WHERE_LABEL_IN_NUMERATOR = "where_label_in_numerator"  # ratio 题 label 误入 WHERE
    # Execution-driven deterministic tags (see execution_compare_v2.py):
    # 这两个 tag 由 execution_comparison 直接派生，不依赖 AST 启发式推断。
    # 一旦出现 → Extractor 必须把 locus 锚定在 SELECT（见 extractor prompt R6）。
    SELECT_ARITY_MISMATCH = "select_arity_mismatch"
    """pred/gold SELECT 列数不同。decisive signal for locus=SELECT。"""
    SELECT_ROLE_DTYPE_MISMATCH = "select_role_dtype_mismatch"
    """行数等但结果列类型变（典型 name→identifier 替换）。decisive signal for locus=SELECT + target=slot。"""


# =============================================================================
# Action Primitive（Action Compiler 支持的动作基元）
# Phase 1 MVP 固定 10 个。更多基元走 escape hatch 或后续扩展
# =============================================================================


class ActionPrimitive(str, Enum):
    ADD_SELECT_SLOT = "ADD_SELECT_SLOT"
    REPLACE_SELECT_SLOT = "REPLACE_SELECT_SLOT"
    DROP_SELECT_SLOT = "DROP_SELECT_SLOT"
    REROUTE_FACT = "REROUTE_FACT"
    INSERT_BRIDGE = "INSERT_BRIDGE"
    CHANGE_GRAIN = "CHANGE_GRAIN"
    MOVE_CONDITION = "MOVE_CONDITION"
    DROP_SIDE = "DROP_SIDE"
    SWITCH_CANONICAL_FIELD = "SWITCH_CANONICAL_FIELD"
    MATERIALIZE_RANKING_OUTPUT = "MATERIALIZE_RANKING_OUTPUT"


# =============================================================================
# Edit scope（Memory Rewrite bounded autonomy 契约用）
# rewrite 模型在 action 声明的 edit_scope 内可做 dependency repair，scope 外禁止
# =============================================================================


class EditScope(str, Enum):
    SELECT = "SELECT"
    FROM = "FROM"
    JOIN = "JOIN"
    WHERE = "WHERE"
    GROUP_BY = "GROUP_BY"
    HAVING = "HAVING"
    ORDER_BY = "ORDER_BY"
    LIMIT = "LIMIT"
    SUBQUERY = "SUBQUERY"


# =============================================================================
# Group type / status / confidence
# =============================================================================


class GroupType(str, Enum):
    SINGLETON = "singleton"
    FAMILY = "family"
    PATTERN = "pattern"


class GroupStatus(str, Enum):
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    DEPRECATED = "deprecated"


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# =============================================================================
# 阈值常量（分两类）
# =============================================================================


# --- Prior: 先验可直接定（已通过讨论 + codex 评审）---

PROMOTION_SUPPORT_MIN: int = 2
"""family -> pattern promotion 的最小 support。单例只能作 singleton 或 seed。"""

MAX_ACTION_COUNT_PER_CASE: int = 3
"""Action Compiler 对一个 case 输出的 action 总数上限。超过视为 pattern 过宽。"""

MAX_REPLAY_REGRESSION: float = 0.1
"""runtime family 准入 / pattern 保留的最大 replay regression rate。"""

SINGLETON_MAX_ACTIONS: int = 1
"""singleton exact-trigger 最多输出 1 个 action。"""

RISK_HIGH_BRANCH_COUNT: int = 2
"""branch_rules > 此数，ErrorInstance.risk_level 升级为 high。"""

RISK_HIGH_FREE_SLOT_COUNT: int = 3
"""instantiation_slots 中 free slot > 此数，risk_level 升 high。"""


# --- TBD: 必须数据校准（Phase 1-2 后确定）---

OVERLAP_WEIGHT_QUESTION: float = 0.35  # TBD
OVERLAP_WEIGHT_MANIFEST: float = 0.35  # TBD
OVERLAP_WEIGHT_STRUCTURAL: float = 0.20  # TBD
OVERLAP_WEIGHT_SLOT: float = 0.10  # TBD

QUESTION_OVERLAP_DECISIVE_WEIGHT: float = 0.7  # TBD
QUESTION_OVERLAP_DESCRIPTIVE_WEIGHT: float = 0.3  # TBD

FAMILY_MATCH_MIN_QUESTION: float = 0.5  # TBD
FAMILY_MATCH_MIN_MANIFEST: float = 0.5  # TBD
FAMILY_MATCH_MIN_STRUCTURAL: float = 0.5  # TBD

RUNTIME_FAMILY_MIN_COMPILE_COVERAGE: float = 0.7  # TBD
RUNTIME_FAMILY_MIN_REPLAY_IMPROVEMENT: float = 0.3  # TBD

PATTERN_PROMOTION_MIN_COMPILE_COVERAGE: float = 0.8  # TBD
PATTERN_PROMOTION_MIN_REPLAY_IMPROVEMENT: float = 0.6  # TBD

LOCAL_REORGANIZE_NEIGHBOR_SCORE_MIN: float = 0.55  # TBD
LOCAL_REORGANIZE_NEIGHBOR_MAX: int = 5  # TBD


TBD_THRESHOLDS = {
    "overlap_weights": (
        OVERLAP_WEIGHT_QUESTION,
        OVERLAP_WEIGHT_MANIFEST,
        OVERLAP_WEIGHT_STRUCTURAL,
        OVERLAP_WEIGHT_SLOT,
    ),
    "question_overlap_weights": (
        QUESTION_OVERLAP_DECISIVE_WEIGHT,
        QUESTION_OVERLAP_DESCRIPTIVE_WEIGHT,
    ),
    "family_match_mins": (
        FAMILY_MATCH_MIN_QUESTION,
        FAMILY_MATCH_MIN_MANIFEST,
        FAMILY_MATCH_MIN_STRUCTURAL,
    ),
    "runtime_family_mins": (
        RUNTIME_FAMILY_MIN_COMPILE_COVERAGE,
        RUNTIME_FAMILY_MIN_REPLAY_IMPROVEMENT,
    ),
    "pattern_promotion_mins": (
        PATTERN_PROMOTION_MIN_COMPILE_COVERAGE,
        PATTERN_PROMOTION_MIN_REPLAY_IMPROVEMENT,
    ),
    "local_reorganize": (
        LOCAL_REORGANIZE_NEIGHBOR_SCORE_MIN,
        LOCAL_REORGANIZE_NEIGHBOR_MAX,
    ),
}


# =============================================================================
# Codex 5 风险 patch 相关常量
# =============================================================================


# 哪些 PredManifestationType 只能在离线（有 gold）时 emit——runtime 看不到 gold，
# accumulate 侧从 ErrorInstance 产 singleton 的 trigger_signature 必须过滤掉这些 tag，
# 否则 runtime 永远命中不了（详见 runtime_v2.retrieve_groups）
EXECUTION_ONLY_PRED_TAGS: frozenset[str] = frozenset(
    {
        "select_arity_mismatch",  # 需要 pred/gold 列数对比
        "select_role_dtype_mismatch",  # 需要 execution dtype 对比
        "missing_bridge",  # 需要对比 gold tables
        "misroute_fact",  # 需要对比 gold tables
        "missing_second_slot",  # 需要比对 gold arity
        "missing_rank_slot",  # 需要比对 gold 是否要 rank 列
        "where_label_in_numerator",  # 需要 gold 的 CASE WHEN 结构
    }
)


# Patch 1: LocalSchemaView 召回失败独立诊断通道
SCHEMA_RECALL_MISS_CATEGORIES: tuple[str, ...] = (
    "missing_bridge_path",
    "missing_column_candidate",
    "missing_role_family_match",
    "two_hop_extension_denied",
)
"""schema-recall-miss 的分类枚举。这些理由下的 compiler empty output
**不**进入 memory demotion 信号——它们是 schema view 问题，不是 pattern 问题。"""


# Patch 2: Promotion leave-one-out
PROMOTION_REQUIRE_LEAVE_ONE_OUT: bool = True
"""family->pattern promotion test 必须做 leave-one-out，防止自我确认。"""


# Patch 3: C0 upstream fingerprint
C0_FINGERPRINT_FIELDS: tuple[str, ...] = (
    "base_model_id",
    "beam_size",
    "temperature",
    "prompt_hash",
)
"""C0 snapshot 必须携带的 upstream fingerprint 字段。generator 变化则 C0 作废。"""


# Patch 4 : GroupSummary 新增字段
# 见 data_structures_v2.GroupSummary 的 `slot_required_or_optional`
# 和 `action_realization_trace` 字段


# Patch 5: 周期性 global conflict scan 的间隔（以 case 数计）
GLOBAL_CONFLICT_SCAN_EVERY_N_CASES: int = 20
"""local_reorganize 之外，每 N 个 case 对 library 做一次全局冲突检查。"""
