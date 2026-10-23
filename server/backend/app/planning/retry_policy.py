"""Pure eligibility rules shared by the planning scheduler and read-only state."""
from .models import utcnow

AUTO_RETRY_LIMIT = 5
AUTO_RETRY_ERRORS = frozenset({
    "MODEL_TIMEOUT", "MODEL_CONNECTION_ERROR", "MODEL_PROVIDER_ERROR", "MODEL_RATE_LIMITED",
    "OUTPUT_INVALID", "INVALID_OUTLINE_STRUCTURE",
})


def missing_courses(content):
    return [course for stage in content.get("stages", [])
        for course in stage.get("courses", []) if not course.get("units")]


def automatic_retry_eligible(task, state, error_code, content, *, confirmed=False):
    if (confirmed or state.abandoned_at or state.wait_state or task.cancel_requested
            or (task.deadline_at is not None and task.deadline_at <= utcnow())):
        return False
    if task.status == "partial":
        missing = missing_courses(content)
        return bool(missing) and all(course.get("refinement_error", "OUTPUT_INVALID")
            in AUTO_RETRY_ERRORS for course in missing)
    return task.status == "failed" and error_code in AUTO_RETRY_ERRORS
