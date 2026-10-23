"""Pinned question roles on existing material tasks; no separate worker queue."""
from __future__ import annotations

import json
import uuid
from dataclasses import replace

from sqlalchemy import select

from ..agents.model_routing import role_config
from ..agents.task_binding import AgentBudgetRow, bind_task_deps
from ..prompts.service import PromptBindingRow, PromptService
from ..slice1.errors import Slice1Error
from ..slice1.models import ServiceConfigRow
from .question_extraction import PROMPTS, VERSION, add_native_pdf, extract_questions


class QuestionService:
    def __init__(self, attachments):
        self.attachments = attachments
        self.db, self.configs = attachments.db, attachments.configs
        self.prompts = PromptService(self.db)

    def defaults(self, owner):
        return {"semantic": role_config(self.configs, owner, "planning"),
                "visual": role_config(self.configs, owner, "video_understanding")}

    def target(self, session, owner, snapshot):
        row = session.get(ServiceConfigRow, snapshot["config_id"])
        if (not row or row.owner_scope != "user" or row.owner_user_id != owner
                or row.kind != "content_model" or not row.is_active
                or row.config_version != snapshot["config_version"]):
            raise Slice1Error("disclosure_revoked", "抽题模型配置已变化，请重新准备资料。", 409)
        return self.configs.public_view(row)

    def bind(self, session, *, owner, task_id, material_id, sha256, targets, pages=1,
             previous_task=None, previous=None):
        binding = {"targets": {}, "prompts": {}}
        from ..tasks.service import LearningTaskRow
        task = session.get(LearningTaskRow, task_id)
        scope = {"material_ids": [material_id]}
        if task.kind == "material_parse":
            scope["sha256"] = sha256
        for role in ("semantic", "visual"):
            snapshot = targets.get(role)
            if not snapshot:
                continue
            view = self.target(session, owner, snapshot)
            grant = self.attachments.disclosures.create(actor_id=owner, task_id=task_id,
                config_id=view["config_id"], config_version=view["config_version"],
                content_category="material", scope_snapshot=scope, session=session)
            binding["targets"][role] = {k: view[k] for k in
                ("config_id", "config_version", "endpoint", "protocol", "model_name")}
            binding["targets"][role]["grant_id"] = grant["grant_id"]
        if "semantic" not in binding["targets"]:
            return None
        for name in PROMPTS:
            if previous:
                old = session.scalar(select(PromptBindingRow).where(
                    PromptBindingRow.task_id == previous_task, PromptBindingRow.prompt_name == name))
                if not old or self.prompts._sha256(old.template_snapshot) != old.template_sha256:
                    raise ValueError("question_prompt_snapshot_invalid")
                template = old.template_snapshot
            else:
                template = self.prompts.load_local_template(name)["template"]
            hashed = self.prompts._sha256(template)
            session.add(PromptBindingRow(binding_id="pbind_" + uuid.uuid4().hex[:12],
                task_id=task_id, prompt_name=name, source="local_fallback",
                template_snapshot=template, template_sha256=hashed,
                output_schema_version=f"material-questions-{VERSION}", agent_code_version=str(VERSION)))
            binding["prompts"][name] = hashed
        budget = session.get(AgentBudgetRow, task_id)
        if budget is None:
            budget = AgentBudgetRow(task_id=task_id)
            session.add(budget)
        budget.request_limit = min(20000, max(32, (pages or 1) * 8 + 16))
        budget.tool_limit = 0
        if previous_task:
            old_budget = session.get(AgentBudgetRow, previous_task)
            if old_budget:
                budget.request_limit = old_budget.request_limit
                budget.logical_requests = old_budget.logical_requests
                budget.wire_requests = old_budget.wire_requests
        return binding

    def guard(self, session, owner, task_id, material, binding):
        from ..tasks.service import LearningTaskRow
        scope = {"material_ids": [material.material_id]}
        if session.get(LearningTaskRow, task_id).kind == "material_parse":
            scope["sha256"] = material.source_bytes_sha256
        for snapshot in binding.get("targets", {}).values():
            row = session.get(ServiceConfigRow, snapshot["config_id"], with_for_update=True)
            self.target(session, owner, snapshot)
            if row.endpoint != snapshot["endpoint"] or row.model_name != snapshot["model_name"]:
                raise Slice1Error("disclosure_revoked", "抽题目的地已变化。", 409)
            self.attachments.disclosures.verify(task_id=task_id, config_id=row.config_id,
                actor_id=owner, content_category="material", scope_snapshot=scope,
                expected_grant_id=snapshot["grant_id"], session=session)

    def run(self, state, *, owner, lease, source, kind, guard, checkpoint, window_limit=None):
        binding = state.get("extraction_binding")
        if not binding:
            return
        guard()
        if kind == "pdf":
            add_native_pdf(state, source, guard)
        prompts = [self.prompts.resolve_for_task(lease.task_id, name) for name in PROMPTS]
        if any(p["template_sha256"] != binding["prompts"][p["prompt_name"]] for p in prompts):
            raise ValueError("question_prompt_snapshot_invalid")
        from pydantic_ai import BinaryContent
        from pydantic_ai.messages import ModelRequest, UserPromptPart

        from ..agents.runner import run_structured_agent
        from ..slice3_app import _configured_model_descriptor

        def invoke(model_cls, prompt, payload, images, validator, name):
            guard()
            target = binding["targets"]["visual" if images else "semantic"]
            with self.db.session() as session:
                view = self.target(session, owner, target)
            descriptor = _configured_model_descriptor(view, self.configs.resolve_credentials(view["config_id"]))
            descriptor.disclosure_grant_id = target["grant_id"]
            descriptor.omit_prompt_trace = True
            descriptor.timeout = 300
            descriptor.separate_connect_timeout = True
            deps = bind_task_deps(self.db, descriptor, actor_id=owner,
                task_id=lease.task_id, disclosures=self.attachments.disclosures)
            check = deps.check_state
            deps = replace(deps, check_state=lambda: (guard(), check()), omit_content_trace=True)
            history = []
            if images:
                content = []
                for page_no, image in images:
                    label = f"Source page {page_no}" if isinstance(page_no, int) else f"Source figure {page_no}"
                    content.extend([label, BinaryContent(data=image,
                        media_type="image/png" if image.startswith(b"\x89PNG") else "image/jpeg")])
                history = [ModelRequest(parts=[UserPromptPart(content=content)])]
            try:
                return run_structured_agent(descriptor, model_cls, [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                    deps=deps, max_requests=6, message_history=history,
                    output_validator=lambda _, output: validator(output),
                    agent_name=name, stage="parse", timeout_seconds=300).payload
            finally:
                descriptor.close()

        from .figure_crops import visual_crops
        material_id = self.attachments.tasks.get_task(lease.task_id)["subject_id"]
        extract_questions(state, invoke=invoke, prompts=[p["template_snapshot"] for p in prompts],
            checkpoint=checkpoint, guard=guard, source=source,
            visual=kind == "pdf" and "visual" in binding["targets"], window_limit=window_limit,
            crop_loader=lambda window, check: visual_crops(
                self.attachments.storage, material_id, window, check))
