"""按 (cert_no, institution) 幂等入库，并写审计。

用法：
  python cert-registry/upsert.py tmp/fields_S01.json samples/CQ_型式A_001.pdf
"""
import json
import sqlite3
import sys
from pathlib import Path

from normalize import normalize_fields

ROOT = Path(__file__).resolve().parent
DB = ROOT / "registry.db"


def unwrap(fields: dict) -> dict:
    """兼容 {ok, fields} 和直接 {cert_no: {...}} 两种输入。"""
    return fields.get("fields", fields)


def value_of(fields: dict, name: str):
    item = fields.get(name) or {}
    if not isinstance(item, dict):
        return None
    if item.get("value") is not None:
        return item.get("value")
    values = item.get("values")
    if values:
        return "、".join(str(v) for v in values)
    return None


def list_of(fields: dict, name: str) -> list[str]:
    item = fields.get(name) or {}
    if not isinstance(item, dict):
        return []
    values = item.get("values")
    if values:
        return [str(v) for v in values]
    value = item.get("value")
    return [str(value)] if value else []


def upsert_certificate(conn: sqlite3.Connection, payload: dict, source_file: str) -> str:
    fields = unwrap(payload)
    normalize_fields(fields)
    cert_no = value_of(fields, "cert_no")
    inst = value_of(fields, "institution")
    if not cert_no or not inst:
        raise ValueError("缺少唯一键 cert_no / institution，拒绝入库")

    row = conn.execute(
        "SELECT id FROM certificate WHERE cert_no=? AND institution=?", (cert_no, inst)
    ).fetchone()

    values = {name: value_of(fields, name) for name in fields}
    raw = json.dumps(fields, ensure_ascii=False)
    conf = min(
        (
            item.get("confidence", 1)
            for item in fields.values()
            if isinstance(item, dict) and isinstance(item.get("confidence"), (int, float))
        ),
        default=1,
    )

    if row is None:
        cur = conn.execute(
            """INSERT INTO certificate
               (cert_no,institution,manufacturer,product_name,model_spec,ex_marking,standard,
                drawing_no,issue_date,valid_until,raw_json,confidence_min)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                cert_no, inst,
                values.get("manufacturer"), values.get("product_name"),
                values.get("model_spec"), values.get("ex_marking"),
                values.get("standard"), values.get("drawing_no"),
                values.get("issue_date"), values.get("valid_until"),
                raw, conf,
            ),
        )
        cert_id = cur.lastrowid
        action = "inserted"
    else:
        cert_id = row[0]
        conn.execute(
            """UPDATE certificate SET manufacturer=?, product_name=?, model_spec=?,
               ex_marking=?, standard=?, drawing_no=?, issue_date=?, valid_until=?,
               raw_json=?, confidence_min=?, updated_at=datetime('now')
               WHERE id=?""",
            (
                values.get("manufacturer"), values.get("product_name"),
                values.get("model_spec"), values.get("ex_marking"),
                values.get("standard"), values.get("drawing_no"),
                values.get("issue_date"), values.get("valid_until"),
                raw, conf, cert_id,
            ),
        )
        action = "updated"

    # 产品参数（简化：先删后插，后续可改成字段级 diff）
    conn.execute("DELETE FROM product WHERE cert_id=?", (cert_id,))
    conn.execute(
        "INSERT INTO product (cert_id,name,model,voltage,current,enclosure) VALUES (?,?,?,?,?,?)",
        (
            cert_id,
            values.get("product_name"),
            values.get("model_spec"),
            ",".join(list_of(fields, "voltage")),
            ",".join(list_of(fields, "current")),
            values.get("enclosure"),
        ),
    )

    conn.execute(
        "INSERT INTO import_audit (cert_no,institution,action,source_file) VALUES (?,?,?,?)",
        (cert_no, inst, action, source_file),
    )
    conn.commit()
    return action


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("用法: python cert-registry/upsert.py <fields.json> <source_file>")
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    conn = sqlite3.connect(DB)
    print(upsert_certificate(conn, payload, sys.argv[2]))
    conn.close()
