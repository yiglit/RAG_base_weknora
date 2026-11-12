"""回答出口的出处校验。输入是检索引用 + 分块原文索引。

对应《新手实施手册》13.2 / 13.3：
- 结论无证书编号            -> 删除该条
- 有编号但 quote 不在原文中 -> 视为幻觉，删除
- quote 存在但 page 缺失    -> 降级为“仅文本依据”，保留并标记
- 全部结论都不合格          -> 回“未在库中找到可靠依据，无法作答。”
"""
import re
from dataclasses import dataclass, field

FALLBACK_ANSWER = "未在库中找到可靠依据，无法作答。"

# 摄入修正会把 ∥B / IIC / T95C 之类 OCR 变体改成 ⅡB / ⅡC / T95℃；
# 引用侧的 quote 可能仍保留旧写法，比对前统一到同一形态。
_SYMBOL_RULES = (
    (re.compile(r"(?:∥|‖|\|\|)\s*([ABC])"), r"Ⅱ\1"),
    (re.compile(r"III\s*([ABC])", re.I), r"Ⅲ\1"),
    (re.compile(r"II\s*([ABC])", re.I), r"Ⅱ\1"),
    (re.compile(r"(Ⅱ|Ⅲ)\s*([ABC])"), r"\1\2"),
    (re.compile(r"T\s*(\d{2,3})\s*[C℃]"), r"T\1℃"),
    (re.compile(r"(\d{2,3})\s*(?:°\s*C|[oO]\s*C)"), r"\1℃"),
    (re.compile(r"(\d)\s*m\s*[2²]"), r"\1㎡"),
)


def normalize_symbols(text: str) -> str:
    out = text or ""
    for pattern, repl in _SYMBOL_RULES:
        out = pattern.sub(repl, out)
    return out


@dataclass
class CheckResult:
    kept: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    degraded: list = field(default_factory=list)


def _squash(text: str) -> str:
    """去掉所有空白，并把 OCR 特殊符号变体归一，用于宽松比对。"""
    return re.sub(r"\s+", "", normalize_symbols(text))


def match_quote(quote: str, chunk_text: str) -> str | None:
    """返回 exact / normalized / None。

    OCR 分块里常有换行、空格差异，原文引用不一定逐字节相等；
    因此先精确匹配，失败再去掉空白比对。两者都不中才算幻觉。
    """
    if not quote or not chunk_text:
        return None
    if quote in chunk_text:
        return "exact"
    if _squash(quote) and _squash(quote) in _squash(chunk_text):
        return "normalized"
    return None


def build_text_index(chunks: list) -> dict[str, str]:
    """chunk_id -> content，用于验证 quote 是否真的存在。"""
    return {
        (c.get("id") or c.get("chunk_id")): (c.get("content") or "")
        for c in chunks
    }


def validate_references(refs: list, text_index: dict[str, str]) -> CheckResult:
    result = CheckResult()
    for ref in refs:
        cert_no = ref.get("cert_no")
        quote = ref.get("quote")
        page = ref.get("page")
        chunk_id = ref.get("chunk_id")
        chunk_text = text_index.get(chunk_id, "")

        quote_match = match_quote(quote, chunk_text)
        cert_ok = bool(cert_no)

        if not cert_ok:
            result.dropped.append({**ref, "reason": "no_cert_no"})
        elif not quote_match:
            result.dropped.append({**ref, "reason": "quote_not_found"})
        elif page is None:
            # 降级：正文可核对，但缺页码，保留并标记
            result.degraded.append({**ref, "reason": "no_page"})
            result.kept.append({**ref, "reason": "degraded_text_only", "quote_match": quote_match})
        else:
            result.kept.append({**ref, "reason": "ok", "quote_match": quote_match})
    return result


def final_answer(answer: str, result: CheckResult) -> str:
    if not result.kept:
        return FALLBACK_ANSWER
    if result.dropped:
        answer += f"\n\n（已剔除 {len(result.dropped)} 条无可靠出处的结论）"
    if result.degraded:
        answer += f"\n（{len(result.degraded)} 条结论仅有文本依据，缺少页码）"
    return answer
