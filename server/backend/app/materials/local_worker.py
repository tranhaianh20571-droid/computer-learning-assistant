"""Local parsing subprocess entry point. Protocol carries private file paths only."""
import json
from pathlib import Path
import sys


def main():
    request = json.loads(sys.stdin.buffer.read(16384))
    source, output = Path(request["source"]), Path(request["output"])
    operation = request["operation"]
    try:
        if operation == "text":
            from app.materials.local_documents import parse_text
            result = parse_text(source.read_bytes(), request["kind"])
        elif operation == "pdf":
            from app.materials.document_pipeline import plan_batches
            result = plan_batches(source, kind="pdf", pages=request["pages"],
                size=source.stat().st_size, target_bytes=request["target_bytes"],
                provider_bytes=request["provider_bytes"])
        elif operation == "pptx":
            from app.materials.pptx_documents import plan_pptx_batches
            result = plan_pptx_batches(source, provider_bytes=request["provider_bytes"])
        elif operation == "pdf_bytes":
            from app.materials.document_pipeline import pdf_batch_bytes
            output.write_bytes(pdf_batch_bytes(source, request["start"], request["end"]))
            return
        elif operation == "pptx_bytes":
            from app.materials.pptx_documents import pptx_batch_bytes
            output.write_bytes(pptx_batch_bytes(source, request["start"], request["end"]))
            return
        elif operation == "image":
            from app.materials.source_processing import image_payload
            output.write_bytes(image_payload(source.read_bytes()))
            return
        else:
            raise ValueError()
        output.write_text(json.dumps(result, ensure_ascii=False), encoding="utf8")
    except Exception as exc:
        code = getattr(exc, "code", "OCR_OUTPUT_INVALID")
        allowed = {"file_too_large", "empty_file", "invalid_file_signature", "text_too_large",
                   "pptx_invalid", "OCR_OUTPUT_INVALID"}
        sys.stdout.write(json.dumps({"error_code": code if code in allowed else "OCR_OUTPUT_INVALID"}))
        sys.exit(2)


if __name__ == "__main__":
    main()
