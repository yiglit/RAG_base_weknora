"""⑦ 参数卡幂等入库，并回写 WeKnora 知识库。

链路位置：④ 抽取 JSON → ⑤ 校验 → ⑥ 人工确认 → **⑦ 入库 + 同步知识库**

为什么新建独立的 manual 文档，而不是改原 PDF：
    PUT /api/v1/knowledge/manual/{id} 只允许编辑 type=manual 的知识，
    原始 PDF 是 type=file，直接 PUT 会被 400 拒绝。所以「参数卡」作为
    同一知识库里的独立文档存在，幂等键记录在 certificate.weknora_knowledge_id。

用法：
    python cert-registry/publish.py <fields.json> <source_file>
    python cert-registry/publish.py <fields.json> <source_file> --kb <kb_id>
    python cert-registry/publish.py <fields.json> <source_file> --dry-run

环境变量（缺省回退仓库根目录 .env）：
    WEKNORA_BASE     默认 http://127.0.0.1
    WEKNORA_API_KEY  必填
    WEKNORA_KB_ID    默认知识库（未传 --kb 时使用）

退出码：0 成功；2 缺唯一键；3 API 失败。
"""
import argparse
import json
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
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


class PublishError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


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


def _load_env() -> tuple[str, str, str]:
    base = ""
    key = ""
    kb = ""
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if name.strip() == "WEKNORA_BASE":
                base = value
            elif name.strip() == "WEKNORA_API_KEY":
                key = value
            elif name.strip() == "WEKNORA_KB_ID":
                kb = value
    import os
    return (
        os.getenv("WEKNORA_BASE") or base or "http://127.0.0.1",
        os.getenv("WEKNORA_API_KEY") or key,
        os.getenv("WEKNORA_KB_ID") or kb,
    )


def _request(method: str, path: str, payload: dict | None = None, timeout: int = 180) -> dict:
    """调用 WeKnora 管理 API，返回 data 段；失败抛 PublishError。"""
    base, key, _ = _load_env()
    if not key:
        raise PublishError(0, "未配置 WEKNORA_API_KEY（环境变量或仓库根目录 .env）")
    body = None
    headers = {"X-API-Key": key}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(
        f"{base.rstrip('/')}/api/v1{path}", data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
    except urllib.error.HTTPError as exc:
        raise PublishError(exc.code, exc.read().decode("utf-8", "replace")) from exc
    except urllib.error.URLError as exc:
        raise PublishError(0, f"连接失败：{exc.reason}") from exc

    data = json.loads(raw)
    if isinstance(data, dict) and data.get("success") is False:
        raise PublishError(200, json.dumps(data, ensure_ascii=False))
    return data.get("data", data) if isinstance(data, dict) else data


def _as_list(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for name in ("items", "list", "records", "knowledge"):
            value = data.get(name)
            if isinstance(value, list):
                return value
    return []


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


def _find_manual_knowledge(kb_id: str, title: str) -> str:
    """按标题在本 KB 里找已有的 manual 文档，作为 knowledge_id 写库失败时的兜底。"""
    try:
        deps = _request(
            "GET",
            f"/knowledge-bases/{kb_id}/knowledge?" + urllib.parse.urlencode(
                {"page": 1, "page_size": 100, "keyword": title}
            ),
        )
    except PublishError:
        return ""
    for item in _as_list(deps):
        if not isinstance(item, dict):
            continue
        if _text(item.get("title")) != title:
            continue
        if _text(item.get("type")).lower() != "manual":
            continue
        kid = _text(item.get("id"))
        if kid:
            return kid
    return ""


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
    kb_id: str | None = None,
    dry_run: bool = False,
    source_knowledge_id: str = "",
) -> dict:
    """幂等入库 + 回写知识库。返回本次动作与 knowledge_id。"""
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
        _, _, default_kb = _load_env()
        kb_id = (kb_id or default_kb).strip()
        if not dry_run and not kb_id:
            raise ValueError("缺少知识库 ID（--kb 或环境变量 WEKNORA_KB_ID）")

        result = {
            "cert_no": cert_no,
            "institution": inst,
            "title": title,
            "knowledge_id": "",
            "kb_id": kb_id,
            "markdown_chars": len(markdown),
            "dry_run": dry_run,
        }
        if dry_run:
            result["mode"] = "dry-run"
            result["markdown"] = markdown
            return result

        existing = _existing_certificate(conn, cert_no, inst)
        if existing:
            stored, existing_kid = existing
            incoming = _incoming_values(fields, cert_no, inst)
            if existing_kid and _same_values(stored, incoming):
                _bind_source_knowledge(conn, cert_no, inst, source_knowledge_id)
                conn.execute(
                    "INSERT INTO import_audit (cert_no,institution,action,source_file) "
                    "VALUES (?,?,?,?)",
                    (cert_no, inst, "publish_skipped", source_file),
                )
                conn.commit()
                result.update({
                    "mode": "skipped",
                    "db_action": "unchanged",
                    "knowledge_id": existing_kid,
                })
                return result

        action = upsert_certificate(conn, {"fields": fields}, source_file)
        row = conn.execute(
            "SELECT param_card_knowledge_id FROM certificate "
            "WHERE cert_no=? AND institution=?",
            (cert_no, inst),
        ).fetchone()
        kid = _text(row[0]) if row else ""

        item_payload = {"title": title, "content": markdown, "status": "publish"}
        mode = "created"
        if kid:
            try:
                _request("PUT", f"/knowledge/manual/{kid}", item_payload)
                mode = "updated"
            except PublishError as exc:
                # 400：历史值指向了非 manual 文档；404：文档已被删除。都回落到重建。
                if exc.status not in (400, 404):
                    raise
                kid = ""

        if not kid:
            # 写库失败/首次发布：先按标题查重，避免重复文档
            kid = _find_manual_knowledge(kb_id, title)
            if kid:
                _request("PUT", f"/knowledge/manual/{kid}", item_payload)
                mode = "updated"
            else:
                created = _request(
                    "POST", f"/knowledge-bases/{kb_id}/knowledge/manual", item_payload
                )
                if not isinstance(created, dict) or not _text(created.get("id")):
                    raise PublishError(200, f"创建成功但未返回 id：{created}")
                kid = _text(created.get("id"))
                mode = "created"

        conn.execute(
            "UPDATE certificate SET param_card_knowledge_id=?, weknora_kb_id=?, "
            "updated_at=datetime('now') WHERE cert_no=? AND institution=?",
            (kid, kb_id, cert_no, inst),
        )
        _bind_source_knowledge(conn, cert_no, inst, source_knowledge_id)
        conn.execute(
            "INSERT INTO import_audit (cert_no,institution,action,source_file) VALUES (?,?,?,?)",
            (cert_no, inst, f"publish_{mode}", source_file),
        )
        conn.commit()
    finally:
        conn.close()

    result.update({"mode": mode, "knowledge_id": kid, "db_action": action})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="参数卡入库并同步到 WeKnora 知识库")
    parser.add_argument("fields_json", help="MCP 抽取结果 JSON（{ok, fields:{...}} 或直接字段表）")
    parser.add_argument("source_file", help="来源文件名，用于审计")
    parser.add_argument("--kb", default="", help="知识库 ID，默认读 WEKNORA_KB_ID")
    parser.add_argument("--dry-run", action="store_true", help="只打印参数卡，不写库不调 API")
    args = parser.parse_args()

    payload = json.loads(Path(args.fields_json).read_text(encoding="utf-8"))
    try:
        result = publish(payload, args.source_file, kb_id=args.kb, dry_run=args.dry_run)
    except ValueError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        sys.exit(2)
    except PublishError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        sys.exit(3)

    if args.dry_run:
        print(result["markdown"])
        print(f"\n[dry-run] {result['markdown_chars']} 字符，未写库", file=sys.stderr)
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
