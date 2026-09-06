"""
Unified retry budget per job kind — PR-004 AM-04.

queue_max: maximum queue-level attempts
service_max: maximum service-layer attempts per queue attempt
total: queue_max × service_max (must not exceed this)
"""

RETRY_BUDGETS: dict[str, dict[str, int]] = {
    "send_asset":     {"queue_max": 2, "service_max": 3, "total": 6},
    "analyze_image":  {"queue_max": 3, "service_max": 1, "total": 3},
    "render_reading": {"queue_max": 3, "service_max": 1, "total": 3},
    "analyze_combined": {"queue_max": 3, "service_max": 1, "total": 3},
    "render_asset":   {"queue_max": 3, "service_max": 1, "total": 3},
    "send_image":     {"queue_max": 2, "service_max": 3, "total": 6},
}

DEFAULT_BUDGET = {"queue_max": 3, "service_max": 1, "total": 3}


def get_budget(kind: str) -> dict[str, int]:
    return RETRY_BUDGETS.get(kind, DEFAULT_BUDGET)
