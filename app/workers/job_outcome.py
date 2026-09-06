"""Job outcome exception classes — PR-004 AM-04/AM-05."""


class TerminalJobError(Exception):
    """Raised by a handler when the job cannot ever succeed."""


class CancelledStaleError(Exception):
    """Raised when the job is superseded by a newer generation_id."""
