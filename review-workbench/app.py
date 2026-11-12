"""人工校对工作台后端（只依赖 Python 标准库）。

启动：
    python review-workbench/app.py
访问：
    http://127.0.0.1:8770
"""
import csv
import json
import os
import re
import sys
import threading
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
RESULTS_DIR = ROOT / "results"
CACHE_DIR = RESULTS_DIR / ".cache"
CERT_REGISTRY = ROOT.parent / "cert-registry"
EVAL_CSV = ROOT.parent / "eval" / "field_labels.csv"

HOST = "127.0.0.1"
PORT = 8770

FIELDS = [
    "cert_no", "institution", "manufacturer", "product_name", "model_spec",
    "ex_marking", "standard", "drawing_no", "issue_date", "valid_until",
    "voltage", "current", "enclosure",
]

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

LABEL_HEADER = ["file", "field", "gold"]
LABEL_LOCK = threading.Lock()


def _field_value(item) -> str:
    """单个字段的可读值：优先 value，多值字段退到 values。"""
    if not isinstance(item, dict):
        return ""
    value = item.get("value")
    if isinstance(value, str) and value.strip():
        return value.strip()
    values = item.get("values")
    if isinstance(values, list):
        return " / ".join(str(v).strip() for v in values if str(v).strip())
    return ""


def _field_confidence(item):
    if not isinstance(item, dict):
        return None
    conf = item.get("confidence")
    if isinstance(conf, (int, float)) and not isinstance(conf, bool):
        return float(conf)
    return None


def _summarize_fields(fields: dict) -> tuple[int, float]:
    filled = 0
    scores = []
    for name in FIELDS:
        item = fields.get(name)
        if _field_value(item):
            filled += 1
            conf = _field_confidence(item)
            if conf is not None:
                scores.append(conf)
    avg = round(sum(scores) / len(scores), 3) if scores else 0.0
    return filled, avg


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _kid_ok(kid: str) -> bool:
    return bool(kid) and kid not in (".", "..") and bool(re.fullmatch(r"[A-Za-z0-9_.-]+", kid))


def _result_path(kid: str) -> Path:
    return RESULTS_DIR / f"{kid}.json"


def _review_path(kid: str) -> Path:
    return RESULTS_DIR / f"{kid}.review.json"


def _load_review(kid: str) -> dict:
    data = _load_json(_review_path(kid))
    if not isinstance(data, dict):
        return {"knowledge_id": kid, "updated_at": None, "fields": {}}
    data.setdefault("knowledge_id", kid)
    data.setdefault("updated_at", None)
    if not isinstance(data.get("fields"), dict):
        data["fields"] = {}
    return data


def _review_summary(kid: str) -> tuple[bool, str]:
    review = _load_review(kid)
    marked = [
        name for name in FIELDS
        if isinstance(review["fields"].get(name), dict)
        and review["fields"][name].get("status") in ("ok", "reject")
    ]
    if not marked:
        return False, "未开始"
    if len(marked) == len(FIELDS):
        return True, "已完成"
    return True, "进行中"


def _doc_summary(path: Path):
    data = _load_json(path)
    if not isinstance(data, dict):
        return None
    kid = path.stem
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    fields = result.get("fields") if isinstance(result.get("fields"), dict) else {}
    filled, avg = _summarize_fields(fields)
    reviewed, status = _review_summary(kid)
    return {
        "knowledge_id": data.get("knowledge_id") or kid,
        "source_file": data.get("source_file") or result.get("source_file") or kid,
        "extracted_at": data.get("extracted_at") or "",
        "filled": filled,
        "total": len(FIELDS),
        "avg_confidence": avg,
        "reviewed": reviewed,
        "status": status,
    }


def _docs_list() -> list:
    docs = []
    if RESULTS_DIR.is_dir():
        for path in RESULTS_DIR.glob("*.json"):
            if path.name.endswith(".review.json"):
                continue
            summary = _doc_summary(path)
            if summary:
                docs.append(summary)
    docs.sort(key=lambda d: (d.get("extracted_at") or "", d.get("knowledge_id") or ""), reverse=True)
    return docs


def _load_doc(kid: str):
    data = _load_json(_result_path(kid))
    if not isinstance(data, dict):
        return None
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    fields = result.get("fields") if isinstance(result.get("fields"), dict) else {}
    filled, avg = _summarize_fields(fields)
    stats = dict(result.get("stats") or {}) if isinstance(result.get("stats"), dict) else {}
    stats.update({
        "fields_total": len(FIELDS),
        "filled": filled,
        "avg_confidence": avg,
    })
    return {
        "knowledge_id": data.get("knowledge_id") or kid,
        "source_file": data.get("source_file") or result.get("source_file") or kid,
        "extracted_at": data.get("extracted_at") or "",
        "markdown": data.get("markdown") or "",
        "fields": fields,
        "issues": result.get("issues") if isinstance(result.get("issues"), list) else [],
        "stats": stats,
    }


def _weknora_env() -> dict:
    """读取仓库根目录 .env，环境变量优先。"""
    values = {}
    env_file = ROOT.parent / ".env"
    try:
        text = env_file.read_text(encoding="utf-8")
    except OSError:
        text = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    for name in ("WEKNORA_BASE", "WEKNORA_API_KEY", "WEKNORA_KB_ID"):
        if os.getenv(name):
            values[name] = os.getenv(name)
    return values


def _source_knowledge_id(kid: str) -> str:
    """结果 JSON 里的知识库文档 id（原件下载用）。"""
    data = _load_json(_result_path(kid))
    if not isinstance(data, dict):
        return ""
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    value = data.get("knowledge_id") or result.get("knowledge_id") or ""
    return str(value).strip()


def _pdf_pages(path: Path) -> int:
    """用 PyMuPDF 探测 PDF 页数；非 PDF/损坏返回 0。"""
    try:
        import fitz
    except Exception:  # noqa: BLE001 - 缺依赖时降级为不可用
        return 0
    doc = None
    try:
        doc = fitz.open(str(path))
        if not getattr(doc, "is_pdf", True):
            return 0
        return int(doc.page_count)
    except Exception:  # noqa: BLE001 - 非法格式按不可用处理
        return 0
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:  # noqa: BLE001
                pass


def _cache_dir(kid: str) -> Path:
    return CACHE_DIR / kid


def _ensure_source(kid: str):
    """下载并缓存原件，返回 (路径, 页数)；不可用返回 (None, 0)。"""
    target = _cache_dir(kid) / "source.bin"
    if target.is_file() and target.stat().st_size > 0:
        pages = _pdf_pages(target)
        if pages:
            return target, pages
        try:
            target.unlink()
        except OSError:
            pass

    knowledge_id = _source_knowledge_id(kid)
    if not knowledge_id:
        return None, 0
    env = _weknora_env()
    base = (env.get("WEKNORA_BASE") or "http://127.0.0.1").rstrip("/")
    api_key = env.get("WEKNORA_API_KEY") or ""
    if not api_key:
        return None, 0

    url = f"{base}/api/v1/knowledge/{quote(knowledge_id)}/download"
    request = urllib.request.Request(url, headers={"X-API-Key": api_key})
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            data = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return None, 0
    if not data:
        return None, 0

    tmp = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except OSError:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return None, 0
    pages = _pdf_pages(target)
    if not pages:
        try:
            target.unlink()
        except OSError:
            pass
        return None, 0
    return target, pages


def _render_page(kid: str, page_no: int):
    """把第 page_no 页（1 基）渲染成 PNG，返回 (路径, 页数)；越界/失败返回 (None, 页数)。"""
    source, pages = _ensure_source(kid)
    if source is None:
        return None, 0
    if page_no < 1 or page_no > pages:
        return None, pages

    target = _cache_dir(kid) / f"page-{page_no}.png"
    if target.is_file() and target.stat().st_size > 0:
        return target, pages
    try:
        import fitz
    except Exception:  # noqa: BLE001
        return None, pages

    doc = None
    tmp = target.with_name(f"page-{page_no}.tmp.png")
    try:
        doc = fitz.open(str(source))
        page = doc.load_page(page_no - 1)
        pix = page.get_pixmap(dpi=150)
        target.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(tmp))
        os.replace(tmp, target)
    except Exception:  # noqa: BLE001 - 渲染失败按不可用处理
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        return None, pages
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:  # noqa: BLE001
                pass
    return target, pages


_PUBLISH_MODULE = None
_CACHE_LOCK = threading.RLock()


def _publisher():
    """按 sys.path 导入 cert-registry/publish.py 的 publish。"""
    global _PUBLISH_MODULE
    if _PUBLISH_MODULE is None:
        if not (CERT_REGISTRY / "publish.py").is_file():
            raise RuntimeError("未找到 cert-registry/publish.py")
        if str(CERT_REGISTRY) not in sys.path:
            sys.path.insert(0, str(CERT_REGISTRY))
        import publish as module  # noqa: PLC0415 - 延迟导入，避免影响工作台启动
        _PUBLISH_MODULE = module
    return _PUBLISH_MODULE.publish


def _publish_doc(kid: str) -> dict:
    """13 字段全部通过后，幂等入库并同步参数卡。"""
    doc = _load_doc(kid)
    if doc is None:
        raise FileNotFoundError(kid)

    review = _load_review(kid)
    reviewed = review["fields"]
    marked = {
        name: reviewed[name] for name in FIELDS
        if isinstance(reviewed.get(name), dict)
        and reviewed[name].get("status") in ("ok", "reject")
    }
    if len(marked) < len(FIELDS):
        raise ValueError(f"还有 {len(FIELDS) - len(marked)} 个字段未标记，请先完成校对")
    rejected = [name for name in FIELDS if marked[name].get("status") == "reject"]
    if rejected:
        raise ValueError(f"有 {len(rejected)} 个字段被驳回，请修正后通过")

    fields = doc.get("fields") if isinstance(doc.get("fields"), dict) else {}
    for name in FIELDS:
        value = marked[name].get("value")
        value = "" if value is None else str(value)
        item = fields.get(name)
        if not isinstance(item, dict):
            fields[name] = {"value": value}
            continue
        current = item.get("value")
        current = "" if current is None else str(current)
        if current != value:
            item["value"] = value

    env = _weknora_env()
    kb_id = env.get("WEKNORA_KB_ID") or ""
    source_file = doc.get("source_file") or kid
    source_kid = doc.get("knowledge_id") or ""
    if not (len(source_kid) == 36 and source_kid.count("-") == 4):
        source_kid = ""
    publish = _publisher()
    try:
        result = publish(
            {"fields": fields}, source_file, kb_id=kb_id,
            source_knowledge_id=source_kid,
        )
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成可读错误
        raise RuntimeError(f"入库失败：{exc}") from exc

    published_at = datetime.now().isoformat(timespec="seconds")
    review["published_at"] = published_at
    review["publish"] = result
    review_path = _review_path(kid)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = review_path.with_name(review_path.name + ".tmp")
    tmp.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, review_path)
    return {"ok": True, "published_at": published_at, "publish": result}


def _original_info(kid: str) -> dict:
    if not _result_path(kid).is_file():
        return {"available": False, "pages": 0, "file_type": "pdf"}
    with _CACHE_LOCK:
        source, pages = _ensure_source(kid)
    return {"available": bool(source), "pages": pages if source else 0, "file_type": "pdf"}


def _write_label(source_file: str, field: str, value: str) -> None:
    """写入 eval/field_labels.csv，同 (file, field) 覆盖而不是重复追加。"""
    EVAL_CSV.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    if EVAL_CSV.exists() and EVAL_CSV.stat().st_size > 0:
        with EVAL_CSV.open("r", encoding="utf-8", newline="") as fh:
            rows = [row for row in csv.reader(fh) if row]

    body = rows
    if rows and [cell.strip() for cell in rows[0][:3]] == LABEL_HEADER:
        body = rows[1:]

    replaced = False
    for row in body:
        if len(row) >= 2 and row[0] == source_file and row[1] == field:
            row[:] = [source_file, field, value]
            replaced = True
            break
    if not replaced:
        body.append([source_file, field, value])

    tmp = EVAL_CSV.with_name(EVAL_CSV.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(LABEL_HEADER)
        writer.writerows(body)
    os.replace(tmp, EVAL_CSV)


def _save_review(kid: str, body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")
    payload_fields = body.get("fields")
    if not isinstance(payload_fields, dict):
        raise ValueError("fields 必须是对象")

    cleaned = {}
    for name, entry in payload_fields.items():
        if name not in FIELDS:
            raise ValueError(f"未知字段：{name}")
        if not isinstance(entry, dict):
            raise ValueError(f"{name}: 校对项必须是对象")
        status = entry.get("status")
        if status not in ("ok", "reject"):
            raise ValueError(f"{name}: status 只能是 ok 或 reject")
        value = entry.get("value")
        if value is None:
            value = ""
        elif not isinstance(value, str):
            value = str(value)
        note = entry.get("note")
        cleaned[name] = {
            "status": status,
            "value": value,
            "note": "" if note is None else str(note),
        }

    doc_path = _result_path(kid)
    if not doc_path.is_file():
        raise FileNotFoundError(kid)
    doc = _load_json(doc_path) or {}
    result = doc.get("result") if isinstance(doc.get("result"), dict) else {}
    source_file = doc.get("source_file") or result.get("source_file") or kid

    review = _load_review(kid)
    merged = dict(review["fields"])
    merged.update(cleaned)
    updated_at = datetime.now().isoformat(timespec="seconds")
    review = {"knowledge_id": kid, "updated_at": updated_at, "fields": merged}

    review_path = _review_path(kid)
    review_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = review_path.with_name(review_path.name + ".tmp")
    tmp.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, review_path)

    labels_written = 0
    with LABEL_LOCK:
        for name, entry in cleaned.items():
            if entry["status"] == "ok":
                _write_label(source_file, name, entry["value"])
                labels_written += 1

    return {
        "ok": True,
        "knowledge_id": kid,
        "updated_at": updated_at,
        "saved": len(cleaned),
        "labels_written": labels_written,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ReviewWorkbench/1.0"

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _dispatch(self, method: str):
        path = unquote(urlparse(self.path).path)
        try:
            if method == "GET" and path == "/":
                return self._send_file(STATIC_DIR / "index.html")

            if path == "/api/docs":
                if method != "GET":
                    return self._send_json({"error": "method not allowed"}, 405)
                return self._send_json(_docs_list())

            if path.startswith("/api/docs/"):
                parts = path[len("/api/docs/"):].strip("/").split("/")
                kid = parts[0]
                if not _kid_ok(kid):
                    return self._send_json({"error": "非法的文档 id"}, 400)

                if len(parts) == 1:
                    if method != "GET":
                        return self._send_json({"error": "method not allowed"}, 405)
                    doc = _load_doc(kid)
                    if doc is None:
                        return self._send_json({"error": f"文档不存在：{kid}"}, 404)
                    return self._send_json(doc)

                if len(parts) == 2 and parts[1] == "review":
                    if method == "GET":
                        return self._send_json(_load_review(kid))
                    if method == "POST":
                        body = self._read_json()
                        return self._send_json(_save_review(kid, body))
                    return self._send_json({"error": "method not allowed"}, 405)

                if len(parts) == 2 and parts[1] == "original":
                    if method != "GET":
                        return self._send_json({"error": "method not allowed"}, 405)
                    return self._send_json(_original_info(kid))

                if len(parts) == 3 and parts[1] == "page" and parts[2].endswith(".png"):
                    if method != "GET":
                        return self._send_json({"error": "method not allowed"}, 405)
                    raw = parts[2][:-4]
                    if not raw.isdigit():
                        return self._send_json({"error": "非法的页码"}, 400)
                    page_no = int(raw)
                    with _CACHE_LOCK:
                        target, pages = _render_page(kid, page_no)
                        if target is None:
                            message = f"页码超出范围：{page_no}" if pages else "原件不可用"
                            return self._send_json({"error": message}, 404)
                        data = target.read_bytes()
                    return self._send_bytes(data, "image/png")

                if len(parts) == 2 and parts[1] == "publish":
                    if method != "POST":
                        return self._send_json({"error": "method not allowed"}, 405)
                    return self._send_json(_publish_doc(kid))

                return self._send_json({"error": "not found"}, 404)

            if method == "GET" and path.startswith("/static/"):
                return self._serve_static(path)

            return self._send_json({"error": "not found"}, 404)
        except FileNotFoundError:
            self._send_json({"error": "文档不存在"}, 404)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001 - 兜底并保留服务
            traceback.print_exc()
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}")
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _serve_static(self, path: str):
        rel = path[len("/static/"):]
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return self._send_json({"error": "forbidden"}, 403)
        return self._send_file(target)

    def _send_file(self, path: Path):
        if not path.is_file():
            return self._send_json({"error": "not found"}, 404)
        data = path.read_bytes()
        content_type = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, content_type: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj, code: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"人工校对工作台: http://{HOST}:{PORT}")
    print(f"结果目录: {RESULTS_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
