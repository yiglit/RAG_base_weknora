#!/usr/bin/env python3
"""从 WeKnora 导出解析后的 Markdown 到 md_out/。

用法：
    python export_md.py --list-kbs
    python export_md.py --kb <知识库ID>
    python export_md.py --kb <知识库ID> --knowledge <知识ID>
    python export_md.py --kb <知识库ID> --out md_out

API Key 通过 --key 或环境变量 WEKNORA_API_KEY 传入；
BASE 通过 --base 或环境变量 WEKNORA_BASE 传入（默认 http://localhost:8080/api/v1）。
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "http://localhost:8080/api/v1"
PAGE_SIZE = 100


def api_get(base: str, key: str, path: str, params: dict | None = None) -> dict:
    url = base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(
        url,
        headers={"X-API-Key": key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        sys.exit(f"请求失败 HTTP {e.code}: {url}\n{body}")
    except urllib.error.URLError as e:
        sys.exit(f"无法连接 {url}: {e.reason}")


def list_all(base: str, key: str, path: str, params: dict | None = None) -> list[dict]:
    """按页拉取列表接口，直到取完 total 条。"""
    items: list[dict] = []
    page = 1
    while True:
        query = dict(params or {})
        query.update({"page": page, "page_size": PAGE_SIZE})
        payload = api_get(base, key, path, query)
        if not payload.get("success", True):
            sys.exit(f"接口返回失败：{payload.get('message') or payload}")
        batch = payload.get("data") or []
        items.extend(batch)
        total = int(payload.get("total") or 0)
        if not batch or len(items) >= total:
            break
        page += 1
    return items


def append_chunk_content(acc: str, nxt: str, position_overlap: int) -> str:
    """拼接两段分块并去掉重叠部分（与前端 doc-content.vue 的合并逻辑一致）。"""
    if not acc:
        return nxt
    if not nxt:
        return acc
    if position_overlap <= 0:
        return acc + nxt

    min_overlap = 12
    span = max(position_overlap, 0)
    max_k = min(len(acc), len(nxt), max(span * 3, 400))
    head_slack = max(span * 2, 320)

    for k in range(max_k, min_overlap - 1, -1):
        suffix = acc[-k:]
        pos = nxt.find(suffix)
        if pos != -1 and pos <= head_slack:
            return acc + nxt[pos + k:]
    return acc + nxt


def merge_chunks(chunks: list[dict]) -> str:
    """把 text 分块按 start_at 排序后还原为整篇 Markdown。"""
    if not chunks:
        return ""

    ordered = sorted(
        chunks,
        key=lambda c: c.get("start_at") if c.get("start_at") is not None else c.get("chunk_index", 0),
    )

    first = ordered[0]
    merged = first.get("content") or ""
    merged_end = first.get("end_at") or 0

    for chunk in ordered[1:]:
        content = chunk.get("content") or ""
        if not content:
            continue
        start_at = chunk.get("start_at") or 0
        end_at = chunk.get("end_at") or 0

        if start_at > merged_end and merged_end > 0:
            merged = merged + "\n\n" + content
        else:
            merged = append_chunk_content(merged, content, merged_end - start_at)

        if end_at > merged_end:
            merged_end = end_at

    return merged


def safe_filename(title: str, fallback: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title or "").strip(" ._")
    name = name or fallback
    return name if name.lower().endswith(".md") else name + ".md"


def main() -> None:
    parser = argparse.ArgumentParser(description="导出 WeKnora 解析后的 Markdown")
    parser.add_argument("--base", default=os.getenv("WEKNORA_BASE", DEFAULT_BASE), help="API 根地址")
    parser.add_argument("--key", default=os.getenv("WEKNORA_API_KEY"), help="空间级 API Key")
    parser.add_argument("--kb", default=os.getenv("WEKNORA_KB_ID"), help="知识库 ID")
    parser.add_argument("--knowledge", help="只导出指定知识 ID")
    parser.add_argument("--out", default="md_out", help="输出目录（默认 md_out）")
    parser.add_argument("--list-kbs", action="store_true", help="只列出知识库")
    args = parser.parse_args()

    if not args.key:
        sys.exit("缺少 API Key：请设置环境变量 WEKNORA_API_KEY 或传 --key")

    if args.list_kbs:
        for kb in list_all(args.base, args.key, "/knowledge-bases"):
            print(f"{kb.get('id')}\t{kb.get('name')}")
        return

    if not args.kb:
        sys.exit("缺少知识库 ID：请传 --kb，或先用 --list-kbs 查看")

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)

    knowledges = list_all(args.base, args.key, f"/knowledge-bases/{args.kb}/knowledge")
    if args.knowledge:
        knowledges = [k for k in knowledges if k.get("id") == args.knowledge]
        if not knowledges:
            sys.exit(f"知识库中找不到知识 ID：{args.knowledge}")

    used_names: set[str] = set()
    exported = skipped = 0

    for knowledge in knowledges:
        kid = knowledge.get("id", "")
        title = knowledge.get("title") or knowledge.get("file_name") or kid
        status = knowledge.get("parse_status", "")

        if status != "completed":
            print(f"跳过 [{status or '未知状态'}] {title}")
            skipped += 1
            continue

        chunks = list_all(
            args.base,
            args.key,
            f"/chunks/{kid}",
            {"chunk_type": "text"},
        )
        markdown = merge_chunks(chunks)
        if not markdown.strip():
            print(f"跳过 [无文本分块] {title}")
            skipped += 1
            continue

        filename = safe_filename(title, kid)
        stem, ext = os.path.splitext(filename)
        candidate = filename
        index = 2
        while candidate.lower() in used_names or os.path.exists(os.path.join(out_dir, candidate)):
            candidate = f"{stem}_{index}{ext}"
            index += 1
        used_names.add(candidate.lower())

        path = os.path.join(out_dir, candidate)
        with open(path, "w", encoding="utf-8") as f:
            f.write(markdown)

        print(f"导出 {title} -> {path}（{len(chunks)} 个分块）")
        exported += 1

    print(f"完成：导出 {exported} 份，跳过 {skipped} 份，输出目录 {out_dir}")


if __name__ == "__main__":
    main()
