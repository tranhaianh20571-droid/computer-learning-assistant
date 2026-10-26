"""Controlled domain-output diagnostics; model/user strings are never reason codes."""
from pydantic_ai.exceptions import ModelRetry

OUTPUT_CONSTRAINT_REASONS = frozenset({
    'domain_constraint', 'intake_first_batch', 'intake_question_budget',
    'intake_finish', 'intake_depth_question', 'intake_duplicate_question',
    'intake_option_limit', 'intake_scope_question', 'intake_duplicate_option',
    'intake_explicit_scope', 'intake_evidence_reference', 'intake_evidence_missing',
    'intake_detail_profile',
    'teaching_step_count', 'teaching_source_focus', 'teaching_alignment',
    'teaching_board_graph', 'teaching_preserved_text', 'teaching_screen_position',
    'teaching_scope_coverage', 'teaching_stage', 'teaching_recap',
})


class OutputConstraintRetry(ModelRetry):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.diagnostic_reason = reason if reason in OUTPUT_CONSTRAINT_REASONS else 'domain_constraint'


def constraint_reason(exc: BaseException) -> str:
    reason = getattr(exc, 'diagnostic_reason', None)
    return reason if reason in OUTPUT_CONSTRAINT_REASONS else 'domain_constraint'


def schema_constraint_reason(exc):
    """Map known validator messages to fixed codes; never emit their dynamic values."""
    prefixes = {
        "text must equal concatenated": "teaching_text_beats",
        "board cue quote missing": "teaching_cue_quote",
        "board cue target unavailable": "teaching_cue_target",
        "board cue edge precedes": "teaching_cue_order",
        "board cue order": "teaching_cue_order",
        "board cue returns": "teaching_cue_order",
        "board cue writes": "teaching_cue_order",
        "board cue expression differs": "teaching_cue_expression",
        "board cue coverage missing": "teaching_cue_coverage",
        "aligned board requires typed": "teaching_typed_board",
        "conflicting board block": "teaching_conflicting_block",
        "highlight references": "teaching_highlight_order",
    }
    for item in exc.errors(include_input=False, include_url=False):
        message = str(item.get("ctx", {}).get("error", ""))
        for prefix, reason in prefixes.items():
            if message.startswith(prefix):
                return reason
    return "schema_constraint"


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
