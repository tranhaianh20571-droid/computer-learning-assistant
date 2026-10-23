"""Controlled output failures remain diagnosable after process exit; offline only."""
import asyncio
import json

import pytest
from pydantic import BaseModel
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from backend.agents.contracts import AgentDeps, GenerationInput
from backend.agents.runner import execute_agent
from backend.agents.validation import OutputConstraintRetry
from backend.logging_audit.metrics import ModelCallMetrics
from backend.logging_audit.logger import MemoryLogStream, StructuredLogger
from backend.planning.errors import PlanningError


@pytest.fixture(name='native_synthetic_model_session')
def genuine_sdk_only():
    yield


class Candidate(BaseModel):
    value: int


def test_exhausted_validator_failure_has_task_identity_and_fixed_reason_on_disk(tmp_path,monkeypatch):
    monkeypatch.setenv('RUNTIME_LOG_DIR',str(tmp_path))
    from backend.logging_audit import runtime_sink
    runtime_sink._sink.cache_clear()
    from backend.logging_audit.metrics import get_model_call_metrics
    metrics=get_model_call_metrics();metrics.attach_logger(None)
    def model(messages,info):
        return ModelResponse(parts=[TextPart(content='{"value":1}')])
    def reject(ctx,output):
        raise OutputConstraintRetry('intake_evidence_missing','SYNTHETIC_PRIVATE_MODEL_BODY')
    deps=AgentDeps(task_id='task_synthetic_diagnostics',attempt=7,config_id='cfg_synthetic',config_version=3)
    with pytest.raises(PlanningError) as exc:
        asyncio.run(execute_agent(FunctionModel(model,profile={'supports_json_schema_output':True,'supports_tools':True,'supports_text_output':True}),Candidate,
            GenerationInput.from_messages([{'role':'user','content':'SYNTHETIC_PRIVATE_USER_BODY'}]),deps,
            agent_name='learning_intake_agent',output_validator=reject))
    assert exc.value.code=='OUTPUT_INVALID'
    records=[json.loads(line) for path in tmp_path.glob('runtime-*.jsonl') for line in path.read_text(encoding='utf-8').splitlines()]
    failures=[r for r in records if r['event']=='model.call.failed']
    assert len(failures)==1
    failure=failures[0]
    assert failure['task_id']==deps.task_id and failure['attempt']==7
    assert failure['config_id']=='cfg_synthetic' and failure['config_version']==3
    assert failure['capability']=='learning_intake_agent' and failure['requests']==3
    assert 'intake_evidence_missing' in failure['reason']
    retries=[r for r in records if r['event']=='model.output.validation']
    assert len(retries)==3 and all(r['reason']=='intake_evidence_missing' for r in retries)
    assert failure['trace_id'] and all(r['trace_id']==failure['trace_id'] for r in retries)
    serialized=json.dumps(records)
    assert 'SYNTHETIC_PRIVATE_MODEL_BODY' not in serialized and 'SYNTHETIC_PRIVATE_USER_BODY' not in serialized
    assert 'model_prompt' not in serialized and 'model_answer' not in serialized
    runtime_sink._sink.cache_clear()
    # New logger/sink instance reads no memory of the previous run; prior disk lines survive.
    StructuredLogger('api').log('model.call.completed',stage='generate',status='succeeded',duration_ms=0)
    assert sum(len(p.read_text(encoding='utf-8').splitlines()) for p in tmp_path.glob('runtime-*.jsonl'))==len(records)+1


def test_untrusted_retry_message_cannot_become_reason_code():
    from backend.agents.validation import constraint_reason
    from backend.agents.limits import failure_reason
    private='SYNTHETIC_PRIVATE_REASON_BODY'
    exc=OutputConstraintRetry(private,private)
    assert constraint_reason(exc)=='domain_constraint'
    assert private not in failure_reason(exc)
    assert constraint_reason(ModelRetry(private))=='domain_constraint'


def test_task_attempt_and_model_requests_are_separate_from_metric_dimensions():
    stream=MemoryLogStream();metrics=ModelCallMetrics();metrics.attach_logger(StructuredLogger('api','test',stream=stream))
    metrics.record(stage='generate',outcome='failed',duration_ms=1,error_code='OUTPUT_INVALID',attempts=7,requests=3,
        task_id='task_synthetic',config_id='cfg_synthetic',config_version=2,capability='learning_intake_agent')
    record=stream.records()[0]
    assert record['attempt']==7 and record['requests']==3 and record['config_version']==2
    assert 'task_id' not in metrics.snapshot()['calls'][0] and 'capability' not in metrics.snapshot()['calls'][0]
