"""切片 2 T01：OCR 外部契约真实样本 spike 检查器（准入闸门）。

本脚本是 T01 的**可复跑检查器**，不是 T01 结论本身。
它需要真实的 AI Studio PaddleOCR / 百度智能云凭据与网络访问；
在未提供凭据时以明确的受控退出码结束，不伪造通过结果。

用法（凭据只从环境变量读取，禁止写入命令行或仓库）：

    # AI Studio（PaddleOCR-VL-1.6 / PP-DocLayoutV3）
    export PADDLEOCR_ENDPOINT=https://<ai-studio-endpoint>
    export PADDLEOCR_API_KEY=<key>
    python scripts/slice2_ocr_spike.py --provider aistudio --samples .slice2-samples

    # 百度智能云商业 API（备选）
    export BAIDU_OCR_API_KEY=<key> BAIDU_OCR_SECRET_KEY=<secret>
    python scripts/slice2_ocr_spike.py --provider baidu --samples .slice2-samples

退出码：
    0  = 所有样本核验通过
    2  = 缺少凭据（T01 未执行，阻断 T03/T04/T05）
    3  = 样本缺失（先运行 slice2_sample_generate.py）
    4  = 外部契约核验失败（需记录差异并交回规划者）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REQUIRED_PROVIDER_ENV = {
    "aistudio": ("PADDLEOCR_ENDPOINT", "PADDLEOCR_API_KEY"),
    "baidu": ("BAIDU_OCR_API_KEY", "BAIDU_OCR_SECRET_KEY"),
}

# 逐项核对表字段（写入 evidence 文档）
CONTRACT_CHECKS = (
    "http_submit_path",
    "http_status_path",
    "http_result_path",
    "coordinate_unit",
    "coordinate_system",
    "page_width_height",
    "rotation_handling",
    "reading_order",
    "figure_independent_id",
    "caption_association",
    "per_page_failure_info",
    "single_page_timeout_180s",
    "quota_and_retention",
)


def _missing(provider: str) -> list[str]:
    return [name for name in REQUIRED_PROVIDER_ENV[provider] if not os.environ.get(name)]


def _sample_files(samples_dir: Path) -> list[Path]:
    return sorted(p for p in samples_dir.glob("*.pdf"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("aistudio", "baidu"), required=True)
    parser.add_argument("--samples", default=".slice2-samples")
    parser.add_argument("--out", default="slice2-ocr-spike-result.json")
    args = parser.parse_args()

    samples_dir = Path(args.samples)
    if not samples_dir.exists():
        print(f"ERROR: samples dir not found: {samples_dir}", file=sys.stderr)
        print("run: python scripts/slice2_sample_generate.py --out .slice2-samples", file=sys.stderr)
        return 3

    missing = _missing(args.provider)
    if missing:
        print(
            f"BLOCKED: provider={args.provider} missing env: {', '.join(missing)}",
            file=sys.stderr,
        )
        print(
            "T01 未执行：缺少真实 OCR 凭据。T03/T04/T05 的 OCR 写路径必须保持关闭。",
            file=sys.stderr,
        )
        return 2

    files = _sample_files(samples_dir)
    if not files:
        print(f"ERROR: no PDF samples in {samples_dir}", file=sys.stderr)
        return 3

    # 真实核验需要供应商 SDK/HTTP 契约；此处只做骨架，未实现具体调用，
    # 以保证在未核对真实文档前不会伪造通过结果。
    result = {
        "provider": args.provider,
        "status": "not_implemented",
        "reason": "真实 HTTP 契约（路径/字段/坐标系）须按 T01 官方文档对齐后实现",
        "samples": [p.name for p in files],
        "checks": {name: "pending" for name in CONTRACT_CHECKS},
    }
    Path(args.out).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"WROTE {args.out}")
    print("T01 仍未通过：需完成真实端点契约核对并更新 evidence/slice2-ocr-external-contract.md")
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
