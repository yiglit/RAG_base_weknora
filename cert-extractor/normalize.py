import re

CERT_NO_RE = re.compile(r"^[A-Za-z]{1,5}\d{2,4}([.\-]?\d{2,4})?$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EX_RE = re.compile(r"^Ex\s+\S")

def check_format(name: str, value: str | None) -> bool | None:
    """格式校验。True=合法，False=违规，None=该字段无强格式约束。"""
    if not value:
        return False
    if name == "cert_no":
        return bool(CERT_NO_RE.match(value))
    if name in ("issue_date", "valid_until"):
        return bool(DATE_RE.match(value))
    if name == "ex_marking":
        return bool(EX_RE.match(value))
    if name == "voltage":
        return bool(re.fullmatch(r"\d+(\.\d+)?\s?V", value))
    if name == "current":
        return bool(re.fullmatch(r"\d+(\.\d+)?\s?(A|mA)", value))
    return None  # 没有强格式的字段不扣分
def score_field(model_conf: float, value: str, quote: str, full_text: str, fmt_ok: bool) -> float:
    score = 0.5 * (model_conf or 0)
    if value and quote and value in quote:
        score += 0.30
    if quote and quote in full_text:
        score += 0.20
    if fmt_ok:
        score += 0.25
    return round(min(score, 1.0), 3)