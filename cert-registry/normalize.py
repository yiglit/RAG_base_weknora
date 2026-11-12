"""业务库入库前的保护性归一化：只规范格式，不改写原文。

规则（实施手册 11.5）：
- 日期统一 YYYY-MM-DD，原值保留在 raw_value
- 电压/电流拆成多值数组（220/380V -> ["220V", "380V"]）
- 防爆标志只统一空格，不改大小写
- Ⅱ/Ⅲ/℃/□/㎡/≤/≥ 白名单符号原样保留，只回正明确的 OCR 变体
- 机构按别名表归一到规范名
"""
import re

DATE_FIELDS = ("issue_date", "valid_until")

# 规范名 -> 别名（大小写不敏感，比较时去掉空格）
INSTITUTION_ALIASES = {
    "CQ": ("CQ", "南阳防爆所"),
}

_AREA_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:m2|M2|m²|㎡)")
_TEMP_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:°\s*C|o\s*C|0\s*C|℃)")


def normalize_symbols(text: str) -> str:
    """把明确的 OCR 变体回正成白名单符号，其余原样保留。"""
    if not text:
        return text
    text = text.replace("II C", "ⅡC").replace("IIB", "ⅡB").replace("IIA", "ⅡA")
    text = _TEMP_RE.sub(r"\1℃", text)
    text = _AREA_RE.sub(r"\1㎡", text)
    text = text.replace("<=", "≤").replace(">=", "≥")
    return text


def normalize_date(raw: str) -> str | None:
    """把 2024年5月20日 / 2024.5.20 / 2024/5/20 统一成 2024-05-20。"""
    if not raw:
        return None
    m = re.search(r"(\d{4})\s*[年.\-/]\s*(\d{1,2})\s*[月.\-/]\s*(\d{1,2})", raw)
    if not m:
        return None
    y, mo, d = m.groups()
    try:
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    except ValueError:
        return None


def split_values(raw: str, unit: str) -> list[str]:
    """220/380V -> ['220V', '380V']；60 A / 60安 -> ['60A']。只拆分母单位，不改数值。"""
    if not raw:
        return []
    raw = raw.replace("毫安", "mA").replace("安", "A")
    out = []
    for part in re.split(r"[/、,，;；]", raw):
        token = part.strip().replace(" ", "")
        if not token:
            continue
        if token.lower().endswith(unit.lower()):
            token = token[: -len(unit)] + unit
        else:
            token += unit
        out.append(token)
    return out


def normalize_ex_marking(value: str) -> str:
    """只统一空格，禁止改大小写。"""
    if not value:
        return value
    return " ".join(value.split())


def normalize_institution(value: str) -> str:
    """按别名表归一到规范名；未命中则原样返回。"""
    if not value:
        return value
    key = re.sub(r"\s+", "", value).lower()
    for canonical, aliases in INSTITUTION_ALIASES.items():
        if key in {re.sub(r"\s+", "", a).lower() for a in aliases}:
            return canonical
    return value


def normalize_fields(fields: dict) -> dict:
    """原地归一化字段值。只动 value/values/raw_value，不动 quote（证据必须与原文一致）。"""
    for name, item in fields.items():
        if not isinstance(item, dict):
            continue

        if name == "ex_marking":
            if isinstance(item.get("value"), str):
                item["value"] = normalize_ex_marking(item["value"])
            if isinstance(item.get("values"), list):
                item["values"] = [normalize_ex_marking(v) for v in item["values"]]
            continue

        if isinstance(item.get("value"), str):
            if name == "institution":
                item["value"] = normalize_institution(item["value"])
            else:
                item["value"] = normalize_symbols(item["value"])

        if isinstance(item.get("values"), list):
            item["values"] = [
                normalize_symbols(v) if isinstance(v, str) else v for v in item["values"]
            ]

        if name in DATE_FIELDS and item.get("value"):
            raw = item.get("raw_value") or item["value"]
            item["raw_value"] = raw
            normalized = normalize_date(raw)
            if normalized:
                item["value"] = normalized

        if name in ("voltage", "current"):
            unit = "V" if name == "voltage" else "A"
            raw = item.get("raw_value") or item.get("value")
            if isinstance(raw, list):
                raw = "/".join(str(v) for v in raw)
            item["values"] = split_values(raw or "", unit)

    return fields
