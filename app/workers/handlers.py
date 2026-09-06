"""Portfolio stub — core business logic omitted for IP protection.

See README.md architecture section for module role description.
"""

"""Minimal handler registry for portfolio — handlers omitted."""
from app.workers.runner import register_handler

@register_handler('noop')
def handle_noop(payload, job_id, attempt):
    pass
