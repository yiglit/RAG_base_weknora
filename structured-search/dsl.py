"""把用户问句解析成结构化条件 DSL（不依赖大模型，纯规则）。

只输出白名单字段，值统一成参数库里真实存在的写法，配合 query.py 走 SQLite。
"""
import re

UNIT_FIELDS = ("voltage", "current", "enclosure", "ex_marking")
TEXT_FIELDS = ("institution", "product_name", "cert_no")

_GROUP_RE = re.compile(r"(?:^|[^A-Za-z])(Ⅱ|II|Ⅲ|III)\s*([ABC])(?![A-Za-z])", re.I)
_BARE_GROUP_RE = re.compile(r"(?:^|[^A-Za-z])(Ⅲ|III)(?![A-Za-z])")
_VOLT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[Vv](?![A-Za-z])")
_CURRENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:[Aa](?![A-Za-z])|安)")
_IP_RE = re.compile(r"(IP\s*\d{2})", re.I)
_CERT_RE = re.compile(r"\b([A-Z]{2,6}\d{2}[.\-]\d{3,6})\b")
_PRODUCT_WORDS = ("配电箱", "照明", "灯具", "控制箱", "操作柱", "开关", "插座")
_MATERIAL_WORDS = ("全塑", "不锈钢", "铝合金", "塑料", "钢")


def _roman_group(raw: str, letter: str) -> str:
    roman = raw.upper().replace("Ⅱ", "II").replace("Ⅲ", "III")
    numeral = "Ⅲ" if roman == "III" else "Ⅱ"
    return f"{numeral}{letter.upper()}"


def question_to_dsl(question: str) -> dict:
    """返回 {field: {"op": ..., "value": ...}}；解析不到返回 {}。"""
    q = question or ""
    dsl: dict = {}

    group = _GROUP_RE.search(q)
    if group:
        dsl["ex_marking"] = {"op": "contains", "value": _roman_group(group.group(1), group.group(2))}
    else:
        bare = _BARE_GROUP_RE.search(q)
        if bare:
            dsl["ex_marking"] = {"op": "contains", "value": "Ⅲ"}

    voltage = _VOLT_RE.search(q)
    if voltage:
        dsl["voltage"] = {"op": "contains", "value": f"{voltage.group(1)}V"}

    current = _CURRENT_RE.search(q)
    if current:
        dsl["current"] = {"op": "contains", "value": f"{current.group(1)}A"}

    ip = _IP_RE.search(q)
    if ip:
        dsl["enclosure"] = {"op": "contains", "value": re.sub(r"\s+", "", ip.group(1)).upper()}
    else:
        for word in _MATERIAL_WORDS:
            if word in q:
                dsl["enclosure"] = {"op": "contains", "value": word}
                break

    cert = _CERT_RE.search(q)
    if cert:
        dsl["cert_no"] = {"op": "contains", "value": cert.group(1)}

    for word in _PRODUCT_WORDS:
        if word in q:
            dsl["product_name"] = {"op": "contains", "value": word}
            break

    if "天津" in q:
        dsl["institution"] = {"op": "contains", "value": "天津"}

    return dsl
