"""Create a private, explicitly synthetic cross-subject PDF/OCR pair for live roles."""
import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = (root / args.out).resolve()
    if not folder.is_relative_to((root / ".backup").resolve()):
        parser.error("Synthetic experiment files must remain below .backup.")
    folder.mkdir(parents=True, exist_ok=True)
    from reportlab.pdfgen import canvas
    document = folder / "synthetic.pdf"
    pdf = canvas.Canvas(str(document), pagesize=(720, 540))
    pages = [
        [{"type": "title", "content": "Chapter 1: Synthetic mixture chemistry"},
         {"type": "text", "content": "Example 1\nCompare two fictional mixtures.\n"
          "(a) Mixture X contains 5 g of solute in 100 g of solution.\n"
          "(b) Mixture Y contains 12 g of solute in 150 g of solution.\n"
          "Which mixture has the higher mass fraction?"},
         {"type": "text", "content": "Worked result: X is 5 percent; Y is 8 percent; Y is higher."}],
        [{"type": "title", "content": "Chapter 2: Synthetic historical source comparison"},
         {"type": "text", "content": "Example 2\nFictional city Lyr recorded 10 ships in year 1 and 16 ships in year 2.\n"
          "(a) State the recorded change in ship count.\n"
          "(b) Does this observation alone prove increased prosperity? Explain one limitation."},
         {"type": "text", "content": "Answer: six more ships. Ship count alone does not establish prosperity."},
         {"type": "text", "content": "Why should independent records be compared before accepting a historical claim?"}],
        [{"type": "title", "content": "Chapter 3: Synthetic two-column numerical examples"},
         {"type": "text", "content": "Example 3\nCalculate the area of rectangle X, with sides 3 cm and 7 cm."},
         {"type": "text", "content": "Solution: 21 square cm."},
         {"type": "text", "content": "Example 4\nCalculate the perimeter of rectangle Y, with sides 5 cm and 9 cm."},
         {"type": "text", "content": "Solution: 28 cm."}],
    ]
    page_results = []
    for number, blocks in enumerate(pages, 1):
        for ordinal, block in enumerate(blocks):
            if number == 3:
                left = 35 if ordinal < 3 else 390
                top = 480 if ordinal in (0, 3) else 430 if ordinal in (1, 4) else 350
            else:
                left, top = 35, 490 - ordinal * 110
            block.update(block_id=f"p{number}/b{ordinal}", reading_order=ordinal,
                source_bbox=[left / 720, (540-top)/540, min(1, (left+310)/720), min(1, (540-top+80)/540)])
            text = pdf.beginText(left, top)
            text.setFont("Helvetica", 10)
            for line in block["content"].splitlines():
                text.textLine(line)
            pdf.drawText(text)
        pdf.showPage()
        page_results.append({"page_no": number, "status": "success", "blocks": blocks, "review_reasons": []})
    pdf.save()
    import sys
    sys.path.insert(0, str(root / "server/backend"))
    from app.materials.study_sources import build_study_index
    state = {"pages": 3, "pipeline_version": 3, "strategy": "direct", "unit": "page",
        "batches": [{"start": 1, "end": 3, "status": "completed"}],
        "page_results": page_results, "outline": [],
        "chunks": [{"page_no": p["page_no"], "kind": "text", "text": b["content"]}
                   for p in page_results for b in p["blocks"]],
        "experiment_kind": "synthetic_source_real_model"}
    state["study_index"] = build_study_index(state)
    (folder / "ocr-private.json").write_text(json.dumps(state, indent=2), encoding="utf8")
    (folder / "run.json").write_text(json.dumps({"experiment_kind": "synthetic_source",
        "documents": [{"sha256": hashlib.sha256(document.read_bytes()).hexdigest()}]}, indent=2), encoding="utf8")
    print(json.dumps({"synthetic_pages": 3, "subjects": ["mixtures", "historical evidence", "geometry"],
                      "out": args.out}))


if __name__ == "__main__":
    main()
