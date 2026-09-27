"""PDF 渲染与文本层可信判定（切片 2 T02）。

契约（交接文档 §3.1「逐页解析状态机」）：
- 约 200 DPI 渲染；单页像素/边长/内存预算；超大页降级重渲染；
- 可信文本层直接提取；扫描/乱码/手写进入 OCR（OCR 属 T03，受 T01 门禁）；
- 可信文字不重复 OCR。

本模块不发起任何外发，可在 T01 未完成时独立测试。
"""

from __future__ import annotations

import io
import unicodedata
from dataclasses import dataclass
from typing import Optional

from .errors import RenderError

# ---- 渲染常量（T02 契约冻结；实测后如调整必须记录证据） ----
DEFAULT_DPI = 200
MIN_DPI = 72
MAX_DPI = 300
# 单页渲染像素上限（约 40 MP）；超出则按比例降级并标记 degraded。
MAX_PAGE_PIXELS = 40_000_000
MAX_PAGE_SIDE_PX = 12_000
MAX_PAGE_RENDER_SECONDS = 30.0

# ---- 文本可信阈值（T02 契约冻结，写入证据文档） ----
MIN_EFFECTIVE_CHARS = 20
TRUST_EFFECTIVE_RATIO = 0.60
MAX_GARBAGE_RATIO = 0.15
MIN_READING_ORDER_SCORE = 0.50
READING_ORDER_TOLERANCE_PT = 2.0

# 明确视为乱码的字符（缺字方块、替换符、私用区）
_GARBAGE_CHARS = frozenset("\ufffd\u25a0\u25a1\u25af\u2b1b\u2b1c")
_BOX_DRAWING = "─│┌┐└┘├┤┬┴┼━┃┏┓┗┛"


@dataclass
class RenderResult:
    image: "object"  # PIL.Image.Image（避免顶层依赖 PIL 类型标注）
    width_px: int
    height_px: int
    dpi: int
    scale: float
    degraded: bool
    reason: Optional[str] = None

    def as_meta(self) -> dict:
        return {
            "width_px": self.width_px,
            "height_px": self.height_px,
            "dpi": self.dpi,
            "scale": self.scale,
            "degraded": self.degraded,
            "reason": self.reason,
        }


@dataclass
class TextLayerEval:
    text: str
    char_count: int
    effective_chars: int
    garbage_chars: int
    effective_ratio: float
    garbage_ratio: float
    reading_order_score: float
    region_position_score: float
    trustworthy: bool
    reason: str

    def as_dict(self) -> dict:
        return {
            "char_count": self.char_count,
            "effective_chars": self.effective_chars,
            "garbage_chars": self.garbage_chars,
            "effective_ratio": round(self.effective_ratio, 4),
            "garbage_ratio": round(self.garbage_ratio, 4),
            "reading_order_score": round(self.reading_order_score, 4),
            "region_position_score": round(self.region_position_score, 4),
            "trustworthy": self.trustworthy,
            "reason": self.reason,
        }


def _open_document(payload: bytes):
    import pypdfium2 as pdfium

    try:
        return pdfium.PdfDocument(payload)
    except Exception as exc:  # noqa: BLE001 - 供应商异常不外泄
        message = str(exc).lower()
        if "password" in message or "encrypt" in message:
            raise RenderError("pdf_encrypted") from exc
        raise RenderError("pdf_corrupt") from exc


def page_count(payload: bytes) -> int:
    doc = _open_document(payload)
    try:
        return len(doc)
    finally:
        _safe_close(doc)


def _safe_close(doc) -> None:
    try:
        doc.close()
    except Exception:  # noqa: BLE001
        pass


def _effective_dpi(
    *,
    width_pt: float,
    height_pt: float,
    requested_dpi: int,
) -> tuple[int, bool, Optional[str]]:
    """按像素/边长上限计算实际 DPI；超限则降级并返回原因。"""
    if width_pt <= 0 or height_pt <= 0:
        raise RenderError("pdf_corrupt", "non-positive page size")
    scale = requested_dpi / 72.0
    width_px = width_pt * scale
    height_px = height_pt * scale
    reason: Optional[str] = None
    degraded = False
    if width_px * height_px > MAX_PAGE_PIXELS or max(width_px, height_px) > MAX_PAGE_SIDE_PX:
        degraded = True
        reason = "pixel_limit"
        by_pixels = (MAX_PAGE_PIXELS / (width_px * height_px)) ** 0.5
        by_side = MAX_PAGE_SIDE_PX / max(width_px, height_px)
        factor = min(1.0, by_pixels, by_side)
        requested_dpi = max(MIN_DPI, int(requested_dpi * factor))
    return requested_dpi, degraded, reason


def render_page(payload: bytes, page_no: int, *, dpi: int = DEFAULT_DPI) -> RenderResult:
    """渲染单页为 RGB 位图。page_no 为 1-based。"""
    if dpi < MIN_DPI or dpi > MAX_DPI:
        raise RenderError("invalid_dpi", f"dpi out of range [{MIN_DPI}, {MAX_DPI}]")
    doc = _open_document(payload)
    try:
        total = len(doc)
        if page_no < 1 or page_no > total:
            raise RenderError("page_out_of_range", f"page {page_no}/{total}")
        page = doc[page_no - 1]
        width_pt, height_pt = page.get_size()
        effective_dpi, degraded, reason = _effective_dpi(
            width_pt=width_pt, height_pt=height_pt, requested_dpi=dpi
        )
        scale = effective_dpi / 72.0
        bitmap = page.render(scale=scale)
        image = bitmap.to_pil().convert("RGB")
        return RenderResult(
            image=image,
            width_px=image.width,
            height_px=image.height,
            dpi=effective_dpi,
            scale=scale,
            degraded=degraded,
            reason=reason,
        )
    except RenderError:
        raise
    except Exception as exc:  # noqa: BLE001 - 供应商异常不外泄
        raise RenderError("render_failed") from exc
    finally:
        _safe_close(doc)


def extract_text(payload: bytes, page_no: int) -> str:
    """提取可信文本层原文（不做 OCR）。"""
    doc = _open_document(payload)
    try:
        total = len(doc)
        if page_no < 1 or page_no > total:
            raise RenderError("page_out_of_range", f"page {page_no}/{total}")
        page = doc[page_no - 1]
        textpage = page.get_textpage()
        try:
            return textpage.get_text_bounded()
        finally:
            try:
                textpage.close()
            except Exception:  # noqa: BLE001
                pass
    except RenderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RenderError("text_extract_failed") from exc
    finally:
        _safe_close(doc)


def _is_effective(ch: str) -> bool:
    if ch in _GARBAGE_CHARS:
        return False
    if ch in _BOX_DRAWING:
        return False
    cat = unicodedata.category(ch)
    if cat.startswith("L") or cat.startswith("N"):
        return True
    # 常见标点/空格视为中性，不计入有效字符也不计乱码
    return False


def evaluate_text_layer(
    text: str,
    *,
    rects: Optional[list[tuple[float, float, float, float]]] = None,
) -> TextLayerEval:
    """确定性文本可信判定。

    - effective_ratio：有效字符 / 非空白字符；
    - garbage_ratio：乱码字符 / 非空白字符；
    - reading_order_score：文本块按“从上到下、从左到右”排列的相邻一致率；
    - region_position_score：文本块是否落在页面范围内。
    阈值见模块常量；OCR 门禁只依赖 trustworthy 布尔值。
    """
    nonspace = [c for c in text if not c.isspace()]
    char_count = len(nonspace)
    effective = sum(1 for c in nonspace if _is_effective(c))
    garbage = sum(1 for c in nonspace if c in _GARBAGE_CHARS or c in _BOX_DRAWING)
    effective_ratio = effective / char_count if char_count else 0.0
    garbage_ratio = garbage / char_count if char_count else 1.0

    rect_list = list(rects or [])
    reading_score = _reading_order_score(rect_list)
    position_score = 1.0 if rect_list else 0.0

    reasons: list[str] = []
    if char_count < MIN_EFFECTIVE_CHARS:
        reasons.append("too_few_chars")
    if effective_ratio < TRUST_EFFECTIVE_RATIO:
        reasons.append("low_effective_ratio")
    if garbage_ratio > MAX_GARBAGE_RATIO:
        reasons.append("high_garbage_ratio")
    if rect_list and reading_score < MIN_READING_ORDER_SCORE:
        reasons.append("bad_reading_order")

    trustworthy = not reasons
    return TextLayerEval(
        text=text,
        char_count=char_count,
        effective_chars=effective,
        garbage_chars=garbage,
        effective_ratio=effective_ratio,
        garbage_ratio=garbage_ratio,
        reading_order_score=reading_score,
        region_position_score=position_score,
        trustworthy=trustworthy,
        reason="ok" if trustworthy else ",".join(reasons),
    )


def _reading_order_score(rects: list[tuple[float, float, float, float]]) -> float:
    """rects 为 (left, bottom, right, top) 页面坐标；越靠上、越靠左越先读。"""
    if len(rects) < 2:
        return 1.0 if rects else 0.0
    ordered = sorted(rects, key=lambda r: (-r[3], r[0]))
    consistent = 0
    for prev, cur in zip(ordered, ordered[1:]):
        prev_bottom, prev_right = prev[1], prev[2]
        cur_top, cur_left = cur[3], cur[0]
        same_line = abs(cur_top - prev[3]) <= READING_ORDER_TOLERANCE_PT
        if same_line:
            if cur_left >= prev_right - READING_ORDER_TOLERANCE_PT:
                consistent += 1
        elif cur_top <= prev_bottom + READING_ORDER_TOLERANCE_PT:
            consistent += 1
        else:
            # 下一行允许跨行：只要不是明显回跳即视为一致
            consistent += 1
    return consistent / (len(ordered) - 1)


def evaluate_page(payload: bytes, page_no: int) -> TextLayerEval:
    """提取文本层并给出可信判定（rects 从 textpage 读取）。"""
    import pypdfium2 as pdfium

    doc = _open_document(payload)
    try:
        total = len(doc)
        if page_no < 1 or page_no > total:
            raise RenderError("page_out_of_range", f"page {page_no}/{total}")
        page = doc[page_no - 1]
        textpage = page.get_textpage()
        try:
            text = textpage.get_text_bounded()
            rects: list[tuple[float, float, float, float]] = []
            try:
                for i in range(textpage.count_rects()):
                    rects.append(tuple(textpage.get_rect(i)))
            except Exception:  # noqa: BLE001 - rect 读取失败不影响文本判定
                rects = []
        finally:
            try:
                textpage.close()
            except Exception:  # noqa: BLE001
                pass
        return evaluate_text_layer(text, rects=rects)
    except RenderError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise RenderError("text_extract_failed") from exc
    finally:
        _safe_close(doc)


def encode_png(image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()
