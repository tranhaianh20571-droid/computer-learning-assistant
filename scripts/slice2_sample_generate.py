"""切片 2 T01：生成合成 PDF 样本矩阵（不涉密、不引用教材）。

覆盖：扫描文字、手写、表格公式、线路图、神经网络结构图、旋转页、
超大尺寸、多栏目录、含图像/无文本层混合页。
所有样本均为程序合成，仅用于 OCR 外部契约核验与裁切回归。
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path

from reportlab.lib.pagesizes import A3, A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas


def _text_page(c, lines, *, font="Helvetica", size=11, x=20 * mm, y=None):
    if y is None:
        y = A4[1] - 25 * mm
    c.setFont(font, size)
    for line in lines:
        c.drawString(x, y, line)
        y -= size * 1.6


def sample_text_layer() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _text_page(
        c,
        [
            "Chapter 1: Machine Learning Basics",
            "Gradient descent minimizes loss 3.14 with learning_rate=0.01",
            "Identifier: conv2d_block_7, version v1.2.3",
            "Page 1 of 4",
        ],
    )
    c.showPage()
    c.save()
    return buf.getvalue()


def sample_scanned_text() -> bytes:
    """无文本层：整页只画位图（模拟扫描件）。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1240, 1754), "white")
    d = ImageDraw.Draw(img)
    d.text((80, 120), "SCANNED TEXT PAGE", fill="black")
    for i in range(12):
        d.line((80, 200 + i * 40, 900, 200 + i * 40), fill="black", width=3)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=A4)
    from reportlab.lib.utils import ImageReader

    c.drawImage(ImageReader(buf), 0, 0, width=A4[0], height=A4[1])
    c.showPage()
    c.save()
    return out.getvalue()


def sample_handwriting() -> bytes:
    """模拟手写：倾斜、不规则的短线条，无文本层。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (1240, 1754), "white")
    d = ImageDraw.Draw(img)
    for i in range(8):
        x0, y0 = 120, 200 + i * 120
        pts = [(x0 + j * 18, y0 + (j % 3) * 6) for j in range(30)]
        d.line(pts, fill=(20, 20, 120), width=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=A4)
    from reportlab.lib.utils import ImageReader

    c.drawImage(ImageReader(buf), 0, 0, width=A4[0], height=A4[1])
    c.showPage()
    c.save()
    return out.getvalue()


def sample_table_formula() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 11)
    # 表格线
    x0, y0, w, h, rows, cols = 20 * mm, A4[1] - 140 * mm, 120 * mm, 60 * mm, 6, 4
    for r in range(rows + 1):
        c.line(x0, y0 + r * h / rows, x0 + w, y0 + r * h / rows)
    for col in range(cols + 1):
        c.line(x0 + col * w / cols, y0, x0 + col * w / cols, y0 + h)
    for r in range(rows):
        for col in range(cols):
            c.drawString(x0 + col * w / cols + 4, y0 + (rows - r - 1) * h / rows + 8, f"c{r}{col}")
    c.setFont("Helvetica-Oblique", 12)
    c.drawString(20 * mm, y0 - 20, "E = m c^2 ; integral_0^1 x^2 dx = 1/3")
    c.showPage()
    c.save()
    return buf.getvalue()


def sample_circuit_diagram() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setLineWidth(2)
    # 简单线路图
    c.rect(40 * mm, 120 * mm, 40 * mm, 25 * mm)
    c.rect(110 * mm, 120 * mm, 40 * mm, 25 * mm)
    c.line(80 * mm, 132 * mm, 110 * mm, 132 * mm)
    c.line(40 * mm, 132 * mm, 20 * mm, 132 * mm)
    c.line(150 * mm, 132 * mm, 170 * mm, 132 * mm)
    c.circle(90 * mm, 160 * mm, 6 * mm)
    c.drawString(20 * mm, 110 * mm, "R1")
    c.drawString(120 * mm, 110 * mm, "C1")
    c.showPage()
    c.save()
    return buf.getvalue()


def sample_neural_network() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    layers = [3, 4, 2]
    xs = [40 * mm, 100 * mm, 160 * mm]
    ys = {0: [100, 130, 160], 1: [90, 115, 140, 165], 2: [115, 140]}
    for li in range(len(layers) - 1):
        for ya in ys[li]:
            for yb in ys[li + 1]:
                c.line(xs[li], ya * mm, xs[li + 1], yb * mm)
    for li, coords in ys.items():
        for yy in coords:
            c.circle(xs[li], yy * mm, 4 * mm)
    c.drawString(40 * mm, 60 * mm, "input -> hidden -> output")
    c.showPage()
    c.save()
    return buf.getvalue()


def sample_rotated(rotation: int) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.saveState()
    c.translate(A4[0] / 2, A4[1] / 2)
    c.rotate(rotation)
    c.setFont("Helvetica", 14)
    c.drawString(-100, 0, f"ROTATED {rotation} PAGE marker_abc")
    c.restoreState()
    c.showPage()
    c.save()
    return buf.getvalue()


def sample_large_page() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A3)
    _text_page(c, [f"Large page line {i}" for i in range(40)], y=A3[1] - 25 * mm)
    c.showPage()
    c.save()
    return buf.getvalue()


def sample_multi_column() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=landscape(A4))
    _text_page(c, [f"L{i}" for i in range(30)], x=20 * mm, y=A4[0] - 25 * mm)
    _text_page(c, [f"R{i}" for i in range(30)], x=170 * mm, y=A4[0] - 25 * mm)
    c.showPage()
    c.save()
    return buf.getvalue()


SAMPLES = {
    "text_layer": sample_text_layer,
    "scanned_text": sample_scanned_text,
    "handwriting": sample_handwriting,
    "table_formula": sample_table_formula,
    "circuit_diagram": sample_circuit_diagram,
    "neural_network": sample_neural_network,
    "rotated_90": lambda: sample_rotated(90),
    "rotated_180": lambda: sample_rotated(180),
    "rotated_270": lambda: sample_rotated(270),
    "large_page": sample_large_page,
    "multi_column": sample_multi_column,
}


def generate(out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name, fn in SAMPLES.items():
        payload = fn()
        path = out_dir / f"{name}.pdf"
        path.write_bytes(payload)
        manifest[name] = {"path": str(path), "bytes": len(payload)}
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=".slice2-samples")
    args = parser.parse_args()
    manifest = generate(Path(args.out))
    for name, meta in manifest.items():
        print(f"{name}: {meta['bytes']} bytes")


if __name__ == "__main__":
    main()
