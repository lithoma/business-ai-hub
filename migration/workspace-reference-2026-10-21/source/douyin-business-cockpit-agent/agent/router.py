"""Deterministic routing for clearly identifiable business questions.

This module provides a lightweight pre-router that uses keyword matching
to determine the correct Genie Agent (A1/A2/both) for a given question.
When the domain is ambiguous, it returns None and lets the LLM decide.
"""

# A1 — Internal sales operations indicators
A1_KEYWORDS = [
    "含券", "不含券", "退前", "退后", "退款",
    "销售目标", "目标达成", "达成率",
    "直播", "千川", "自播", "roi",
    "漏斗", "转化",
    "广告费", "广告费用", "广告投放",
    "内部销售", "内部gmv", "内部经营", "经营业绩",
    "退单", "退货",
]

# A2 — External competition / market indicators
A2_KEYWORDS = [
    "外部市场", "外部销售", "外部市场销售",
    "市场份额", "市场规模", "市场占有率",
    "竞品", "竞争对手", "品牌排名",
    "5a", "消费者资产",
    "外部", "竞品销售", "竞品gmv",
    "市场占比",
]

# Both indicators — when a question explicitly asks for both internal AND external
BOTH_PATTERNS = [
    ("内部", "外部"),
    ("内部gmv", "市场"),
    ("内部销售", "市场"),
]


def deterministic_route(question: str) -> str | None:
    """Route a question deterministically based on business-domain keywords.

    Returns:
        "A1" — route to A1 (internal sales) only
        "A2" — route to A2 (external market) only
        "BOTH" — route to both A1 and A2
        None — ambiguous, let the LLM decide
    """
    q = question.lower().strip()

    a1_hit = any(kw in q for kw in A1_KEYWORDS)
    a2_hit = any(kw in q for kw in A2_KEYWORDS)

    # Check for explicit "both" patterns
    has_internal = any(kw in q for kw in ["内部", "内部gmv", "内部销售"])
    has_external = any(kw in q for kw in ["外部", "外部市场", "外部销售"])
    if has_internal and has_external:
        return "BOTH"

    if a1_hit and a2_hit:
        return "BOTH"
    if a1_hit:
        return "A1"
    if a2_hit:
        return "A2"

    # Ambiguous — let LLM decide
    return None
