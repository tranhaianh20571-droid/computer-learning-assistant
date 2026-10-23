"""Controlled domain-output diagnostics; model/user strings are never reason codes."""
from pydantic_ai.exceptions import ModelRetry

OUTPUT_CONSTRAINT_REASONS = frozenset({
    'domain_constraint', 'intake_first_batch', 'intake_question_budget',
    'intake_finish', 'intake_depth_question', 'intake_duplicate_question',
    'intake_option_limit', 'intake_scope_question', 'intake_duplicate_option',
    'intake_explicit_scope', 'intake_evidence_reference', 'intake_evidence_missing',
    'intake_detail_profile',
})


class OutputConstraintRetry(ModelRetry):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.diagnostic_reason = reason if reason in OUTPUT_CONSTRAINT_REASONS else 'domain_constraint'


def constraint_reason(exc: BaseException) -> str:
    reason = getattr(exc, 'diagnostic_reason', None)
    return reason if reason in OUTPUT_CONSTRAINT_REASONS else 'domain_constraint'


def record_output_validation(deps, *, reason: str, status: str, capability='learning_intake_agent'):
    """Best-effort runtime detail, separate from low-cardinality model metrics."""
    from ..logging_audit.logger import StructuredLogger
    from ..logging_audit.exporter import HEALTH
    reason = reason if reason in OUTPUT_CONSTRAINT_REASONS else 'domain_constraint'
    try:
        StructuredLogger('api').log('model.output.validation', level='WARN',
            stage='generate', status=status, reason=reason, error_code='OUTPUT_INVALID',
            task_id=deps.task_id or None, attempt=deps.attempt, config_id=deps.config_id,
            config_version=deps.config_version, capability=capability)
    except Exception:
        HEALTH.add('measurement_failures')
