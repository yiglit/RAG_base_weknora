"""把 WeKnora 的检索结果组装成统一 refs，并做一次出口校验。

对应《新手实施手册》13.4 / 13.5。
用法：
  python citation-validator/build_refs.py "CE22.1912 的防爆标志是什么？"
"""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

from validator import build_text_index, final_answer, validate_references

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "cert-registry" / "registry.db"

load_dotenv(ROOT / ".env")

BASE = os.getenv("WEKNORA_BASE", "http://localhost:8080").rstrip("/")
KEY = os.getenv("WEKNORA_API_KEY", "")
KB_ID = os.getenv("WEKNORA_KB_ID", "")


def search(query: str, kb_ids: list | None = None, match_count: int = 5) -> list:
    """POST /api/v1/knowledge-search，返回 data 数组（不打 LLM 总结）。"""
    resp = requests.post(
        f"{BASE}/api/v1/knowledge-search",
        headers={"X-API-Key": KEY},
        json={
            "query": query,
            "knowledge_base_ids": kb_ids or [KB_ID],
            "match_count": match_count,
        },
        timeout=120,
    )
    resp.raise_for_status()
    body = resp.json()
    if isinstance(body, dict) and body.get("success") is False:
        raise RuntimeError(body)
    return (body.get("data") if isinstance(body, dict) else body) or []


def cert_no_by_knowledge(db_path: Path = DB) -> dict[str, str]:
    """knowledge_id -> cert_no，来自第 11 节业务库。

    同时收录原件（weknora_knowledge_id）与 ⑦ 回写的参数卡（param_card_knowledge_id），
    两类文档都能作为出处依据。
    """
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT weknora_knowledge_id, cert_no FROM certificate "
            "WHERE weknora_knowledge_id IS NOT NULL "
            "UNION "
            "SELECT param_card_knowledge_id, cert_no FROM certificate "
            "WHERE param_card_knowledge_id IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        # 老库还没有 param_card_knowledge_id 列
        rows = conn.execute(
            "SELECT weknora_knowledge_id, cert_no FROM certificate "
            "WHERE weknora_knowledge_id IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return {kid: cert_no for kid, cert_no in rows if kid}


_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def _cjk_terms(text: str) -> set[str]:
    """把问句切成中文 2 字片段，用于挑选最相关的 locator。"""
    terms = set()
    for run in _CJK_RUN.findall(text or ""):
        for i in range(len(run) - 1):
            terms.add(run[i:i + 2])
    return terms


def pick_locator(item: dict, query: str = "") -> dict:
    """挑一条 locator 作为引用依据。

    一个分块可能带十几条 locator（段落级 bbox + quote），直接取第一条往往
    是“电了”这类 OCR 碎片。这里优先取与问句中文片段重合度最高的，
    其次取带 page 的，最后取更长的 quote。
    """
    locators = item.get("source_locators") or []
    if not locators:
        return {}
    terms = _cjk_terms(query)

    def score(loc: dict) -> tuple:
        quote = loc.get("quote") or ""
        hits = sum(1 for term in terms if term in quote)
        return hits, loc.get("page") is not None, len(quote)

    return max(locators, key=score)


def to_refs(items: list, cert_no_map: dict, query: str = "") -> list[dict]:
    refs = []
    for item in items:
        loc = pick_locator(item, query)
        kid = item.get("knowledge_id")
        refs.append({
            "cert_no": cert_no_map.get(kid),
            "knowledge_id": kid,
            "knowledge_title": item.get("knowledge_title") or item.get("knowledge_filename"),
            "chunk_id": item.get("chunk_id") or item.get("id"),
            "quote": loc.get("quote") or (item.get("content") or "")[:120],
            "page": loc.get("page"),
            "bbox": loc.get("bbox"),
        })
    return refs


def format_report(result) -> str:
    lines = [
        f"kept={len(result.kept)} dropped={len(result.dropped)} degraded={len(result.degraded)}"
    ]
    for ref in result.kept:
        page = f"第{ref['page']}页" if ref.get("page") is not None else "无页码（仅文本依据）"
        lines.append(
            f"[保留] 证书编号={ref.get('cert_no')}｜{ref.get('knowledge_title')}｜"
            f"{page}｜原文=\"{ref.get('quote')}\""
        )
    for ref in result.dropped:
        lines.append(
            f"[剔除] 原因={ref.get('reason')}｜{ref.get('knowledge_title')}｜"
            f"原文=\"{ref.get('quote')}\""
        )
    return "\n".join(lines)


def main():
    query = sys.argv[1] if len(sys.argv) > 1 else "CE22.1912 的防爆标志是什么？"
    items = search(query)
    print(f"检索命中 {len(items)} 条")

    if items:
        # 手册 13.4：不同版本字段名可能不同，先打印一条看
        print("首个检索结果的字段：", sorted(items[0].keys()))
        print(
            "首个 source_locators：",
            json.dumps(items[0].get("source_locators"), ensure_ascii=False),
        )

    text_index = build_text_index(items)
    refs = to_refs(items, cert_no_by_knowledge(), query)
    result = validate_references(refs, text_index)

    print(format_report(result))
    print()
    if result.kept:
        print(f"出口校验通过：{len(result.kept)} 条结论均有出处，可输出。")
    else:
        print(final_answer("", result))


if __name__ == "__main__":
    main()
