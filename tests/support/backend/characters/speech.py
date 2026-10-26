"""Pure speech compiler: clean display text is immutable, control tokens are generated."""
import hashlib
import json
import re
from dataclasses import dataclass, replace

VERSION = "character-speech-v4"
NONVERBAL = ("laughs", "chuckle", "coughs", "clear-throat", "groans", "breath", "pant",
    "inhale", "exhale", "gasps", "sniffs", "sighs", "snorts", "burps", "lip-smacking",
    "humming", "hissing", "emm", "sneezes")
TOKEN_RE = re.compile(r"\((?:" + "|".join(NONVERBAL) + r")\)")


@dataclass(frozen=True)
class SpeechPlan:
    display_text: str
    speech_text: str
    spans: tuple[tuple[int, int, int, int], ...]
    fingerprint: str
    emotion: str | None = None
    speed: float = 1.
    volume: float = 1.
    pitch: int = 0
    voice_modify: tuple[tuple[str, int | str], ...] = ()
    pronunciation: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    segments: tuple[tuple[int, int, float, float], ...] = ()


def speech_phrases(text):
    start = 0
    for match in re.finditer(r"[。！？；\n]+|[.!?;](?:[\"’”]*)\s+", text):
        prefix = text[:match.start()]
        if match.group().startswith(".") and re.search(r"(?:\b(?:e\.g|i\.e|Fig|Eq|Dr|Mr|Mrs|Prof|vs|et al)|\b[A-Z])$", prefix, re.I):
            continue
        yield start, match.end(), text[start:match.end()]
        start = match.end()
    if start < len(text):
        yield start, len(text), text[start:]


def compile_speech(text, profile, model, voice_id, *, split=True, expression=None, event="explaining"):
    # Vendor grammar in ordinary lesson text is not a control operation.
    safe = re.sub(r"<#.*?#>", "", text)
    safe = TOKEN_RE.sub("", safe)
    if safe != text:
        # Preserve the lesson text externally; use plain pronunciation of brackets.
        safe = text.replace("<#", "小于井号").replace("#>", "井号大于")
        safe = TOKEN_RE.sub(lambda match: "括号" + match.group()[1:-1] + "括号", safe)
        return SpeechPlan(text, safe, (), hashlib.sha256(json.dumps([VERSION, text, safe, model, voice_id]).encode()).hexdigest())
    if not profile:
        return SpeechPlan(text, text, (), hashlib.sha256(json.dumps([VERSION, text, model, voice_id]).encode()).hexdigest())
    from .expression import derive_expression
    expression = expression or derive_expression(profile)
    style = " ".join(str((profile or {}).get(key, "")) for key in
        ("personality", "identity", "background", "appearance", "speaking_style"))
    pause = .35 if any(word in style for word in ("成熟", "沉稳", "温柔")) else .20
    emotion = "calm" if any(word in style for word in ("成熟", "沉稳", "温柔")) else "happy" if any(word in style for word in ("活泼", "开朗")) else None
    supportive = event in ("partial", "incorrect", "unsure", "frustrated", "feedback")
    if supportive:
        emotion = "calm"
        pause = max(pause, .35)
    parts, spans, display_start, speech_start = [], [], 0, 0
    for phrase_start, phrase_end, value in speech_phrases(text):
        if phrase_start > display_start:
            gap = text[display_start:phrase_start]
            parts.append(gap); speech_start += len(gap)
        parts.append(value)
        spans.append((speech_start, speech_start + len(value), phrase_start, phrase_end))
        speech_start += len(value); display_start = phrase_end
        if phrase_end < len(text) and value.strip():
            token = f"<#{pause:.2f}#>"
            if model in ("speech-2.8-hd", "speech-2.8-turbo") and not any(TOKEN_RE.search(part) for part in parts):
                cue = ("chuckle" if event == "correct" and expression.liveliness > .6 else
                    "breath" if len(text) > 220 else "")
                if cue:
                    token += f"({cue})"
            parts.append(token); speech_start += len(token)
    speech = "".join(parts)
    if not parts:
        speech = text
    supported = model in ("speech-2.8-hd", "speech-2.8-turbo", "speech-2.6-hd", "speech-2.6-turbo",
        "speech-02-hd", "speech-02-turbo", "speech-01-hd", "speech-01-turbo")
    if not supported:
        speech, spans, emotion = text, [], None
    speed = round(max(.85, min(1.12, 1 + (expression.liveliness-expression.restraint)*.12)), 2) if profile else 1.
    if supportive:
        speed = max(.85, round(speed-.08, 2))
    pitch = 1 if supported and "轻甜" in style and "低沉" not in style else -1 if supported and "低沉" in style else 0
    modify = []
    for key, positive, negative in (("intensity", "有力", "柔和"), ("timbre", "明亮", "低沉")) if supported else ():
        if positive in style or negative in style:
            modify.append((key, 10 if positive in style else -10))
    effects = {"电话音效": "lofi_telephone", "机器人音效": "robotic", "礼堂音效": "auditorium_echo", "空旷音效": "spacious_echo"}
    for word, effect in effects.items() if supported else ():
        if word in style:
            modify.append(("sound_effects", effect))
            break
    capabilities = ("pause", "emotion", "pronunciation", "voice_setting", "voice_modify") + (
        ("nonverbal",) if model.startswith("speech-2.8-") else ()) if supported else ()
    # Native dictionaries correct only unambiguous domain phrases; never replace
    # ordinary single characters or execute author-supplied vendor controls.
    pronunciations = {"重载": "重载/(chong2)(zai4)", "重构": "重构/(chong2)(gou4)",
        "缓存": "缓存/(huan3)(cun2)", "线程": "线程/(xian4)(cheng2)"}
    pronunciation = tuple(value for word, value in pronunciations.items() if word in text) if supported else ()
    segments = []
    if split and supported:
        matches = list(re.finditer(r"[^。！？；\n]+[。！？；\n]*|[。！？；\n]+", text))
        stressed = next((m for m in matches if len(m.group()) <= 60
            and any(word in m.group() for word in ("关键", "重点", "注意", "记住"))), None)
        ranges = []
        if stressed:
            ranges.append((stressed.start(), stressed.end(), max(.85, speed-.06), 1.1))
        if expression.liveliness > .6 and matches:
            last = matches[-1]
            tail = re.search(r"[^，,。！？；\n]{2,18}(?:[。！？]+)?$", last.group())
            if tail and any(word in tail.group() for word in ("好不好", "对不对", "啦", "嘛", "呀")):
                start = last.start()+tail.start()
                if not ranges or start >= ranges[-1][1]:
                    ranges.append((start, last.end(), max(.85, speed-.12), .95))
        # One selected semantic phrase plus surrounding context costs at most
        # three requests. Effects which cannot fit are omitted, never retried
        # with an unbounded sequence of synthesis calls.
        if ranges:
            start, end, phrase_speed, phrase_volume = ranges[0]
            if start:
                segments.append((0, start, speed, 1.))
            segments.append((start, end, phrase_speed, phrase_volume))
            if end < len(text):
                segments.append((end, len(text), speed, 1.))
    options = [emotion, speed, pitch, modify, capabilities, pronunciation, segments]
    digest = hashlib.sha256(json.dumps([VERSION, text, speech, model, voice_id, options], ensure_ascii=False).encode()).hexdigest()
    return SpeechPlan(text, speech, tuple(spans), digest, emotion, speed, 1., pitch, tuple(modify),
        pronunciation, capabilities, tuple(segments))


def synthesize_plan(plan, profile, model, voice_id, synthesize, authorize):
    """Bounded native phrase synthesis; decode/concatenate with mature FFmpeg."""
    def one(part):
        authorize(plan.display_text)
        result = synthesize(part.speech_text, voice_id=voice_id,
            **({"emotion": part.emotion} if part.emotion else {}),
            speed=part.speed, volume=part.volume, pitch=part.pitch,
            voice_modify=dict(part.voice_modify) or None,
            pronunciation=list(part.pronunciation) or None)
        result["subtitle"] = map_subtitles(part, result.get("subtitle"))
        return result
    if not plan.segments:
        return one(plan)
    import subprocess
    import imageio_ffmpeg
    from ..slice1.adapters.base import AdapterError
    executable = imageio_ffmpeg.get_ffmpeg_exe()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    pcm, subtitles, offset = [], [], 0
    for start, end, speed, volume in plan.segments:
        spans = []
        for speech_start, speech_end, display_start, display_end in plan.spans:
            lo, hi = max(start, display_start), min(end, display_end)
            if lo < hi:
                spans.append((speech_start+lo-display_start, speech_end-(display_end-hi), lo, hi))
        if not spans:
            raise AdapterError("capability_test_failed")
        origin, speech_end = spans[0][0], spans[-1][1]
        part = replace(plan, display_text=plan.display_text[start:end],
            speech_text=plan.speech_text[origin:speech_end],
            spans=tuple((a-origin,b-origin,c-start,d-start) for a,b,c,d in spans),
            speed=speed, volume=volume, segments=())
        result = one(part)
        if not result.get("subtitle"):
            raise AdapterError("capability_test_failed")
        try:
            audio = bytes.fromhex(result.get("audio_hex", ""))
            if not audio or len(audio) > 4*1024*1024:
                raise ValueError("invalid audio")
            decoded = subprocess.run([executable, "-v", "error", "-i", "pipe:0", "-t", "120",
                "-f", "s16le", "-ac", "1", "-ar", "32000", "pipe:1"], input=audio,
                capture_output=True, timeout=15, creationflags=flags, check=True).stdout
        except (ValueError, OSError, subprocess.SubprocessError):
            raise AdapterError("capability_test_failed") from None
        duration = len(decoded)/64
        if not decoded or offset+duration > 120000:
            raise AdapterError("response_too_large")
        for cue in result["subtitle"]:
            if not (0 <= cue["begin_ms"] < cue["end_ms"] <= duration+250):
                raise AdapterError("capability_test_failed")
            subtitles.append({**cue, "begin_ms": round(cue["begin_ms"]+offset),
                "end_ms": round(min(cue["end_ms"], duration)+offset),
                "display_start": start+cue["display_start"], "display_end": start+cue["display_end"]})
        pcm.append(decoded)
        offset += duration
    authorize(plan.display_text)
    try:
        encoded = subprocess.run([executable, "-v", "error", "-f", "s16le", "-ac", "1", "-ar", "32000",
            "-i", "pipe:0", "-f", "mp3", "-b:a", "128k", "pipe:1"], input=b"".join(pcm),
            capture_output=True, timeout=15, creationflags=flags, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        raise AdapterError("capability_test_failed") from None
    if not encoded or len(encoded) > 4*1024*1024:
        raise AdapterError("response_too_large")
    return {"audio_hex": encoded.hex(), "audio_format": "mp3", "subtitle": subtitles,
        "speech_fingerprint": plan.fingerprint, "synthesis_requests": len(plan.segments)}


def map_subtitles(plan, subtitles):
    """Map exact vendor text slices to clean display spans; reject fuzzy alignment."""
    if (plan.speech_text != plan.display_text and not plan.spans) or not isinstance(subtitles, list):
        return None
    out, cursor = [], 0
    for cue in subtitles:
        value = cue.get("text", "") if isinstance(cue, dict) else ""
        if not isinstance(value, str) or not value.strip():
            continue
        value = re.sub(r"<#\d+(?:\.\d+)?#>", "", value).strip()
        value = TOKEN_RE.sub("", value).strip()
        if not value:
            continue
        start = plan.display_text.find(value, cursor)
        if start < 0:
            return None
        end = start + len(value)
        out.append({**cue, "text": plan.display_text[start:end], "display_start": start, "display_end": end})
        cursor = end
    return out or None
