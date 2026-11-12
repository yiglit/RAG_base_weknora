"""把 DSL 拼成参数化 SQL，查业务库。禁止字符串拼接值。"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "cert-registry" / "registry.db"

FIELD_MAP = {
    "cert_no": "c.cert_no",
    "institution": "c.institution",
    "product_name": "p.name",
    "model_spec": "c.model_spec",
    "ex_marking": "c.ex_marking",
    "valid_until": "c.valid_until",
    "voltage": "p.voltage",
    "current": "p.current",
    "enclosure": "p.enclosure",
}
OP_MAP = {"eq": "=", "ne": "!=", "gte": ">=", "lte": "<="}


def build_sql(dsl: dict) -> tuple[str, list]:
    where, params = [], []
    for field, spec in dsl.items():
        column = FIELD_MAP.get(field)
        if not column:
            continue
        op = spec.get("op")
        value = str(spec.get("value"))
        if op == "contains":
            if field in ("voltage", "current"):
                # 多值字段按逗号分隔精确匹配，避免 60A 误命中 160A。
                where.append(f"(',' || REPLACE({column}, ' ', '') || ',') LIKE ?")
                params.append(f"%,{value.replace(' ', '')},%")
            else:
                where.append(f"{column} LIKE ?")
                params.append(f"%{value}%")
        elif op in OP_MAP:
            where.append(f"{column} {OP_MAP[op]} ?")
            params.append(value)
    sql = (
        "SELECT DISTINCT c.cert_no, c.institution, c.product_name, c.model_spec, "
        "p.voltage, p.current, p.enclosure, c.valid_until "
        "FROM certificate c LEFT JOIN product p ON p.cert_id = c.id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    return sql, params


def search(dsl: dict) -> list[dict]:
    sql, params = build_sql(dsl)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def format_rows(rows: list[dict]) -> str:
    if not rows:
        return "没有符合条件的结果"
    lines = [f"符合条件 {len(rows)} 条："]
    for i, r in enumerate(rows, 1):
        product = r.get("product_name") or "（未抽取）"
        model = r.get("model_spec") or "（未抽取）"
        voltage = r.get("voltage") or "（未抽取）"
        current = r.get("current") or "（未抽取）"
        enclosure = r.get("enclosure") or "（未抽取）"
        valid = r.get("valid_until") or "（未抽取）"
        lines.append(f"[{i}] 证书编号 {r.get('cert_no')}｜{r.get('institution')}")
        lines.append(f"    产品：{product} / {model}")
        lines.append(f"    参数：电压 {voltage}；电流 {current}；外壳 {enclosure}")
        lines.append(f"    有效期至：{valid}")
    return "\n".join(lines)


if __name__ == "__main__":
    dsl = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {"current": {"op": "eq", "value": "60A"}}
    print(build_sql(dsl))
    print(format_rows(search(dsl)))
