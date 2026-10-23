"""Local PDF inventory and contact sheets, with private outputs only."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    destination = (root / args.out).resolve()
    if not destination.is_relative_to((root / ".backup").resolve()):
        parser.error("Private PDF contents must remain below .backup.")
    import pypdfium2 as pdfium
    from PIL import Image, ImageDraw
    destination.mkdir(parents=True, exist_ok=True)
    pdf = pdfium.PdfDocument(args.pdf)
    pages = []
    for index in range(len(pdf)):
        page = pdf[index]
        text = page.get_textpage().get_text_range()
        lines = [line.strip() for line in text.splitlines() if len(line.strip()) > 3]
        pages.append({"page": index + 1, "text": text, "long_lines": lines})
        if index % 20 == 0:
            sheet = Image.new("RGB", (1200, 5 * 220), "white")
            draw = ImageDraw.Draw(sheet)
        thumb = page.render(scale=0.38).to_pil().convert("RGB")
        thumb.thumbnail((290, 190))
        position = index % 20
        x, y = (position % 4) * 300, (position // 4) * 220
        sheet.paste(thumb, (x, y + 22))
        draw.text((x + 8, y + 5), f"Page {index + 1}", fill="black")
        if index % 20 == 19 or index == len(pdf) - 1:
            sheet.save(destination / f"contact-{index // 20 + 1}.png")
    (destination / "pages.json").write_text(
        json.dumps(pages, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps({"pages": len(pages), "contact_sheets": (len(pages) + 19) // 20}))


if __name__ == "__main__":
    main()
