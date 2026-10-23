"""Personal role bindings and immutable task destination snapshots."""
from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from ..logging_audit.models import Base
from ..slice1.errors import error, Slice1Error

ROLES = ("planning", "teaching", "dialogue", "research", "research_search", "video_understanding")


class ModelRoleRow(Base):
    __tablename__ = "model_role_bindings"
    owner_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(24), primary_key=True)
    config_id: Mapped[str | None] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer, default=0)


class TaskModelRoleRow(Base):
    __tablename__ = "task_model_roles"
    task_id: Mapped[str] = mapped_column(ForeignKey("learning_tasks.task_id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(24), primary_key=True)
    config_id: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[int] = mapped_column(Integer)
    grant_id: Mapped[str | None] = mapped_column(String(64))


def role_for_task(task):
    if task.kind == "chat_answer":
        return "dialogue"
    if task.kind.startswith("character_build"):
        return "research"
    if task.kind in ("lesson_step_generate", "lesson_first_prepare", "feynman", "review_generate", "review_grade"):
        return "teaching"
    return "planning"


def role_config(configs, owner, role):
    from ..slice1.models import ServiceConfigRow
    if role not in ROLES:
        raise error("invalid_field")
    if not hasattr(configs, "db"):
        return configs.latest_active_for_owner(owner, "content_model") if role not in ("research","research_search","video_understanding") else None
    with configs.db.session() as sess:
        row = sess.get(ModelRoleRow, (owner, role))
        if row is not None:
            ident = row.config_id
            if not ident:
                return None
            config=sess.get(ServiceConfigRow,ident)
            if (not config or not config.is_active or config.kind!="content_model"
                or config.owner_scope!="user" or config.owner_user_id!=owner):
                return None
            # Metadata lookup must not take ConfigService.get's write lock:
            # lesson consent may already hold this configuration in its transaction.
            return configs.public_view(config)
        else:
            ident = None
    if not ident:
        return configs.latest_active_for_owner(owner, "content_model") if role not in ("research","research_search","video_understanding") else None


def role_views(configs, owner):
    result = []
    for role in ROLES:
        with configs.db.session() as sess:
            row = sess.get(ModelRoleRow, (owner, role))
            revision = row.revision if row else 0
        view = role_config(configs, owner, role)
        result.append({"role": role, "revision": revision,
                       "config_id": view["config_id"] if view else None, "configuration": view})
    return result


def set_role(configs, owner, role, config_id, expected):
    from sqlalchemy import select, func
    from ..slice1.models import ServiceConfigRow
    if role not in ROLES:
        raise error("invalid_field")
    with configs.db.session() as sess:
        sess.execute(select(func.pg_advisory_xact_lock(func.hashtextextended("model-roles:" + owner, 0))))
        config = sess.get(ServiceConfigRow, config_id) if config_id else None
        if config_id and (not config or config.owner_scope != "user"
                or config.owner_user_id != owner or config.kind != "content_model" or not config.is_active):
            raise error("config_not_found")
        if role=="research_search" and config and config.protocol not in ("openai","openai_responses"):
            raise error("invalid_field","研究搜索需要支持 Responses web_search 的 OpenAI 兼容服务。")
        from sqlalchemy.dialects.postgresql import insert
        sess.execute(insert(ModelRoleRow).values(owner_user_id=owner, role=role, revision=0)
                     .on_conflict_do_nothing())
        row = sess.get(ModelRoleRow, (owner, role), with_for_update=True)
        if row.revision != expected:
            raise Slice1Error("state_invalid", "role binding changed", 409)
        row.config_id, row.revision = config_id, row.revision + 1
        configs.audit.append("config.changed", actor_id=owner, object_type="model_role",
                            object_id=role, result="selected", session=sess)
    return role_views(configs, owner)


def save_role_configuration(configs, owner, role, body):
    try:
        return _save_role_configuration(configs, owner, role, body)
    except Slice1Error as exc:
        configs.audit.append("config.changed", actor_id=owner, object_type="model_role",
            object_id=role if role in ROLES else "unknown", result="denied", reason=exc.code)
        raise


def _save_role_configuration(configs, owner, role, body):
    """Copy on write keeps other roles and already authorized tasks unchanged."""
    from sqlalchemy import select, func
    from sqlalchemy.dialects.postgresql import insert
    from ..slice1.models import ServiceConfigRow
    if role not in ROLES:
        raise error("invalid_field")
    if role=="research_search" and body.protocol not in ("openai","openai_responses"):
        raise error("invalid_field","研究搜索需要支持 Responses web_search 的 OpenAI 兼容服务。")
    with configs.db.session() as sess:
        sess.execute(select(func.pg_advisory_xact_lock(func.hashtextextended("model-roles:" + owner, 0))))
        binding = sess.get(ModelRoleRow, (owner, role), with_for_update=True)
        if (binding.revision if binding else 0) != body.expected_revision:
            raise Slice1Error("state_invalid", "role configuration changed", 409)
        current = role_config(configs, owner, role)
        if (current["config_id"] if current else None) != body.config_id:
            raise Slice1Error("state_invalid", "role configuration changed", 409)
        source = sess.get(ServiceConfigRow, body.config_id, with_for_update=True) if body.config_id else None
        if source and (not source.is_active or source.owner_user_id != owner
                or source.owner_scope != "user" or source.kind != "content_model"
                or source.config_version != body.config_version):
            raise Slice1Error("state_invalid", "role configuration changed", 409)
        key = body.api_key.get_secret_value().strip() if body.api_key else ""
        if not key and source:
            key = configs._decrypt(source.encrypted_credentials).get("api_key", "")
        if not key:
            raise error("credential_invalid")
        # Freeze legacy fallback choices before creating a newer configuration.
        for other in ROLES:
            previous = role_config(configs, owner, other)
            sess.execute(insert(ModelRoleRow).values(owner_user_id=owner, role=other,
                config_id=previous["config_id"] if previous else None, revision=0)
                .on_conflict_do_nothing())
        view = configs.create(actor_id=owner, is_admin=False, kind="content_model",
            protocol=body.protocol, endpoint=body.endpoint.strip(), model_name=body.model_name.strip(),
            credentials={"api_key": key}, reasoning_settings=body.reasoning_settings.model_dump(exclude_none=True),
            session=sess)
        binding = sess.get(ModelRoleRow, (owner, role), with_for_update=True, populate_existing=True)
        binding.config_id, binding.revision = view["config_id"], binding.revision + 1
        configs.audit.append("config.changed", actor_id=owner, object_type="model_role",
            object_id=role, result="selected", session=sess)
    configs._notify(view["config_id"], owner, "created")
    return role_views(configs, owner)


def reveal_content_key(configs, owner, config_id):
    from ..slice1.models import ServiceConfigRow
    try:
        with configs.db.session() as sess:
            row = sess.get(ServiceConfigRow, config_id, with_for_update=True)
            if (not row or not row.is_active or row.kind != "content_model"
                    or row.owner_scope != "user" or row.owner_user_id != owner):
                raise error("config_not_found")
            key = configs._decrypt(row.encrypted_credentials).get("api_key", "")
            configs.audit.append("config.changed", actor_id=owner, object_type="model_credential",
                object_id=config_id, result="revealed", session=sess)
            return key
    except Slice1Error:
        configs.audit.append("config.changed", actor_id=owner, object_type="model_credential",
            object_id=config_id, result="denied", reason="config_not_found")
        raise


def pin_role(sess, task_id, role, view, grant_id=None, *, rebind=False):
    row = sess.get(TaskModelRoleRow, (task_id, role))
    if row:
        if rebind:
            row.config_id,row.config_version,row.grant_id=view["config_id"],view["config_version"],grant_id
        elif (row.config_id, row.config_version) != (view["config_id"], view["config_version"]):
            raise Slice1Error("disclosure_revoked", "task model changed", 409)
        return row
    row = TaskModelRoleRow(task_id=task_id, role=role, config_id=view["config_id"],
                          config_version=view["config_version"], grant_id=grant_id)
    sess.add(row)
    sess.flush()
    return row


def task_role_config(configs, owner, task_id):
    from ..tasks.service import LearningTaskRow
    with configs.db.session() as sess:
        task = sess.get(LearningTaskRow, task_id)
        if not task or task.owner_user_id != owner:
            raise Slice1Error("task_not_found", "task unavailable", 404)
        role = role_for_task(task)
        pinned = sess.get(TaskModelRoleRow, (task_id, role))
        if pinned is None and role == "dialogue":
            # Chats started before the dialogue role retain their old snapshot.
            pinned = sess.get(TaskModelRoleRow, (task_id, "planning"))
        if pinned:
            ident, version, grant = pinned.config_id, pinned.config_version, pinned.grant_id
        else:
            explicit=sess.get(ModelRoleRow,(owner,role)) is not None
            view=role_config(configs, owner, role)
            if view is None and (explicit or role=="research"):
                raise Slice1Error("config_not_found","selected role configuration unavailable",409)
            if view:
                pin_role(sess,task_id,role,view)
            return view
    view = configs.get(ident, actor_id=owner, is_admin=False)
    if not view["is_active"] or view["config_version"] != version:
        raise Slice1Error("disclosure_revoked", "task model configuration changed", 409)
    return {**view, "_planning_grant_id": grant}
