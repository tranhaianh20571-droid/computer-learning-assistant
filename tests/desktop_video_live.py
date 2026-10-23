"""Load the shipped CPU model and transcribe a synthetic spoken phrase offline."""
import argparse
import json
from pathlib import Path

import imageio_ffmpeg
from faster_whisper import WhisperModel

parser = argparse.ArgumentParser()
parser.add_argument("--model", type=Path, required=True)
parser.add_argument("--audio", type=Path, required=True)
args = parser.parse_args()
assert Path(imageio_ffmpeg.get_ffmpeg_exe()).is_file()
model = WhisperModel(str(args.model), device="cpu", compute_type="int8",
    local_files_only=True, cpu_threads=2)
segments, info = model.transcribe(str(args.audio), language="en", vad_filter=True)
text = " ".join(part.text.strip() for part in segments)
assert "desktop" in text.lower(), text
assert "test" in text.lower(), text
print(json.dumps({"passed": True, "device": "cpu", "model": "small",
    "scope": "offline synthetic speech transcription", "text": text}))
