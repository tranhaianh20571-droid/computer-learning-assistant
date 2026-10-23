"""Synthetic classroom authorization and execution lifecycle boundaries."""
from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from backend.teaching.board_blocks import CodeBlock
from backend.teaching.code_execution import ContainerRunner, ExecutionIn, execution_language, runtime_for
from backend.teaching.errors import TeachingError
from backend.teaching.models import BoardSceneRow, TeachingStepRow
from backend.planning.models import ConversationRow
from test_classroom_interactive import ready
from test_teaching_assessments import database, env  # noqa: F401


@pytest.mark.parametrize("language,code,expected", [
    ("java", "class Main {}", "java"), ("C", "int main(){}", "c"),
    ("c++", "int main(){}", "cpp"), ("cxx", "int main(){}", "cpp"),
    ("pytorch", "print(1)", "pytorch"), ("python", "import torch\nprint(torch.ones(2))", "pytorch"),
    ("python3", "from torch.nn import Linear", "pytorch"),
    ("py", "import numpy, torch.nn as nn", "pytorch"),
    ("python", "# import torch\nprint('torch')", ""),
    ("javascript", "console.log('torch')", ""), ("python", "import numpy", ""),
])
def test_runtime_routing_and_board_interaction(language, code, expected):
    assert runtime_for(language, code) == expected
    block = CodeBlock(id="example", type="code", language=language, code=code)
    assert block.interaction is True


def request():
    return ExecutionIn(playback_version=0, step_no=1, block_id="example",
        original_code="value = 2\nprint(value * 3)", code="import torch\nprint(torch.ones(2))")


def test_execute_uses_published_language_and_private_fences(env, monkeypatch):
    client, state, owner, headers = env
    ident = ready(env)
    assert execution_language(state.teaching, ident, owner, request()) == "pytorch"
    calls = []
    def execute(_self, run, actor, conv, language, code):
        calls.append((actor, conv, language))
        return {"status": "success", "output": "synthetic result", "images": []}
    monkeypatch.setattr(ContainerRunner, "execute", execute)
    url = f"/api/teaching/{ident}/interactive/runs/{uuid4()}"
    value = request().model_dump()
    response = client.post(url, headers=headers, json=value)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert calls == [(owner, ident, "pytorch")]
    for field, invalid in [("playback_version", 8), ("generation", 99),
                           ("original_code", "wrong"), ("block_id", "unknown")]:
        bad = deepcopy(value)
        bad[field] = invalid
        assert client.post(url, headers=headers, json=bad).status_code == 409
    assert len(calls) == 1
    assert client.post(url, json=value).status_code == 401
    with pytest.raises(TeachingError) as error:
        execution_language(state.teaching, ident, "other-synthetic-owner", request())
    assert error.value.status == 404
    bad = {**value, "command": "untrusted"}
    assert client.post(url, headers=headers, json=bad).status_code == 422
    with state.db.session() as sess:
        from backend.planning.models import utcnow
        sess.get(ConversationRow, ident).deleted_at = utcnow()
    assert client.post(url, headers=headers, json=value).status_code == 404


def test_results_are_not_released_after_classroom_deleted(env, monkeypatch):
    client, state, _, headers = env
    ident = ready(env)
    def execute(*_):
        from backend.planning.models import utcnow
        with state.db.session() as sess:
            sess.get(ConversationRow, ident).deleted_at = utcnow()
        return {"status": "success", "output": "must not be delivered", "images": []}
    monkeypatch.setattr(ContainerRunner, "execute", execute)
    result = client.post(f"/api/teaching/{ident}/interactive/runs/{uuid4()}",
                        headers=headers, json=request().model_dump())
    assert result.status_code == 404
    assert "must not be delivered" not in result.text


def test_unavailable_and_cancel_require_owner_and_valid_uuid(env, monkeypatch):
    client, _, _, headers = env
    ident = ready(env)
    monkeypatch.setattr(ContainerRunner, "available", lambda _: False)
    result = client.get(f"/api/teaching/{ident}/interactive/runtimes", headers=headers)
    assert result.json()["container"] is False
    monkeypatch.setattr(ContainerRunner, "_control", lambda *_args, **_kwargs: None)
    url = f"/api/teaching/{ident}/interactive/runs/not-a-uuid"
    assert client.post(url, headers=headers, json=request().model_dump()).status_code == 400
    assert client.delete(url, headers=headers).status_code == 400
    url = f"/api/teaching/{ident}/interactive/runs/{uuid4()}"
    assert client.post(url, headers=headers, json=request().model_dump()).status_code == 503
    assert client.delete(url, headers=headers).status_code == 503
    from types import SimpleNamespace
    monkeypatch.setattr(ContainerRunner, "_control",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=b"", stderr=b""))
    assert client.delete(url, headers=headers).status_code == 200


def test_cancel_before_start_and_cross_owner_never_start_containers(monkeypatch):
    runner = ContainerRunner(command=["synthetic-docker"])
    controls = []
    monkeypatch.setattr(runner, "_control", lambda args, **_: controls.append(args))
    run = str(uuid4())
    runner.cancel(run, "owner", "conversation")
    assert runner.execute(run, "owner", "conversation", "java", "code")["status"] == "stopped"
    assert all(args[:2] == ["rm", "-f"] for args in controls)
    for method in (lambda: runner.execute(run, "other", "conversation", "java", "code"),
                   lambda: runner.cancel(run, "owner", "other")):
        with pytest.raises(TeachingError) as error:
            method()
        assert error.value.status == 404


def test_container_options_failure_cleanup_and_duplicate(monkeypatch):
    runner = ContainerRunner(command=["synthetic-docker"])
    controls = []
    monkeypatch.setattr(runner, "_control", lambda args, **_: controls.append(args))
    run = str(uuid4())
    with pytest.raises(TeachingError) as error:
        runner.execute(run, "owner", "conversation", "cpp", "int main(){}")
    assert error.value.status == 503
    create = controls[0]
    for option in ("--read-only", "--network", "--cap-drop", "--security-opt", "--memory",
                   "--cpus", "--pids-limit", "--tmpfs", "--user", "--log-driver"):
        assert option in create
    assert "--volume" not in create and "--privileged" not in create
    assert "int main(){}" not in create
    assert controls[-1][:2] == ["rm", "-f"]
    assert runner.runs[run.replace("-", "")].cleanup_pending
    from types import SimpleNamespace
    monkeypatch.setattr(runner, "_control", lambda args, **_: SimpleNamespace(returncode=0, stdout=b"", stderr=b""))
    runner._recover_cleanup()
    assert runner.runs[run.replace("-", "")].finished
    with pytest.raises(TeachingError) as error:
        runner.execute(run, "owner", "conversation", "cpp", "code")
    assert error.value.status == 409


def test_concurrency_and_request_extra_fields(monkeypatch):
    from backend.teaching.code_execution import Run
    runner = ContainerRunner(command=["synthetic-docker"])
    runner.runs["active"] = Run("owner", "conversation", "synthetic")
    with pytest.raises(TeachingError) as error:
        runner.execute(str(uuid4()), "owner", "conversation", "c", "code")
    assert error.value.status == 429
    runner.runs.update({str(i): Run(str(i), "conversation", "synthetic") for i in range(2)})
    with pytest.raises(TeachingError) as error:
        runner.execute(str(uuid4()), "other", "conversation", "c", "code")
    assert error.value.status == 429
    with pytest.raises(ValidationError):
        ExecutionIn(**{**request().model_dump(), "image": "untrusted-image"})
