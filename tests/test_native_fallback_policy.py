"""Fallback policy must never hide authorization, lease, or budget failures."""
from types import SimpleNamespace

import pytest
from pydantic_ai.exceptions import ModelRetry

from backend.agents.limits import MODEL_FAILURE_CODES, allows_content_fallback
from backend.agents.tools import make_search_materials_tool
from backend.planning.errors import PlanningError, error


ALLOWED = ["OUTPUT_INVALID", "INVALID_OUTLINE_STRUCTURE", *MODEL_FAILURE_CODES]
DISALLOWED = [
    "MODEL_CREDENTIAL_INVALID", "AGENT_LOOP_LIMIT", "TASK_CANCELLED", "TASK_DEADLINE_EXCEEDED",
    "LEASE_LOST", "DISCLOSURE_REQUIRED", "ACCESS_DENIED", "OUTPUT_TOO_LARGE",
    "QUOTA_EXCEEDED", "QUOTA_REQUEST_LIMIT", "QUOTA_WIRE_LIMIT", "QUOTA_TOOL_LIMIT",
    "TASK_EXPIRED", "TASK_NOT_FOUND", "invalid_state", "invalid_field", "PROMPT_UNAVAILABLE",
    "disclosure_required", "disclosure_expired", "disclosure_revoked", "access_denied",
    "course_not_found", "route_not_found", "invalid_owner", "UNKNOWN_ERROR",
]


@pytest.mark.parametrize("code", ALLOWED)
def test_only_controlled_planning_errors_allow_content_fallback(code):
    assert allows_content_fallback(error(code)) is (code != "MODEL_CREDENTIAL_INVALID")


@pytest.mark.parametrize("code", DISALLOWED)
def test_security_budget_and_unknown_errors_never_allow_fallback(code):
    assert not allows_content_fallback(error(code))


@pytest.mark.parametrize("exc", [RuntimeError("provider"), ValueError("bad output"),
    ModelRetry("retry")])
def test_non_planning_exceptions_never_allow_fallback(exc):
    assert not allows_content_fallback(exc)


@pytest.mark.parametrize("code", ["OUTPUT_INVALID", "INVALID_OUTLINE_STRUCTURE", "MODEL_TIMEOUT"])
def test_retrieval_allowed_error_becomes_single_model_retry(code):
    calls = []
    class Retrieval:
        def assemble(self, **kwargs):
            calls.append(kwargs)
            raise error(code)
    tool = make_search_materials_tool(Retrieval(), owner_user_id="owner", material_ids=["m1"])
    with pytest.raises(ModelRetry):
        tool("topic")
    assert len(calls) == 1


@pytest.mark.parametrize("code", ["MODEL_CREDENTIAL_INVALID", "DISCLOSURE_REQUIRED", "LEASE_LOST", "AGENT_LOOP_LIMIT"])
def test_retrieval_security_or_budget_error_propagates_without_retry(code):
    calls = []
    class Retrieval:
        def assemble(self, **kwargs):
            calls.append(kwargs)
            raise error(code)
    tool = make_search_materials_tool(Retrieval(), owner_user_id="owner", material_ids=["m1"])
    with pytest.raises(PlanningError) as caught:
        tool("topic")
    assert caught.value.code == code and len(calls) == 1


def test_retrieval_success_is_not_changed_by_fallback_policy():
    class Retrieval:
        def assemble(self, **kwargs):
            return SimpleNamespace(excerpts=[{"title": "A", "excerpt": "B"}], chars_total=1,
                truncation_reasons=[])
    tool = make_search_materials_tool(Retrieval(), owner_user_id="owner", material_ids=["m1"])
    assert '"items"' in tool("topic")


@pytest.mark.parametrize('code', ['disclosure_required', 'disclosure_revoked', 'access_denied'])
@pytest.mark.parametrize('wrapped', [False, True])
def test_wrapped_slice_security_retains_code_and_cannot_fallback(code, wrapped):
    from pydantic_ai.exceptions import UnexpectedModelBehavior
    from backend.agents.limits import to_controlled_error
    from backend.slice1.errors import Slice1Error
    exc = Slice1Error(code, 'synthetic internal detail', status=403)
    if wrapped:
        outer = UnexpectedModelBehavior('SDK wrapper')
        outer.__cause__ = exc
        exc = outer
    controlled = to_controlled_error(exc)
    assert controlled.code == code.upper()
    assert controlled.status == 403
    assert 'synthetic internal detail' not in str(controlled)
    assert not allows_content_fallback(controlled)


@pytest.mark.parametrize('kind', ['lease', 'disclosure', 'audit', 'ownership'])
@pytest.mark.parametrize('wrapped', [False, True])
def test_native_guard_security_stops_before_first_model_call(kind, wrapped):
    import asyncio
    from pydantic import BaseModel
    from pydantic_ai.exceptions import UnexpectedModelBehavior
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel
    from backend.agents.contracts import AgentDeps, GenerationInput
    from backend.agents.runner import execute_agent
    from backend.logging_audit.audit import AuditWriteError
    from backend.slice1.errors import Slice1Error
    from backend.tasks.service import LeaseError
    from backend.logging_audit.task_events import AccessDeniedError
    class Candidate(BaseModel):
        value: int
    calls = []
    model = FunctionModel(lambda messages, info: calls.append(1) or ModelResponse(
        parts=[TextPart('{"value":1}')]), profile={'supports_json_schema_output': True})
    original = {'lease': LeaseError('LEASE_LOST', 'synthetic lost lease'),
        'disclosure': Slice1Error('disclosure_required', 'synthetic disclosure'),
        'audit': AuditWriteError('synthetic audit failure'),
        'ownership': AccessDeniedError('synthetic ownership rejected')}[kind]
    def check():
        if wrapped:
            raise UnexpectedModelBehavior('SDK wrapper') from original
        raise original
    deps = AgentDeps(check_state=check)
    with pytest.raises(Exception) as caught:
        asyncio.run(execute_agent(model, Candidate,
            GenerationInput.from_messages([{'role':'user', 'content':'synthetic'}]), deps))
    assert calls == [] and deps.budget.logical_requests == 0
    if kind == 'disclosure':
        assert caught.value.code == 'DISCLOSURE_REQUIRED'
    else:
        assert caught.value is original
    assert not allows_content_fallback(caught.value)


@pytest.mark.parametrize('code', ['OUTPUT_INVALID', 'MODEL_TIMEOUT', 'MODEL_CREDENTIAL_INVALID',
    'AGENT_LOOP_LIMIT', 'QUOTA_EXCEEDED', 'disclosure_required', 'TASK_CANCELLED'])
def test_route_refinement_stops_next_course_on_security_or_budget(code):
    from backend.planning.service import PlanningService
    calls, events, state_reads = [], [], []
    def refine(adapter, state, course, prompt, budget):
        calls.append(course.title)
        if len(calls) == 1:
            raise error(code)
        return {'units':[{'title':'Unit', 'lessons':[{'title':'Lesson', 'goal':'Goal'}]}],
            'prerequisites':[]}
    service = SimpleNamespace(prompts=SimpleNamespace(resolve_for_task=lambda *args: {'template':'synthetic'}),
        _require_model_adapter=lambda *args: object(), _refine_course_units=refine,
        _state=lambda task_id: state_reads.append(task_id),
        events=SimpleNamespace(record=lambda *args, **kwargs: events.append(kwargs)))
    content = {'stages':[{'courses':[{'title':'First'}, {'title':'Second'}]}]}
    if allows_content_fallback(error(code)):
        PlanningService._fill_route_units(service, SimpleNamespace(owner_user_id='owner'), 'task', content)
        assert calls == ['First', 'Second'] and len(events) == 2
        assert content['stages'][0]['courses'][0]['refinement_status'] == 'failed'
        assert content['stages'][0]['courses'][1]['refinement_status'] == 'ready'
    else:
        with pytest.raises(PlanningError) as caught:
            PlanningService._fill_route_units(service, SimpleNamespace(owner_user_id='owner'), 'task', content)
        assert caught.value.code == code
        assert calls == ['First'] and events == [] and len(state_reads) == 1


@pytest.mark.parametrize('kind', ['lease', 'disclosure', 'audit', 'ownership'])
def test_official_sdk_authorization_error_never_sends_or_falls_back(monkeypatch, kind):
    import asyncio
    import socket
    import httpx2 as httpx
    from pydantic import BaseModel
    from backend.agents import native
    from backend.agents.contracts import AgentDeps, GenerationInput
    from backend.agents.runner import execute_agent
    from backend.logging_audit.audit import AuditWriteError
    from backend.slice1.adapters.base import AdapterBase
    from backend.slice1.errors import Slice1Error
    from backend.tasks.service import LeaseError
    from backend.logging_audit.task_events import AccessDeniedError
    class Candidate(BaseModel):
        value: int
    sent, authorized = [], []
    original = {'lease': LeaseError('LEASE_LOST', 'synthetic lost lease'),
        'disclosure': Slice1Error('disclosure_required', 'synthetic disclosure'),
        'audit': AuditWriteError('synthetic audit failure'),
        'ownership': AccessDeniedError('synthetic ownership rejected')}[kind]
    def authorize(body, destination, scope):
        authorized.append((body, destination))
        raise original
    def resolver(*args):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]
    def handler(request):
        sent.append(request)
        raise AssertionError('authorization failure must not send')
    transport_cls = native.SafeModelTransport
    monkeypatch.setattr(native, 'SafeModelTransport', lambda endpoint, deps, **kwargs:
        transport_cls(endpoint, deps, inner=httpx.MockTransport(handler), resolver=resolver, **kwargs))
    source = AdapterBase(endpoint='https://model.invalid/v1', model_name='synthetic-model',
        credentials={'api_key':'synthetic-key'})
    source.protocol = 'openai'
    deps = AgentDeps(authorize_payload=authorize)
    with pytest.raises(Exception) as caught:
        asyncio.run(execute_agent(source, Candidate,
            GenerationInput.from_messages([{'role':'user', 'content':'synthetic'}]), deps))
    assert len(authorized) == 1 and sent == []
    assert deps.budget.logical_requests == 1 and deps.budget.wire_requests == 0
    if kind == 'disclosure':
        assert caught.value.code == 'DISCLOSURE_REQUIRED'
    else:
        assert caught.value is original
    assert not allows_content_fallback(caught.value)


@pytest.mark.parametrize('method', ['procedural', 'image'])
@pytest.mark.parametrize('code', ['OUTPUT_INVALID', 'MODEL_TIMEOUT', 'MODEL_CREDENTIAL_INVALID',
    'AGENT_LOOP_LIMIT', 'QUOTA_EXCEEDED', 'disclosure_required', 'TASK_CANCELLED'])
def test_cover_uses_local_procedural_or_rejects_unpinned_image_without_content_model(monkeypatch, method, code):
    from backend.planning import features
    calls, renders = [], []
    def forbidden_model(*args, **kwargs):
        calls.append(1)
        raise error(code)
    monkeypatch.setattr(features.cover_agent, 'propose', forbidden_model)
    monkeypatch.setattr(features, 'render_ink', lambda recipe, seed: renders.append(recipe) or b'synthetic')
    service = SimpleNamespace(planning=SimpleNamespace(model_adapter_factory=forbidden_model),
        prompts=SimpleNamespace(resolve_for_task=forbidden_model), image_adapter_factory=forbidden_model)
    asset = SimpleNamespace(recipe_json='{}', asset_id='asset', owner_user_id='owner',
        task_id='task', method=method)
    if method == 'procedural':
        result = features.PlanningFeatureService._draw(service, asset, 'Synthetic subject')
        assert result['image'] == b'synthetic' and len(renders) == 1
        assert result['recipe']['requested_method'] == result['recipe']['actual_method'] == 'procedural'
        assert result['recipe']['fallback_reason'] is None and result['recipe']['error_code'] is None
        assert 'agent_fallback' not in result['recipe']
    else:
        with pytest.raises(PlanningError) as caught:
            features.PlanningFeatureService._draw(service, asset, 'Synthetic subject')
        assert caught.value.code == 'disclosure_required' and renders == []
    assert calls == []


@pytest.mark.parametrize('code', ['OUTPUT_INVALID', 'MODEL_TIMEOUT', 'MODEL_CREDENTIAL_INVALID',
    'AGENT_LOOP_LIMIT', 'QUOTA_EXCEEDED', 'disclosure_required', 'TASK_CANCELLED', 'LEASE_LOST'])
def test_direct_cover_guard_failure_never_opens_adapter_or_renders(monkeypatch, code):
    from backend.planning import automatic_cover
    original = error(code)
    calls, renders, gates = [], [], []
    def deny(*args, **kwargs):
        gates.append(1)
        raise original
    monkeypatch.setattr(automatic_cover, 'validate', deny)
    monkeypatch.setattr(automatic_cover, 'render_ink', lambda *args: renders.append(1))
    service = SimpleNamespace(image_adapter_factory=lambda *args: calls.append(1))
    asset = SimpleNamespace(asset_id='cover_abcdef', owner_user_id='owner', task_id='task')
    with pytest.raises(PlanningError) as caught:
        automatic_cover.draw(service, asset, {'title': 'Synthetic subject'}, object())
    assert caught.value is original and gates == [1] and calls == renders == []


@pytest.mark.parametrize('code', ['timeout', 'connection_error', 'rate_limited', 'provider_unavailable',
    'credential_invalid', 'invalid_response', 'invalid_endpoint', 'disclosure_required',
    'disclosure_revoked', 'disclosure_expired'])
def test_direct_cover_fallback_accepts_only_transient_provider_errors(monkeypatch, code):
    from backend.planning import automatic_cover
    from backend.slice1.adapters.base import AdapterError
    original = AdapterError(code, status=502)
    calls, renders, gates, closed = [], [], [], []
    class Adapter:
        def generate_image(self, prompt):
            self.authorize_request()
            calls.append(prompt)
            raise original
        def close(self):
            closed.append(1)
    monkeypatch.setattr(automatic_cover, 'validate', lambda *args, **kwargs: gates.append(1))
    monkeypatch.setattr(automatic_cover, 'render_ink', lambda *args: renders.append(1) or b'synthetic')
    service = SimpleNamespace(image_adapter_factory=lambda *args: Adapter())
    asset = SimpleNamespace(asset_id='cover_abcdef', owner_user_id='owner', task_id='task')
    metadata = {'title': 'Synthetic subject', 'requested_method': 'image'}
    if code in ('timeout', 'connection_error', 'rate_limited', 'provider_unavailable'):
        result = automatic_cover.draw(service, asset, metadata, object())
        assert result['image'] == b'synthetic' and renders == [1] and len(gates) == 4
        assert result['recipe']['requested_method'] == 'image'
        assert result['recipe']['actual_method'] == 'procedural'
        assert result['recipe']['fallback'] is True and result['recipe']['fallback_reason'] == code
    else:
        with pytest.raises(AdapterError) as caught:
            automatic_cover.draw(service, asset, metadata, object())
        assert caught.value is original and renders == [] and len(gates) == 3
    assert len(calls) == 1 and closed == [1]


@pytest.mark.parametrize('code', ['LEASE_LOST', 'TASK_CANCELLED', 'TASK_DEADLINE_EXCEEDED',
    'QUOTA_EXCEEDED', 'disclosure_revoked', 'disclosure_expired', 'access_denied'])
def test_transient_cover_error_cannot_override_changed_fallback_guard(monkeypatch, code):
    from backend.planning import automatic_cover
    from backend.slice1.adapters.base import AdapterError
    original = error(code)
    gates, calls, renders, closed = [], [], [], []
    def check(*args, **kwargs):
        gates.append(1)
        if len(gates) == 4:
            raise original
    class Adapter:
        def generate_image(self, prompt):
            self.authorize_request()
            calls.append(prompt)
            raise AdapterError('timeout', status=504)
        def close(self):
            closed.append(1)
    monkeypatch.setattr(automatic_cover, 'validate', check)
    monkeypatch.setattr(automatic_cover, 'render_ink', lambda *args: renders.append(1))
    service = SimpleNamespace(image_adapter_factory=lambda *args: Adapter())
    asset = SimpleNamespace(asset_id='cover_abcdef', owner_user_id='owner', task_id='task')
    with pytest.raises(PlanningError) as caught:
        automatic_cover.draw(service, asset, {'title': 'Synthetic subject'}, object())
    assert caught.value is original and len(gates) == 4
    assert len(calls) == 1 and renders == [] and closed == [1]
