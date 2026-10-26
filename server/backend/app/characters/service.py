"""One identity per card; immutable revisions, private drafts, explicit publication."""
import json
import uuid
from sqlalchemy import select, or_
from .models import (CharacterCardRow, CharacterRevisionRow, CharacterLibraryRow,
    CharacterAssetRow, CharacterBindingRow, CharacterPreferenceRow, utcnow)
from .schemas import Profile
from .persona import CharacterPersonaRow, read_persona, copy_persona  # noqa: F401
from ..slice1.errors import Slice1Error


def reject(code="config_not_found", status=404, *, message=None):
    raise Slice1Error(code, message or code, status)


class CharacterService:
    def __init__(self, db, configs, audit, storage):
        self.db, self.configs, self.audit, self.storage = db, configs, audit, storage
        self.teaching = None
        self.voice_catalog = None

    def _card(self, sess, card_id, owner, *, write=False):
        row = sess.get(CharacterCardRow, card_id, with_for_update=write)
        if not row or row.deleted_at or (write and row.owner_user_id != owner):
            reject()
        if row.owner_user_id != owner and row.published_revision is None:
            reject()
        return row

    def _view(self, sess, row, owner, *, revision=None, include_persona=True):
        revision = revision or (row.current_revision if owner == row.owner_user_id else row.published_revision)
        if owner != row.owner_user_id and revision != row.published_revision:
            reject()
        profile = sess.get(CharacterRevisionRow, (row.card_id, revision))
        if not profile:
            reject()
        assets = sess.scalars(select(CharacterAssetRow).where(CharacterAssetRow.card_id == row.card_id)).all()
        data = json.loads(profile.profile_json)
        if owner != row.owner_user_id:
            data["voice_config_id"] = ""
        from .models import CharacterVoiceChoiceRow
        from .voice_policy import automatic_speaking_style
        choice = sess.get(CharacterVoiceChoiceRow, (owner, row.card_id))
        if choice:
            data.update(voice_id=choice.voice_id, voice_config_id=choice.config_id, voice_selection="manual")
        data["speaking_style"] = automatic_speaking_style(data)
        from .models import CharacterMediaJobRow
        parts = {}
        for job in sess.scalars(select(CharacterMediaJobRow).where(CharacterMediaJobRow.card_id == row.card_id)):
            request = json.loads(job.request_json)
            part = request.get("image_part", "portrait")
            if request.get("replace_current") and request["scene"]=="neutral":
                result_ids=json.loads(job.result_json)
                parts.update(dict(zip(result_ids[1:],("pointing_body","upper_arm","forearm"))))
            if part != "portrait":
                parts.update({asset_id:part for asset_id in json.loads(job.result_json)})
        rig = data.get("pointing_rig") or {}
        parts.update({rig[key]:part for key,part in (
            ("upper_arm_asset_id","upper_arm"),("forearm_asset_id","forearm"),
            ("body_asset_id","pointing_body")) if rig.get(key)})
        from .defaults import system_default
        default=system_default(sess)
        builtin=bool(default and default.card_id==row.card_id and default.revision==revision)
        from .persona import publication_quality
        package=read_persona(sess,row.card_id,revision)
        return dict(card_id=row.card_id, revision=revision, published_revision=row.published_revision,system_default=builtin,
            persona=package if include_persona else None,can_publish=publication_quality(sess,row.card_id,package),
            quality_summary={key:package.get("quality",{}).get(key) for key in (
                "grade","default_eligible","warnings")} if package and package.get("quality",{}).get("schema_version")==2 else None,
            voice_choice_version=choice.version if choice else 0,
            editable=row.owner_user_id == owner, profile=data,
            collected=builtin or sess.get(CharacterLibraryRow, (owner, row.card_id)) is not None,
            assets=[dict(asset_id=a.asset_id, kind=a.kind, scene=a.scene, image_part=parts.get(a.asset_id,"portrait"), confirmed=a.confirmed,
                url=f"/api/cards/{row.card_id}/assets/{a.asset_id}") for a in assets
                if owner == row.owner_user_id or (a.confirmed and a.asset_id in data["media_ids"])])

    def list(self, owner, *, market=False):
        with self.db.session() as sess:
            query = select(CharacterCardRow).where(CharacterCardRow.deleted_at.is_(None))
            if market:
                query = query.where(CharacterCardRow.published_revision.is_not(None))
            else:
                collected = select(CharacterLibraryRow.card_id).where(CharacterLibraryRow.owner_user_id == owner)
                from .defaults import system_default
                default=system_default(sess)
                query = query.where(or_(CharacterCardRow.owner_user_id == owner, CharacterCardRow.card_id.in_(collected),
                    CharacterCardRow.card_id==default.card_id if default else False))
            rows = sess.scalars(query.order_by(CharacterCardRow.created_at.desc()).limit(100)).all()
            return [self._view(sess, row, owner, revision=row.published_revision if market else None,include_persona=False) for row in rows
                    if row.owner_user_id == owner or row.published_revision is not None]

    def get(self, owner, card_id):
        with self.db.session() as sess:
            return self._view(sess, self._card(sess, card_id, owner), owner)

    def _profile(self, sess, profile: Profile, row, owner):
        data = profile.model_dump()
        from .voice_policy import automatic_speaking_style
        data["speaking_style"] = automatic_speaking_style(data)
        if not profile.name.strip():
            reject("invalid_field", 422, message="人物名字不能为空。")
        if any(len(tag) > 40 or not tag.strip() for tag in profile.tags):
            reject("invalid_field", 422, message="人物标签不能为空，且每个标签最多 40 字。")
        if len(set(profile.media_ids)) != len(profile.media_ids):
            reject("invalid_field", 422, message="人物素材列表有重复项，请重新选择素材。")
        if profile.base_asset_id and profile.base_asset_id not in profile.media_ids:
            reject("invalid_field", 422, message="基准形象不在已选素材中，请重新选择。")
        for asset_id in profile.media_ids:
            asset = sess.get(CharacterAssetRow, asset_id)
            if not asset or asset.card_id != row.card_id or not asset.confirmed:
                reject("invalid_field", 422, message="所选人物素材未确认或已不可用，请重新选择。")
            if profile.base_asset_id == asset_id and asset.kind != "image":
                reject("invalid_field", 422, message="基准形象必须是图片。")
        if profile.pointing_rig:
            if profile.pointing_rig.body_asset_id and profile.pointing_rig.reference_asset_id != profile.base_asset_id:
                reject("invalid_field", 422, message="指向素材与当前基准形象不匹配。")
            for asset_id in (profile.pointing_rig.upper_arm_asset_id, profile.pointing_rig.forearm_asset_id,
                    *([profile.pointing_rig.body_asset_id] if profile.pointing_rig.body_asset_id else [])):
                asset = sess.get(CharacterAssetRow, asset_id)
                if asset_id not in profile.media_ids or not asset or asset.kind != "image" or not asset.confirmed:
                    reject("invalid_field", 422)
        if profile.voice_id:
            from ..slice1.models import ServiceConfigRow
            config = sess.get(ServiceConfigRow, profile.voice_config_id, with_for_update=True)
            if not config:
                reject()
            self.configs._authorize(config, owner, is_admin=False, write=False)
            if not config.is_active or config.kind != "tts":
                reject("invalid_field", 422, message="当前语音服务已不可用，请重新选择语音服务。")
        return json.dumps(data, ensure_ascii=False)

    def create(self, owner, profile):
        with self.db.session() as sess:
            row = CharacterCardRow(card_id="card_" + uuid.uuid4().hex, owner_user_id=owner, current_revision=1)
            sess.add(row)
            sess.flush()
            sess.add(CharacterRevisionRow(card_id=row.card_id, revision=1, profile_json=self._profile(sess, profile, row, owner)))
            sess.flush()
            return self._view(sess, row, owner)

    def edit(self, owner, card_id, expected, profile):
        with self.db.session() as sess:
            row = self._card(sess, card_id, owner, write=True)
            if row.current_revision != expected:
                reject("invalid_field", 409)
            serialized = self._profile(sess, profile, row, owner)
            row.current_revision += 1
            sess.add(CharacterRevisionRow(card_id=card_id, revision=row.current_revision, profile_json=serialized))
            sess.flush()
            copy_persona(sess,card_id,expected,card_id,row.current_revision)
            return self._view(sess, row, owner)

    def edit_persona(self, owner, card_id, expected, package):
        with self.db.session() as sess:
            card = self._card(sess,card_id,owner,write=True)
            if card.current_revision != expected:
                reject("state_invalid",409)
            original = sess.get(CharacterRevisionRow,(card_id,expected))
            card.current_revision += 1
            sess.add(CharacterRevisionRow(card_id=card_id,revision=card.current_revision,
                                           profile_json=original.profile_json))
            sess.flush()
            sess.add(CharacterPersonaRow(card_id=card_id,revision=card.current_revision,
                                          payload_json=package.model_dump_json()))
            self.audit.append("config.changed",actor_id=owner,object_type="character_persona",
                              object_id=card_id,result="updated",session=sess)
            sess.flush()
            return self._view(sess,card,owner)

    def publish(self, owner, card_id, expected):
        with self.db.session() as sess:
            row = self._card(sess, card_id, owner, write=True)
            if row.current_revision != expected:
                reject("invalid_field", 409)
            from .persona import publication_quality
            if not publication_quality(sess,card_id,read_persona(sess,card_id,expected)):
                self.audit.append("config.changed",actor_id=owner,object_type="character_card",
                    object_id=card_id,result="denied",reason="state_invalid")
                reject("state_invalid",409,message="人物硬性验收尚未通过，请核对身份、引文及试答；当前草稿已保留。")
            row.published_revision = expected
            self.audit.append("config.changed", actor_id=owner, object_type="character_card", object_id=card_id,
                result="published", session=sess)
            sess.flush()
            return self._view(sess, row, owner)

    def collect(self, owner, card_id, enabled=True):
        with self.db.session() as sess:
            self._card(sess, card_id, owner)
            from .defaults import system_default
            default=system_default(sess)
            if default and default.card_id==card_id:
                return {"collected":True}
            row = sess.get(CharacterLibraryRow, (owner, card_id))
            if enabled and row is None:
                sess.add(CharacterLibraryRow(owner_user_id=owner, card_id=card_id))
            elif not enabled and row:
                sess.delete(row)
        return {"collected": enabled}

    def delete(self, owner, card_id):
        from .models import CharacterMediaJobRow
        from ..tasks.service import LearningTaskRow
        from ..materials.models import DeletionTombstoneRow
        from .distillation_models import CharacterBuildRow, CharacterBuildStageRow
        with self.db.session() as sess:
            row = self._card(sess, card_id, owner, write=True)
            row.deleted_at = utcnow()
            sess.add(DeletionTombstoneRow(
                tombstone_id="tmb_" + uuid.uuid4().hex[:12], object_type="character_card",
                object_id=card_id, owner_user_id=owner, deleted_at=row.deleted_at,
                cleanup_stage="pending",
            ))
            for preference in sess.scalars(select(CharacterPreferenceRow).where(
                    CharacterPreferenceRow.card_id == card_id)):
                preference.card_id = None
                preference.revision = None
                preference.version += 1
                preference.updated_at = utcnow()
            jobs = sess.scalars(select(CharacterMediaJobRow).where(CharacterMediaJobRow.card_id == card_id)).all()
            for job in jobs:
                task = sess.get(LearningTaskRow, job.task_id, with_for_update=True)
                if task:
                    task.cancel_requested = True
            builds = list(sess.scalars(select(CharacterBuildRow.build_id).where(CharacterBuildRow.card_id == card_id)))
            task_ids = list(sess.scalars(select(CharacterBuildRow.task_id).where(CharacterBuildRow.build_id.in_(builds))))
            task_ids.extend(sess.scalars(select(CharacterBuildStageRow.task_id).where(CharacterBuildStageRow.build_id.in_(builds))))
            for task in sess.scalars(select(LearningTaskRow).where(LearningTaskRow.task_id.in_(task_ids)).with_for_update()):
                task.cancel_requested = True
            self.audit.append("resource.delete.requested", actor_id=owner, object_type="character_card", object_id=card_id,
                result="deleted", session=sess)
        return {"deleted": True}

    def preference(self, owner, conversation_id=None):
        with self.db.session() as sess:
            row = sess.get(CharacterPreferenceRow, owner)
            from .defaults import effective_preference
            effective,builtin=effective_preference(sess,owner)
            result = {"version": row.version if row else 0,
                "card_id": effective.card_id if effective else None,
                "revision": effective.revision if effective else None}
            if effective and builtin:result["using_system_default"]=True
            if conversation_id:
                from ..planning.models import ConversationRow
                from .models import ClassroomPlaybackRow
                conv = sess.get(ConversationRow, conversation_id)
                if not conv or conv.owner_user_id != owner or conv.deleted_at:
                    reject()
                binding = sess.get(CharacterBindingRow, conversation_id)
                playback = sess.get(ClassroomPlaybackRow, conversation_id)
                result["classroom"] = {"card_id":binding.card_id if binding else None,
                    "revision":binding.revision if binding else None,
                    "voice_id":json.loads(binding.profile_json).get("voice_id", "") if binding else "",
                    "version":playback.version if playback else 0}
            return result

    def use(self, owner, value):
        try:
            return self._use(owner, value)
        except Slice1Error as exc:
            self.audit.append("config.changed", actor_id=owner, object_type="character_preference",
                object_id=owner, result="denied", reason=exc.code)
            raise

    def prepare_profile(self, owner, card_id, revision=None):
        with self.db.session() as sess:
            card = self._card(sess, card_id, owner)
            view = self._view(sess, card, owner, revision=revision)
            profile = dict(view["profile"])
            if view.get("persona"):
                profile["_persona"] = view["persona"]
        voice = self.configs.latest_active_for_owner(owner, "tts")
        if voice and self.voice_catalog:
            from .voice_policy import resolve_voice
            selected = resolve_voice(profile, self.voice_catalog(owner, voice["config_id"]), profile.get("voice_id", ""))
            if not selected:
                reject("capability_test_failed", 409)
            profile.update(voice_id=selected, voice_config_id=voice["config_id"])
        elif profile.get("voice_id") and (not voice or profile.get("voice_config_id") != voice["config_id"]):
            reject("disclosure_required", 409)
        return view["revision"], profile

    def choose_voice(self, owner, card_id, value):
        from .models import CharacterVoiceChoiceRow
        from sqlalchemy.dialects.postgresql import insert
        with self.db.session() as sess:
            self._card(sess, card_id, owner)
        if not self.voice_catalog or not any(voice["voice_id"] == value.voice_id
                for voice in self.voice_catalog(owner, value.config_id)):
            reject("invalid_field", 422)
        with self.db.session() as sess:
            card = self._card(sess, card_id, owner)
            view = self._view(sess, card, owner)
            if view["revision"] != value.revision:
                reject("state_invalid", 409)
            sess.execute(insert(CharacterVoiceChoiceRow).values(owner_user_id=owner, card_id=card_id,
                version=0, voice_id=value.voice_id, config_id=value.config_id).on_conflict_do_nothing())
            row = sess.get(CharacterVoiceChoiceRow, (owner, card_id), with_for_update=True)
            if row.version != value.expected_version:
                reject("state_invalid", 409)
            row.voice_id, row.config_id = value.voice_id, value.config_id
            row.version += 1
            self.audit.append("config.changed", actor_id=owner, object_type="character_voice",
                object_id=card_id, result="selected", session=sess)
            sess.flush()
            return self._view(sess, card, owner)

    def _use(self, owner, value):
        from sqlalchemy.dialects.postgresql import insert
        from ..planning.models import ConversationRow, CourseRow
        selected_card_id,selected_revision=value.card_id,value.revision
        if not selected_card_id:
            from .defaults import system_default
            with self.db.session() as sess:
                default=system_default(sess)
                if default:selected_card_id,selected_revision=default.card_id,default.revision
        prepared_profile = self.prepare_profile(owner, selected_card_id, selected_revision) if selected_card_id else None
        with self.db.session() as sess:
            sess.execute(insert(CharacterPreferenceRow).values(owner_user_id=owner,
                version=0, updated_at=utcnow()).on_conflict_do_nothing())
            preference = sess.get(CharacterPreferenceRow, owner, with_for_update=True)
            if preference.version != value.expected_version:
                reject("state_invalid", 409)
            view = None
            if selected_card_id:
                card = self._card(sess, selected_card_id, owner)
                view = self._view(sess, card, owner, revision=selected_revision)
                if prepared_profile[0] != view["revision"]:
                    reject("state_invalid", 409)
                from .defaults import system_default
                default=system_default(sess)
                if card.owner_user_id != owner and not sess.get(CharacterLibraryRow, (owner, card.card_id)) and not (
                    default and default.card_id==card.card_id and default.revision==selected_revision):
                    reject()
            effective = None
            if value.conversation_id:
                conv = sess.get(ConversationRow, value.conversation_id, with_for_update=True)
                course = sess.get(CourseRow, conv.course_id) if conv else None
                if (not conv or conv.owner_user_id != owner or conv.deleted_at or conv.kind != "teaching"
                        or not course or course.deleted_at or course.current_outline_version_id != conv.outline_version_id):
                    reject()
                if self.teaching:
                    from .classroom import switch_mentor
                    effective = switch_mentor(self.teaching, sess, conv, view, prepared_profile[1] if prepared_profile else None)
                else:
                    effective = self._legacy_binding(sess, conv, view, owner, card if view else None)
            preference.card_id = view["card_id"] if view and value.card_id else None
            preference.revision = view["revision"] if view and value.card_id else None
            preference.version += 1
            preference.updated_at = utcnow()
            self.audit.append("config.changed", actor_id=owner, object_type="character_preference",
                object_id=owner, result="selected" if view else "default", session=sess)
            result = {"version": preference.version, "card_id": preference.card_id,
                "revision": preference.revision, "effective_step_no": effective}
        return result

    def _legacy_binding(self, sess, conv, view, owner, card):
                from ..teaching.models import TeachingStepRow
                from ..tasks.service import LearningTaskRow
                from .models import CharacterSegmentRow
                protected = set(sess.scalars(select(TeachingStepRow.step_no).where(
                    TeachingStepRow.conversation_id == conv.conversation_id, TeachingStepRow.step_no > 0)))
                protected.update(sess.scalars(select(CharacterSegmentRow.step_no).where(
                    CharacterSegmentRow.conversation_id == conv.conversation_id)))
                for task in sess.scalars(select(LearningTaskRow).where(
                        LearningTaskRow.owner_user_id == owner,
                        LearningTaskRow.kind == "lesson_step_generate",
                        LearningTaskRow.subject_id.like(conv.conversation_id + ":%"),
                        LearningTaskRow.status.in_(("queued", "leased", "running", "succeeded")))):
                    parts = task.subject_id.split(":")
                    if len(parts) == 3 and parts[1].isdigit():
                        protected.add(int(parts[1]))
                for number in protected:
                    segment_character(sess, conv.conversation_id, number, freeze=True)
                effective = max(protected, default=0) + 1
                if effective > 24:
                    reject("state_invalid", 409)
                binding = sess.get(CharacterBindingRow, conv.conversation_id)
                if view:
                    profile = dict(view["profile"])
                    voice = self.configs.latest_active_for_owner(owner, "tts")
                    if profile.get("voice_id"):
                        if not voice:
                            reject("disclosure_required", 409)
                        if card.owner_user_id != owner:
                            profile["voice_config_id"] = voice["config_id"]
                        if profile.get("voice_config_id") != voice["config_id"]:
                            reject("disclosure_required", 409)
                    if binding is None:
                        binding = CharacterBindingRow(conversation_id=conv.conversation_id)
                        sess.add(binding)
                    binding.card_id, binding.revision = view["card_id"], view["revision"]
                    binding.effective_step_no, binding.profile_json = effective, json.dumps(profile, ensure_ascii=False)
                elif binding:
                    sess.delete(binding)
                return effective

    def copy(self, owner, card_id):
        keys = []
        try:
            with self.db.session() as sess:
                source = self._card(sess, card_id, owner)
                original = self._view(sess, source, owner)
                if original["editable"]:
                    reject("invalid_field", 409)
                row = CharacterCardRow(card_id="card_"+uuid.uuid4().hex, owner_user_id=owner, current_revision=1)
                sess.add(row)
                sess.flush()
                mapping = {}
                for asset in sess.scalars(select(CharacterAssetRow).where(
                        CharacterAssetRow.card_id == card_id, CharacterAssetRow.confirmed.is_(True))):
                    if asset.asset_id not in original["profile"]["media_ids"]:
                        continue
                    ident = "asset_"+uuid.uuid4().hex
                    key = f"characters/{row.card_id}/{ident}.bin"
                    self.storage.put(key, self.storage.get(asset.storage_key))
                    keys.append(key)
                    mapping[asset.asset_id] = ident
                    sess.add(CharacterAssetRow(asset_id=ident, card_id=row.card_id, owner_user_id=owner,
                        source_revision=1, kind=asset.kind, scene=asset.scene, storage_key=key,
                        mime_type=asset.mime_type, confirmed=True))
                sess.flush()
                data = {**original["profile"], "voice_id": "", "voice_config_id": "",
                    "voice_selection": "auto",
                    "media_ids": list(mapping.values()), "base_asset_id": mapping.get(
                        original["profile"]["base_asset_id"], ""), "pointing_rig": None}
                rig = original["profile"].get("pointing_rig")
                if rig and all(rig.get(key) in mapping for key in ("upper_arm_asset_id", "forearm_asset_id")):
                    data["pointing_rig"] = {**rig, **{key: mapping[rig[key]] for key in
                        ("upper_arm_asset_id", "forearm_asset_id", "body_asset_id", "reference_asset_id")
                        if rig.get(key) in mapping}}
                sess.add(CharacterRevisionRow(card_id=row.card_id, revision=1,
                    profile_json=self._profile(sess, Profile.model_validate(data), row, owner)))
                sess.flush()
                copy_persona(sess,card_id,original["revision"],row.card_id,1)
                self.audit.append("config.changed", actor_id=owner, object_type="character_card",
                    object_id=row.card_id, result="copied", session=sess)
                sess.flush()
                return self._view(sess, row, owner)
        except Exception:
            for key in keys:
                self.storage.delete(key)
            raise

    def asset(self, owner, card_id, asset_id):
        with self.db.session() as sess:
            row = self._card(sess, card_id, owner)
            asset = sess.get(CharacterAssetRow, asset_id)
            if not asset or asset.card_id != card_id:
                reject()
            view = self._view(sess, row, owner)
            if not any(item["asset_id"] == asset_id for item in view["assets"]):
                reject()
            return self.storage.get(asset.storage_key), asset.mime_type

    def segment_assets(self, owner, conversation_id, step_no, asset_id=None):
        from ..planning.models import ConversationRow
        with self.db.session() as sess:
            conversation = sess.get(ConversationRow, conversation_id)
            if not conversation or conversation.deleted_at or conversation.owner_user_id != owner:
                reject()
            character = segment_character(sess, conversation_id, step_no)
            if not character:
                reject()
            ids = character["profile"]["media_ids"]
            assets = sess.scalars(select(CharacterAssetRow).where(
                CharacterAssetRow.card_id == character["card_id"],
                CharacterAssetRow.asset_id.in_(ids), CharacterAssetRow.confirmed.is_(True))).all()
            if asset_id:
                asset = next((a for a in assets if a.asset_id == asset_id), None)
                if not asset:
                    reject()
                return self.storage.get(asset.storage_key), asset.mime_type
            return [dict(asset_id=a.asset_id, kind=a.kind, scene=a.scene,
                url=f"/api/teaching/{conversation_id}/character-assets/{step_no}/{a.asset_id}") for a in assets]

    def add_asset(self, owner, card_id, revision, payload, kind, mime, scene="neutral", *, confirmed=False):
        asset_id = "asset_" + uuid.uuid4().hex
        key = f"characters/{card_id}/{asset_id}.bin"
        self.storage.put(key, payload)
        try:
            with self.db.session() as sess:
                card = self._card(sess, card_id, owner, write=True)
                if card.current_revision != revision:
                    reject("invalid_field", 409)
                sess.add(CharacterAssetRow(asset_id=asset_id, card_id=card_id, owner_user_id=owner,
                    source_revision=revision, kind=kind, scene=scene, storage_key=key, mime_type=mime, confirmed=confirmed))
            return asset_id
        except Exception:
            self.storage.delete(key)
            raise

    def confirm_asset(self, owner, card_id, asset_id):
        with self.db.session() as sess:
            self._card(sess, card_id, owner, write=True)
            asset = sess.get(CharacterAssetRow, asset_id, with_for_update=True)
            if not asset or asset.card_id != card_id:
                reject()
            asset.confirmed = True
        return {"confirmed": True}

    def bind(self, owner, conversation_id, card_id, revision, effective_step_no):
        from ..planning.models import ConversationRow
        from ..teaching.models import TeachingStepRow
        with self.db.session() as sess:
            conversation = sess.get(ConversationRow, conversation_id, with_for_update=True)
            if not conversation or conversation.deleted_at or conversation.owner_user_id != owner:
                reject()
            row = self._card(sess, card_id, owner)
            view = self._view(sess, row, owner, revision=revision)
            if view["profile"]["voice_id"]:
                voice = self.configs.latest_active_for_owner(owner, "tts")
                if row.owner_user_id != owner and voice and voice["protocol"] == "minimax":
                    view["profile"]["voice_config_id"] = voice["config_id"]
                if not voice or voice["config_id"] != view["profile"]["voice_config_id"]:
                    reject("disclosure_required", 409)
            # Already generated segments keep their previous text/audio/identity.
            latest = sess.scalar(select(TeachingStepRow.step_no).where(TeachingStepRow.conversation_id == conversation_id)
                .order_by(TeachingStepRow.step_no.desc()).limit(1)) or 0
            if effective_step_no <= latest or effective_step_no > 24:
                reject("invalid_field", 409)
            binding = sess.get(CharacterBindingRow, conversation_id)
            if binding is None:
                binding = CharacterBindingRow(conversation_id=conversation_id)
                sess.add(binding)
            binding.card_id, binding.revision, binding.effective_step_no = card_id, revision, effective_step_no
            binding.profile_json = json.dumps(view["profile"], ensure_ascii=False)
        return {"card_id": card_id, "revision": revision, "effective_step_no": effective_step_no}


def segment_character(sess, conversation_id, step_no, *, freeze=False):
    from .models import CharacterSegmentRow
    saved = sess.get(CharacterSegmentRow, (conversation_id, step_no))
    if saved:
        card = sess.get(CharacterCardRow, saved.card_id)
        if not card or card.deleted_at:
            return None
        return dict(card_id=saved.card_id, revision=saved.revision, profile=json.loads(saved.profile_json))
    binding = sess.get(CharacterBindingRow, conversation_id)
    if not binding or step_no < binding.effective_step_no:
        return None
    card = sess.get(CharacterCardRow, binding.card_id)
    if not card or card.deleted_at:
        return None
    if freeze:
        sess.add(CharacterSegmentRow(conversation_id=conversation_id, step_no=step_no, card_id=binding.card_id,
            revision=binding.revision, profile_json=binding.profile_json, speech_fingerprint=""))
        sess.flush()
    return dict(card_id=binding.card_id, revision=binding.revision, profile=json.loads(binding.profile_json))


def default_mentor_snapshot(sess, owner):
    from .defaults import effective_preference
    preference,builtin = effective_preference(sess, owner)
    if not preference or not preference.card_id:
        return
    card = sess.get(CharacterCardRow, preference.card_id)
    if not card or card.deleted_at:
        return
    if not builtin and card.owner_user_id != owner and (
            not card.published_revision or not sess.get(CharacterLibraryRow, (owner, card.card_id))):
        return
    revision = sess.get(CharacterRevisionRow, (card.card_id, preference.revision))
    if revision:
        profile = json.loads(revision.profile_json)
        package = read_persona(sess,card.card_id,preference.revision)
        if package:
            profile["_persona"] = package
        from .models import CharacterVoiceChoiceRow
        from .voice_policy import automatic_speaking_style
        choice = sess.get(CharacterVoiceChoiceRow, (owner, card.card_id))
        profile["speaking_style"] = automatic_speaking_style(profile)
        if choice:
            profile.update(voice_id=choice.voice_id, voice_config_id=choice.config_id, voice_selection="manual")
        if card.owner_user_id != owner:
            from ..slice1.models import ServiceConfigRow
            voice = sess.scalar(select(ServiceConfigRow).where(
                ServiceConfigRow.owner_user_id == owner,
                ServiceConfigRow.kind == "tts", ServiceConfigRow.is_active.is_(True))
                .order_by(ServiceConfigRow.created_at.desc()))
            profile["voice_config_id"] = voice.config_id if voice else ""
        return {"card_id":card.card_id,"revision":preference.revision,"profile":profile}


def apply_default_mentor(sess, conversation):
    snapshot=default_mentor_snapshot(sess,conversation.owner_user_id)
    if snapshot:
        sess.add(CharacterBindingRow(conversation_id=conversation.conversation_id, card_id=snapshot["card_id"],
            revision=snapshot["revision"], effective_step_no=1,
            profile_json=json.dumps(snapshot["profile"], ensure_ascii=False)))
