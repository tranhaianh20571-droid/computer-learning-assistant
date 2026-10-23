"""Derive desktop artwork from the existing product logo without redesigning it."""
from pathlib import Path
import shutil

from PIL import Image

root = Path(__file__).resolve().parents[2]
source = root / "frontend/src/assets/logo-unfold.png"
destination = root / "desktop/assets"
destination.mkdir(parents=True, exist_ok=True)
shutil.copyfile(source, destination / "logo.png")
with Image.open(source) as image:
    image.convert("RGBA").save(
        destination / "icon.ico",
        sizes=[(size, size) for size in (16, 24, 32, 48, 64, 128, 256)],
    )
