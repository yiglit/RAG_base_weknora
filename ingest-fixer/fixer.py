#!/usr/bin/env python3
"""知识库摄入修正器。

网页端上传文档后，WeKnora（MinerU）已经解析并分块。本脚本做两件事：
1. 修正 MinerU/OCR 常见的特殊符号误识别，并写回分块（PUT /chunks）；
2. 把修正后的分块按 seq_id 拼接成 Markdown，导出到 e:\\weknora\\md_out。

只依赖 Python 标准库。用法：
    python ingest-fixer/fixer.py --once
    python ingest-fixer/fixer.py --watch --interval 30
    python ingest-fixer/fixer.py --once --dry-run
"""
import argparse
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

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
MD_OUT = PROJECT_ROOT / "md_out"
STATE_PATH = ROOT / "state.json"
LOG_PATH = ROOT / "logs" / "fixer.log"

REQUEST_TIMEOUT = 120
PAGE_SIZE = 100


# ---------------------------------------------------------------- 配置 / 日志

def _read_env_file() -> dict:
    values = {}
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return values
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


_FILE_ENV = _read_env_file()
BASE_URL = (os.getenv("WEKNORA_BASE") or _FILE_ENV.get("WEKNORA_BASE") or "http://localhost:8080").rstrip("/")
API_KEY = os.getenv("WEKNORA_API_KEY") or _FILE_ENV.get("WEKNORA_API_KEY") or ""
DEFAULT_KB_ID = os.getenv("WEKNORA_KB_ID") or _FILE_ENV.get("WEKNORA_KB_ID") or ""


def log(message: str) -> None:
    """同时输出到 stdout 与 logs/fixer.log（追加，时间戳行）。"""
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        # 日志失败不影响主流程
        pass


# ---------------------------------------------------------------- HTTP 客户端

class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _request(method: str, path: str, params: dict = None, payload: dict = None):
    if not API_KEY:
        raise ApiError(0, "未配置 WEKNORA_API_KEY（环境变量或仓库根目录 .env）")
    url = f"{BASE_URL}/api/v1{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = None
    headers = {"X-API-Key": API_KEY}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise ApiError(exc.code, f"{method} {path} -> {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(0, f"{method} {path} 网络错误: {exc.reason}") from exc
    try:
        body = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise ApiError(0, f"{method} {path} 响应不是 JSON") from exc
    if isinstance(body, dict) and body.get("success") is False:
        raise ApiError(0, str(body.get("error") or body.get("message") or body))
    return body.get("data", body) if isinstance(body, dict) else body


def _as_list(data) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "list", "records", "knowledge", "chunks"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


# ---------------------------------------------------------------- 文本修正规则
#
# 扩展点：如需支持其它 OCR 变体，可继续在此追加保守正则，例如
#   - 欧姆符号：`Ω` / `ΩΩ` / `ohm` -> `Ω`；
#   - 温度再扩展：`T4/T5/T6` 组别不动，仅处理带 C 的完整温度；
#   - 型号连接符：`X` / `×` / `*` 只在型号串上下文里统一。
# 原则：宁可漏改，也不要在普通叙述里做全局替换。

_EX_GROUP_3 = re.compile(r"(?<![A-Za-z0-9])III([ABC])(?![A-Za-z0-9])")
_EX_GROUP_3_ALONE = re.compile(r"(?<![A-Za-z0-9])III(?![A-Za-z0-9])")
_EX_GROUP_2 = re.compile(r"(?<![A-Za-z0-9])II([BC])(?![A-Za-z0-9])")
# OCR 也常把 Ⅱ 识别成 ll / Il / lI / 1l 等字母数字组合：llB、IlB、lIB -> ⅡB。
_EX_GROUP_3_LC = re.compile(r"(?<![A-Za-z0-9])[Il1i]{3}[ \t]*([ABC])(?![A-Za-z0-9])")
_EX_GROUP_2_LC = re.compile(r"(?<![A-Za-z0-9])[Il1i]{2}[ \t]*([BC])(?![A-Za-z0-9])")
# OCR 常把 Ⅱ 识别成竖线：∥B / ‖B / ||B。不处理单个 |，避免误伤 Markdown 表格。
_EX_PIPE = re.compile(r"(?:∥|‖|\|\|)[ \t]*([BC])(?![A-Za-z0-9])")

# T95C / T80C / T130C -> T95℃
_TEMP_T = re.compile(r"(?<=[Tt])(\d{2,3})[ \t]*C(?![A-Za-z0-9])")
# 95°C / 95 oC / 95OC -> 95℃（仅 60~200，避免误伤编号、电流等）
_TEMP_DEG = re.compile(r"(?<![0-9])(\d{2,3})[ \t]*(?:°[ \t]*|[oO][ \t]*)C(?![0-9A-Za-z])")

# 5m2 / 5M2 / 5m² / 5M² / 5平方米 -> 5㎡
_AREA_M2 = re.compile(r"(?<=\d)[ \t]*[mM](?:2|²)(?![0-9A-Za-z])")
_AREA_WORD = re.compile(r"(?<=\d)[ \t]*平方米")

# 型号串里的方框占位：X52-S/K/口 -> X52-S/K/□；行内单独一个占位符也修正。
_BOX_AFTER_SEP = re.compile(r"(?<=[/\-（(])[ \t]*[口囗▢■]+(?![一-龥A-Za-z0-9])")
_BOX_ALONE_LINE = re.compile(r"(?m)^([ \t]*)[口囗▢■]+([ \t]*)$")


def _replace_temp_deg(match: re.Match) -> str:
    value = int(match.group(1))
    if 60 <= value <= 200:
        return f"{value}℃"
    return match.group(0)


def fix_text(text: str) -> str:
    """保守修正 MinerU/OCR 误识别的防爆标志、温度、面积与方框占位符。"""
    if not text:
        return text

    # 防爆等级：先 Ⅲ 级（IIIA/IIIB/IIIC），再单独 III，最后 Ⅱ 级，避免互相截断。
    text = _EX_GROUP_3_LC.sub(lambda m: f"Ⅲ{m.group(1)}", text)
    text = _EX_GROUP_3.sub(lambda m: f"Ⅲ{m.group(1)}", text)
    text = _EX_GROUP_3_ALONE.sub("Ⅲ", text)
    text = _EX_GROUP_2_LC.sub(lambda m: f"Ⅱ{m.group(1)}", text)
    text = _EX_GROUP_2.sub(lambda m: f"Ⅱ{m.group(1)}", text)
    text = _EX_PIPE.sub(lambda m: f"Ⅱ{m.group(1)}", text)

    text = _TEMP_T.sub(lambda m: f"{m.group(1)}℃", text)
    text = _TEMP_DEG.sub(_replace_temp_deg, text)

    text = _AREA_M2.sub("㎡", text)
    text = _AREA_WORD.sub("㎡", text)

    text = _BOX_AFTER_SEP.sub("□", text)
    text = _BOX_ALONE_LINE.sub(lambda m: f"{m.group(1)}□{m.group(2)}", text)
    return text


# ---------------------------------------------------------------- 分块 / 导出

def _fetch_chunks(knowledge_id: str) -> list:
    chunks = []
    page = 1
    while True:
        batch = _as_list(_request(
            "GET",
            f"/chunks/{knowledge_id}",
            params={"page": page, "page_size": PAGE_SIZE, "chunk_type": "text"},
        ))
        chunks.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        page += 1
    return sorted(chunks, key=lambda c: c.get("seq_id") or 0)


def _join_chunks(chunks: list, max_overlap: int = 200) -> str:
    """按 seq_id 拼接分块，并裁掉相邻块之间的重叠部分。"""
    out = ""
    for chunk in chunks:
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


_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_filename(title: str) -> str:
    name = _UNSAFE_FILENAME.sub("_", title or "untitled")
    name = name.rstrip(" .") or "untitled"
    return name + ".md"


def _change_preview(old: str, new: str, width: int = 90) -> str:
    """截取第一处差异附近的片段，供 dry-run 日志展示。"""
    i = 0
    while i < min(len(old), len(new)) and old[i] == new[i]:
        i += 1
    start = max(0, i - 25)
    before = old[start:start + width].replace("\n", " ")
    after = new[start:start + width].replace("\n", " ")
    return f"{before}  =>  {after}"


def _write_chunk(knowledge_id: str, chunk: dict, content: str) -> dict:
    payload = {"content": content}
    revision = chunk.get("content_revision")
    if revision is not None:
        payload["expected_revision"] = revision
    return _request("PUT", f"/chunks/{knowledge_id}/{chunk['id']}", payload=payload) or {}


def correct_knowledge(kid: str, title: str, dry_run: bool = False, chunks: list = None) -> dict:
    """修正一个文档的所有 text 分块并导出 Markdown。

    chunks 可选：调用方已拉取时可传入，避免重复请求。
    """
    title = title or kid
    chunks = sorted(chunks if chunks is not None else _fetch_chunks(kid),
                    key=lambda c: c.get("seq_id") or 0)
    final = {}
    changed = 0
    skipped = 0
    errors = []

    for chunk in chunks:
        original = chunk.get("content") or ""
        fixed = fix_text(original)
        if fixed == original:
            final[chunk["id"]] = original
            continue
        if not fixed.strip():
            skipped += 1
            final[chunk["id"]] = original
            log(f"[跳过] {title} chunk {chunk.get('seq_id')} 修正后为空")
            continue
        if dry_run:
            changed += 1
            final[chunk["id"]] = fixed
            log(f"[dry-run] {title} chunk {chunk.get('seq_id')} rev {chunk.get('content_revision')}: "
                f"{_change_preview(original, fixed)}")
            continue
        try:
            try:
                _write_chunk(kid, chunk, fixed)
            except ApiError as exc:
                if exc.status != 409:
                    raise
                # 版本冲突：重新拉取该分块的最新 revision 后重试一次
                log(f"[409] {title} chunk {chunk.get('seq_id')} 版本冲突，重拉后重试")
                refreshed = {c.get("id"): c for c in _fetch_chunks(kid)}
                latest = refreshed.get(chunk.get("id"))
                if not latest:
                    raise ApiError(409, "重拉后未找到该分块") from exc
                retry_content = fix_text(latest.get("content") or "")
                if retry_content == (latest.get("content") or ""):
                    final[chunk["id"]] = latest.get("content") or ""
                    skipped += 1
                    continue
                _write_chunk(kid, latest, retry_content)
                final[chunk["id"]] = retry_content
                changed += 1
                continue
            final[chunk["id"]] = fixed
            changed += 1
        except ApiError as exc:
            errors.append(f"chunk {chunk.get('seq_id')}: {exc}")
            final[chunk["id"]] = original
            log(f"[失败] {title} chunk {chunk.get('seq_id')}: {exc}")

    merged = [dict(c, content=final.get(c["id"], c.get("content") or "")) for c in chunks]
    markdown = _join_chunks(merged)
    md_path = MD_OUT / safe_filename(title)
    if not chunks:
        log(f"[跳过] {title} 没有 text 分块，不导出")
    elif not dry_run:
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown, encoding="utf-8", newline="\n")
        log(f"[导出] {md_path}（{len(markdown)} 字符）")
    else:
        log(f"[dry-run] 导出路径 {md_path}（预计 {len(markdown)} 字符，未写入）")

    return {
        "knowledge_id": kid,
        "title": title,
        "chunks": len(chunks),
        "changed_chunks": changed,
        "skipped_chunks": skipped,
        "md_path": str(md_path),
        "md_chars": len(markdown),
        "errors": errors,
    }


# ---------------------------------------------------------------- 状态 / 扫描

def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as exc:
        log(f"[警告] 读取状态文件失败，按空状态处理: {exc}")
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_PATH)
    except OSError as exc:
        log(f"[警告] 写入状态文件失败: {exc}")


def _list_docs(kb_id: str) -> list:
    docs = []
    page = 1
    while True:
        batch = _as_list(_request(
            "GET",
            f"/knowledge-bases/{kb_id}/knowledge",
            params={"page": page, "page_size": PAGE_SIZE},
        ))
        docs.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        page += 1
    return docs


def scan(kb_id: str, dry_run: bool = False) -> dict:
    """遍历知识库文档，修正分块并导出 Markdown。"""
    state = _load_state()
    results = []
    failed_docs = 0

    try:
        docs = _list_docs(kb_id)
    except ApiError as exc:
        log(f"[失败] 拉取文档列表: {exc}")
        return {"documents": 0, "changed_docs": 0, "failed_docs": 1, "results": []}

    targets = [
        doc for doc in docs
        if doc.get("type") in ("file", "manual") and doc.get("parse_status") == "completed"
    ]
    log(f"知识库 {kb_id} 命中 {len(targets)}/{len(docs)} 个已完成文档"
        + ("（dry-run，不写回）" if dry_run else ""))

    for doc in targets:
        kid = doc.get("id")
        title = doc.get("title") or doc.get("file_name") or kid
        if not kid:
            continue
        entry = state.get(kid) or {}
        try:
            chunks = _fetch_chunks(kid)
        except ApiError as exc:
            failed_docs += 1
            log(f"[失败] {title} 拉取分块: {exc}")
            continue

        pending = any(fix_text(c.get("content") or "") != (c.get("content") or "") for c in chunks)
        md_target = MD_OUT / safe_filename(title)
        unchanged = (
            entry.get("updated_at") == doc.get("updated_at")
            and not pending
            and md_target.exists()
        )
        if unchanged and not dry_run:
            log(f"[跳过] {title} 无未修正分块且已导出")
            continue

        try:
            result = correct_knowledge(kid, title, dry_run=dry_run, chunks=chunks)
        except ApiError as exc:
            failed_docs += 1
            log(f"[失败] {title}: {exc}")
            continue

        if result["errors"]:
            failed_docs += 1
        results.append(result)

        if not dry_run:
            state[kid] = {
                "title": title,
                "updated_at": doc.get("updated_at"),
                "revisions": {
                    c.get("id"): c.get("content_revision") for c in chunks
                },
                "md_path": result["md_path"],
                "changed_chunks": result["changed_chunks"],
                "processed_at": datetime.now().isoformat(timespec="seconds"),
            }
            _save_state(state)

    changed_docs = sum(1 for r in results if r["changed_chunks"])
    log(f"本轮完成：文档 {len(results)}，有修正 {changed_docs}，"
        f"修正分块 {sum(r['changed_chunks'] for r in results)}，失败 {failed_docs}")
    return {
        "documents": len(results),
        "changed_docs": changed_docs,
        "failed_docs": failed_docs,
        "results": results,
    }


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="WeKnora 知识库摄入修正器")
    parser.add_argument("--once", action="store_true", help="跑一轮后退出")
    parser.add_argument("--watch", action="store_true", help="持续轮询")
    parser.add_argument("--interval", type=int, default=30, help="轮询间隔秒数，默认 30")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写回、不导出")
    parser.add_argument("--kb", default=DEFAULT_KB_ID, help="知识库 id（默认读 .env 的 WEKNORA_KB_ID）")
    args = parser.parse_args(argv)

    if not args.kb:
        log("[失败] 未指定知识库：请传 --kb 或在 .env 配置 WEKNORA_KB_ID")
        return 1
    if not args.once and not args.watch:
        args.once = True

    try:
        if args.watch:
            interval = max(1, args.interval)
            while True:
                try:
                    scan(args.kb, dry_run=args.dry_run)
                except Exception as exc:  # 单轮异常不退出轮询
                    log(f"[失败] 本轮异常: {exc}")
                time.sleep(interval)
        summary = scan(args.kb, dry_run=args.dry_run)
        return 1 if summary["failed_docs"] else 0
    except KeyboardInterrupt:
        log("收到中断，退出")
        return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
