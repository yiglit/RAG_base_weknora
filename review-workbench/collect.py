"""采集脚本：调用 MCP 抽取服务，并把抽取结果与原文落盘。

用法：
    python review-workbench/collect.py <knowledge_id> [<knowledge_id> ...]
    python review-workbench/collect.py --all --kb <kb_id>

环境变量：
    MCP_URL            默认 http://127.0.0.1:8765/mcp
    WEKNORA_BASE       默认 http://127.0.0.1
    WEKNORA_API_KEY    为空时回退读取仓库根目录 .env
    MCP_CALL_TIMEOUT   单份抽取超时秒数，默认 900
"""
import argparse
import asyncio
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent
RESULTS_DIR = ROOT / "results"

MCP_URL = os.getenv("MCP_URL", "http://127.0.0.1:8765/mcp")
WEKNORA_BASE = os.getenv("WEKNORA_BASE", "http://127.0.0.1").rstrip("/")
CALL_TIMEOUT = float(os.getenv("MCP_CALL_TIMEOUT", "900"))


def _api_key() -> str:
    key = os.getenv("WEKNORA_API_KEY", "").strip()
    if key:
        return key
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("WEKNORA_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _weknora_get(path: str, **params):
    key = _api_key()
    if not key:
        raise RuntimeError("未配置 WEKNORA_API_KEY（环境变量或仓库根目录 .env）")
    url = f"{WEKNORA_BASE}/api/v1{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-API-Key": key})
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if isinstance(body, dict) and body.get("success") is False:
        raise RuntimeError(body.get("error") or body)
    return body.get("data", body) if isinstance(body, dict) else body


def _as_list(data) -> list:
    """兼容 data 直接是数组、{items: [...]}、{data: [...]} 三种形态。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "list", "records", "knowledge", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def _join_chunks(chunks: list) -> str:
    parts = []
    for chunk in sorted(chunks, key=lambda c: c.get("seq_id") or 0):
        text = (chunk.get("content") or "").strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def fetch_markdown(kid: str) -> tuple[str, str]:
    """返回 (正文, 标题)。优先详情正文，取不到再用 text 分块拼接。"""
    detail = _weknora_get(f"/knowledge/{kid}")
    if not isinstance(detail, dict):
        detail = {}
    title = detail.get("title") or detail.get("file_name") or ""

    for key in ("markdown", "markdown_content", "content", "text"):
        value = detail.get(key)
        if isinstance(value, str) and value.strip():
            return value, title

    data = _weknora_get(
        f"/chunks/{kid}", page=1, page_size=100, chunk_type="text"
    )
    return _join_chunks(_as_list(data)), title


def _tool_result_json(result) -> dict:
    texts = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if text:
            texts.append(text)
    raw = "\n".join(texts).strip()

    if getattr(result, "isError", False) or getattr(result, "is_error", False):
        raise RuntimeError(f"MCP 工具返回错误：{raw or result}")
    if not raw:
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return structured
        raise RuntimeError("MCP 未返回文本结果")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "raw": raw}


async def extract_via_mcp(kid: str) -> dict:
    async with streamable_http_client(MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            server_info = getattr(init, "server_info", None)
            if server_info is not None:
                print(f"  已连接 MCP：{server_info.name} {server_info.version}")
            result = await session.call_tool(
                "extract_from_knowledge",
                {"knowledge_id": kid},
                read_timeout_seconds=CALL_TIMEOUT,
            )
    return _tool_result_json(result)


def _kids_from_kb(kb_id: str) -> list:
    kids = []
    page = 1
    while True:
        data = _weknora_get(
            f"/knowledge-bases/{kb_id}/knowledge", page=page, page_size=100
        )
        batch = _as_list(data)
        for item in batch:
            if not isinstance(item, dict):
                continue
            kid = item.get("id") or item.get("knowledge_id")
            if kid:
                kids.append(str(kid))
        if len(batch) < 100:
            break
        page += 1
    return kids


def collect_one(kid: str, index: int, total: int) -> None:
    print(f"[{index}/{total}] 开始抽取 {kid}")
    started = time.time()

    markdown, title = "", ""
    try:
        print("  拉取原文 ...")
        markdown, title = fetch_markdown(kid)
        print(f"  原文 {len(markdown)} 字符" + (f"，标题：{title}" if title else ""))
    except Exception as exc:  # 原文只影响前端展示，不阻断抽取
        print(f"  [警告] 拉取原文失败：{type(exc).__name__}: {exc}")

    print("  调用 MCP extract_from_knowledge（约 150 秒）...")
    result = asyncio.run(extract_via_mcp(kid))
    if not isinstance(result, dict):
        result = {"ok": False, "raw": result}

    source_file = title or result.get("source_file") or kid
    payload = {
        "knowledge_id": kid,
        "source_file": source_file,
        "extracted_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_s": round(time.time() - started, 1),
        "markdown": markdown,
        "result": result,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"{kid}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    stats = result.get("stats") if isinstance(result.get("stats"), dict) else {}
    print(
        f"  完成：ok={result.get('ok')} "
        f"filled={stats.get('filled')} "
        f"avg_confidence={stats.get('avg_confidence')} "
        f"用时 {time.time() - started:.1f}s"
    )
    print(f"  已写入 {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description="调用 MCP 抽取证书字段并落盘")
    parser.add_argument("knowledge_ids", nargs="*", help="知识 ID，可传多个")
    parser.add_argument("--all", action="store_true", help="遍历知识库全部文档")
    parser.add_argument(
        "--kb",
        default=os.getenv("WEKNORA_KB_ID", ""),
        help="知识库 ID，配合 --all 使用（默认读 WEKNORA_KB_ID）",
    )
    args = parser.parse_args()

    kids = []
    if args.all:
        kb_id = args.kb.strip()
        if not kb_id:
            parser.error("--all 需要 --kb <kb_id>（或环境变量 WEKNORA_KB_ID）")
        print(f"拉取知识库 {kb_id} 的文档列表 ...")
        kids.extend(_kids_from_kb(kb_id))
        print(f"知识库共 {len(kids)} 份文档")

    kids.extend(args.knowledge_ids)

    seen = set()
    ordered = []
    for kid in kids:
        kid = kid.strip()
        if kid and kid not in seen:
            seen.add(kid)
            ordered.append(kid)
    if not ordered:
        parser.error("请提供 knowledge_id，或使用 --all --kb <kb_id>")

    failures = []
    for i, kid in enumerate(ordered, 1):
        try:
            collect_one(kid, i, len(ordered))
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            failures.append((kid, f"{type(exc).__name__}: {exc}"))
            print(f"  [失败] {kid}: {type(exc).__name__}: {exc}")

    print(f"\n完成 {len(ordered) - len(failures)}/{len(ordered)}")
    for kid, err in failures:
        print(f"  失败 {kid}: {err}")


if __name__ == "__main__":
    main()
