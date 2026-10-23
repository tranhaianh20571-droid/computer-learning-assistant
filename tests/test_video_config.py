"""Video configs use existing encryption, ownership, revision and audit boundaries."""
import json
import socket
import uuid

import pytest

from backend.logging_audit.db import Database
from backend.slice1.adapters import CapabilityTestService
from backend.slice1.config_service import ConfigService
from backend.slice1.errors import Slice1Error
from backend.slice1.models import ServiceConfigRow


@pytest.fixture
def configs(monkeypatch):
    resolve = socket.getaddrinfo
    def synthetic_dns(host, *args, **kwargs):
        if host == "video.example.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        return resolve(host, *args, **kwargs)
    monkeypatch.setattr(socket, "getaddrinfo", synthetic_dns)
    db = Database()
    db.create_all()
    try:
        yield ConfigService(db, encryption_key="synthetic-video-encryption-material")
    finally:
        db.dispose()


def create(configs, protocol="minimax_video", **changes):
    args = dict(actor_id="synthetic-video-" + uuid.uuid4().hex, is_admin=False, kind="video_generation",
        protocol=protocol, endpoint="https://video.example.test/gateway", model_name="MiniMax-H3" if protocol == "minimax_video" else "synthetic-seedance-model",
        credentials={"api_key": "synthetic-video-key"})
    args.update(changes)
    return args["actor_id"], configs.create(**args)


@pytest.mark.parametrize("protocol", ["minimax_video", "seedance"])
def test_video_encrypted_masked_owned_and_versioned(configs, protocol):
    owner, view = create(configs, protocol)
    assert view["kind"] == "video_generation" and view["capability_status"] == {"video_generate": "unknown"}
    assert "synthetic-video-key" not in json.dumps(view)
    with configs.db.session() as session:
        row = session.get(ServiceConfigRow, view["config_id"])
        assert "synthetic-video-key" not in row.encrypted_credentials
    assert configs.resolve_credentials(view["config_id"]) == {"api_key": "synthetic-video-key"}
    with pytest.raises(Slice1Error):
        configs.get(view["config_id"], actor_id="synthetic-other", is_admin=False)
    with pytest.raises(Slice1Error):
        configs.update(view["config_id"], actor_id="synthetic-other", is_admin=False, model_name="other-model")
    changed = configs.update(view["config_id"], actor_id=owner, is_admin=False, endpoint="https://video.example.test/new-base", model_name="synthetic-new-model")
    assert changed["config_version"] == 2 and changed["capability_status"] == {"video_generate": "unknown"}
    configs.deactivate(view["config_id"], actor_id=owner, is_admin=False)
    with pytest.raises(Slice1Error):
        configs.resolve_credentials(view["config_id"])


@pytest.mark.parametrize("changes", [{"endpoint": "http://video.example.test"}, {"endpoint": "https://user:secret@video.example.test"},
    {"model_name": ""}, {"credentials": {"api_key": "bad\nkey"}}, {"credentials": {"api_key": "synthetic", "headers": {}}}])
def test_invalid_create_and_update_are_atomic(configs, changes):
    with pytest.raises(Slice1Error):
        create(configs, **changes)
    owner, view = create(configs)
    with pytest.raises(Slice1Error):
        configs.update(view["config_id"], actor_id=owner, is_admin=False, **changes)
    assert configs.get(view["config_id"], actor_id=owner, is_admin=False)["config_version"] == 1


def test_capability_probe_never_generates_or_claims_available(configs, monkeypatch):
    owner, view = create(configs)
    from backend.slice1.adapters.video import MiniMaxVideoAdapter
    monkeypatch.setattr(MiniMaxVideoAdapter, "_request", lambda *a, **k: pytest.fail("probe must not send"))
    result = CapabilityTestService(configs).run(view["config_id"], actor_id=owner, is_admin=False)
    assert result["capability_status"]["video_generate"]["state"] == "unknown"
