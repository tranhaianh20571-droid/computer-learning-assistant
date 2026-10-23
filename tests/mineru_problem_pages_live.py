"""Opt-in MinerU same-input comparison with private credentials and checkpoints."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import ipaddress
import json
import logging
import re
import shutil
import socket
import sys
import time
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx

API = "https://mineru.net/api/v4"
LIMIT = 128_000_000
STATES = {"waiting-file", "pending", "running", "converting", "done", "failed"}
ERROR_CODES = {"A0202", "A0211", "-500", "-10001", "-10002"} | {
    str(-60000 - i) for i in range(1, 30)}


class ProbeError(Exception):
    """Controlled diagnostics, excluding provider bodies and signed URLs."""


def transfer_url(value):
    parsed = urlsplit(value if isinstance(value, str) else "")
    host = parsed.hostname or ""
    allowed = (
        host == "mineru.net" or host.endswith(".mineru.net")
        or host == "cdn-mineru.openxlab.org.cn"
        or host == "mineru.oss-cn-shanghai.aliyuncs.com"
    )
    if not allowed or parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ProbeError("unexpected_transfer_destination")
    return value


def api(client, method, path, credential, payload=None):
    response = client.request(method, API + path, headers={"Authorization": "Bearer " + credential},
                              json=payload, follow_redirects=False)
    if response.status_code in (401, 403):
        raise ProbeError(f"credential_rejected_http_{response.status_code}")
    if response.status_code == 429:
        raise ProbeError("rate_limited")
    if response.status_code != 200:
        raise ProbeError(f"provider_http_{response.status_code}")
    if len(response.content) > 2_000_000:
        raise ProbeError("api_response_too_large")
    result = response.json()
    if not isinstance(result, dict):
        raise ProbeError("api_response_invalid")
    code = str(result.get("code"))
    if code != "0":
        raise ProbeError("provider_code_" + (code if code in ERROR_CODES else "unclassified"))
    data = result.get("data")
    if not isinstance(data, dict):
        raise ProbeError("api_data_invalid")
    return data


def inspect_archive(path):
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > 2000 or sum(v.file_size for v in members) > LIMIT:
            raise ProbeError("archive_too_large")
        # Do not extract arbitrary ZIP paths or return private document text.
        if not any(Path(v.filename).name == "full.md" for v in members):
            raise ProbeError("markdown_missing")
        return {"entries": len(members),
                "json_files": [v.filename for v in members if v.filename.endswith(".json")],
                "image_files": sum(v.filename.lower().endswith((".jpg", ".png", ".jpeg")) for v in members)}


def download(client, url, path):
    size = 0
    with client.stream("GET", url) as response:
        if response.status_code != 200:
            raise ProbeError(f"download_http_{response.status_code}")
        with path.open("wb") as stream:
            for part in response.iter_bytes():
                size += len(part)
                if size > LIMIT:
                    raise ProbeError("result_too_large")
                stream.write(part)
    return size


def download_with_public_dns(client, url, path):
    """Resolve only the validated result host; retain HTTPS hostname verification."""
    host = urlsplit(url).hostname
    response = client.get("https://dns.google/resolve", params={"name": host, "type": "A"})
    if response.status_code != 200:
        raise ProbeError("public_dns_failed")
    addresses = [item.get("data") for item in response.json().get("Answer", [])
                 if item.get("type") == 1]
    if not addresses or not all(ipaddress.ip_address(item).is_global for item in addresses):
        raise ProbeError("public_dns_address_invalid")
    resolve = socket.getaddrinfo

    def mapped(name, port, *args, **kwargs):
        return resolve(addresses[0] if name == host else name, port, *args, **kwargs)

    with patch("socket.getaddrinfo", mapped), httpx.Client(timeout=60, trust_env=False) as direct:
        return download(direct, url, path)


def compare(folder, baseline, progress):
    from app.materials.document_pipeline import TableText
    from app.materials.question_extraction import LABEL
    from somark_problem_pages_live import metrics

    originals = progress["original_pages"]
    with zipfile.ZipFile(folder / "result-private.zip") as archive:
        inspect_archive(folder / "result-private.zip")
        names = [v for v in archive.namelist() if v.endswith("_content_list.json")]
        if len(names) != 1:
            raise ProbeError("content_list_missing_or_ambiguous")
        content = json.loads(archive.read(names[0]))
        layout = json.loads(archive.read("layout.json"))
        if not isinstance(content, list) or {v.get("page_idx") for v in content} != set(range(len(originals))):
            raise ProbeError("parsed_page_indices_invalid")
    pages = [{"page": i + 1, "blocks": []} for i in range(len(originals))]
    for source in content:
        raw_type = source["type"]
        kind = {"image": "figure", "equation": "formula"}.get(raw_type, raw_type)
        text = source.get("text", "")
        block = {"type": kind, "content": text, "provider_bbox": source.get("bbox"),
                 "coordinate_space": "provider", "source_kind": "ocr"}
        if raw_type == "image":
            block.update(content=source.get("content", ""), image_path=source.get("img_path"))
        if raw_type == "table":
            block["html"] = source.get("table_body", "")
            parsed = TableText()
            parsed.feed(block["html"])
            block["content"] = "".join(parsed.text)
        blocks = pages[source["page_idx"]]["blocks"]
        blocks.append(block)
        # Captions remain separate; do not report them as OCR inside the image.
        for field in ("image_caption", "image_footnote", "table_caption", "table_footnote"):
            for caption in source.get(field, []):
                if not isinstance(caption, str):
                    raise ProbeError("caption_format_invalid")
                blocks.append({"type": "text", "source_kind": field, "content": caption})
    previous = json.loads((baseline / "comparison-private.json").read_text(encoding="utf8"))
    previous = {v["original_page"]: v for v in previous["comparison"]}
    rows = [{**previous[original], "mineru": metrics(page, LABEL)}
            for original, page in zip(originals, pages, strict=True)]
    result = {"scope": {"source_sha256": progress["source_sha256"],
                        "sample_sha256": progress["sample_sha256"], "original_pages": originals},
              "mineru": {"requested": progress["features"],
                         "reported": {k: layout.get(k) for k in
                                      ("_backend", "_effort", "_ocr_enable", "_version_name")},
                         "parse_seconds": progress["parse_seconds"],
                         "download_recovery_seconds": progress.get("download_recovery_seconds")},
              "comparison": rows}
    for name, value in (("document-private.json", {"page_results": pages}),
                        ("comparison-private.json", result)):
        (folder / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf8")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--baseline", required=True, help="Completed SoMark comparison directory.")
    parser.add_argument("--out", required=True)
    parser.add_argument("--resume-download", action="store_true",
                        help="Resume a diagnosed connection failure after parsing completed; never resubmit.")
    parser.add_argument("--direct-cdn-dns", action="store_true",
                        help="Use public DNS and direct HTTPS for result download only.")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "server/backend"))
    baseline = (root / args.baseline).resolve(strict=True)
    folder = (root / args.out).resolve()
    if not folder.is_relative_to((root / ".backup").resolve()):
        parser.error("Private output must remain below .backup.")
    original = json.loads((baseline / "run.json").read_text(encoding="utf8"))
    sample = baseline / "problem-pages.pdf"
    sha = hashlib.sha256(sample.read_bytes()).hexdigest()
    if original["status"] != "completed" or sha != original["sample_sha256"]:
        parser.error("Baseline must contain a completed, unchanged input PDF.")
    folder.mkdir(parents=True, exist_ok=True)
    progress_path = folder / "run.json"
    progress = json.loads(progress_path.read_text(encoding="utf8")) if progress_path.exists() else {}

    def save():
        progress_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf8")

    if progress and (progress["sample_sha256"] != sha or progress["original_pages"] != original["original_pages"]):
        parser.error("Existing experiment scope changed.")
    if progress.get("failure"):
        if not (args.resume_download and progress.get("parse_seconds") and progress.get("batch_id")
                and progress["failure"]["code"] == "ConnectError"):
            parser.error("Prior failure requires diagnosis; no automatic repeat submission.")
        progress.setdefault("recovered_failures", []).append(progress.pop("failure"))
        save()
    if args.resume_download and not (progress.get("batch_id") and progress.get("parse_seconds")):
        parser.error("Download recovery requires an already completed provider parse.")
    if not progress:
        shutil.copyfile(sample, folder / "problem-pages.pdf")
        progress = {"source_sha256": original["sha256"], "sample_sha256": sha,
                    "original_pages": original["original_pages"], "status": "prepared",
                    "features": {"model_version": "vlm", "enable_formula": True,
                                 "enable_table": True, "language": "ch", "is_ocr": True}}
        save()
    if hashlib.sha256((folder / "problem-pages.pdf").read_bytes()).hexdigest() != sha:
        parser.error("Experiment input changed.")
    logging.getLogger("httpx").disabled = True
    logging.getLogger("httpcore").disabled = True
    if progress["status"] != "completed":
        credential = getpass.getpass("MinerU API token (hidden): ").strip()
        if not credential:
            parser.error("Missing credential.")
        started = time.monotonic()
        try:
            with httpx.Client(timeout=httpx.Timeout(60, connect=20), follow_redirects=False) as client:
                if progress["status"] == "submitting" and not progress.get("batch_id"):
                    raise ProbeError("ambiguous_submission_requires_diagnosis")
                if not progress.get("batch_id"):
                    progress["status"] = "submitting"
                    save()
                    features = progress["features"]
                    data = api(client, "POST", "/file-urls/batch", credential, {
                        **{k: v for k, v in features.items() if k != "is_ocr"},
                        "files": [{"name": "problem-pages.pdf", "data_id": "problem-pages",
                                   "is_ocr": features["is_ocr"]}]})
                    ident = data.get("batch_id")
                    urls = data.get("file_urls")
                    if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident):
                        raise ProbeError("batch_id_invalid")
                    if not isinstance(urls, list) or len(urls) != 1:
                        raise ProbeError("upload_url_invalid")
                    progress.update(batch_id=ident, status="uploading")
                    save()
                    print(json.dumps({"credential_probe": "accepted", "pages": len(original["original_pages"])}),
                          flush=True)
                    # No MinerU bearer token is sent to object storage.
                    uploaded = client.put(transfer_url(urls[0]), content=(folder / "problem-pages.pdf").read_bytes())
                    if uploaded.status_code not in (200, 201, 204):
                        raise ProbeError(f"upload_http_{uploaded.status_code}")
                    progress["status"] = "processing"
                    save()
                elif progress["status"] == "uploading":
                    raise ProbeError("ambiguous_upload_requires_diagnosis")
                last_notice = 0.0
                while time.monotonic() - started < 900:
                    result = api(client, "GET", "/extract-results/batch/" + progress["batch_id"], credential)
                    items = result.get("extract_result")
                    if not isinstance(items, list) or len(items) != 1:
                        raise ProbeError("batch_result_invalid")
                    item = items[0]
                    state = item.get("state")
                    if state not in STATES:
                        raise ProbeError("unknown_provider_state")
                    if state == "failed":
                        raise ProbeError("provider_parse_failed")
                    if state == "done":
                        progress.setdefault("parse_seconds", round(time.monotonic() - started, 2))
                        destination = folder / "result-private.zip"
                        url = transfer_url(item.get("full_zip_url"))
                        size = (download_with_public_dns(client, url, destination) if args.direct_cdn_dns
                                else download(client, url, destination))
                        manifest = inspect_archive(destination)
                        progress.update(status="completed", seconds=round(time.monotonic() - started, 2),
                                        result_bytes=size, archive=manifest)
                        if args.resume_download:
                            progress["download_recovery_seconds"] = progress["seconds"]
                        progress["download_route"] = "public_dns_direct_https" if args.direct_cdn_dns else "default"
                        save()
                        break
                    if time.monotonic() - last_notice >= 15:
                        print(json.dumps({"status": state, "elapsed_seconds": round(time.monotonic() - started)}),
                              flush=True)
                        last_notice = time.monotonic()
                    time.sleep(4)
                else:
                    raise ProbeError("probe_deadline_exceeded")
        except Exception as exc:  # noqa: BLE001 - never output provider exceptions or request URLs
            code = str(exc) if isinstance(exc, ProbeError) else type(exc).__name__
            progress["failure"] = {"code": code}
            save()
            print(json.dumps({"status": "failed", "code": code}), flush=True)
            return 1
        finally:
            credential = ""
    print(json.dumps({"status": progress["status"], "pages": len(progress["original_pages"]),
                      "parse_seconds": progress["parse_seconds"],
                      "download_recovery_seconds": progress.get("download_recovery_seconds"),
                      "archive": progress["archive"]}), flush=True)
    result = compare(folder, baseline, progress)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
