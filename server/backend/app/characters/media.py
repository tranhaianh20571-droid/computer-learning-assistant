"""Private prepared media: bounded decode, no crop, no RGB flattening."""
import base64
import hashlib
import io
import json
import uuid
from time import monotonic
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, Thread

import httpx
from PIL import Image, ImageChops, ImageDraw, ImageFilter
from sqlalchemy import select

from .models import CharacterCardRow, CharacterMediaJobRow, CharacterRevisionRow, CharacterAssetRow
from .service import reject
from ..slice1.errors import AdapterError, Slice1Error
from ..slice1.adapters import build_adapter
from ..slice1.adapters.base import validate_endpoint
from ..tasks.service import LearningTaskRow, LeaseError, utcnow

IMAGE_REQUEST_TIMEOUT_SECONDS = 300
IMAGE_LEASE_SECONDS = IMAGE_REQUEST_TIMEOUT_SECONDS + 60


def isolated_pointing_part(image):
    """Keep one horizontal cutout when the provider also repeats the reference."""
    alpha=image.getchannel("A")
    mask=alpha.point(lambda value:255 if value>=192 else 0)
    mask.thumbnail((512,512),Image.Resampling.NEAREST)
    candidates=[]
    for _ in range(128):
        seed_index=next((index for index,value in enumerate(mask.tobytes()) if value),None)
        if seed_index is None:
            break
        seed=(seed_index%mask.width,seed_index//mask.width)
        ImageDraw.floodfill(mask,seed,128)
        component=mask.point(lambda value:255 if value==128 else 0)
        bounds=component.getbbox()
        area=component.histogram()[255]
        ImageDraw.floodfill(mask,seed,0)
        if area>=16 and bounds[2]-bounds[0]>=1.4*(bounds[3]-bounds[1]):
            candidates.append(component)
    else:
        raise AdapterError("invalid_response")
    if len(candidates)!=1:
        raise AdapterError("invalid_response")
    selected=candidates[0].resize(image.size,Image.Resampling.NEAREST).filter(ImageFilter.MaxFilter(3))
    output=image.copy()
    output.putalpha(ImageChops.multiply(alpha,selected))
    return output.crop(output.getchannel("A").getbbox())


def validate_generated_cutout(payload):
    with Image.open(io.BytesIO(payload)) as image:
        alpha=image.getchannel("A")
        if alpha.getextrema()[0]!=0 or alpha.getextrema()[1]<192:
            raise AdapterError("invalid_response")
        if not alpha.point(lambda value:255 if value>=192 else 0).getbbox():
            raise AdapterError("invalid_response")


def normalize_media(payload, kind):
    if not payload:
        reject("invalid_field", 422)
    if kind == "image":
        try:
            with Image.open(io.BytesIO(payload)) as image:
                if image.width * image.height > 16000000 or image.width < 64 or image.height < 64:
                    reject("invalid_field", 422)
                image.load()
                output = io.BytesIO()
                image.convert("RGBA").save(output, format="PNG")
                return output.getvalue(), "image/png"
        except (OSError, ValueError, Image.DecompressionBombError):
            reject("invalid_field", 422)
    if kind == "video" and len(payload) >= 24 and payload[4:8] == b"ftyp":
        import tempfile
        from pathlib import Path
        import imageio_ffmpeg
        with tempfile.TemporaryDirectory(prefix="character-video-") as directory:
            path = Path(directory) / "sample.mp4"
            path.write_bytes(payload)
            try:
                reader = imageio_ffmpeg.read_frames(str(path), output_params=["-vf", "scale=2:2", "-frames:v", "1"])
                try:
                    metadata = next(reader)
                    duration = float(metadata["duration"])
                    width, height = metadata["source_size"]
                    if not 0 < duration <= 20 or width * height > 16000000:
                        raise ValueError()
                    if len(next(reader)) != 12:
                        raise ValueError()
                finally:
                    reader.close()
            except (RuntimeError, OSError, StopIteration, ValueError, KeyError):
                reject("invalid_field", 422)
        return payload, "video/mp4"
    reject("invalid_field", 422)


def download(url, limit):
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise AdapterError("invalid_endpoint", status=400)
    validate_endpoint(url, require_https=True, allow_private=False)
    import socket
    from ..net_policy import addresses_allowed, is_literal_address
    target=httpx.URL(url)
    infos=socket.getaddrinfo(target.host,target.port or 443,0,socket.SOCK_STREAM)
    addresses=[item[4][0] for item in infos]
    if not addresses_allowed(addresses, from_hostname=not is_literal_address(target.host)):
        raise AdapterError("invalid_endpoint",status=400)
    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
        with client.stream("GET", target.copy_with(host=addresses[0]), headers={"Host":target.netloc.decode()},extensions={"sni_hostname":target.host}) as response:
            if response.status_code != 200:
                raise AdapterError("capability_test_failed")
            parts, size = [], 0
            for part in response.iter_bytes():
                size += len(part)
                if size > limit:
                    raise AdapterError("response_too_large")
                parts.append(part)
            return b"".join(parts)


class CharacterMediaService:
    def __init__(self, cards, tasks, disclosures):
        self.cards, self.db, self.configs = cards, cards.db, cards.configs
        self.tasks, self.disclosures = tasks, disclosures
        self.adapter_factory = build_adapter
        self.stop, self.wake, self.lock = Event(), Event(), Lock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="character-media")
        self.thread = None
        self.future = None
        self.voice_cache = {}
        self.voice_lock = Lock()
        cards.voice_catalog = self._voice_catalog

    def preview_voice(self, owner, request):
        from .voice_policy import resolve_voice
        from .expression import derive_expression
        from .speech import compile_speech
        profile = request.profile.model_dump()
        config = self.configs.get(request.config_id, actor_id=owner, is_admin=False)
        voices = self._voice_catalog(owner, request.config_id)
        voice = resolve_voice(profile, voices, profile.get("voice_id", ""))
        if not voice:
            reject("capability_test_failed", 409)
        fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        scope = {"request_hash": fingerprint}
        task = self.tasks.create_task(owner_user_id=owner, kind="tts", subject_id="voice-preview:"+uuid.uuid4().hex,
            idempotency_key="voice-preview:"+uuid.uuid4().hex, stage="tts", lane="maintenance",
            deadline_seconds=90, model_config_version=f"{config['config_id']}:{config['config_version']}")
        self.disclosures.create(actor_id=owner, task_id=task["task_id"], config_id=request.config_id,
            content_category="character_media", scope_snapshot=scope, config_version=config["config_version"])
        lease = self.tasks.claim(task["task_id"], lease_owner="character-preview", lease_seconds=90)
        adapter = None
        try:
            def authorize():
                self.tasks.validate_lease(lease, actor_id=owner)
                current = self.configs.get(request.config_id, actor_id=owner, is_admin=False)
                if not current["is_active"] or current["config_version"] != config["config_version"]:
                    reject("config_not_found", 409)
                self.disclosures.verify_or_audit(task_id=task["task_id"], config_id=request.config_id,
                    content_category="character_media", actor_id=owner, scope_snapshot=scope)
            authorize()
            adapter = self.adapter_factory("minimax", endpoint=config["endpoint"], model_name=config["model_name"],
                credentials=self.configs.resolve_credentials(request.config_id), timeout=45, max_response_bytes=8*1024*1024)
            style = derive_expression(profile)
            sample = ("嗯嗯，我们先看这一小步，好不好？把输入和输出分清，就抓住重点啦。"
                if style.liveliness > .6 else "嗯，我们先看这一小步。把条件和结论分开，再一步一步验证。")
            plan = compile_speech(sample, profile, config["model_name"], voice, split=False)
            result = adapter.synthesize(plan.speech_text, voice_id=voice, emotion=plan.emotion,
                speed=plan.speed, volume=plan.volume, pitch=plan.pitch, voice_modify=dict(plan.voice_modify) or None)
            payload = bytes.fromhex(result.get("audio_hex", ""))
            if not payload or len(payload) > 4*1024*1024:
                raise AdapterError("capability_test_failed")
            authorize()
            self.tasks.submit_result(lease, stage="tts", status="succeeded")
            return payload
        except (AdapterError, ValueError) as exc:
            self.tasks.submit_result(lease, stage="tts", status="failed", error_code="OUTPUT_INVALID")
            code = getattr(exc, "code", "capability_test_failed")
            if code not in ("credential_invalid", "rate_limited", "capability_test_failed"):
                code = "capability_test_failed"
            reject(code, 502)
        except Exception:
            self.tasks.submit_result(lease, stage="tts", status="failed", error_code="OUTPUT_INVALID")
            raise
        finally:
            if adapter:
                adapter.close()

    def create(self, owner, card_id, request, key, *, creation=None):
        if not key or len(key) > 120:
            reject("invalid_field", 422)
        excluded = ({"image_part"} if request.image_part == "portrait" else set()) | (
            {"replace_current"} if not request.replace_current else set())
        serialized = request.model_dump_json(exclude=excluded)
        if creation is not None:
            from .creation_delivery import guard_media_creation
            guard_media_creation(self,owner,card_id,request.revision,creation)
            serialized=json.dumps({**json.loads(serialized),"creation":creation},ensure_ascii=False)
        digest = hashlib.sha256((card_id + serialized).encode()).hexdigest()
        with self.db.session() as sess:
            card = self.cards._card(sess, card_id, owner, write=True)
            if card.current_revision != request.revision:
                reject("invalid_field", 409)
            config = self.configs.get(request.config_id, actor_id=owner, is_admin=False)
            expected = {"image": "image_generation", "video": "video_generation", "voice": "tts"}[request.kind]
            if not config["is_active"] or config["kind"] != expected or config["config_version"] != request.config_version:
                reject("config_not_found", 409)
            if request.kind != "image" and request.count != 1:
                reject("invalid_field", 422)
            if request.replace_current and (request.kind != "image" or request.count != 1 or request.image_part != "portrait"):
                reject("invalid_field", 422)
            if request.image_part != "portrait":
                reject("invalid_field", 422, message="指向使用独立教鞭，不再生成身体或手臂部件。")
            active = sess.scalars(select(CharacterMediaJobRow).join(LearningTaskRow,
                CharacterMediaJobRow.task_id == LearningTaskRow.task_id).where(CharacterMediaJobRow.owner_user_id == owner,
                LearningTaskRow.status.in_(("queued", "leased", "running")))).all()
            created = self.tasks.create_task(owner_user_id=owner, kind="character_media", subject_id=card_id,
                idempotency_key="character:" + key, stage="generate", lane="maintenance",
                deadline_seconds=max(900, request.count
                    * IMAGE_LEASE_SECONDS + 60) if request.kind == "image" else 900,
                model_config_version=f"{config['config_id']}:{config['config_version']}", session=sess)
            if created["deduped"]:
                existing = sess.get(CharacterMediaJobRow, created["task_id"])
                if not existing or existing.request_hash != digest:
                    reject("invalid_field", 409)
            else:
                if len(active) >= 4:
                    reject("invalid_field", 429)
                sess.add(CharacterMediaJobRow(task_id=created["task_id"], card_id=card_id, owner_user_id=owner,
                    revision=request.revision, config_id=request.config_id, config_version=request.config_version,
                    request_json=serialized, request_hash=digest, submitted=False, result_json="[]"))
                self.disclosures.create(actor_id=owner, task_id=created["task_id"], config_id=request.config_id,
                    content_category="character_media", scope_snapshot={"card_id": card_id, "revision": request.revision,
                        "request_hash": digest}, config_version=request.config_version, session=sess)
        self.wake.set()
        return created

    def list(self, owner, card_id):
        self.cards.get(owner, card_id)
        with self.db.session() as sess:
            rows = sess.scalars(select(CharacterMediaJobRow).where(CharacterMediaJobRow.card_id == card_id,
                CharacterMediaJobRow.owner_user_id == owner).limit(50)).all()
            return [dict(task_id=row.task_id, kind=json.loads(row.request_json)["kind"],
                status=self.tasks.get_task(row.task_id, actor_id=owner)["status"], assets=json.loads(row.result_json)) for row in rows]

    def cancel(self, owner, card_id, task_id):
        self.cards.get(owner, card_id)
        with self.db.session() as sess:
            job = sess.get(CharacterMediaJobRow, task_id)
            if not job or job.owner_user_id != owner or job.card_id != card_id:
                reject()
        result=self.tasks.request_cancel(task_id, actor_id=owner)
        task=self.tasks.get_task(task_id,actor_id=owner)
        if task["status"]=="queued":
            self.tasks.fail_queued(task_id,expected_generation=task["generation"],expected_revision=task["revision"],error_code="TASK_CANCELLED")
        return result

    def voices(self, owner, card_id, config_id):
        card = self.cards.get(owner, card_id) if card_id else {"profile": {}}
        return self.expression(owner, card["profile"], config_id)

    def expression(self, owner, profile, config_id=""):
        from .voice_policy import recommend_voices
        if not config_id:
            return recommend_voices(profile, [])
        try:
            return recommend_voices(profile, self._voice_catalog(owner, config_id))
        except AdapterError:
            # Profile derivation is local and remains available when the vendor
            # catalog fails. Ownership/config failures retain their HTTP status.
            return {**recommend_voices(profile, []), "match_status": "catalog_unavailable"}

    def _voice_catalog(self, owner, config_id):
        config = self.configs.get(config_id, actor_id=owner, is_admin=False)
        if not config["is_active"] or config["kind"] != "tts" or config["protocol"] != "minimax":
            reject()
        key = (owner, config_id, config["config_version"])
        # Coalesce overlapping draft requests; never reuse another owner's catalog.
        with self.voice_lock:
            cached = self.voice_cache.get(key)
            if cached and cached[0] > monotonic():
                return cached[1]
            credentials = self.configs.resolve_credentials(config_id)
            adapter = self.adapter_factory("minimax", endpoint=config["endpoint"], model_name=config["model_name"],
                credentials=credentials, timeout=20, max_response_bytes=1024*1024)
            try:
                voices = adapter.list_voices()
            finally:
                adapter.close()
            self.voice_cache = {key: value for key, value in self.voice_cache.items() if value[0] > monotonic()}
            if len(self.voice_cache) >= 32:
                self.voice_cache.pop(next(iter(self.voice_cache)))
            self.voice_cache[key] = (monotonic()+60, voices)
            return voices

    def _guard(self, job, lease, sess=None):
        self.tasks.validate_lease(lease, actor_id=job.owner_user_id, session=sess)
        with self.db.session() as check:
            card = self.cards._card(check, job.card_id, job.owner_user_id, write=False)
            if card.current_revision != job.revision:
                reject("disclosure_revoked", 409)
        creation=json.loads(job.request_json).get("creation")
        if creation is not None:
            from .creation_delivery import guard_media_creation
            guard_media_creation(self,job.owner_user_id,job.card_id,job.revision,creation)
        self.disclosures.verify_or_audit(task_id=job.task_id, config_id=job.config_id, content_category="character_media",
            actor_id=job.owner_user_id, scope_snapshot={"card_id": job.card_id, "revision": job.revision, "request_hash": job.request_hash}, session=sess)

    def run(self, task_id):
        task = self.tasks.get_task(task_id)
        if task["status"] in ("leased", "running"):
            # Requeue only an expired lease, and only when a durable upstream ID exists.
            with self.db.session() as sess:
                saved = sess.get(CharacterMediaJobRow, task_id)
                resumable = saved and saved.provider_job_id
            if not resumable:
                lease = self.tasks.claim(task_id, lease_owner="character-media", lease_seconds=90, allow_reclaim_active=False)
                self.tasks.submit_result(lease, stage="generate", status="failed", error_code="OUTPUT_INVALID")
                return
            lease = self.tasks.claim(task_id, lease_owner="character-media", lease_seconds=90, allow_reclaim_active=False)
        else:
            lease = self.tasks.claim_next("maintenance",lease_owner="character-media",lease_seconds=90,task_ids=[task_id])
        if lease is None:
            return
        adapter, output = None, []
        try:
            with self.db.session() as sess:
                job = sess.get(CharacterMediaJobRow, task_id)
                profile = json.loads(sess.get(CharacterRevisionRow, (job.card_id, job.revision)).profile_json)
                sess.expunge(job)
            request = json.loads(job.request_json)
            if request.get("creation"):
                profile=request["creation"]["profile"]
            self._guard(job, lease)
            config = self.configs.get(job.config_id, actor_id=job.owner_user_id, is_admin=False)
            credentials = self.configs.resolve_credentials(job.config_id)
            adapter = self.adapter_factory(config["protocol"], endpoint=config["endpoint"], model_name=config["model_name"], credentials=credentials,
                timeout=IMAGE_REQUEST_TIMEOUT_SECONDS if request["kind"] == "image" else 45,
                max_response_bytes=16*1024*1024)
            adapter.task_id, adapter.config_id, adapter.config_version = task_id, job.config_id, job.config_version
            # Durable submission fence: after a crash with no provider ID, do not resubmit.
            with self.db.session() as sess:
                saved = sess.get(CharacterMediaJobRow, task_id, with_for_update=True)
                if saved.submitted and not saved.provider_job_id:
                    raise AdapterError("capability_test_failed")
                saved.submitted = True
            if request["kind"] == "video":
                if not job.provider_job_id:
                    import imageio_ffmpeg
                    imageio_ffmpeg.get_ffmpeg_exe()
                    reference = None
                    if profile["base_asset_id"]:
                        frame, mime = self.cards.asset(job.owner_user_id, job.card_id, profile["base_asset_id"])
                        reference = f"data:{mime};base64," + base64.b64encode(frame).decode()
                    self._guard(job, lease)
                    submitted = adapter.submit(self._prompt(profile, request), first_frame_url=reference,
                        duration=request["duration"], resolution=request["resolution"] or ("768P" if config["protocol"] == "minimax_video" else "720p"), ratio=request["ratio"])
                    job.provider_job_id = submitted.job_id
                    with self.db.session() as sess:
                        sess.get(CharacterMediaJobRow, task_id).provider_job_id = submitted.job_id
                while not self.stop.is_set():
                    self.tasks.heartbeat(lease, lease_seconds=90)
                    self._guard(job, lease)
                    result = adapter.query(job.provider_job_id)
                    if result.status == "succeeded":
                        payload = download(result.result_url, 32 * 1024 * 1024)
                        output.append(normalize_media(payload, "video"))
                        break
                    if result.status in ("failed", "cancelled", "expired"):
                        raise AdapterError("capability_test_failed")
                    if self.stop.wait(3):
                        return
                if not output:
                    return
            elif request["kind"] == "image":
                # This is a character-specific request; the cover crop adapter is not used.
                for index in range(request["count"]):
                    self.tasks.heartbeat(lease, lease_seconds=IMAGE_LEASE_SECONDS)
                    self._guard(job, lease)
                    adapter.authorize_request = lambda: self._guard(job, lease)
                    reference = None
                    reference_id=request.get("creation",{}).get("reference_asset_id") or profile["base_asset_id"]
                    if reference_id:
                        reference, _ = self.cards.asset(job.owner_user_id, job.card_id, reference_id)
                    body = adapter._image_request({"model": config["model_name"],
                        "prompt": self._prompt(profile, request) + f"; candidate {index+1}",
                        "n": 1, "size": "1536x1024" if request.get("image_part") in ("upper_arm","forearm") else "1024x1024",
                        "response_format": "b64_json"}, **({"reference": reference} if reference else {}))
                    data = body.get("data") if isinstance(body, dict) else None
                    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
                        raise AdapterError("capability_test_failed")
                    image = data[0]
                    if image.get("b64_json"):
                        if len(image["b64_json"]) > 12 * 1024 * 1024:
                            raise AdapterError("response_too_large")
                        payload = base64.b64decode(image["b64_json"], validate=True)
                    else:
                        payload = download(image["url"], 8 * 1024 * 1024)
                    normalized=normalize_media(payload,"image")
                    validate_generated_cutout(normalized[0])
                    if request.get("image_part") in ("upper_arm","forearm"):
                        with Image.open(io.BytesIO(normalized[0])) as decoded:
                            part=isolated_pointing_part(decoded)
                            prepared=io.BytesIO()
                            part.save(prepared,format="PNG")
                            normalized=(prepared.getvalue(),"image/png")
                    output.append(normalized)
            else:
                voice = profile["voice_id"] or credentials.get("voice_id")
                from .voice_policy import profile_gender, resolve_voice
                if profile_gender(profile) != "unknown" and not request.get("creation"):
                    voice = resolve_voice(profile, adapter.list_voices(), profile.get("voice_id", ""))
                    if not voice:
                        raise AdapterError("capability_test_failed")
                self._guard(job, lease)
                from .expression import derive_expression
                from .speech import compile_speech
                style = derive_expression(profile)
                sample = ("嗯嗯，先别急嘛。我们先看这一小步，你把输入和输出分开，就抓住重点啦。"
                    if style.liveliness > .6 else "嗯，我们先别急。先观察题目的条件，再一步一步找出答案。"
                    if style.warmth > .6 else "先明确题目条件。再分析关键关系，最后验证结论。")
                plan = compile_speech(sample, profile, adapter.model_name, voice)
                result = adapter.synthesize(plan.speech_text, voice_id=voice, emotion=plan.emotion,
                    speed=plan.speed, volume=plan.volume, pitch=plan.pitch,
                    voice_modify=dict(plan.voice_modify) or None)
                payload = bytes.fromhex(result.get("audio_hex", ""))
                if not payload or len(payload) > 4 * 1024 * 1024:
                    raise AdapterError("capability_test_failed")
                output.append((payload, "audio/mpeg"))
            asset_ids, keys = [], []
            try:
                def publish(sess):
                    self._guard(job, lease, sess)
                    card = self.cards._card(sess, job.card_id, job.owner_user_id, write=True)
                    if card.current_revision != job.revision:
                        reject("disclosure_revoked", 409)
                    for payload, mime in output:
                        asset_id = "asset_" + uuid.uuid4().hex
                        key = f"characters/{job.card_id}/{asset_id}.bin"
                        self.cards.storage.put(key, payload)
                        keys.append(key)
                        asset_ids.append(asset_id)
                        sess.add(CharacterAssetRow(asset_id=asset_id, card_id=job.card_id, owner_user_id=job.owner_user_id,
                            source_revision=job.revision, kind=request["kind"], scene=request["scene"], storage_key=key, mime_type=mime,
                            confirmed=bool(request.get("replace_current"))))
                    sess.get(CharacterMediaJobRow, task_id).result_json = json.dumps(asset_ids)
                    if request.get("replace_current"):
                        from .schemas import Profile
                        from .models import CharacterVoiceChoiceRow
                        data=dict(profile)
                        choice=sess.get(CharacterVoiceChoiceRow,(job.owner_user_id,job.card_id))
                        if choice:
                            data.update(voice_id=choice.voice_id,voice_config_id=choice.config_id,voice_selection="manual")
                        old_rig=data.get("pointing_rig") or {}
                        old_parts={old_rig.get(key) for key in ("body_asset_id","upper_arm_asset_id","forearm_asset_id")}
                        retained=[]
                        for selected_id in data.get("media_ids",[]):
                            asset=sess.get(CharacterAssetRow,selected_id)
                            if asset and not (asset.kind=="image" and asset.scene==request["scene"]) and not (
                                    request["scene"]=="neutral" and selected_id in old_parts):
                                retained.append(selected_id)
                        data["media_ids"]=retained+asset_ids
                        if request["scene"]=="neutral":
                            data["base_asset_id"]=asset_ids[0]
                            data["pointing_rig"]=None
                        sess.flush()
                        serialized=self.cards._profile(sess,Profile.model_validate(data),card,job.owner_user_id)
                        previous_revision=card.current_revision
                        card.current_revision+=1
                        sess.add(CharacterRevisionRow(card_id=card.card_id,revision=card.current_revision,profile_json=serialized))
                        sess.flush()
                        from .persona import copy_persona
                        copy_persona(sess,card.card_id,previous_revision,card.card_id,card.current_revision)
                result = self.tasks.submit_result(lease, stage="generate", status="succeeded", publish=publish)
                if not result.get("accepted"):
                    for key in keys:
                        self.cards.storage.delete(key)
                    self.tasks.recover_worker_failure(lease,stage="generate",error_code="LEASE_LOST")
            except Exception:
                for key in keys:
                    self.cards.storage.delete(key)
                raise
        except LeaseError:
            if adapter and job.provider_job_id:
                try:
                    adapter.cancel(job.provider_job_id)
                except Exception:
                    pass
            self.tasks.recover_worker_failure(lease,stage="generate",error_code="LEASE_LOST")
        except Exception as exc:
            code = {"timeout": "MODEL_TIMEOUT", "credential_invalid": "MODEL_CREDENTIAL_INVALID",
                "disclosure_revoked": "DISCLOSURE_REVOKED", "disclosure_required": "DISCLOSURE_REQUIRED"}.get(getattr(exc, "code", ""), "OUTPUT_INVALID")
            failed=self.tasks.submit_result(lease, stage="generate", status="failed", error_code=code)
            if not failed.get("accepted"):
                self.tasks.recover_worker_failure(lease,stage="generate",error_code=code)
        finally:
            if adapter:
                adapter.close()

    @staticmethod
    def _prompt(profile, request):
        identity = (f"Character portrait. Identity: {profile['identity'][:240]}. "
            f"Background and relationship: {profile.get('background','')[:1200]}. "
            f"Appearance: {profile['appearance'][:1200]}. Personality: {profile['personality'][:600]}. "
            "Preserve this character's own identity and agreed outfit; teaching is only the use case, "
            "not a requirement to change their profession, age, relationship or clothing. ")
        part = request.get("image_part", "portrait")
        if part == "pointing_body":
            return identity + (
                "Edit the reference cutout for a 2D puppet. Preserve face, hair, clothing, body position, "
                "the other arm and existing props, canvas dimensions and exact scale. "
                "REMOVE ENTIRELY the character's RIGHT arm on the VIEWER LEFT, including its sleeve, "
                "cuff, upper arm, forearm and hand. Reconstruct the torso behind it naturally. "
                "Leave a clean attachment area at that shoulder on the viewer left. "
                "Only the other original arm remains; preserve its pose and existing props without adding any. "
                "Do not add an arm, sleeve, hand or pointer. Genuine transparent alpha background. "
                "No other changes, no glow, no shadow, no text or watermark.")
        if part != "portrait":
            detail = ("ONLY one detached upper arm: matching the reference character's actual sleeve and cuff, "
                "from rounded shoulder joint at LEFT to elbow joint at RIGHT. No hand, no forearm."
                if part == "upper_arm" else
                "ONLY one detached forearm and hand holding a slim teaching pointer horizontally: "
                "elbow joint at LEFT, hand near right side, pointer tip at RIGHT. No upper arm.")
            return (
                "Create a NEW isolated limb cutout on an empty transparent canvas. "
                "The supplied person is only a clothing and skin reference, not content to preserve. "
                "Remove the entire person; retain no face, head, torso or legs anywhere in the output. "
                "Match the reference clothing, skin tone, line art and lighting. "
                + detail + " 2D animation cutout. Straight horizontal pose, joint centers at vertical midpoint. "
                "Fill a wide horizontal composition. Genuine transparent alpha background, not a checkerboard. "
                "No body, face, duplicate limbs, labels, text, shadow or watermark.")
        actions = {
            "neutral": "Relaxed upright pose, friendly smile.",
            "explaining": "Teaching with an attentive lively expression and one open welcoming hand.",
            "thinking": "Thoughtful curious expression, head slightly tilted, free hand near chin.",
            "encouraging": "Reassuring smile and a gentle supportive thumbs-up.",
            "celebrating": "Joyful bright smile and a small delighted celebratory gesture.",
        }
        return identity + (f"Action: {actions[request['scene']]} "
            "Preserve the reference character identity, face, hairstyle, clothing and framing. "
            "Polished 2D anime character cutout, entire head and waist visible, no cropped hair or hands. "
            "Keep both arms naturally attached to the body in a complete, anatomically coherent pose. "
            "No detached limbs, separate arm parts or teaching pointer. "
            "Genuine transparent alpha background, not a checkerboard; no scene, text or watermark.")

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = Thread(target=self._loop, daemon=True, name="character-media-dispatch")
            self.thread.start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                self.tasks.sweep_expired_tasks(kinds=["character_media"])
                if self.future is None or self.future.done():
                    with self.db.session() as sess:
                        task_id = sess.scalar(select(LearningTaskRow.task_id).where(LearningTaskRow.kind == "character_media",
                            LearningTaskRow.cancel_requested.is_(False), LearningTaskRow.status.in_(("queued", "leased", "running")),
                            or_expired_lease()).order_by(LearningTaskRow.created_at).limit(1))
                    if task_id:
                        self.future = self.pool.submit(self.run, task_id)
            except Exception:
                pass
            self.wake.wait(1)
            self.wake.clear()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=2)
        self.pool.shutdown(wait=True, cancel_futures=True)


def or_expired_lease():
    from sqlalchemy import or_
    return or_(LearningTaskRow.status == "queued", LearningTaskRow.lease_until <= utcnow())
