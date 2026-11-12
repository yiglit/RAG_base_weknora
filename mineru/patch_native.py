"""给 MinerU 4.0.12 打补丁：绕过损坏的 Rust 快路径。

背景：4.0.12 的 `_owned_span_texts` 调用 Rust 侧 `prepare_span_texts` 时参数签名不匹配，
表现为 PDF 解析直接失败（Document parsing failed）。这里强制该函数返回 None，
让 MinerU 走纯 Python 回退路径。

Dockerfile 构建时自动执行；也可在容器内手动执行：
    docker exec mineru-api python /tmp/patch_native.py

脚本可重复执行，已打过补丁会直接跳过。
"""
import sys
from pathlib import Path

DEFAULT_TARGET = Path(
    "/usr/local/lib/python3.11/site-packages/mineru/backend/analysis/pdf/text/native.py"
)
MARKER = "patched: mineru 4.0.12 rust fast path"
SIGNATURE = (
    "def _owned_span_texts(owner: Any, spans: list[_AnalyzeSpan], "
    "median_height: float, detect_scripts: bool) -> list[tuple] | None:"
)


def patch(target: Path) -> None:
    source = target.read_text(encoding="utf-8")
    if MARKER in source:
        print(f"already patched: {target}")
        return

    start = source.find(SIGNATURE)
    if start < 0:
        raise SystemExit(f"signature not found in {target}; MinerU version mismatch?")

    line_end = source.find("\n", start) + 1
    injected = (
        f"    # {MARKER}: prepare_span_texts signature mismatch,\n"
        "    # force the pure-python fallback.\n"
        "    return None\n"
    )
    target.write_text(source[:line_end] + injected + source[line_end:], encoding="utf-8")
    print(f"patched: {target}")


if __name__ == "__main__":
    patch(Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TARGET)
