"""Single-job video adapter. stdin/stdout are a private internal data channel."""
from __future__ import annotations

import base64
import contextlib
import io
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request


MAX_DOWNLOAD = 500 * 1024 * 1024
PHASE = "initializing"


def public_dns():
    original = socket.getaddrinfo
    def resolve(*args, **kwargs):
        infos = original(*args, **kwargs)
        if not infos or any(not ipaddress.ip_address(item[4][0]).is_global for item in infos):
            raise ValueError("private_network_denied")
        return infos
    socket.getaddrinfo = resolve


def load_subtitles(path):
    data = path.read_text(encoding="utf8")
    if path.suffix == ".json3":
        events = json.loads(data).get("events", [])
        return [{"start": e["tStartMs"] / 1000, "end": (e["tStartMs"] + e.get("dDurationMs", 0)) / 1000,
                 "text": "".join(s.get("utf8", "") for s in e.get("segs", [])).strip()}
                for e in events if e.get("segs")]
    if path.suffix == ".json":
        body = json.loads(data).get("body", [])
        return [{"start": x["from"], "end": x["to"], "text": x["content"]} for x in body]
    import re
    segments = []
    def seconds(value):
        parts = value.replace(",", ".").split(":")
        return sum(float(x) * 60 ** i for i, x in enumerate(reversed(parts)))
    for block in re.split(r"\r?\n\r?\n", data):
        lines = block.splitlines()
        for index, line in enumerate(lines):
            if " --> " in line:
                start, end = line.split(" --> ", 1)
                text = re.sub(r"<[^>]*>", "", " ".join(lines[index + 1:])).strip()
                if text:
                    segments.append({"start": seconds(start), "end": seconds(end.split()[0]), "text": text})
                break
    return segments


def execute(job):
    global PHASE
    public_dns()
    from yt_dlp import YoutubeDL
    import imageio_ffmpeg
    from PIL import Image, ImageOps

    class Quiet:
        def debug(self, *_): pass
        def info(self, *_): pass
        def warning(self, *_): pass
        def error(self, *_): pass

    options = job["options"]
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="source-video-") as temporary:
        root = Path(temporary)
        source = job.get("local_path")
        segments, source_kind, title = [], "", ""
        if not source:
            PHASE = "metadata_subtitles"
            base = {"logger": Quiet(), "quiet": True, "noprogress": True, "retries": 0,
                "fragment_retries": 0, "socket_timeout": 30, "max_filesize": MAX_DOWNLOAD,
                "noplaylist": True, "proxy": "", "cachedir": False, "ffmpeg_location": ffmpeg,
                "outtmpl": str(root / "media.%(ext)s"), "overwrites": False,
                "progress_hooks": [lambda d: (_ for _ in ()).throw(ValueError("download_too_large"))
                    if d.get("downloaded_bytes", 0) > MAX_DOWNLOAD else None]}
            with YoutubeDL({**base, "skip_download": True, "writesubtitles": True,
                           "writeautomaticsub": True, "subtitleslangs": ["zh.*", "ai-zh.*", "en.*"],
                           "subtitlesformat": "json3/srt/vtt/best"}) as ydl:
                info = ydl.extract_info(job["url"], download=True)
            duration = float(info.get("duration") or 0)
            if duration <= 0 or duration > 7200:
                raise ValueError("duration_out_of_range")
            title = str(info.get("title", ""))[:300]
            for subtitle in root.glob("media.*"):
                if subtitle.suffix in (".json", ".json3", ".srt", ".vtt"):
                    segments = load_subtitles(subtitle)
                    if segments:
                        source_kind = "platform_subtitle"
                        break
            if segments and not options.get("screenshot", True) and not options.get("video_understanding", True):
                return {"segments": segments, "frames": [], "duration": duration, "title": title,
                    "transcript_kind": source_kind, "visual_coverage_complete": False, "sample_interval": None}
            # Visual processing stays enabled even when platform subtitles exist.
            PHASE = "video_download"
            with YoutubeDL({**base, "format": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
                           "merge_output_format": "mp4", "writesubtitles": False}) as ydl:
                info = ydl.extract_info(job["url"], download=True)
            source = next((str(p) for p in root.glob("media.*")
                           if p.suffix.lower() in (".mp4", ".webm", ".mkv", ".mov")), None)
            if not source:
                raise ValueError("download_failed")
        if Path(source).stat().st_size > MAX_DOWNLOAD:
            raise ValueError("download_too_large")
        # FFmpeg stderr contains paths and metadata; it is captured privately.
        probe = subprocess.run([ffmpeg, "-protocol_whitelist", "file,pipe", "-i", source, "-f", "null", "-t", "0", "-"],
            capture_output=True, timeout=30)
        import re
        found = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", probe.stderr.decode(errors="replace"))
        duration = sum(float(v) * factor for v, factor in zip(found.groups(), (3600, 60, 1))) if found else 0
        if not 0 < duration <= 7200:
            raise ValueError("duration_out_of_range")
        if not segments:
            PHASE = "audio_transcription"
            audio = root / "audio.wav"
            subprocess.run([ffmpeg, "-nostdin", "-protocol_whitelist", "file,pipe", "-i", source, "-vn", "-ar", "16000", "-ac", "1", str(audio)],
                capture_output=True, check=True, timeout=300)
            from faster_whisper import WhisperModel
            models = Path(os.environ.get("SOURCE_ASR_MODELS_DIR", "/models"))
            model_path = models / options.get("asr_model", "small")
            if not (model_path / "model.bin").is_file():
                raise ImportError("asr_model_not_prepared")
            model = WhisperModel(str(model_path), device="cpu", compute_type="int8", local_files_only=True)
            parts, _ = model.transcribe(str(audio), vad_filter=True)
            segments = [{"start": x.start, "end": x.end, "text": x.text.strip()} for x in parts]
            source_kind = "asr"
        interval = max(1, int(options.get("interval", 6)))
        if not options.get("screenshot", True) and not options.get("video_understanding", True):
            return {"segments": segments, "frames": [], "duration": duration, "title": title,
                "transcript_kind": source_kind, "visual_coverage_complete": False, "sample_interval": None}
        PHASE = "video_frames"
        frame_limit = min(600, int(options.get("max_frames", 60)))
        import math
        wanted = max(1, math.ceil(duration / interval))
        count = min(frame_limit, wanted)
        effective_interval = interval if wanted <= frame_limit else max(0, duration - .25) / max(1, count - 1)
        times = [min(max(0, duration - .25), index * effective_interval) for index in range(count)]
        for index, timestamp in enumerate(times):
            subprocess.run([ffmpeg, "-nostdin", "-y", "-ss", f"{timestamp:.3f}", "-protocol_whitelist", "file,pipe", "-i", source,
                "-vf", "scale=1280:-2", "-frames:v", "1", str(root / f"frame-{index:05d}.jpg")],
                capture_output=True, check=True, timeout=30)
        frames = []
        for index, path in enumerate(sorted(root.glob("frame-*.jpg"))):
            with Image.open(path) as image:
                output = io.BytesIO()
                ImageOps.exif_transpose(image).convert("RGB").save(output, format="JPEG", quality=80)
                frames.append({"time_ms": round(times[index] * 1000), "mime": "image/jpeg",
                               "data": base64.b64encode(output.getvalue()).decode()})
        return {"segments": segments, "frames": frames, "duration": duration, "title": title,
                "transcript_kind": source_kind, "visual_coverage_complete": False,
                "sample_interval": effective_interval, "timeline_coverage": "uniform"}


if __name__ == "__main__":
    request = json.loads(sys.stdin.buffer.read(100_000))
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = execute(request)
        sys.stdout.write(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    except BaseException as failure:
        # No exception repr, URLs, credentials, subprocess output or source content.
        code = "VIDEO_DEPENDENCY_UNAVAILABLE" if isinstance(failure, ImportError) else "VIDEO_PROCESSING_FAILED"
        detail = str(failure)[:3000].lower()
        reason = ("platform_rejected" if "412" in detail or "403" in detail or "request was banned" in detail
                  else "network_target_denied" if "private_network_denied" in detail
                  else "network_timeout" if "timed out" in detail
                  else "video_dependency_unavailable" if isinstance(failure, ImportError)
                  else "video_processing_failed")
        sys.stdout.write(json.dumps({"ok": False, "error_code": code, "phase": PHASE, "reason": reason}))
