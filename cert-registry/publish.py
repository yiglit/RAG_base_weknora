"""⑦ 人工校验通过后写入参数库（registry.db），不再写 WeKnora 知识库。

链路位置：④ 抽取 JSON → ⑤ 校验 → ⑥ 人工确认 → **⑦ 写入参数库**

条件检索（structured-search / cert-extractor 的 structured_search 工具）直接读
registry.db，所以知识库里不再需要「参数卡」文档；问答侧用知识库检索原文 +
参数库做条件过滤，两路结果在回答里合并。

用法：
    python cert-registry/publish.py <fields.json> <source_file>
    python cert-registry/publish.py <fields.json> <source_file> --dry-run

退出码：0 成功；2 缺唯一键。
"""
import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
DB = ROOT / "registry.db"

sys.path.insert(0, str(ROOT))
from normalize import normalize_fields  # noqa: E402
from upsert import list_of, unwrap, upsert_certificate, value_of  # noqa: E402

FIELD_LABELS = {
    "cert_no": "证书编号",
    "institution": "发证机构",
    "manufacturer": "制造商",
    "product_name": "产品名称",
    "model_spec": "型号规格",
    "ex_marking": "防爆标志",
    "standard": "依据标准",
    "drawing_no": "图纸编号",
    "issue_date": "发证日期",
    "valid_until": "有效期至",
    "voltage": "额定电压",
    "current": "额定电流",
    "enclosure": "防护等级/材质",
}

IDENTITY_FIELDS = ("cert_no", "institution", "manufacturer", "product_name")
TECH_FIELDS = ("model_spec", "ex_marking", "standard", "drawing_no",
               "issue_date", "valid_until", "voltage", "current", "enclosure")
BUSINESS_FIELDS = (*IDENTITY_FIELDS, *TECH_FIELDS)


def _squash(text: str) -> str:
    """去空白 + 全角括号归半角，仅用于唯一键比对，不改写入的值。"""
    return (
        (text or "")
        .replace("（", "(")
        .replace("）", ")")
        .replace("　", "")
        .replace(" ", "")
        .strip()
        .lower()
    )


def _align_institution(conn: sqlite3.Connection, fields: dict, cert_no: str, inst: str) -> str:
    """库里已有同一证书、机构写法仅全/半角不同时，沿用库里的写法，避免重复行。"""
    rows = conn.execute(
        "SELECT institution FROM certificate WHERE cert_no=?", (cert_no,)
    ).fetchall()
    for (stored,) in rows:
        if _squash(stored) == _squash(inst):
            item = fields.get("institution")
            if isinstance(item, dict):
                item["value"] = stored
            return stored
    return inst


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _cmp_value(name: str, value) -> str:
    """折成可比较形式：压缩空白；电压/电流忽略分隔符差异。"""
    text = " ".join(_text(value).split())
    if name in ("voltage", "current"):
        parts = [part.strip() for part in re.split(r"[,、;；]", text) if part.strip()]
        return "|".join(parts)
    return text


def _incoming_values(fields: dict, cert_no: str, inst: str) -> dict:
    values = {"cert_no": cert_no, "institution": inst}
    for name in BUSINESS_FIELDS[2:]:
        if name in ("voltage", "current"):
            values[name] = ",".join(list_of(fields, name))
        else:
            values[name] = _text(value_of(fields, name))
    return values


def _existing_certificate(conn: sqlite3.Connection, cert_no: str, inst: str):
    """返回 (库内 13 个业务值, param_card_knowledge_id)；无行返回 None。"""
    row = conn.execute(
        "SELECT c.cert_no, c.institution, c.manufacturer, c.product_name, c.model_spec, "
        "c.ex_marking, c.standard, c.drawing_no, c.issue_date, c.valid_until, "
        "p.voltage, p.current, p.enclosure, c.param_card_knowledge_id "
        "FROM certificate c LEFT JOIN product p ON p.cert_id = c.id "
        "WHERE c.cert_no=? AND c.institution=?",
        (cert_no, inst),
    ).fetchone()
    if row is None:
        return None
    stored = {name: row[i] for i, name in enumerate(BUSINESS_FIELDS)}
    return stored, _text(row[len(BUSINESS_FIELDS)])


def _same_values(stored: dict, incoming: dict) -> bool:
    return all(
        _cmp_value(name, incoming.get(name)) == _cmp_value(name, stored.get(name))
        for name in BUSINESS_FIELDS
    )


def _multi_value(fields: dict, name: str) -> str:
    item = fields.get(name) or {}
    if not isinstance(item, dict):
        return ""
    values = item.get("values")
    if isinstance(values, list) and values:
        return "、".join(_text(v) for v in values if _text(v))
    return _text(item.get("value"))


def card_title(fields: dict) -> str:
    cert_no = _text(value_of(fields, "cert_no")) or "未知证书"
    product = _text(value_of(fields, "product_name"))
    title = f"参数卡｜{cert_no}"
    return f"{title}｜{product}" if product else title


def build_card_markdown(fields: dict, source_file: str = "") -> str:
    """把 13 个字段拼成「人可读 + 可检索 + 可机读」的参数卡。"""
    inbox = unwrap(fields)
    title = card_title(inbox)

    lines = [
        f"# {title}",
        "",
        f"> 由防爆合格证抽取链路生成，来源文件：{source_file or '（未记录）'}。",
        "> 数值已做格式归一化；原文片段保留在「原文证据」一节，可用于复核。",
        "",
        "## 标识信息",
        "",
        "| 字段 | 值 |",
        "| --- | --- |",
    ]
    for name in IDENTITY_FIELDS:
        lines.append(f"| {FIELD_LABELS[name]} | {_multi_value(inbox, name) or '（未抽取）'} |")

    lines += ["", "## 技术参数", "", "| 字段 | 值 |", "| --- | --- |"]
    for name in TECH_FIELDS:
        lines.append(f"| {FIELD_LABELS[name]} | {_multi_value(inbox, name) or '（未抽取）'} |")

    lines += ["", "## 原文证据", ""]
    evidence = 0
    for name in [*IDENTITY_FIELDS, *TECH_FIELDS]:
        item = inbox.get(name)
        if not isinstance(item, dict):
            continue
        quote = _text(item.get("quote"))
        if not quote:
            continue
        evidence += 1
        page = item.get("page")
        where = f"第{page}页" if page is not None else "无页码"
        conf = item.get("confidence")
        conf_text = f"，置信度 {conf:.3f}" if isinstance(conf, (int, float)) else ""
        lines.append(f"- **{FIELD_LABELS[name]}**（{where}{conf_text}）：\"{quote}\"")
    if evidence == 0:
        lines.append("- （本次未记录可复核的原文片段）")

    lines += [
        "",
        "## 机器可读 JSON",
        "",
        "```json",
        json.dumps(inbox, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


def _bind_source_knowledge(
    conn: sqlite3.Connection, cert_no: str, inst: str, knowledge_id: str
) -> None:
    """记录原件知识 ID，供出处校验把 knowledge_id 映射回证书编号。"""
    if not knowledge_id or len(knowledge_id) != 36 or knowledge_id.count("-") != 4:
        return
    conn.execute(
        "UPDATE certificate SET weknora_knowledge_id=? WHERE cert_no=? AND institution=?",
        (knowledge_id, cert_no, inst),
    )


def publish(
    payload: dict,
    source_file: str,
    dry_run: bool = False,
    source_knowledge_id: str = "",
) -> dict:
    """幂等写入参数库（registry.db），不再写 WeKnora 知识库。"""
    # 兼容三种输入：MCP 原始 {ok, fields}、直接字段表、collect.py 落盘的 {result:{...}}
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        payload = payload["result"]
    fields = unwrap(payload)
    normalize_fields(fields)
    cert_no = _text(value_of(fields, "cert_no"))
    inst = _text(value_of(fields, "institution"))
    if not cert_no or not inst:
        raise ValueError("缺少唯一键 cert_no / institution，拒绝入库")
    # 原件知识 ID 用于 ⑩ 出处校验把 chunk.knowledge_id 映射回证书编号。
    source_knowledge_id = _text(source_knowledge_id)

    conn = sqlite3.connect(DB)
    try:
        inst = _align_institution(conn, fields, cert_no, inst)
        markdown = build_card_markdown(fields, source_file)
        title = card_title(fields)

        result = {
            "cert_no": cert_no,
            "institution": inst,
            "title": title,
            "markdown_chars": len(markdown),
            "dry_run": dry_run,
        }
        if dry_run:
            result["mode"] = "dry-run"
            result["markdown"] = markdown
            return result

        existing = _existing_certificate(conn, cert_no, inst)
        if existing:
            stored, _ = existing
            incoming = _incoming_values(fields, cert_no, inst)
            if _same_values(stored, incoming):
                _bind_source_knowledge(conn, cert_no, inst, source_knowledge_id)
                conn.execute(
                    "INSERT INTO import_audit (cert_no,institution,action,source_file) "
                    "VALUES (?,?,?,?)",
                    (cert_no, inst, "publish_skipped", source_file),
                )
                conn.commit()
                result.update({"mode": "skipped", "db_action": "unchanged"})
                return result

        action = upsert_certificate(conn, {"fields": fields}, source_file)
        _bind_source_knowledge(conn, cert_no, inst, source_knowledge_id)
        conn.execute(
            "INSERT INTO import_audit (cert_no,institution,action,source_file) VALUES (?,?,?,?)",
            (cert_no, inst, "publish_registry", source_file),
        )
        conn.commit()
    finally:
        conn.close()

    result.update({"mode": "registry_only", "db_action": action})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="人工校验结果写入参数库（不写知识库）")
    parser.add_argument("fields_json", help="MCP 抽取结果 JSON（{ok, fields:{...}} 或直接字段表）")
    parser.add_argument("source_file", help="来源文件名，用于审计")
    parser.add_argument("--dry-run", action="store_true", help="只打印参数卡，不写库")
    args = parser.parse_args()

    payload = json.loads(Path(args.fields_json).read_text(encoding="utf-8"))
    try:
        result = publish(payload, args.source_file, dry_run=args.dry_run)
    except ValueError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        sys.exit(2)

    if args.dry_run:
        print(result["markdown"])
        print(f"\n[dry-run] {result['markdown_chars']} 字符，未写库", file=sys.stderr)
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
