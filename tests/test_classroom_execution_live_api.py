"""Explicit real container/API checks against synthetic, isolated classroom data."""
import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.teaching.models import BoardSceneRow, TeachingStepRow
from test_classroom_interactive import ready
from test_teaching_assessments import database, env  # noqa: F401

pytestmark = pytest.mark.skipif(os.environ.get("CLASSROOM_CONTAINER_LIVE") != "1",
                                reason="explicit real local container validation only")


@pytest.mark.parametrize("language,code,output", [
    ("java", "public class Main {public static void main(String[] args){System.out.println(6);}}", "6"),
    ("c", '#include <stdio.h>\nint main(){printf("%d",6);}', "6"),
    ("c++", "#include <iostream>\nint main(){std::cout<<6;}", "6"),
    ("python", "import torch\nx=torch.tensor(2.,requires_grad=True)\n(x*x).backward()\nprint(x.grad.item())", "4.0"),
])
def test_actual_authorized_api_runs_compilers_and_torch(env, language, code, output):
    client, state, _, headers = env
    ident = ready(env)
    with state.db.session() as sess:
        step = sess.scalar(select(TeachingStepRow).where(TeachingStepRow.conversation_id == ident))
        scene = sess.get(BoardSceneRow, step.board_scene_id)
        value = json.loads(scene.scene_json)
        value["presentation"]["blocks"][0].update(language=language, code=code)
        scene.scene_json = json.dumps(value)
    prefix = f"/api/teaching/{ident}/interactive"
    assert client.get(prefix+"/runtimes", headers=headers).json()["container"] is True
    response = client.post(prefix+f"/runs/{uuid4()}", headers=headers,
        json={"playback_version": 0, "step_no": 1, "generation": 1, "block_id": "example",
              "original_code": code, "code": code})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["status"] == "success", response.text
    assert response.json()["output"].strip() == output
