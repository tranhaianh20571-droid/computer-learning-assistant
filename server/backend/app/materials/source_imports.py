"""Durable, owner-bound staged imports and standard private source publication."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select

from .errors import error
from .models import MaterialRow, utcnow
from .source_models import SourceBatchRow, SourceItemRow, ImportedSourceRow
from .source_settings import SourceSettings
from .source_clients import classify_url, scrape
from .storage import MaterialStorage
from ..agents.model_routing import role_config
from ..tasks.service import LearningTaskRow, LeaseError
from ..slice1.errors import Slice1Error


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class SourceImports:
    def __init__(self, attachments):
        self.attachments = attachments
        self.db, self.configs, self.tasks = attachments.db, attachments.configs, attachments.tasks
        self.storage, self.disclosures = attachments.storage, attachments.disclosures
        self.settings = SourceSettings(self.configs)
        self.stop = threading.Event()
        self.thread = None
        self.lock = threading.Lock()

    def _batch(self, session, owner, ident, *, lock=False):
        row = session.get(SourceBatchRow, ident, with_for_update=lock)
        if not row or row.owner_user_id != owner:
            self.configs.audit.append("access.denied", actor_id=owner, object_type="source_batch",
                object_id=ident, result="denied", reason="material_not_found")
            raise error("material_not_found")
        return row

    def _input(self, item):
        return self.configs._decrypt(item.encrypted_input)

    def create(self, owner, key, items):
        if not 1 <= len(items) <= 10:
            raise error("too_many_files")
        canonical, clients = [], set()
        for item in items:
            data = item.model_dump()
            if data["client_id"] in clients:
                raise error("invalid_field")
            clients.add(data["client_id"])
            if data.get("url"):
                route = classify_url(data["url"])
                if route["kind"] == "collection":
                    raise error("unsupported_media_type", "请选择具体文章或视频，暂不展开合集或搜索结果。")
                data.update(url=route["url"], detected_kind=route["kind"], platform=route["platform"])
                from .web_directory import selected_urls
                data["chapter_urls"] = selected_urls(route["url"], data.get("chapter_urls", []))
            else:
                if data.get("chapter_urls"):
                    raise error("invalid_field")
                kind = data["name"].rsplit(".", 1)[-1].lower()
                data["detected_kind"] = ("image" if kind in ("png", "jpg", "jpeg", "webp")
                    else "video" if kind in ("mp4", "webm", "mov", "mkv") else "document")
            if not data.get("chapter_urls"):
                data.pop("chapter_urls", None)
            canonical.append(data)
        manifest = digest(canonical)
        with self.db.session() as session:
            if session.bind.dialect.name == "postgresql":
                from sqlalchemy import func
                session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended("source:" + owner, 0))))
            existing = session.scalar(select(SourceBatchRow).where(
                SourceBatchRow.owner_user_id == owner, SourceBatchRow.upload_key == digest(key)))
            if existing:
                if existing.manifest_hash != manifest:
                    raise error("page_state_conflict", "相同发送请求包含不同资料。")
                ident = existing.batch_id
            else:
                ident = "src_" + uuid.uuid4().hex[:20]
                session.add(SourceBatchRow(batch_id=ident, owner_user_id=owner, upload_key=digest(key),
                    manifest_hash=manifest, status="draft"))
                session.flush()
                for data in canonical:
                    session.add(SourceItemRow(item_id="si_" + uuid.uuid4().hex[:20],
                        batch_id=ident, owner_user_id=owner, client_id=data["client_id"],
                        kind=data["detected_kind"], encrypted_input=self.configs._encrypt(data),
                        source_hash=digest(data), status="uploaded" if data.get("url") else "pending"))
        return self.view(owner, ident)

    def upload(self, owner, ident, item_id, path):
        with self.db.session() as session:
            batch = self._batch(session, owner, ident, lock=True)
            item = session.get(SourceItemRow, item_id, with_for_update=True)
            if not item or item.batch_id != ident or item.owner_user_id != owner or batch.status != "draft":
                raise error("page_state_conflict")
            metadata = self._input(item)
            if metadata.get("url"):
                raise error("invalid_field")
            size = path.stat().st_size
            if size <= 0 or size > 512_000_000:
                raise error("file_too_large")
            with path.open("rb") as uploaded:
                current = hashlib.file_digest(uploaded, "sha256").hexdigest()
            if item.status == "uploaded":
                if item.source_hash != current:
                    raise error("page_state_conflict")
                return
            # The staged area has its own per-owner hard cap, including abandoned drafts.
            from sqlalchemy import func
            session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended("source-upload:" + owner, 0)))) if session.bind.dialect.name == "postgresql" else None
            staged = session.scalar(select(func.coalesce(func.sum(SourceItemRow.size_bytes), 0)).where(
                SourceItemRow.owner_user_id == owner, SourceItemRow.material_id.is_(None)))
            if staged + size > 1024 ** 3:
                raise error("quota_exceeded")
            if item.kind == "document":
                from .attachments import validate_document
                validate_document(metadata["name"], metadata.get("mime"), path)
            elif item.kind == "image":
                if size > 20 * 1024 ** 2:
                    raise error("file_too_large")
                from .source_processing import image_payload
                image_payload(path.read_bytes())
            self.storage.put_file("imports/" + item_id + ".bin", path)
            item.source_hash, item.size_bytes, item.status = current, size, "uploaded"

    def prepare(self, owner, ident):
        with self.db.session() as session:
            batch = self._batch(session, owner, ident)
            if batch.status not in ("draft", "prepared"):
                raise error("page_state_conflict")
            items = list(session.scalars(select(SourceItemRow).where(SourceItemRow.batch_id == ident)))
        manifests = {}
        destinations = {}
        for item in items:
            if item.status != "uploaded":
                raise error("page_state_conflict", "请等待文件上传完成。")
            data = self._input(item)
            binding = {"item_id": item.item_id, "sha256": item.source_hash, "connections": []}
            if item.kind == "web":
                binding["reader"] = self.settings.snapshot(owner, "firecrawl")
            elif item.kind in ("video", "image"):
                binding["reader"] = self.settings.snapshot(owner, "bilinote")
                if item.kind == "image" or binding["reader"]["options"]["video_understanding"]:
                    model = role_config(self.configs, owner, "video_understanding")
                    if not model:
                        raise Slice1Error("config_not_found", "请在 BiliNote 页配置多模态模型。", 409)
                    binding["model"] = {k: model[k] for k in ("config_id", "config_version", "endpoint", "protocol", "model_name")}
            elif item.kind in ("document", "file"):
                ext = data.get("name", "").rsplit(".", 1)[-1].lower()
                if item.kind == "file":
                    from urllib.parse import urlsplit
                    ext = urlsplit(data["url"]).path.rsplit(".", 1)[-1].lower()
                if ext not in ("txt", "md", "markdown"):
                    config = self.attachments.config(owner)
                    if not config:
                        raise Slice1Error("config_not_found", "请先配置文档识别服务。", 409)
                    binding["document"] = {k: config[k] for k in ("config_id", "config_version", "endpoint")}
                for role, model in self.attachments.questions.defaults(owner).items():
                    if model:
                        binding[role] = {k: model[k] for k in
                            ("config_id", "config_version", "endpoint", "protocol", "model_name")}
            for slot in ("reader", "model", "document", "semantic", "visual"):
                if slot in binding:
                    config = binding[slot]
                    destinations[config["config_id"]] = {"endpoint": config["endpoint"],
                        "config_id": config["config_id"], "config_version": config["config_version"],
                        "purpose": slot}
                    binding["connections"].append(config["config_id"])
            manifests[item.item_id] = binding
        manifest_hash = digest(manifests)
        with self.db.session() as session:
            batch = self._batch(session, owner, ident, lock=True)
            if batch.status not in ("draft", "prepared"):
                raise error("page_state_conflict")
            for item_id, binding in manifests.items():
                session.get(SourceItemRow, item_id, with_for_update=True).config_json = json.dumps(binding)
            batch.status = "prepared"
            batch.manifest_hash = manifest_hash
        return {**self.view(owner, ident), "manifest_hash": manifest_hash, "destinations": list(destinations.values())}

    def commit(self, owner, ident, manifest_hash, confirmed):
        with self.db.session() as session:
            batch = self._batch(session, owner, ident, lock=True)
            if batch.manifest_hash != manifest_hash or not confirmed:
                raise Slice1Error("disclosure_required", "请确认当前资料处理范围。", 409)
            if batch.status in ("processing", "ready"):
                return self.view(owner, ident)
            if batch.status not in ("prepared", "committing"):
                raise error("page_state_conflict")
            batch.status = "committing"
            items = list(session.scalars(select(SourceItemRow).where(SourceItemRow.batch_id == ident)))
        try:
            for item in items:
                if item.material_id:
                    continue
                data, binding = self._input(item), json.loads(item.config_json)
                for slot in ("reader", "model", "document", "semantic", "visual"):
                    if slot in binding:
                        self._connection(owner, binding[slot])
                if item.kind == "document":
                    from .attachments import validate_document
                    path = self.storage.local_path("imports/" + item.item_id + ".bin")
                    validated = validate_document(data["name"], data.get("mime"), path)
                    target = binding.get("document", {})
                    material = self.attachments.create(owner, validated, target.get("config_id"),
                        target.get("config_version"), item.item_id,
                        extraction={k: binding[k] for k in ("semantic", "visual") if k in binding})
                    material_id = material["material_id"]
                else:
                    material_id = self._create_source(owner, item, binding)
                with self.db.session() as session:
                    current_batch = self._batch(session, owner, ident, lock=True)
                    if current_batch.status != "committing":
                        raise error("page_state_conflict")
                    current = session.get(SourceItemRow, item.item_id, with_for_update=True)
                    current.material_id, current.status = material_id, "processing"
                self.storage.delete("imports/" + item.item_id + ".bin")
            with self.db.session() as session:
                self._batch(session, owner, ident, lock=True).status = "processing"
        except Exception:
            with self.db.session() as session:
                batch = self._batch(session, owner, ident, lock=True)
                if batch.status == "committing":
                    batch.status = "prepared"
            raise
        return self.view(owner, ident)

    def _connection(self, owner, snapshot):
        view = self.configs.get(snapshot["config_id"], actor_id=owner, is_admin=False)
        if not view["is_active"] or view["config_version"] != snapshot["config_version"]:
            raise Slice1Error("disclosure_revoked", "处理配置已变化，请重新确认。", 409)
        return view

    def _create_source(self, owner, item, binding):
        material_id = "mat_" + digest(item.item_id)[:20]
        with self.db.session() as session:
            if session.bind.dialect.name == "postgresql":
                from sqlalchemy import func
                session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended("source-material:" + owner, 0))))
            current = session.get(MaterialRow, material_id, with_for_update=True)
            if current:
                if current.owner_user_id != owner or current.deleted_at:
                    raise error("material_not_found")
                return material_id
            from .models import MaterialUploadQuotaRow
            self.attachments.materials.quotas._row(session, owner)
            quota = session.get(MaterialUploadQuotaRow, owner, with_for_update=True)
            reservation = max(item.size_bytes, (384 if item.kind=="web" else 768 if item.kind in ("video","file") else 32) * 1024 ** 2)
            if quota.quota_bytes - quota.used_bytes - quota.reserved_bytes < reservation:
                raise error("quota_exceeded", "资料处理的预留存储空间不足。")
            quota.reserved_bytes += reservation
            material = MaterialRow(material_id=material_id, owner_user_id=owner,
                filename_hash=digest(item.item_id), source_bytes_sha256=item.source_hash,
                kind=item.kind, size_bytes=item.size_bytes, quota_reserved_bytes=reservation, status="parsing")
            session.add(material)
            session.flush()
            if item.size_bytes:
                self.storage.put_file(MaterialStorage.original_key(material_id),
                    self.storage.local_path("imports/" + item.item_id + ".bin"))
            task = self.tasks.create_task(owner_user_id=owner, kind="source_import", subject_id=material_id,
                idempotency_key=item.item_id, stage="parse", lane="maintenance",
                deadline_seconds=28800 if item.kind == "file" else 7200 if item.kind == "video" or self._input(item).get("chapter_urls") else 600,
                session=session)
            from ..agents.task_binding import AgentBudgetRow
            budget = session.get(AgentBudgetRow, task["task_id"])
            budget.request_limit, budget.tool_limit = (512 if self._input(item).get("chapter_urls") else 256), 0
            binding["grants"] = {}
            for slot in ("reader", "model", "document", "semantic", "visual"):
                if slot in binding:
                    target = binding[slot]
                    grant = self.disclosures.create(actor_id=owner, task_id=task["task_id"],
                        config_id=target["config_id"], config_version=target["config_version"],
                        content_category="material", scope_snapshot={"material_ids": [material_id]},
                        session=session)
                    binding["grants"][target["config_id"]] = grant["grant_id"]
            state = {}
            if binding.get("document", {}).get("protocol") == "paddleocr_vl":
                from ..slice1.adapters.paddle_vl import document_options, crop_document_options
                state["ocr_options"] = document_options()
                state["ocr_crop_options"] = crop_document_options()
            if binding.get("semantic"):
                state["extraction_binding"] = self.attachments.questions.bind(session,
                    owner=owner, task_id=task["task_id"], material_id=material_id,
                    sha256=item.source_hash, targets={k: binding[k] for k in ("semantic", "visual") if k in binding},
                    pages=2000 if item.kind == "file" else 1)
            session.add(ImportedSourceRow(material_id=material_id, owner_user_id=owner,
                task_id=task["task_id"], source_version=item.source_hash,
                binding_json=json.dumps(binding), result_json=json.dumps(state)))
        return material_id

    def view(self, owner, ident):
        with self.db.session() as session:
            batch = self._batch(session, owner, ident)
            items = list(session.scalars(select(SourceItemRow).where(SourceItemRow.batch_id == ident)))
            output, destinations = [], {}
            for item in items:
                status, failure = item.status, item.failure_code
                details = {}
                if item.material_id:
                    material = session.get(MaterialRow, item.material_id)
                    if not material or material.deleted_at:
                        status, failure = "failed", "material_not_found"
                    else:
                        status = "ready" if material.status == "parsed" else material.status
                        source = session.get(ImportedSourceRow, item.material_id)
                        if source:
                            failure = source.failure_code
                            task = session.get(LearningTaskRow, source.task_id)
                            if task and task.status in ("failed", "cancelled", "expired"):
                                status = "failed"
                            result = json.loads(source.result_json)
                            details["question_extraction_status"] = result.get("question_bank", {}).get("status")
                            details.update({k: result[k] for k in ("title", "unit", "visual_frames",
                                "duration", "visual_coverage_complete", "review_reasons", "web_completed",
                                "web_total") if k in result})
                        else:
                            from .models import DocumentAttachmentRow
                            document = session.get(DocumentAttachmentRow, item.material_id)
                            failure = document.failure_code if document else failure
                            if document:
                                result = json.loads(document.result_json)
                                details["question_extraction_status"] = result.get("question_bank", {}).get("status")
                metadata = self._input(item)
                binding = json.loads(item.config_json)
                for slot in ("reader", "model", "document", "semantic", "visual"):
                    if slot in binding:
                        destination = binding[slot]
                        destinations[destination["config_id"]] = {
                            "endpoint": destination["endpoint"], "config_id": destination["config_id"],
                            "config_version": destination["config_version"], "purpose": slot}
                output.append({"item_id": item.item_id, "client_id": item.client_id, "kind": item.kind,
                    "status": status, "failure_code": failure, "material_id": item.material_id,
                    "name": metadata.get("name"), "url": metadata.get("url"), **details})
                output[-1]["chapter_count"] = len(metadata.get("chapter_urls", []))
            ready = bool(output) and all(x["status"] == "ready" for x in output)
            return {"batch_id": ident, "status": "ready" if ready else batch.status,
                    "manifest_hash": batch.manifest_hash, "items": output,
                    "destinations": list(destinations.values()),
                    "material_ids": [x["material_id"] for x in output if x["status"] == "ready"]}

    def retry(self, owner, ident, manifest_hash, confirmed):
        from ..slice1.models import DisclosureGrantRow
        from ..agents.task_binding import AgentBudgetRow
        view = self.view(owner, ident)
        if not confirmed or manifest_hash != view["manifest_hash"]:
            raise Slice1Error("disclosure_required", "请确认重试范围。", 409)
        failed = {value["item_id"] for value in view["items"] if value["status"] == "failed"}
        if not failed:
            return view
        with self.db.session() as session:
            batch = self._batch(session, owner, ident, lock=True)
            if batch.status != "processing":
                raise error("page_state_conflict")
            items = list(session.scalars(select(SourceItemRow).where(
                SourceItemRow.batch_id == ident, SourceItemRow.item_id.in_(failed))))
            for item in items:
                binding = json.loads(item.config_json)
                for slot in ("reader", "model", "document", "semantic", "visual"):
                    if slot in binding:
                        self._connection(owner, binding[slot])
                row = session.get(ImportedSourceRow, item.material_id, with_for_update=True)
                if not row:
                    continue
                previous = session.get(LearningTaskRow, row.task_id)
                if previous.status not in ("failed", "cancelled", "expired"):
                    continue
                old_binding = json.loads(row.binding_json)
                for grant_id in old_binding.get("grants", {}).values():
                    grant = session.get(DisclosureGrantRow, grant_id)
                    if grant and grant.revoked_at:
                        raise Slice1Error("disclosure_revoked", "原授权已撤销，请重新准备资料和配置。", 409)
                task = self.tasks.create_task(owner_user_id=owner, kind="source_import",
                    subject_id=item.material_id, idempotency_key="source-retry:" + previous.task_id,
                    stage="parse", lane="maintenance",
                    deadline_seconds=28800 if item.kind == "file" else 7200, session=session)
                old_budget = session.get(AgentBudgetRow, previous.task_id)
                budget = session.get(AgentBudgetRow, task["task_id"])
                budget.request_limit, budget.tool_limit = (old_budget.request_limit if old_budget else 256), 0
                budget.logical_requests = old_budget.logical_requests if old_budget else 0
                budget.wire_requests = old_budget.wire_requests if old_budget else 0
                binding["grants"] = {}
                for slot in ("reader", "model", "document", "semantic", "visual"):
                    if slot in binding:
                        target = binding[slot]
                        grant = self.disclosures.create(actor_id=owner, task_id=task["task_id"],
                            config_id=target["config_id"], config_version=target["config_version"],
                            content_category="material", scope_snapshot={"material_ids": [item.material_id]}, session=session)
                        binding["grants"][target["config_id"]] = grant["grant_id"]
                row.task_id, row.binding_json, row.failure_code = task["task_id"], json.dumps(binding), None
                if item.kind == "web":
                    state = json.loads(row.result_json)
                    state.pop("web_pending_attempt", None)
                    row.result_json = json.dumps(state, ensure_ascii=False)
                if item.kind == "file":
                    state = json.loads(row.result_json)
                    extraction = state.get("extraction_binding")
                    if extraction:
                        self.attachments.questions.guard(session, owner, previous.task_id,
                            session.get(MaterialRow, item.material_id), extraction)
                        state["extraction_binding"] = self.attachments.questions.bind(session,
                            owner=owner, task_id=task["task_id"], material_id=item.material_id,
                            sha256=session.get(MaterialRow, item.material_id).source_bytes_sha256,
                            targets=extraction["targets"], pages=state.get("pages"),
                            previous_task=previous.task_id, previous=extraction)
                    for block in state.get("batches", []):
                        if block["status"] != "completed" and block.get("job_id") == "submitting":
                            block.pop("job_id", None)
                            block["status"] = "pending"
                    row.result_json = json.dumps(state, ensure_ascii=False)
                session.get(MaterialRow, item.material_id).status = "parsing"
        for item in items:
            if item.material_id and item.kind == "document":
                binding = json.loads(item.config_json).get("document", {})
                self.attachments.retry(item.material_id, owner, binding.get("config_id"), binding.get("config_version"))
        return self.view(owner, ident)

    def _guard(self, lease, owner, material_id):
        self.tasks.validate_lease(lease, actor_id=owner)
        from ..auth.models import UserRow
        with self.db.session() as session:
            user = session.get(UserRow, owner)
            material = session.get(MaterialRow, material_id)
            row = session.get(ImportedSourceRow, material_id)
            if (user and user.status != "approved" or not material or material.deleted_at
                    or material.owner_user_id != owner or not row or row.task_id != lease.task_id):
                raise LeaseError("TASK_CANCELLED", "source unavailable")
            binding = json.loads(row.binding_json)
            item = session.get(SourceItemRow, binding["item_id"])
            batch = session.get(SourceBatchRow, item.batch_id) if item else None
            if not batch or batch.status == "cancelled":
                raise LeaseError("TASK_CANCELLED", "source batch cancelled")
            extraction = json.loads(row.result_json).get("extraction_binding")
            if extraction:
                self.attachments.questions.guard(session, owner, lease.task_id, material, extraction)
        for slot in ("reader", "model", "document", "semantic", "visual"):
            if slot in binding:
                config = binding[slot]
                self._connection(owner, config)
                self.disclosures.verify_or_audit(task_id=lease.task_id, config_id=config["config_id"],
                    actor_id=owner, content_category="material", scope_snapshot={"material_ids": [material_id]},
                    expected_grant_id=binding["grants"][config["config_id"]])
        return binding

    def run(self, task_id, owner, material_id):
        lease = self.tasks.claim(task_id, lease_owner="source-reader", lease_seconds=180, allow_reclaim_active=False)
        if not lease:
            return
        kind = None
        try:
            binding = self._guard(lease, owner, material_id)
            with self.db.session() as session:
                item = session.get(SourceItemRow, binding["item_id"])
                data, kind = self._input(item), item.kind
                source_path = self.storage.local_path(MaterialStorage.original_key(material_id)) if not data.get("url") else None
            last_heartbeat = [0.]
            def guard():
                if self.stop.is_set():
                    raise LeaseError("TASK_CANCELLED", "worker stopping")
                if time.monotonic() - last_heartbeat[0] > 20:
                    self.tasks.heartbeat(lease, lease_seconds=600)
                    last_heartbeat[0] = time.monotonic()
                self._guard(lease, owner, material_id)
            def dependencies(slot):
                from ..agents.task_binding import bind_task_deps
                target = binding.get(slot, {})
                descriptor = SimpleNamespace(**target, disclosure_grant_id=binding["grants"].get(target.get("config_id")))
                deps = bind_task_deps(self.db, descriptor, actor_id=owner, task_id=task_id, disclosures=self.disclosures)
                from dataclasses import replace
                check = deps.check_state
                return replace(deps, check_state=lambda: (guard(), check()))
            if kind == "web":
                target = binding["reader"]
                credentials = self.configs.resolve_credentials(target["config_id"])
                if data.get("chapter_urls"):
                    state = self._web_book(lease, owner, material_id, data, target,
                        credentials["api_key"], dependencies, guard)
                else:
                    state = asyncio.run(scrape(target["endpoint"], credentials["api_key"],
                        data["url"], target["options"], dependencies("reader")))
                from .source_processing import image_payload
                from .source_clients import fetch_public
                state["assets"] = []
                images_started = time.monotonic()
                for remote in state.pop("remote_images", [])[:16]:
                    guard()
                    if time.monotonic() - images_started >= 120:
                        state.setdefault("review_reasons", []).append("source_image_budget_exhausted")
                        break
                    try:
                        payload, _, _ = asyncio.run(fetch_public(remote, dependencies("reader"), timeout=12))
                        image = image_payload(payload)
                        index = len(state["assets"])
                        self.storage.put(f"source-assets/{material_id}/{index}.jpg", image)
                        page_no = next((page["page_no"] for page in state["page_results"]
                            if any(remote in block.get("content", "") for block in page["blocks"])), None)
                        state["assets"].append({"index": index, "page_no": page_no,
                            "url": f"/api/source-materials/{material_id}/assets/{index}"})
                    except Exception as failure:
                        if isinstance(failure, LeaseError) or getattr(failure, "code", "") in (
                            "disclosure_revoked", "disclosure_expired", "MODEL_CREDENTIAL_INVALID"):
                            raise
                        state.setdefault("review_reasons", []).append("source_image_unavailable")
            elif kind == "file":
                state = self._remote_document(data["url"], material_id, binding, dependencies, guard,
                    lease=lease, owner=owner)
                if state is None:
                    return
            elif kind in ("video", "image"):
                from .source_processing import image_payload, extract_video, understand_frames, video_state
                from ..slice3_app import _configured_model_descriptor
                target = self._connection(owner, binding["model"]) if binding.get("model") else None
                model = _configured_model_descriptor(target, self.configs.resolve_credentials(target["config_id"])) if target else None
                if kind == "image":
                    if data.get("url"):
                        from .source_clients import fetch_public
                        raw, _, _ = asyncio.run(fetch_public(data["url"], dependencies("reader")))
                    else:
                        raw = source_path.read_bytes()
                    payload = image_payload(raw)
                    frames = [{"time_ms": 0, "mime": "image/jpeg", "data": base64.b64encode(payload).decode()}]
                    visual = asyncio.run(understand_frames(model, frames, dependencies("model")))
                    from .local_documents import parse_text
                    text = visual[0]["transcription"].strip()
                    if not text and not visual[0]["explanation"].strip():
                        raise error("empty_file", "图片没有可识别原文，无法作为文字资料。")
                    state = parse_text((text or visual[0]["explanation"]).encode(), "txt")
                    state.update(source_kind="image", unit="image", visual_frames=1)
                    if not text:
                        for page in state["page_results"]:
                            for block in page["blocks"]:
                                block["source_kind"] = "image_understanding"
                        state["chunks"] = []
                        state["review_reasons"] = ["image_without_verbatim_text"]
                    state["chunks"].append({"page_no": 1, "kind": "image_understanding", "text": visual[0]["explanation"]})
                    from .study_sources import build_study_index
                    state["study_index"] = build_study_index(state)
                else:
                    reader = dict(binding["reader"])
                    reader["api_key"] = self.configs.resolve_credentials(reader["config_id"])["api_key"]
                    checkpoint_key = f"source-checkpoints/{material_id}.json"
                    try:
                        cached = self.storage.get(checkpoint_key)
                    except Exception:
                        cached = None
                    if cached:
                        result = json.loads(cached)
                    else:
                        result = extract_video(data.get("url"), source_path, reader, dependencies("reader"))
                        guard()
                        self.storage.put(checkpoint_key, json.dumps(result, ensure_ascii=False).encode())
                    frames = result.get("frames", [])
                    if not isinstance(frames, list) or len(frames) > reader["options"]["max_frames"]:
                        raise error("invalid_field", "视频服务返回的帧数超过已确认范围。")
                    if not frames and reader["options"]["video_understanding"]:
                        raise error("invalid_field", "视频服务未返回截图。")
                    visual_key = f"source-checkpoints/{material_id}-visual.json"
                    try:
                        cached_visual = json.loads(self.storage.get(visual_key))
                    except Exception:
                        cached_visual = []
                    visual = list(cached_visual)
                    for offset in range(len(visual), len(frames), 4) if reader["options"]["video_understanding"] else ():
                        guard()
                        next_visual = asyncio.run(understand_frames(model, frames[offset:offset + 4], dependencies("model"),
                            grid_size=reader["options"]["grid_size"]))
                        visual.extend([{**value, "index": value["index"] + offset} for value in next_visual])
                        self.storage.put(visual_key, json.dumps(visual, ensure_ascii=False).encode())
                    state = video_state(result, visual)
                    if data.get("url") and reader["options"]["link"]:
                        state["source_url"] = data["url"]
                        from .study_sources import build_study_index
                        state["study_index"] = build_study_index(state)
                saved_frames = frames if kind=="image" or binding["reader"]["options"]["screenshot"] else []
                for index, frame in enumerate(saved_frames):
                    guard()
                    self.storage.put(f"source-assets/{material_id}/{index}.jpg",
                        image_payload(base64.b64decode(frame["data"], validate=True)))
                state["assets"] = [{"index": i, "time_ms": f.get("time_ms", 0),
                    "url": f"/api/source-materials/{material_id}/assets/{i}"} for i, f in enumerate(saved_frames)]
            else:
                raise error("unsupported_media_type", "文件直链请下载后作为文件粘贴上传。")
            guard()
            def publish(session):
                material = session.get(MaterialRow, material_id, with_for_update=True)
                row = session.get(ImportedSourceRow, material_id, with_for_update=True)
                if material.deleted_at or row.task_id != task_id:
                    raise LeaseError("TASK_CANCELLED", "source unavailable")
                from ..slice1.models import ServiceConfigRow
                for slot in ("reader", "model", "document", "semantic", "visual"):
                    if slot in binding:
                        target = binding[slot]
                        config = session.get(ServiceConfigRow, target["config_id"], with_for_update=True)
                        if not config or not config.is_active or config.config_version != target["config_version"]:
                            raise LeaseError("TASK_CANCELLED", "source configuration changed")
                        self.disclosures.verify(task_id=task_id, config_id=target["config_id"], actor_id=owner,
                            content_category="material", scope_snapshot={"material_ids": [material_id]}, session=session)
                if state.get("extraction_binding"):
                    self.attachments.questions.guard(session, owner, task_id, material, state["extraction_binding"])
                material.status = "parsed"
                serialized = json.dumps(state, ensure_ascii=False)
                stored = len(serialized.encode()) + material.size_bytes
                from .figure_crops import crop_storage_bytes
                stored += crop_storage_bytes(self.storage, material_id, state)
                for asset in state.get("assets", []):
                    stored += self.storage.local_path(f"source-assets/{material_id}/{asset['index']}.jpg").stat().st_size
                for suffix in (".json", "-visual.json"):
                    checkpoint = self.storage._resolve(f"source-checkpoints/{material_id}{suffix}")
                    if checkpoint.is_file():
                        stored += checkpoint.stat().st_size
                from .models import MaterialUploadQuotaRow
                quota = session.get(MaterialUploadQuotaRow, owner, with_for_update=True)
                delta = stored - material.quota_reserved_bytes
                if delta > quota.quota_bytes - quota.used_bytes - quota.reserved_bytes:
                    raise error("quota_exceeded", "资料结果超过可用存储空间。")
                quota.reserved_bytes += delta
                material.quota_reserved_bytes = stored
                row.result_json = serialized
                row.failure_code = None
                from .corpus import ensure_document
                ensure_document(session, owner, material, row)
            self.tasks.submit_result(lease, stage="parse", status="succeeded", publish=publish)
        except Exception as exc:
            if isinstance(exc, LeaseError):
                return
            if kind == "file":
                from .attachments import document_failure_code
                with self.db.session() as session:
                    saved = json.loads(session.get(ImportedSourceRow, material_id).result_json)
                code = document_failure_code(exc, saved.get("document_input", {}).get("kind"))
                if getattr(exc, "code", "") in ("invalid_file_signature", "file_too_large",
                        "unsupported_media_type", "too_many_pages"):
                    code = "UPLOAD_TYPE_REJECTED"
            else:
                code = getattr(exc, "code", "OCR_OUTPUT_INVALID")
            from ..logging_audit.contracts import ERROR_CODES
            if code not in ERROR_CODES:
                code = "OCR_OUTPUT_INVALID"
            def fail(session):
                material = session.get(MaterialRow, material_id, with_for_update=True)
                row = session.get(ImportedSourceRow, material_id, with_for_update=True)
                if not material.deleted_at and row.task_id == task_id:
                    material.status, row.failure_code = "failed", code
                    state = json.loads(row.result_json) if kind in ("file", "web") else {}
                    state.update({"failure_phase": getattr(exc, "source_phase", "processing"),
                        "failure_reason": getattr(exc, "source_reason", "controlled_failure"),
                        "failure_type": type(exc).__name__})
                    if code == "PPTX_SLIDE_TOO_LARGE":
                        state["failed_slide"] = getattr(exc, "page_no", None)
                    row.result_json = json.dumps(state, ensure_ascii=False)
            self.tasks.submit_result(lease, stage="parse", status="failed", error_code=code, publish=fail)

    def _web_book(self, lease, owner, material_id, data, target, key, dependencies, guard):
        from .web_directory import assemble_book
        with self.db.session() as session:
            checkpoint = json.loads(session.get(ImportedSourceRow, material_id).result_json)
        entries = checkpoint.get("web_entries", [])
        selected = data["chapter_urls"]
        if any(entry["url"] != selected[i] for i, entry in enumerate(entries)):
            raise error("page_state_conflict")
        # A reclaimed attempt cannot replay a request with an unknown result.
        if checkpoint.get("web_pending_attempt"):
            raise error("OCR_OUTPUT_INVALID", "章节提交结果未知，请显式重试。")
        for url in selected[len(entries):]:
            guard()
            checkpoint = {"web_entries": entries, "web_total": len(selected),
                "web_completed": len(entries), "web_pending_attempt": lease.attempt}
            self._document_checkpoint(lease, owner, material_id, checkpoint, guard)
            result = asyncio.run(scrape(target["endpoint"], key, url, target["options"], dependencies("reader")))
            entries.append({"url": url, "state": result})
            # Check the aggregate bound before persisting more private content.
            from .web_directory import MAX_BOOK_BYTES
            size = sum(len(block.get("content", "").encode()) for entry in entries
                for page in entry["state"].get("page_results", []) for block in page.get("blocks", []))
            if size > MAX_BOOK_BYTES:
                raise error("file_too_large", "所选章节正文超过 20 MiB，请减少章节。")
            checkpoint = {"web_entries": entries, "web_total": len(selected), "web_completed": len(entries)}
            self._document_checkpoint(lease, owner, material_id, checkpoint, guard)
        state = assemble_book(data["url"], entries)
        state.update(web_total=len(selected), web_completed=len(entries))
        return state

    def _document_checkpoint(self, lease, owner, material_id, state, guard):
        guard()
        with self.db.session() as session:
            self.tasks.validate_lease(lease, actor_id=owner, session=session)
            material = session.get(MaterialRow, material_id, with_for_update=True)
            row = session.get(ImportedSourceRow, material_id, with_for_update=True)
            if (not material or material.deleted_at or material.owner_user_id != owner
                    or not row or row.owner_user_id != owner or row.task_id != lease.task_id):
                raise LeaseError("TASK_CANCELLED", "source unavailable")
            row.result_json = json.dumps(state, ensure_ascii=False)

    def _remote_document(self, url, material_id, binding, dependencies, guard, *, lease, owner):
        from urllib.parse import unquote, urlsplit
        from .source_clients import fetch_public
        from .attachments import validate_document, LOCAL_KINDS
        guard()
        with self.db.session() as session:
            row = session.get(ImportedSourceRow, material_id)
            state = json.loads(row.result_json)
        path = self.storage._resolve(MaterialStorage.original_key(material_id))
        original = state.get("document_input")
        if original:
            if original["source_url"] != url:
                raise ValueError("document_source_changed")
            validated = validate_document("document." + original["kind"], original["mime"], path)
            if validated.sha256 != original["sha256"]:
                raise ValueError("document_source_changed")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            staging = path.with_suffix(".download")
            try:
                _, mime, final = asyncio.run(fetch_public(url, dependencies("document"),
                    limit=512_000_000, destination=staging))
                guard()
                name = unquote(urlsplit(final).path.rsplit("/", 1)[-1])
                validated = validate_document(name, mime.split(";")[0], staging)
                original = {"source_url": url, "final_url": final, "kind": validated.kind,
                    "mime": mime.split(";")[0], "sha256": validated.sha256}
                self.storage.put_file(MaterialStorage.original_key(material_id), staging)
            finally:
                staging.unlink(missing_ok=True)
            state.update(pages=validated.page_count)
        def checkpoint(value):
            value["document_input"] = original
            self._document_checkpoint(lease, owner, material_id, value, guard)
        checkpoint(state)
        adapter, view = None, None
        try:
            if validated.kind not in LOCAL_KINDS:
                target = binding.get("document")
                if not target:
                    raise Slice1Error("disclosure_required", "链接返回的文件需要文档识别授权。", 409)
                view = self.attachments._config(owner, target["config_id"], target["config_version"])
                adapter = self.attachments.adapter_factory(view,
                    self.configs.resolve_document_credentials(view["config_id"], owner_user_id=owner,
                        expected_version=view["config_version"]))
                if hasattr(adapter, "request_guard"):
                    adapter.request_guard = guard
            state = self.attachments.process_document(path, validated.kind, state,
                adapter=adapter, protocol=view["protocol"] if view else None, guard=guard,
                checkpoint=checkpoint, stop=self.stop, material_id=material_id)
            if state is not None:
                self.attachments.questions.run(state, owner=owner, lease=lease, source=path,
                    kind=validated.kind, guard=guard, checkpoint=checkpoint)
            if state is not None:
                state.update(source_url=url, final_url=original["final_url"], document_input=original)
                checkpoint(state)
            return state
        finally:
            if adapter:
                adapter.close()

    def remove(self, owner, ident):
        with self.db.session() as session:
            batch = self._batch(session, owner, ident, lock=True)
            batch.status = "cancelled"
            items = list(session.scalars(select(SourceItemRow).where(SourceItemRow.batch_id == ident)))
        for item in items:
            if item.material_id:
                with self.db.session() as session:
                    row = session.get(ImportedSourceRow, item.material_id)
                    task_id = row.task_id if row else None
                    material = session.get(MaterialRow, item.material_id)
                    already_deleted = not material or bool(material.deleted_at)
                if task_id:
                    self.tasks.request_cancel(task_id, actor_id=owner)
                if not already_deleted:
                    self.attachments.materials.delete(item.material_id, actor_id=owner)
            self.storage.delete("imports/" + item.item_id + ".bin")
            with self.db.session() as session:
                current = session.get(SourceItemRow, item.item_id, with_for_update=True)
                current.size_bytes, current.status = 0, "cancelled"

    def start(self):
        if os.environ.get("SOURCE_WORKER_MODE") == "external":
            return
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop.clear()
            self.thread = threading.Thread(target=self.loop, daemon=True, name="source-imports")
            self.thread.start()

    def expire_drafts(self):
        with self.db.session() as session:
            batches = list(session.scalars(select(SourceBatchRow).where(
                SourceBatchRow.status.in_(("draft", "prepared")),
                SourceBatchRow.created_at < utcnow() - timedelta(hours=24))
                .with_for_update(skip_locked=True).limit(100)))
            for batch in batches:
                # A partially committed batch must keep its frozen manifest.
                items = list(session.scalars(select(SourceItemRow).where(
                    SourceItemRow.batch_id == batch.batch_id)))
                if any(item.material_id for item in items):
                    continue
                batch.status = "cancelled"
                for item in items:
                    self.storage.delete("imports/" + item.item_id + ".bin")
                    item.size_bytes, item.status, item.encrypted_input = 0, "cancelled", self.configs._encrypt({})

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)

    def loop(self):
        last_cleanup = 0.
        while not self.stop.wait(1):
            try:
                if time.monotonic() - last_cleanup > 60:
                    self.expire_drafts()
                    last_cleanup = time.monotonic()
                with self.db.session() as session:
                    from sqlalchemy import or_
                    rows = list(session.scalars(select(LearningTaskRow).where(
                        LearningTaskRow.kind == "source_import",
                        LearningTaskRow.status.in_(("queued", "leased", "running")),
                        or_(LearningTaskRow.status == "queued", LearningTaskRow.lease_until <= utcnow()))
                        .order_by(LearningTaskRow.created_at).limit(1)))
                for task in rows:
                    self.run(task.task_id, task.owner_user_id, task.subject_id)
            except Exception:
                # Content/provider failures are mapped by run; no raw exception logging.
                continue
