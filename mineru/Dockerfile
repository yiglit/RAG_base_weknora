# MinerU 4.0 解析服务（CPU / ONNX 基础档）
# 说明：WeKnora 的 mineru 引擎通过 MinerU 4.0 的 V1 API
# （/v1/uploads、/v1/parse/jobs）调用，4.1 已删除这些路由，因此必须锁定 4.0.x。
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn \
    MINERU_MODEL_SOURCE=local \
    MINERU_MODEL_SMALL_BACKEND=onnx

# ONNX 推理所需的系统库（apt 源换成阿里云，避免 deb.debian.org 在国内过慢）
RUN sed -i 's|http://deb.debian.org|https://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 锁定 4.0.12：4.1 的 /v1/tasks 协议与 WeKnora 现有客户端不兼容
RUN pip install --no-cache-dir "mineru==4.0.12"

# 绕过 4.0.12 Rust 快路径的 prepare_span_texts 签名 bug（否则 PDF 解析必失败）
COPY patch_native.py /tmp/patch_native.py
RUN python /tmp/patch_native.py

# 构建期预下载基础档模型（来自 ModelScope），运行期即可完全离线
RUN mineru-kit models download --tier basic --small-backend onnx -s modelscope

EXPOSE 8000

ENTRYPOINT ["mineru-kit"]
CMD ["api-server", "--host", "0.0.0.0", "--port", "8000", "--tier", "basic"]
