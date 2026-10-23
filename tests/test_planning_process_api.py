"""Read-only, private planning process HTTP contract using synthetic PostgreSQL tasks."""
from common_fixtures import make_user, auth_headers
from test_planning_resume import env, interview_env, database, endpoint, run


def test_process_get_is_read_only_private_and_not_cacheable(env):
    initial = env[0].get(endpoint(env, "/process"), headers=env[3])
    assert initial.status_code == 200 and initial.json() == {"process": None}
    assert initial.headers["cache-control"] == "no-store"
    assert not env[5].interview_contexts
    run(env)
    calls = len(env[5].interview_contexts)
    for _ in range(3):
        response = env[0].get(endpoint(env, "/process"), headers=env[3])
        assert response.status_code == 200
        data = response.json()["process"]
        assert data["task_id"] == env[4] and data["stage"] == "intake"
        assert not any(key.startswith("_") for key in data)
        assert response.headers["cache-control"] == "no-store"
    assert len(env[5].interview_contexts) == calls
    _, token = make_user(env[0], env[1].db)
    assert env[0].get(endpoint(env, "/process"), headers=auth_headers(token)).status_code == 404


def test_abandoned_process_cannot_be_read_or_resumed(env):
    run(env)
    response = env[0].post(endpoint(env, "/abandon"), headers=env[3], json={})
    assert response.status_code == 200
    response = env[0].get(endpoint(env, "/process"), headers=env[3])
    assert response.status_code in (200, 404)
    if response.status_code == 200:
        assert response.json() == {"process": None}
    assert env[0].post(endpoint(env, "/resume"), headers=env[3], json={"automatic": True}).status_code == 409
