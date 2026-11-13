"""防爆合格证抽取 MCP 服务（HTTP Streamable）。

WeKnora 的 MCP 客户端禁用 stdio、只支持 sse / http-streamable，
所以这里把抽取能力包成 HTTP Streamable 服务，供 smart-reasoning 智能体调用。

启动：
    python cert-extractor/server.py
    # 默认 0.0.0.0:8765，MCP 端点为 /mcp

环境变量：
    OLLAMA_URL          默认 http://127.0.0.1:11434/api/chat
    EXTRACT_MODEL       默认 qwen3-json-16k:latest
    EXTRACT_TIMEOUT     默认 300 秒
    MCP_HOST / MCP_PORT 默认 0.0.0.0 / 8765

工具：
    extract_certificate(markdown, source_file, locators_json) -> 统一 JSON
    extract_from_knowledge(knowledge_id | doc_ref+kb_ref)      -> 直接从知识库已入库文档抽取
    validate_extraction(fields_json, markdown)                -> 只校验打分，不调模型
"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import jsonschema
from mcp.server.mcpserver import MCPServer

from normalize import check_format, score_field
from prompt import SYSTEM_PROMPT, build_user_prompt

ROOT = Path(__file__).resolve().parent
SCHEMA = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))

# 抽取结果直接落到人工校对台的结果目录，工作台刷新即可看到，无需手工跑 collect.py。
REVIEW_DIR = ROOT.parent / "review-workbench" / "results"
REVIEW_URL = os.getenv("REVIEW_URL", "http://127.0.0.1:8770")
VALIDATOR_URL = os.getenv("VALIDATOR_URL", "http://127.0.0.1:8780")

# 结构化条件检索复用 structured-search/ 下的白名单 DSL + 参数化 SQL。
STRUCTURED_DIR = ROOT.parent / "structured-search"
if str(STRUCTURED_DIR) not in sys.path:
    sys.path.insert(0, str(STRUCTURED_DIR))
from dsl import question_to_dsl  # noqa: E402
from query import format_rows, search as structured_query  # noqa: E402

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
# 默认用 Qwen3.5-4B：3.4 GB 权重，100% 上 GPU，单份抽取约 20 s；
# 换回 8B（更准、但 8 GB 显存只能 80% 上 GPU、约 80-155 s）：
#   $env:EXTRACT_MODEL="qwen3-json-16k:latest"
MODEL = os.getenv("EXTRACT_MODEL", "modelscope.cn/unsloth/Qwen3.5-4B-GGUF:latest")
TIMEOUT = int(os.getenv("EXTRACT_TIMEOUT", "300"))
MAX_CHARS = 32000

# 知识库取文：默认走本机 WeKnora API；KEY 未注入时回退读仓库根目录 .env
WEKNORA_BASE = os.getenv("WEKNORA_BASE", "http://127.0.0.1")
WEKNORA_KEY = os.getenv("WEKNORA_API_KEY", "")
if not WEKNORA_KEY:
    _env_file = ROOT.parent / ".env"
    if _env_file.exists():
        for _line in _env_file.read_text(encoding="utf-8").splitlines():
            if _line.strip().startswith("WEKNORA_API_KEY="):
                WEKNORA_KEY = _line.split("=", 1)[1].strip().strip('"').strip("'")
                break

# 接口只返回 text 分块；扫描件的正文在 image_ocr，检索件在 image_caption
CHUNK_TYPES = ("text", "parent_text", "image_ocr", "image_caption")
IMAGE_ONLY_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
DOC_REF_RE = re.compile(r"^(weknora://doc/|doc://)", re.I)
UUID_SEARCH_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
DOC_EXT_RE = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|md|markdown|txt|jpe?g|png|bmp|tiff?|webp)$", re.I)
TITLE_WS_RE = re.compile(r"[\s\u3000]+")

FIELD_NAMES = [
    "cert_no", "institution", "manufacturer", "product_name", "model_spec",
    "ex_marking", "standard", "drawing_no", "issue_date", "valid_until",
    "voltage", "current", "enclosure",
]
REQUIRED_FIELDS = SCHEMA.get("required", [])

mcp = MCPServer(
    "cert-extractor",
    instructions=(
        "防爆合格证字段抽取服务。输入一份证书 Markdown（可含定位信息），"
        "返回统一 JSON：字段名 / 值 / 置信度 / 页码 / 区域 / 原文片段。"
        "confidence 由服务端按“原文可核验 + 格式合法”重算，不采用模型自报值。"
    ),
)


_FULLWIDTH = str.maketrans({
    "：": ":", "，": ",", "；": ";", "（": "(", "）": ")", "、": ",", "。": ".",
    "％": "%", "　": "", "－": "-", "～": "~", "！": "!", "？": "?",
})


_SYMBOL_RULES = (
    (re.compile(r"(?:∥|‖|\|\|)\s*([ABC])"), r"Ⅱ\1"),
    (re.compile(r"III\s*([ABC])", re.I), r"Ⅲ\1"),
    (re.compile(r"II\s*([ABC])", re.I), r"Ⅱ\1"),
    (re.compile(r"(Ⅱ|Ⅲ)\s*([ABC])"), r"\1\2"),
    (re.compile(r"T\s*(\d{2,3})\s*[C℃]"), r"T\1℃"),
    (re.compile(r"(\d{2,3})\s*(?:°\s*C|[oO]\s*C)"), r"\1℃"),
)


def _squash(text: str) -> str:
    """去空白 + 全角标点归半角 + OCR 特殊符号归一，容忍排版与修正差异。"""
    out = (text or "").translate(_FULLWIDTH)
    for pattern, repl in _SYMBOL_RULES:
        out = pattern.sub(repl, out)
    return re.sub(r"\s+", "", out)


def _contains(needle: str, haystack: str) -> bool:
    """原文包含判断，容忍 OCR 造成的空白差异。"""
    if not needle or not haystack:
        return False
    return needle in haystack or _squash(needle) in _squash(haystack)


def _strip_fence(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    return re.sub(r"\s*```$", "", text)


def _review_key(knowledge_id: str, source_file: str, markdown: str) -> str:
    """知识库文档用 knowledge_id；对话附件没有 id，用内容哈希生成稳定键。"""
    if knowledge_id and UUID_RE.match(knowledge_id):
        return knowledge_id
    digest = hashlib.sha1(
        (source_file or "").encode("utf-8") + b"\0" + (markdown or "").encode("utf-8")
    ).hexdigest()[:16]
    return f"attach_{digest}"


def _attach_review(result: dict, markdown: str, source_file: str, knowledge_id: str = "") -> dict:
    """把抽取结果与原文写入 review-workbench/results，并回填人工校对台地址。"""
    if not isinstance(result, dict):
        return result
    key = _review_key(knowledge_id, source_file, markdown)
    payload = {
        "knowledge_id": key,
        "source_file": source_file or key,
        "extracted_at": datetime.now().isoformat(timespec="seconds"),
        "markdown": markdown or "",
        "result": result,
    }
    try:
        REVIEW_DIR.mkdir(parents=True, exist_ok=True)
        target = REVIEW_DIR / f"{key}.json"
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
        result["review_key"] = key
        result["review_url"] = f"{REVIEW_URL.rstrip('/')}/?doc={urllib.parse.quote(key)}"
    except OSError as exc:
        result["review_error"] = f"{type(exc).__name__}: {exc}"
    return result



def _call_llm(markdown: str) -> dict:
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(markdown)},
        ],
        "stream": False,
        "format": "json",
        # Qwen3.5 默认开启 thinking，抽取任务会一直"想"到上下文耗尽（实测 14300 tokens、
        # content 为空、done_reason=length），必须显式关闭。
        "think": False,
        # 抽完立刻卸载：8 GB 显存装不下「抽取 8B + 对话模型」两个模型同时常驻，
        # 不卸载会互相挤显存，MCP 调用会超时（实测 300 s deadline exceeded）
        "keep_alive": 0,
        "options": {"temperature": 0.0},
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        content = json.loads(resp.read())["message"]["content"]
    if not (content or "").strip():
        raise ValueError("抽取模型返回空内容（thinking 占满了上下文，确认已关闭 think 且 num_ctx 足够）")
    return json.loads(_strip_fence(content))


def _normalize_fields(fields: dict) -> dict:
    """补齐模型常漏的键，避免结构问题被误报成「schema 校验失败」。

    实测模型返回空字段时经常省略 quote（{"value": null, "confidence": 0}），
    而 schema 要求 value/confidence/quote 三者都在，这里统一补齐。
    """
    if not isinstance(fields, dict):
        return {}
    for name, item in list(fields.items()):
        if not isinstance(item, dict):
            fields[name] = {"value": None, "confidence": 0, "quote": None}
            continue
        item.setdefault("quote", None)
        item.setdefault("confidence", 0)
        if "values" in item:
            values = item.get("values")
            if isinstance(values, list):
                item["values"] = [str(v) for v in values if isinstance(v, (str, int, float))]
            else:
                item["values"] = [] if values in (None, "") else [str(values)]
        elif "value" not in item:
            item["value"] = None
        elif item["value"] is not None and not isinstance(item["value"], str):
            item["value"] = str(item["value"])
    return fields


def _field_value(item: dict) -> str:
    value = item.get("value")
    if value:
        return str(value)
    values = item.get("values") or []
    return str(values[0]) if values else ""


def _attach_locators(fields: dict, locators: list) -> int:
    """按 quote/字段值与定位块的重合度贴页码和区域，返回命中个数。

    定位块已按行/单元格拆分；匹配时把复合值按分隔符拆开逐段比对，
    同等条件下优先选择更短（更精确）且带 bbox 的定位块。
    """
    if not locators:
        return 0
    squashed = [(loc, _squash(loc.get("quote") or "")) for loc in locators]
    hit = 0
    for name in FIELD_NAMES:
        item = fields.get(name)
        if not isinstance(item, dict):
            continue
        needles = []
        for raw in (item.get("quote") or "", _field_value(item)):
            raw = str(raw or "").strip()
            if len(raw) >= 4:
                needles.append(_squash(raw))
            for piece in re.split(r"[;；,，、/|]", raw):
                piece = piece.strip()
                if len(piece) >= 4:
                    needles.append(_squash(piece))
        needles = [needle for needle in dict.fromkeys(needles) if needle]
        if not needles:
            continue
        best = None
        best_score = 0
        for loc, loc_quote in squashed:
            if not loc_quote:
                continue
            score = 0
            for needle in needles:
                if needle == loc_quote:
                    score = max(score, 30000)
                elif needle in loc_quote:
                    score = max(score, 20000)
                elif loc_quote in needle:
                    score = max(score, 10000)
            if not score:
                continue
            score -= min(len(loc_quote), 999)  # 定位块越短越精确
            if not loc.get("bbox"):
                score -= 1000
            if score > best_score:
                best_score = score
                best = loc
        if best:
            item["page"] = best.get("page")
            item["bbox"] = best.get("bbox")
            hit += 1
    return hit


def _validate_and_score(fields: dict, markdown: str) -> tuple[list, dict]:
    """schema 校验 + 可计算置信度。返回 (issues, stats)。"""
    issues = []
    try:
        jsonschema.validate(instance=fields, schema=SCHEMA)
    except jsonschema.ValidationError as exc:
        issues.append(f"schema: {exc.message} at {list(exc.absolute_path)}")

    covered = 0
    missing_quote = 0
    filled = 0
    scores = []

    for name in FIELD_NAMES:
        item = fields.get(name)
        if not isinstance(item, dict):
            continue
        value = _field_value(item)
        quote = item.get("quote") or ""
        if not value:
            item["confidence"] = 0.0
            continue

        filled += 1
        value_in_quote = _contains(value, quote) if quote else False
        quote_in_text = _contains(quote, markdown) if quote else False
        fmt_ok = check_format(name, value)
        model_conf = item.get("confidence") or 0.0

        item["confidence"] = score_field(model_conf, value, quote, markdown, bool(fmt_ok))
        item["confidence_basis"] = {
            "model": round(float(model_conf), 3),
            "value_in_quote": value_in_quote,
            "quote_in_text": quote_in_text,
            "format_ok": fmt_ok,
        }
        if fmt_ok is False:
            issues.append(f"{name}: 值不符合标准格式（{value}）")
        scores.append(item["confidence"])
        if quote_in_text:
            covered += 1
        else:
            missing_quote += 1
            issues.append(f"{name}: quote 未能在原文中命中")

    for name in REQUIRED_FIELDS:
        if not _field_value(fields.get(name) or {}):
            issues.append(f"{name}: 必填字段缺失")

    stats = {
        "fields_total": len(FIELD_NAMES),
        "filled": filled,
        "quote_verified": covered,
        "quote_missing": missing_quote,
        "avg_confidence": round(sum(scores) / len(scores), 3) if scores else 0.0,
        "min_confidence": round(min(scores), 3) if scores else 0.0,
    }
    return issues, stats


def _weknora_get(path: str, **params) -> dict:
    """调用 WeKnora 管理 API，返回 data 段。"""
    if not WEKNORA_KEY:
        raise RuntimeError("未配置 WEKNORA_API_KEY（环境变量或仓库根目录 .env）")
    url = f"{WEKNORA_BASE.rstrip('/')}/api/v1{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-API-Key": WEKNORA_KEY})
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read())
    if isinstance(body, dict) and body.get("success") is False:
        raise RuntimeError(body.get("error") or body)
    return body.get("data", body) if isinstance(body, dict) else body


def _as_list(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "list", "records", "knowledge"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _is_image_only(content: str) -> bool:
    lines = [line.strip() for line in (content or "").splitlines() if line.strip()]
    return bool(lines) and all(IMAGE_ONLY_RE.match(line) for line in lines)


def _join_chunks(chunks: list, max_overlap: int = 200) -> str:
    """按 seq_id 拼接分块，并裁掉相邻块之间的重叠部分。"""
    out = ""
    for chunk in sorted(chunks, key=lambda c: c.get("seq_id") or 0):
        piece = (chunk.get("content") or "").strip()
        if not piece:
            continue
        if out:
            limit = min(len(out), len(piece), max_overlap)
            cut = 0
            for k in range(limit, 0, -1):
                if out.endswith(piece[:k]):
                    cut = k
                    break
            piece = piece[cut:]
        if piece:
            out = f"{out}\n\n{piece}" if out else piece
    return out


def _normalize_ref(value: str) -> str:
    """去掉引用两侧的空白、引号和括号，兼容模型/运行时给出的各种包裹形式。"""
    return (value or "").strip().strip(" \t\r\n\"'`<>()[]{}，,、；;")


def _norm_title(value: str) -> str:
    """标题归一化：去掉全部空白（含全角空格）并忽略大小写，避免模型在文件名里插空格导致匹配失败。"""
    return TITLE_WS_RE.sub("", _normalize_ref(value)).casefold()


def _extract_uuid(value: str) -> str:
    """从任意引用中提取第一个 UUID；没有则返回空字符串。"""
    match = UUID_SEARCH_RE.search(_normalize_ref(value))
    return match.group(0) if match else ""


def _fetch_knowledge_doc(knowledge_id: str) -> tuple[str, str, list]:
    """取已入库文档的正文、标题和分块定位。返回 (markdown, title, locators)。"""
    knowledge_id = _extract_uuid(knowledge_id) or _normalize_ref(knowledge_id)
    if not knowledge_id:
        raise ValueError("knowledge_id 不能为空")
    detail = _weknora_get(f"/knowledge/{knowledge_id}")
    title = detail.get("title") or detail.get("file_name") or ""

    markdown = ""
    for key in ("markdown", "markdown_content", "content", "text"):
        value = detail.get(key)
        if isinstance(value, str) and value.strip() and not _is_image_only(value):
            markdown = value
            break

    chunks = []
    for chunk_type in CHUNK_TYPES:
        page = 1
        while True:
            batch = _as_list(_weknora_get(
                f"/chunks/{knowledge_id}", page=page, page_size=100, chunk_type=chunk_type
            ))
            chunks.extend(batch)
            if len(batch) < 100:
                break
            page += 1

    locators = []
    seen = set()
    for chunk in chunks:
        text = (chunk.get("content") or "").strip()
        if _is_image_only(text):
            continue
        chunk_quote = text[:400]
        # 一个分块可能跨多行/多个单元格，逐条保留 source_locators，
        # 否则只能拿到分块第一个定位块，字段会被贴到错误的页码和区域。
        for loc in (chunk.get("source_locators") or []):
            page = loc.get("page")
            bbox = loc.get("bbox")
            if page is None and bbox is None:
                continue
            quote = (loc.get("quote") or chunk_quote).strip()
            if not quote:
                continue
            bbox_key = tuple(bbox) if isinstance(bbox, (list, tuple)) else bbox
            key = (page, bbox_key, quote[:120])
            if key in seen:
                continue
            seen.add(key)
            locators.append({"page": page, "bbox": bbox, "quote": quote[:400]})

    # 扫描件 detail.markdown 只有图片占位符：按“正文最长的分块组”回退，
    # 内置引擎在 image_ocr，MinerU 解析后的正文在 text。
    if not markdown:
        groups = [
            [c for c in chunks if c.get("chunk_type") in ("text", "parent_text")],
            [c for c in chunks if c.get("chunk_type") == "image_ocr"],
            [c for c in chunks if c.get("chunk_type") == "image_caption"],
        ]
        usable = [
            [c for c in group if not _is_image_only(c.get("content"))]
            for group in groups
        ]
        markdown = _join_chunks(max(usable, key=lambda g: sum(len((c.get("content") or "").strip()) for c in g)))

    return markdown, title, locators


def _list_kb_docs(kb_id: str) -> list:
    return _as_list(_weknora_get(f"/knowledge-bases/{kb_id}/knowledge"))


def _is_param_card(doc: dict) -> bool:
    """参数卡是入库产物（人工/manual 文档），不是可抽取的原始报告。"""
    return (doc.get("title") or "").startswith("参数卡")


def _resolve_doc_ref(doc_ref: str, kb_ref: str = "") -> tuple[str, str]:
    """把运行时上下文里的文档/知识库引用解析成真实 knowledge_id。

    不依赖固定前缀：引用里只要含 UUID 就直接使用；其余兼容
    doc://N、weknora://doc/N、kb://N、知识库名称和文件名片段。
    """
    ref = _normalize_ref(doc_ref)
    if not ref:
        raise ValueError("doc_ref 不能为空")

    doc_id = _extract_uuid(ref)
    if doc_id:
        return doc_id, doc_id

    kb_ref = _normalize_ref(kb_ref)
    kb_id = _extract_uuid(kb_ref)
    kb_name = ""
    kbs = None

    def load_kbs() -> list:
        nonlocal kbs
        if kbs is None:
            kbs = _as_list(_weknora_get("/knowledge-bases"))
        return kbs

    # kb://N 或裸数字序号
    if not kb_id and kb_ref:
        candidate = kb_ref[5:] if kb_ref.lower().startswith("kb://") else kb_ref
        candidate = candidate.strip().strip("/")
        if candidate.isdigit():
            index = int(candidate)
            items = load_kbs()
            if 1 <= index <= len(items):
                kb_id = items[index - 1].get("id") or ""
                kb_name = items[index - 1].get("name") or ""
            else:
                raise ValueError(f"找不到知识库 {kb_ref}")

    # 名称匹配：允许引用带协议/路径，取最后一段按名称比较
    if kb_ref and not kb_id:
        name_ref = kb_ref.split("://", 1)[-1].rsplit("/", 1)[-1].strip() or kb_ref
        for kb in load_kbs():
            name = (kb.get("name") or "").strip()
            if name and (name == name_ref or name_ref in name or name == kb_ref):
                kb_id = kb.get("id") or ""
                kb_name = name
                break
        if not kb_id:
            raise ValueError(f"找不到知识库 {kb_ref}")

    if not kb_id:
        items = load_kbs()
        if not items:
            raise ValueError("当前没有可用的知识库")
        kb_id = items[0].get("id") or ""
        kb_name = items[0].get("name") or ""

    docs = _list_kb_docs(kb_id)
    if not docs:
        raise ValueError(f"知识库「{kb_name or kb_id}」里没有文档")
    # 用户没有点名“参数卡”时，参数卡不参与序号与名称匹配，避免抽到入库产物
    if "参数卡" not in ref:
        docs = [doc for doc in docs if not _is_param_card(doc)] or docs

    # doc://N / weknora://doc/N 序号别名
    alias = ref
    for prefix in ("weknora://doc/", "doc://"):
        if alias.lower().startswith(prefix):
            alias = alias[len(prefix):].strip().strip("/")
            break
    if alias.isdigit():
        index = int(alias)
        if not 1 <= index <= len(docs):
            raise ValueError(f"doc_ref={ref} 超出范围（该知识库共 {len(docs)} 篇）")
        doc = docs[index - 1]
        return doc.get("id") or "", doc.get("title") or doc.get("file_name") or ""

    def _title(doc: dict) -> str:
        return doc.get("title") or doc.get("file_name") or ""

    # 分层匹配：原样优先，归一化只作为兜底，避免标题本身的空格被判错。
    # 1) 原样精确  2) 原样子串  3) 去空白精确  4) 去空白子串
    for doc in docs:
        if ref == _title(doc):
            return doc.get("id") or "", _title(doc)
    for doc in docs:
        if ref and ref in _title(doc):
            return doc.get("id") or "", _title(doc)

    needle = _norm_title(ref)
    for doc in docs:
        title = _title(doc)
        if needle and needle == _norm_title(title):
            return doc.get("id") or "", title
    for doc in docs:
        title = _title(doc)
        if needle and needle in _norm_title(title):
            return doc.get("id") or "", title
    raise ValueError(f"知识库「{kb_name or kb_id}」里找不到文档：{ref}")


def _looks_like_doc_ref(text: str) -> bool:
    """判断参数其实是文档引用（文件名 / doc://N / uuid / weknora://doc/...）而不是正文。"""
    value = _normalize_ref(text)
    if not value or len(value) > 300 or "\n" in value:
        return False
    if DOC_REF_RE.match(value) or UUID_RE.match(value) or _extract_uuid(value):
        return True
    return bool(DOC_EXT_RE.search(value))


def _extract(markdown: str, source_file: str, locators: list) -> dict:
    """抽取 + 定位 + 打分的公共实现。"""
    started = time.time()
    truncated = len(markdown) > MAX_CHARS
    text = markdown[:MAX_CHARS] if truncated else markdown

    try:
        fields = _normalize_fields(_call_llm(text))
    except Exception as exc:  # 模型不可用 / 超时 / 非法 JSON
        return {
            "ok": False,
            "source_file": source_file,
            "error": f"{type(exc).__name__}: {exc}",
        }

    locator_hits = _attach_locators(fields, locators)
    issues, stats = _validate_and_score(fields, text)
    if truncated:
        issues.append(f"markdown 超过 {MAX_CHARS} 字符，已截断，尾部内容未参与抽取")

    stats.update({
        "locators_provided": len(locators),
        "locators_matched": locator_hits,
        "truncated": truncated,
        "elapsed_s": round(time.time() - started, 2),
        "model": MODEL,
    })
    return {
        "ok": not issues,
        "source_file": source_file,
        "fields": fields,
        "issues": issues,
        "stats": stats,
    }


@mcp.tool()
def extract_certificate(
    markdown: str,
    source_file: str = "",
    locators_json: str = "",
) -> str:
    """从证书 Markdown 抽取结构化字段。

    Args:
        markdown: 证书 Markdown 全文（MinerU 或知识库导出的正文）。
        source_file: 来源文件名，仅用于回溯，可为空。
        locators_json: 可选，JSON 数组，元素形如
            {"page": 1, "bbox": [x,y,w,h], "quote": "原文片段"}，
            通常来自 WeKnora 的 source_locators，用于补齐页码与区域。

    Returns:
        JSON 字符串：{"ok", "source_file", "fields", "issues", "stats"}
    """
    locators = []
    if locators_json:
        try:
            locators = json.loads(locators_json) or []
        except json.JSONDecodeError:
            locators = []

    # 模型经常把文件名或文档引用当成正文塞进 markdown：这里还原成知识库正文，
    # 否则会把文件名喂给抽取模型，导致 13 个字段全空。
    ref = ""
    knowledge_id = ""
    for candidate in (markdown, source_file):
        if _looks_like_doc_ref(candidate):
            ref = candidate.strip()
            if ref.lower().startswith("weknora://doc/"):
                ref = ref[len("weknora://doc/"):]
            break
    if ref:
        try:
            knowledge_id, title = _resolve_doc_ref(ref)
            markdown, title, locators = _fetch_knowledge_doc(knowledge_id)
            source_file = source_file or title
        except Exception as exc:
            return json.dumps({
                "ok": False,
                "source_file": source_file or markdown,
                "error": f"{type(exc).__name__}: {exc}",
            }, ensure_ascii=False)

    result = _extract(markdown, source_file, locators)
    if knowledge_id:
        result["knowledge_id"] = knowledge_id
    return json.dumps(
        _attach_review(result, markdown, source_file, knowledge_id), ensure_ascii=False
    )


@mcp.tool()
def extract_from_knowledge(
    knowledge_id: str = "",
    doc_ref: str = "",
    kb_ref: str = "",
) -> str:
    """直接抽取知识库里已入库文档，不需要把正文贴进参数。

    定位方式二选一：
    - knowledge_id：WeKnora 知识 ID（文档详情页 URL 里的 uuid）；
    - doc_ref + kb_ref：运行时上下文里的别名，如 doc_ref="doc://1"、kb_ref="kb://1"；
      doc_ref 也接受文件名片段（如 "CE22.1912"）。

    Returns:
        JSON 字符串：{"ok", "knowledge_id", "source_file", "doc_chars",
                      "fields", "issues", "stats"}
        其中 stats.locators_* 会自动用文档自身的 source_locators 补齐。
    """
    hint = knowledge_id or doc_ref
    try:
        if knowledge_id:
            knowledge_id = _extract_uuid(knowledge_id) or _normalize_ref(knowledge_id)
        else:
            knowledge_id, _ = _resolve_doc_ref(doc_ref, kb_ref)
        markdown, title, locators = _fetch_knowledge_doc(knowledge_id)
    except Exception as exc:
        return json.dumps({
            "ok": False,
            "knowledge_id": hint,
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False)

    if not markdown.strip():
        return json.dumps({
            "ok": False,
            "knowledge_id": knowledge_id,
            "error": "该文档没有可抽取的文本（可能是纯图片且未 OCR）",
        }, ensure_ascii=False)

    result = _extract(markdown, title or knowledge_id, locators)
    result["knowledge_id"] = knowledge_id
    result["doc_chars"] = len(markdown)
    return json.dumps(
        _attach_review(result, markdown, title or knowledge_id, knowledge_id),
        ensure_ascii=False,
    )


@mcp.tool()
def validate_extraction(fields_json: str, markdown: str) -> str:
    """校验并重算置信度，不调用模型。

    用于评测：把标注或历史结果重新打分，避免“模型自报置信度”不可比。

    Args:
        fields_json: 形如 {"cert_no": {"value": "...", ...}, ...} 的 JSON 字符串。
        markdown: 该字段所依据的原文全文。

    Returns:
        JSON 字符串：{"ok", "fields", "issues", "stats"}
    """
    try:
        fields = json.loads(fields_json)
    except json.JSONDecodeError as exc:
        return json.dumps({"ok": False, "error": f"JSONDecodeError: {exc}"}, ensure_ascii=False)

    issues, stats = _validate_and_score(fields, markdown)
    stats["model"] = "none"
    return json.dumps({
        "ok": not issues,
        "fields": fields,
        "issues": issues,
        "stats": stats,
    }, ensure_ascii=False)


@mcp.tool()
def structured_search(question: str, limit: int = 20) -> str:
    """按结构化参数表做条件检索（SQLite，参数化 SQL）。

    适合“电流 60A 且外壳全塑的证书有哪些”“IP66 的配电箱”这类精确条件，
    与向量/关键词检索互补。字段白名单：voltage / current / enclosure /
    ex_marking / institution / product_name / cert_no。

    Args:
        question: 用户问句。
        limit: 最多返回条数，默认 10。

    Returns:
        JSON 字符串：{"ok","query","dsl","count","rows","text"}
    """
    try:
        dsl = question_to_dsl(question)
        rows = structured_query(dsl)[: max(1, int(limit or 10))]
        return json.dumps({
            "ok": True,
            "query": question,
            "dsl": dsl,
            "count": len(rows),
            "rows": rows,
            "text": format_rows(rows),
        }, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - 工具失败要把原因交给智能体
        return json.dumps({
            "ok": False,
            "query": question,
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False)


@mcp.tool()
def kb_answer(query: str, match_count: int = 8) -> str:
    """知识库问答：BM25+向量召回、重排、结构化条件检索，答案强制带出处。

    Args:
        query: 用户问题。
        match_count: 召回条数，默认 8。

    Returns:
        JSON 字符串，answer 字段可直接作为最终回答。
    """
    body = json.dumps(
        {"query": query, "match_count": max(1, int(match_count or 8))},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{VALIDATOR_URL.rstrip('/')}/ask",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        return json.dumps({"ok": False, "error": f"HTTP {exc.code}: {detail}"}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "hint": "确认 citation-validator/server.py 已在 8780 启动",
        }, ensure_ascii=False)
    if isinstance(payload, dict):
        payload.setdefault("ok", bool(payload.get("kept")))
    return json.dumps(payload, ensure_ascii=False)


if __name__ == "__main__":
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8765"))
    print(f"cert-extractor MCP 启动: http://{host}:{port}/mcp")
    mcp.run(transport="streamable-http", host=host, port=port)
