"""⑩ 出处强制校验边车：把 validator.py 的规则挂到真实问答链路出口。

两个接口：
    POST /validate  纯校验。输入 answer + refs（或 items），输出保留/剔除/降级与守卫后的答案。
    POST /ask       完整 ⑧⑨⑩：知识检索 -> 知识问答(SSE) -> 出处校验。
    GET  /health    存活探针。

为什么是边车而不是改 Go：
    运行中的 app 是预构建镜像 wechatopenai/weknora-app:v0.9.0，本地源码无法热更新。
    这里在 API 边缘做强制校验；Go 内嵌钩子点见 docs/链路贯通与手动验证_2026-11-11.md。

启动：
    python citation-validator/server.py
    # 默认 127.0.0.1:8780，可用 VALIDATOR_PORT 覆盖

环境变量：
    WEKNORA_BASE      默认 http://127.0.0.1
    WEKNORA_API_KEY   必填（本文件读环境变量或仓库根 .env）
    WEKNORA_KB_ID     默认知识库
    VALIDATOR_PORT    默认 8780
"""
import ast
import json
import os
import re
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent

STRUCTURED_DIR = REPO_ROOT / "structured-search"
if str(STRUCTURED_DIR) not in sys.path:
    sys.path.insert(0, str(STRUCTURED_DIR))
from dsl import question_to_dsl  # noqa: E402
from query import format_rows, search as structured_search  # noqa: E402

sys.path.insert(0, str(ROOT))
from build_refs import cert_no_by_knowledge, pick_locator, to_refs  # noqa: E402
from validator import (  # noqa: E402
    FALLBACK_ANSWER,
    build_text_index,
    final_answer,
    validate_references,
)

FALLBACK_PREFIX = "未在库中找到可靠依据"
CITATION_RE = re.compile(r"<kb\b|chunk://|出处[:：]|证书编号")


def _read_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _base() -> str:
    return _read_env("WEKNORA_BASE") or "http://127.0.0.1"


def _api(method: str, path: str, payload: dict | None = None, timeout: int = 180):
    key = _read_env("WEKNORA_API_KEY")
    if not key:
        raise RuntimeError("未配置 WEKNORA_API_KEY（环境变量或仓库根目录 .env）")
    body = None
    headers = {"X-API-Key": key}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(
        f"{_base().rstrip('/')}/api/v1{path}", data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"连接失败：{exc.reason}") from exc

    data = json.loads(raw)
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(json.dumps(data, ensure_ascii=False))
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


def _search(query: str, kb_ids: list, match_count: int) -> list:
    return _as_list(_api("POST", "/knowledge-search", {
        "query": query,
        "knowledge_base_ids": kb_ids,
        "match_count": match_count,
    }))


RERANK_URL = os.getenv("RERANK_URL", "http://127.0.0.1:8081/rerank")


def _rerank(query: str, items: list, top_n: int) -> list:
    """对混合召回结果做一次交叉编码重排；TEI 不可用时保留原顺序。"""
    if not items:
        return items
    texts = [(item.get("content") or "")[:2000] for item in items]
    body = json.dumps({"query": query, "texts": texts, "truncate": True}).encode("utf-8")
    req = urllib.request.Request(
        RERANK_URL, data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            scores = json.loads(resp.read().decode("utf-8"))
        if not isinstance(scores, list) or len(scores) != len(items):
            return items
        ranked = sorted(
            zip(items, scores),
            key=lambda pair: float((pair[1] or {}).get("score") or 0.0),
            reverse=True,
        )
        out = []
        for item, scored in ranked:
            item = dict(item)
            item["rerank_score"] = round(float((scored or {}).get("score") or 0.0), 6)
            out.append(item)
        return out[: max(1, top_n)]
    except Exception:  # noqa: BLE001 - 重排是增强项，失败不阻断检索
        return items


def _merge_delta(answer: str, piece: str) -> str:
    """SSE 可能发增量也可能发累计内容，两种都收敛成最终全文。"""
    if not piece:
        return answer
    if piece.startswith(answer) and len(piece) >= len(answer):
        return piece
    return answer + piece


def _create_session(title: str) -> str:
    data = _api("POST", "/sessions", {"title": title}, timeout=60)
    sid = (data or {}).get("id") if isinstance(data, dict) else ""
    if not sid:
        raise RuntimeError(f"创建会话未返回 id：{data}")
    return str(sid)


def _ask_model(session_id: str, query: str, kb_ids: list) -> tuple[str, list, list]:
    """调用知识问答 SSE，返回 (答案, 事件里带的 references, 事件类型序列)。"""
    key = _read_env("WEKNORA_API_KEY")
    body = json.dumps(
        {"query": query, "knowledge_base_ids": kb_ids, "channel": "api"},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{_base().rstrip('/')}/api/v1/knowledge-chat/{session_id}",
        data=body,
        headers={
            "X-API-Key": key,
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    answer = ""
    refs: list = []
    seen: list = []
    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload_text = line[5:].strip()
            if not payload_text or payload_text == "[DONE]":
                continue
            try:
                event = json.loads(payload_text)
            except json.JSONDecodeError:
                continue
            kind = str(event.get("type") or event.get("response_type") or "")
            if kind and (not seen or seen[-1] != kind):
                seen.append(kind)
            if kind == "answer":
                answer = _merge_delta(answer, event.get("content") or "")
            elif kind in ("references", "reference"):
                # 事件字段是 knowledge_references（新版本）或 data.references
                found = event.get("knowledge_references")
                if not isinstance(found, list):
                    data = event.get("data") or {}
                    found = data.get("references") if isinstance(data, dict) else None
                if isinstance(found, list):
                    refs = found
            elif kind in ("complete", "stop", "error"):
                if kind == "error" and not answer:
                    raise RuntimeError(event.get("content") or "问答流返回 error")
                break
    return answer, refs, seen


def _parse_locators(metadata) -> list:
    """metadata 里的 source_locators 可能是 list，也可能是单引号字典的字符串。"""
    if not isinstance(metadata, dict):
        return []
    locators = metadata.get("source_locators")
    if isinstance(locators, list):
        return [loc for loc in locators if isinstance(loc, dict)]
    if isinstance(locators, str) and locators.strip():
        try:
            parsed = ast.literal_eval(locators)
        except (ValueError, SyntaxError):
            return []
        if isinstance(parsed, list):
            return [loc for loc in parsed if isinstance(loc, dict)]
    return []


def _first_quote(content: str) -> str:
    for line in (content or "").splitlines():
        text = line.strip().lstrip("#").strip()
        if text and not text.startswith("| ---"):
            return text
    return ""


_LOCATOR_CACHE: dict[str, list] = {}


def _chunk_locators(chunk_id: str) -> list:
    """检索结果不带 source_locators，按 chunk id 回查一次（缓存）。"""
    if not chunk_id:
        return []
    if chunk_id in _LOCATOR_CACHE:
        return _LOCATOR_CACHE[chunk_id]
    locators = []
    try:
        chunk = _api("GET", f"/chunks/by-id/{chunk_id}", timeout=60)
        if isinstance(chunk, dict) and isinstance(chunk.get("data"), dict):
            chunk = chunk["data"]
        raw = chunk.get("source_locators") if isinstance(chunk, dict) else None
        if isinstance(raw, str) and raw.strip():
            try:
                raw = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                raw = None
        if isinstance(raw, list):
            locators = [loc for loc in raw if isinstance(loc, dict)]
    except Exception:  # noqa: BLE001 - 定位回查失败只降级，不阻断问答
        locators = []
    _LOCATOR_CACHE[chunk_id] = locators
    return locators


def refs_from_answer(references: list, cert_map: dict, query: str = "") -> tuple[list, dict]:
    """把问答事件的 knowledge_references 转成校验用的 refs + 原文索引。"""
    refs = []
    text_index = {}
    for item in references:
        if not isinstance(item, dict):
            continue
        chunk_id = str(item.get("id") or "")
        content = item.get("content") or ""
        if not chunk_id or not content:
            continue
        text_index[chunk_id] = content
        # 优先用分块自身的完整 source_locators（检索结果里不带），
        # 事件 metadata 里的定位往往只有一条，不够精确。
        locators = _chunk_locators(chunk_id)
        if not locators:
            locators = _parse_locators(item.get("metadata"))
        loc = pick_locator({"source_locators": locators}, query) if locators else {}
        quote = loc.get("quote") or _first_quote(content)
        refs.append({
            "cert_no": cert_map.get(item.get("knowledge_id")),
            "knowledge_id": item.get("knowledge_id"),
            "knowledge_title": item.get("knowledge_title") or item.get("knowledge_filename"),
            "chunk_id": chunk_id,
            "quote": quote,
            "page": loc.get("page"),
            "bbox": loc.get("bbox"),
        })
    return refs, text_index


def _evidence_block(kept: list) -> str:
    """把校验通过的依据拼成固定格式，保证出口一定带证书编号+原文+定位。"""
    lines = ["", "#### 出处（校验通过）"]
    for ref in kept:
        if ref.get("page"):
            locator = f"第{ref['page']}页"
        elif ref.get("source_type") == "registry":
            locator = "参数库（结构化表）"
        else:
            locator = "文本分块"
        quote = (ref.get("quote") or "").replace("\n", " ").strip()
        lines.append(
            f"- 证书编号={ref.get('cert_no') or '片段未包含'}"
            f"｜文件={ref.get('knowledge_title') or '未知'}"
            f"｜定位={locator}"
            f"｜原文=\"{quote[:180]}\""
        )
    return "\n".join(lines)


def guard(answer: str, refs: list, text_index: dict) -> dict:
    result = validate_references(refs, text_index)
    text = (answer or "").strip()
    cited = bool(CITATION_RE.search(text))
    if not text or (result.kept and not cited):
        # 模型没给引用就不要放行；改为直接输出检索到的带出处原文。
        if result.kept:
            text = "依据检索到的原文片段：" + _evidence_block(result.kept)
            cited = True
        else:
            text = FALLBACK_ANSWER
    elif result.kept:
        text = text + _evidence_block(result.kept)
        cited = True
    return {
        "ok": bool(result.kept),
        "kept": result.kept,
        "dropped": result.dropped,
        "degraded": result.degraded,
        "counts": {
            "kept": len(result.kept),
            "dropped": len(result.dropped),
            "degraded": len(result.degraded),
        },
        "answer": text,
        "citation_missing": bool(result.kept) and not cited,
        "fallback": text.startswith(FALLBACK_PREFIX),
    }


def handle_validate(body: dict) -> dict:
    items = body.get("items") or []
    query = body.get("query") or ""
    if items:
        text_index = build_text_index(items)
        refs = to_refs(items, cert_no_by_knowledge(), query)
    else:
        refs = body.get("refs") or []
        text_index = body.get("text_index") or {}
    return guard(body.get("answer") or "", refs, text_index)


def _row_text(row: dict) -> str:
    """把一条结构化参数行压成可引用的原文片段。"""
    return (
        f"证书编号 {row.get('cert_no') or '（空）'}｜机构 {row.get('institution') or '（空）'}"
        f"｜产品 {row.get('product_name') or '（空）'}｜型号 {row.get('model_spec') or '（空）'}"
        f"｜电压 {row.get('voltage') or '（空）'}｜电流 {row.get('current') or '（空）'}"
        f"｜外壳 {row.get('enclosure') or '（空）'}｜有效期至 {row.get('valid_until') or '（空）'}"
    )


def structured_evidence(query: str) -> tuple[dict, list, dict]:
    """结构化条件检索。返回 (摘要, refs, text_index)。

    结构化表没有页码，refs 的 page 固定为 None，由 validator 降级为
    “仅文本依据”，但仍保留证书编号，满足“无出处不得输出”。
    """
    dsl = question_to_dsl(query)
    if not dsl:
        return {"dsl": {}, "count": 0, "rows": [], "text": ""}, [], {}
    try:
        rows = structured_search(dsl)
    except Exception as exc:  # noqa: BLE001 - 结构化库不可用不应阻断语义检索
        return {
            "dsl": dsl, "count": 0, "rows": [], "text": "",
            "error": f"{type(exc).__name__}: {exc}",
        }, [], {}

    refs, index = [], {}
    for i, row in enumerate(rows):
        quote = _row_text(row)
        chunk_id = f"registry:{row.get('cert_no') or 'unknown'}:{i}"
        index[chunk_id] = quote
        refs.append({
            "source_type": "registry",
            "cert_no": row.get("cert_no"),
            "knowledge_id": "",
            "knowledge_title": "参数库（结构化表）",
            "chunk_id": chunk_id,
            "quote": quote,
            "page": None,
            "bbox": None,
        })
    return {
        "dsl": dsl,
        "count": len(rows),
        "rows": rows,
        "text": format_rows(rows),
    }, refs, index


def handle_ask(body: dict) -> dict:
    query = (body.get("query") or "").strip()
    if not query:
        raise ValueError("缺少 query")
    kb_ids = body.get("kb_ids") or ([_read_env("WEKNORA_KB_ID")] if _read_env("WEKNORA_KB_ID") else [])
    if not kb_ids:
        raise ValueError("缺少 kb_ids（或环境变量 WEKNORA_KB_ID）")
    match_count = int(body.get("match_count") or 5)

    items = _search(query, kb_ids, match_count)
    items = _rerank(query, items, max(match_count, 10))
    session_id = _create_session(f"guarded-qa-{query[:20]}")
    answer, references, seen = _ask_model(session_id, query, kb_ids)

    # 以回答实际引用的分块为准；事件没带 references 时退回检索结果。
    cert_map = cert_no_by_knowledge()
    if references:
        refs, text_index = refs_from_answer(references, cert_map, query)
    else:
        text_index = build_text_index(items)
        refs = to_refs(items, cert_map, query)

    # 结构化参数表条件检索：与语义召回一并交给出口校验，
    # 参数行没有页码，按“仅文本依据”降级保留。
    structured, structured_refs, structured_index = structured_evidence(query)
    if structured_refs:
        refs = list(refs) + structured_refs
        text_index = dict(text_index)
        text_index.update(structured_index)

    checked = guard(answer, refs, text_index)
    checked.update({
        "query": query,
        "session_id": session_id,
        "raw_answer": answer,
        "retrieved": len(items),
        "reranked": bool(items) and "rerank_score" in items[0],
        "events": seen,
        "structured": structured,
    })
    return checked


class Handler(BaseHTTPRequestHandler):
    server_version = "citation-validator/1.0"

    def _send(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0] == "/health":
            self._send(200, {"ok": True, "port": self.server.server_address[1]})
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        try:
            body = self._read_json()
            if path == "/validate":
                self._send(200, handle_validate(body))
            elif path == "/ask":
                self._send(200, handle_ask(body))
            else:
                self._send(404, {"ok": False, "error": "not found"})
        except ValueError as exc:
            self._send(400, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 边车要把失败原因原样交给调用方
            self._send(502, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt, *args):  # 保留一行访问日志，方便手动验证
        sys.stderr.write("[validator] " + fmt % args + "\n")


def main() -> None:
    host = os.getenv("VALIDATOR_HOST", "127.0.0.1")
    port = int(os.getenv("VALIDATOR_PORT", "8780"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"出处校验边车已启动: http://{host}:{port}  (POST /validate, POST /ask)")
    server.serve_forever()


if __name__ == "__main__":
    main()
