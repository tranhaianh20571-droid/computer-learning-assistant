"""切片 1：外发确认与 worker 核验（T05，AC-05）。"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

from backend.logging_audit.db import get_database_url  # noqa: E402

os.environ.setdefault("LEARNING_DATABASE_URL", get_database_url())

from backend.logging_audit.db import Database  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from backend.logging_audit.repositories import PgAuditLog  # noqa: E402
from backend.slice1.config_service import ConfigService  # noqa: E402
from backend.slice1.disclosure_service import DisclosureService  # noqa: E402
from backend.slice1.errors import Slice1Error  # noqa: E402
from backend.slice1.models import utcnow  # noqa: E402

FAKE_KEY = "sk-test-fake-key-000000000000"


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    return db


@pytest.fixture()
def services(database: Database):
    audit = PgAuditLog(database)
    configs = ConfigService(database, encryption_key="k")
    disclosures = DisclosureService(database, audit)
    return {"configs": configs, "disclosures": disclosures, "audit": audit}


def _setup_config(configs: ConfigService, owner: str, *, endpoint: str = "https://api.example.test/v1") -> dict:
    return configs.create(
        actor_id=owner,
        is_admin=False,
        kind="content_model",
        protocol="openai",
        endpoint=endpoint,
        model_name="m",
        credentials={"api_key": FAKE_KEY},
    )


class TestDisclosureGrants:
    def test_worker_without_grant_rejected(self, services):
        owner = f"user_{uuid.uuid4().hex[:8]}"
        cfg = _setup_config(services["configs"], owner)
        with pytest.raises(Slice1Error) as exc:
            services["disclosures"].verify(
                task_id="task_x", config_id=cfg["config_id"], content_category="material"
            )
        assert exc.value.code == "disclosure_required"

    def test_grant_allows_covered_category(self, services):
        owner = f"user_{uuid.uuid4().hex[:8]}"
        cfg = _setup_config(services["configs"], owner)
        services["disclosures"].create(
            actor_id=owner,
            task_id="task_1",
            config_id=cfg["config_id"],
            content_category="material",
            scope_snapshot={"material_ids": ["mat_1"]},
        )
        verified = services["disclosures"].verify(
            task_id="task_1", config_id=cfg["config_id"], content_category="material"
        )
        assert verified["config_version"] == 1

    def test_category_must_match(self, services):
        owner = f"user_{uuid.uuid4().hex[:8]}"
        cfg = _setup_config(services["configs"], owner)
        services["disclosures"].create(
            actor_id=owner,
            task_id="task_2",
            config_id=cfg["config_id"],
            content_category="material",
        )
        with pytest.raises(Slice1Error) as exc:
            services["disclosures"].verify(
                task_id="task_2", config_id=cfg["config_id"], content_category="query"
            )
        assert exc.value.code == "disclosure_required"

    def test_revoke_stops_worker(self, services):
        owner = f"user_{uuid.uuid4().hex[:8]}"
        cfg = _setup_config(services["configs"], owner)
        grant = services["disclosures"].create(
            actor_id=owner,
            task_id="task_3",
            config_id=cfg["config_id"],
            content_category="query",
        )
        services["disclosures"].revoke(grant["grant_id"], actor_id=owner)
        with pytest.raises(Slice1Error) as exc:
            services["disclosures"].verify(
                task_id="task_3", config_id=cfg["config_id"], content_category="query"
            )
        assert exc.value.code == "disclosure_revoked"

    def test_expired_grant_rejected(self, services, database: Database):
        owner = f"user_{uuid.uuid4().hex[:8]}"
        cfg = _setup_config(services["configs"], owner)
        grant = services["disclosures"].create(
            actor_id=owner,
            task_id="task_4",
            config_id=cfg["config_id"],
            content_category="material",
            ttl_seconds=1,
        )
        from backend.slice1.models import DisclosureGrantRow

        with database.session() as sess:
            row = sess.get(DisclosureGrantRow, grant["grant_id"])
            row.expires_at = utcnow() - timedelta(seconds=1)
            sess.commit()
        with pytest.raises(Slice1Error) as exc:
            services["disclosures"].verify(
                task_id="task_4", config_id=cfg["config_id"], content_category="material"
            )
        assert exc.value.code == "disclosure_expired"

    def test_config_endpoint_change_invalidates_grant(self, services):
        owner = f"user_{uuid.uuid4().hex[:8]}"
        cfg = _setup_config(services["configs"], owner)
        services["disclosures"].create(
            actor_id=owner,
            task_id="task_5",
            config_id=cfg["config_id"],
            content_category="material",
        )
        services["configs"].update(
            cfg["config_id"], actor_id=owner, is_admin=False, endpoint="https://api2.example.test/v1"
        )
        with pytest.raises(Slice1Error) as exc:
            services["disclosures"].verify(
                task_id="task_5", config_id=cfg["config_id"], content_category="material"
            )
        assert exc.value.code in ("disclosure_revoked", "disclosure_expired")

    def test_deactivate_config_blocks_worker(self, services):
        owner = f"user_{uuid.uuid4().hex[:8]}"
        cfg = _setup_config(services["configs"], owner)
        services["disclosures"].create(
            actor_id=owner, task_id="task_6", config_id=cfg["config_id"], content_category="material"
        )
        services["configs"].deactivate(cfg["config_id"], actor_id=owner, is_admin=False)
        with pytest.raises(Slice1Error) as exc:
            services["disclosures"].verify(
                task_id="task_6", config_id=cfg["config_id"], content_category="material"
            )
        assert exc.value.code == "config_not_found"

    def test_cross_account_grant_hidden(self, services):
        owner = f"user_{uuid.uuid4().hex[:8]}"
        cfg = _setup_config(services["configs"], owner)
        services["disclosures"].create(
            actor_id=owner, task_id="task_7", config_id=cfg["config_id"], content_category="material"
        )
        assert services["disclosures"].list_for_task("task_7", actor_id="intruder") == []
        # 即使知道 config_id，也拿不到他人授权
        with pytest.raises(Slice1Error):
            services["disclosures"].verify(
                task_id="task_7",
                config_id=cfg["config_id"],
                content_category="material",
                actor_id="intruder",
            )

    def test_audit_records_confirmation_and_revocation(self, services):
        owner = f"user_{uuid.uuid4().hex[:8]}"
        cfg = _setup_config(services["configs"], owner)
        grant = services["disclosures"].create(
            actor_id=owner, task_id="task_8", config_id=cfg["config_id"], content_category="material"
        )
        services["disclosures"].revoke(grant["grant_id"], actor_id=owner)
        records = services["audit"].list_all_for_test()
        events = [r.event for r in records]
        assert "external.disclosure.confirmed" in events
        reasons = " ".join(r.result or "" for r in records)
        assert "revoked" in reasons or "granted" in reasons
