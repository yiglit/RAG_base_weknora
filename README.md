# 防爆合格证抽取 · 校对 · 知识问答（WeKnora 扩展）

基于 [WeKnora v0.9.0](https://github.com/Tencent/WeKnora) 的离线二次开发项目：

- 把 PDF 防爆合格证/检验报告解析成 Markdown，抽取 13 个结构化字段，并给出页码与原文出处；
- 提供人工校对台（原件分页 + bbox 高亮），审核通过后**只写入本地参数库**，不再往知识库塞「参数卡」；
- 参数库支持条件检索（电压/电流/外壳/防爆标志/机构等），以 MCP 工具形式供智能体调用；
- 问答智能体同时使用 **WeKnora 知识库语义检索** 与 **MCP 条件检索**，合并结果并逐条给出来源。

全离线运行：PDF 解析用 MinerU，抽取/问答用宿主机 Ollama，不调用任何公有云 OCR 或大模型 API。

---

## 1. 架构与端口

```text
浏览器 ──▶ WeKnora-frontend :80
              │
              ▼
        WeKnora-app :8080 ──▶ postgres / redis / docreader
              │                     │
              │                     └─ pdf → mineru-api :8000
              │
              ├─ 语义检索 ──▶ Ollama :11434（对话 + 嵌入）
              ├─ 重排 ──────▶ TEI rerank :8081
              └─ MCP ──────▶ cert-extractor :8765
                                 ├─ cert-registry/registry.db（参数库）
                                 └─ review-workbench :8770（人工校对）
```

| 端口 | 服务 | 说明 |
| --- | --- | --- |
| 80 | WeKnora 前端 | 网页入口 |
| 8080 | WeKnora API | 知识库、检索、智能体 |
| 8000 | MinerU | PDF 解析 |
| 8081 | TEI Rerank | bge-reranker-v2-m3 |
| 11434 | Ollama（宿主机） | 本地模型 |
| 8765 | MCP 抽取服务 | 抽取 / 校验 / 条件检索 / 带出处问答 |
| 8770 | 人工校对台 | 分页预览、字段核对、发布 |
| 8780 | 出处校验边车 | `/ask`、`/validate` |

---

## 2. 目录结构

```text
<项目根>
├─ WeKnora\                 上游源码（含 2 处本地补丁）+ docker-compose + config
├─ cert-extractor\          MCP 服务（server.py）
├─ review-workbench\        人工校对台（app.py + static/）
├─ cert-registry\           参数库：publish.py / upsert.py / normalize.py / schema.sql / init_db.py
├─ citation-validator\      出处校验边车
├─ structured-search\       条件检索 DSL 与参数化 SQL
├─ ingest-fixer\            摄入修正轮询（fixer.py）
├─ scripts\                 local-services.ps1 等运维脚本
├─ mineru\                  MinerU 4.0 镜像构建与补丁
├─ tei-models\              bge-reranker-v2-m3 模型目录（不入 git）
```

> `WeKnora/` 内含 2 个本地补丁：`internal/application/service/chat_pipeline/references.go`、`internal/modelcontext/model_output.go`，用于让检索块带上「第 N 页」等可读出处。**不要用上游 clone 覆盖该目录**，否则出处能力会丢失。

---

## 3. 环境要求

| 项 | 要求 |
| --- | --- |
| 系统 | Windows 10/11 + Docker Desktop（WSL2）或 Linux + Docker Engine |
| Docker Compose | ≥ 2.20 |
| Ollama | ≥ 0.5，宿主机运行 |
| Python | 3.11 |
| GPU | 最低 8 GB 显存（抽取串行）；抽取 + 问答 + 重排并发建议 12 GB+ |
| 磁盘 | ≥ 50 GB（镜像 + 模型 + 数据） |

---

## 4. 快速开始

### 4.1 准备镜像

```powershell
docker pull wechatopenai/weknora-app:v0.9.0
docker pull wechatopenai/weknora-ui:v0.9.0
docker pull wechatopenai/weknora-docreader:v0.9.0
docker pull paradedb/paradedb:v0.25.11-pg17
docker pull redis:7.0-alpine
docker pull ghcr.io/huggingface/text-embeddings-inference:cuda-1.8

# MinerU 不在公共仓库，需要本地构建（或从源机器 docker save/load）
docker compose -f "<项目根>\mineru\docker-compose.yml" build
```

### 4.2 准备模型

- Ollama：至少 1 个对话/抽取模型 + 1 个嵌入模型，`ollama list` 记录实际 tag；
- TEI：把 `bge-reranker-v2-m3` 放到 `<项目根>\tei-models\bge-reranker-v2-m3\`；
- 4B 模型建议扩 32K 上下文（可选）：

```text
FROM modelscope.cn/unsloth/Qwen3.5-4B-GGUF:latest
PARAMETER num_ctx 32768
```

```powershell
ollama create qwen35-4b-32k -f .\Modelfile
```

### 4.3 两个 `.env`

**（1）`WeKnora\.env`**（容器使用，自行从 `.env.example` 复制）：

```ini
WEKNORA_VERSION=v0.9.0
DB_PASSWORD=<自定义数据库密码>
APP_PORT=8080
FRONTEND_PORT=80
OLLAMA_BASE_URL=http://host.docker.internal:11434
SSRF_WHITELIST_EXTRA=searxng,qdrant,milvus,weaviate,doris-fe,doris-be,minio,mineru-api,host.docker.internal
```

**（2）根目录 `.env`**（本地 Python 服务读取）：

```ini
WEKNORA_BASE=http://127.0.0.1:8080
WEKNORA_API_KEY=<在 WeKnora 界面创建>
WEKNORA_KB_ID=<知识库 ID>
EXTRACT_MODEL=<ollama list 里的抽取模型 tag>
```

> `cert-extractor` 只从根 `.env` 回退读取 `WEKNORA_API_KEY`；`OLLAMA_URL`、`EXTRACT_MODEL`、`WEKNORA_BASE` 需要以进程环境变量方式注入（见 4.6）。

### 4.4 启动容器

```powershell
# 先建好 MinerU 依赖的网络（目录名须为 WeKnora）
docker network create weknora_WeKnora-network

docker compose -f "<项目根>\mineru\docker-compose.yml" up -d

cd "<项目根>\WeKnora"
docker compose up -d frontend app docreader postgres redis

docker run -d --name tei-rerank --restart unless-stopped `
  --gpus all -p 8081:80 `
  -v "<项目根>\tei-models:/models" `
  ghcr.io/huggingface/text-embeddings-inference:cuda-1.8 `
  --model-id /models/bge-reranker-v2-m3
```

### 4.5 初始化 WeKnora

1. 打开 `http://localhost` 创建管理员；
2. 「模型管理」注册并设为默认：Embedding（local）、KnowledgeQA（local）、Rerank（remote，`http://host.docker.internal:8081`）；
3. 解析引擎地址填 `http://mineru-api:8000`；
4. 创建知识库，PDF 路由到 MinerU，检索参数：`enable_rerank=true`、`embedding_top_k=30`、`rerank_top_k=30`、`rerank_threshold=0.3`；
5. 创建 API Key → 写入根 `.env`；知识库 ID → `WEKNORA_KB_ID`；
6. 创建智能体并绑定 MCP 服务 `http://host.docker.internal:8765/mcp`：
   - 抽取智能体：`extract_from_knowledge` / `validate_extraction`；
   - 问答智能体：WeKnora `search_knowledge` + MCP `structured_search`，要求逐条给出出处。
7. MCP 工具变更后刷新元数据：

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8080/api/v1/mcp-services/<service_id>/metadata/refresh" `
  -Headers @{"X-API-Key"="<WEKNORA_API_KEY>"} -ContentType "application/json" -Body "{}"
```

### 4.6 启动本地服务

```powershell
cd "<项目根>"
uv sync                     # 按 pyproject.toml / uv.lock 创建 .venv
# 没有 uv：py -3.11 -m venv .venv，然后按 pyproject.toml 安装依赖

$env:OLLAMA_URL    = "http://127.0.0.1:11434/api/chat"
$env:EXTRACT_MODEL = "<抽取模型 tag>"
$env:WEKNORA_BASE  = "http://127.0.0.1:8080"

# 新机器首次必须初始化参数库（registry.db 不入 git）
.\.venv\Scripts\python.exe cert-registry\init_db.py

# 一键管理 mcp / review / validator / fixer
powershell -ExecutionPolicy Bypass -File scripts\local-services.ps1
powershell -ExecutionPolicy Bypass -File scripts\local-services.ps1 -Action status
```

### 4.7 导入两个智能体配置
仓库的 `docs/agent-extract.json`、`docs/agent-qa.json` 是本实例导出的 `custom_agents.config` 快照，包含系统提示词、模型、工具、MCP 绑定与检索参数（两段提示词全文另见第 8 节）。**配置只存在数据库里，不在代码里**；换实例后需要重新导入。

**注意：JSON 里的 `model_id`、`mcp_services` 是旧实例的 UUID，必须替换成新实例的值**：

```powershell
# 1. 查新实例的模型 ID 与 MCP 服务 ID
$H = @{"X-API-Key" = "<WEKNORA_API_KEY>"}
Invoke-RestMethod "http://127.0.0.1:8080/api/v1/models" -Headers $H |
  Select-Object -ExpandProperty data | Select-Object id, name
Invoke-RestMethod "http://127.0.0.1:8080/api/v1/mcp-services" -Headers $H |
  Select-Object -ExpandProperty data | Select-Object id, name

# 2. 导入问答智能体
$cfg = Get-Content .\docs\agent-qa.json -Raw -Encoding UTF8 | ConvertFrom-Json
$cfg.model_id     = "<新实例对话模型 ID>"
$cfg.mcp_services = @("<新实例 MCP 服务 ID>")
$body = @{
  name        = "证书综合问答（条件+语义）"
  description = "知识库语义检索 + MCP 条件检索，逐条给出处"
  config      = $cfg
} | ConvertTo-Json -Depth 20

Invoke-RestMethod -Method Put `
  -Uri "http://127.0.0.1:8080/api/v1/agents/<新实例 agent_id>" `
  -Headers $H -ContentType "application/json; charset=utf-8" `
  -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

抽取智能体同理，把文件名换成 `agent-extract.json`、名称换成「json抽取智能体」。若新实例还没有这两个智能体，先在界面各建一个空智能体，再取其 `agent_id` 执行上面的 PUT。

**导入后必须检查**：

- `mcp_selection_mode` 为 `selected` 且 `mcp_services` 非空（否则 MCP 工具不会下发）；
- `allowed_tools` 含 `search_knowledge` / `list_documents`（问答）或不需要知识库工具（抽取）；
- `retrieve_kb_only_when_mentioned` 为 `false`，否则用户不 @ 知识库时不检索；
- `max_iterations` 足够（问答建议 10，抽取建议 6-10）。

> 不要直接在「智能体管理」页面保存后再改库：页面保存会用表单内容整体覆盖 `config.system_prompt`。

### 4.8 验证

| 检查 | 命令/操作 | 预期 |
| --- | --- | --- |
| API | `curl.exe -s http://127.0.0.1:8080/health` | 200 |
| MinerU | `curl.exe -s http://127.0.0.1:8000/v1/health` | healthy |
| TEI | `curl.exe -s http://127.0.0.1:8081/info` | 返回模型信息 |
| MCP | `curl.exe -s http://127.0.0.1:8765/mcp` | 非 5xx |
| 校对台 | `http://127.0.0.1:8770` | 页面可打开 |

---

## 5. 使用流程

1. **抽取**：知识库上传 PDF → MinerU 解析 → 智能体调用 `extract_from_knowledge(doc_ref, kb_ref)`，返回字段、置信度与页码/bbox；
2. **校对**：`http://127.0.0.1:8770` 逐字段核对，原件分页与 bbox 高亮；
3. **发布**：13 字段全部通过后点发布，**只写 `cert-registry/registry.db`**，不生成知识库「参数卡」；
4. **条件检索**：`structured_search(question, limit)` 把自然语言条件转成参数化 SQL（电压/电流/外壳/防爆标志/机构等）；
5. **问答**：`search_knowledge` 取原文片段与页码，`structured_search` 取符合条件证书，两路合并后逐条给出处；
6. **校验（可选）**：`citation-validator` 的 `/validate` 按证书编号回查分块，校验 quote 与页码。

### MCP 工具

| 工具 | 作用 |
| --- | --- |
| `extract_certificate` | 对直接传入的 Markdown 正文抽取 |
| `extract_from_knowledge` | 对知识库已入库文档抽取（推荐） |
| `validate_extraction` | 字段级校验（缺失、格式、交叉规则） |
| `structured_search` | 参数库条件检索 |
| `kb_answer` | 检索 + 回答 + 出处校验一体（供边车/脚本使用） |

---

## 6. 换机必改参数（重点）

复制项目到新主机后，按下表逐项替换；只改代码不改这些配置会表现为「连不上 / 找不到知识库 / 模型不存在 / 端口冲突」。

| 位置 | 参数 | 示例/默认 | 说明 |
| --- | --- | --- | --- |
| 项目根路径 | `<项目根>` | `E:\weknora` | 建议无空格、非中文；所有脚本按自身位置推导根目录 |
| `WeKnora\.env` | `DB_PASSWORD` | `<自定义>` | **数据卷首次初始化后不可随意改**，改了需删卷 |
| `WeKnora\.env` | `APP_PORT` / `FRONTEND_PORT` | `8080` / `80` | 改后同步前端代理与 `WEKNORA_BASE` |
| `WeKnora\.env` | `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Linux 下改成宿主可达 IP |
| `WeKnora\.env` | `SSRF_WHITELIST_EXTRA` | 含 `mineru-api,host.docker.internal` | 漏配会导致解析引擎测试连接失败 |
| 根 `.env` | `WEKNORA_BASE` | `http://127.0.0.1:8080` | 本地服务访问 WeKnora 的地址 |
| 根 `.env` | `WEKNORA_API_KEY` | `<新建>` | 换实例必须重新创建 |
| 根 `.env` | `WEKNORA_KB_ID` | `<新知识库 ID>` | 换机后旧 ID 会报「找不到知识库」 |
| 根 `.env` | `EXTRACT_MODEL` | `qwen35-4b-32k:latest` | 必须是目标机 `ollama list` 里的 tag |
| 环境变量 | `OLLAMA_URL` | `http://127.0.0.1:11434/api/chat` | Ollama 不在本机时改地址 |
| 环境变量 | `MCP_PORT` | `8765` | 改后同步智能体里的 MCP 地址 |
| 环境变量 | `VALIDATOR_PORT` | `8780` | — |
| `review-workbench/app.py` | `PORT` | `8770` | 代码常量，改端口需改这里 |
| `WeKnora\config\config.yaml` | `conversation.*` | 见 4.5 | 挂载进容器，改完重启 app |
| TEI 挂载 | `tei-models` 路径 | `<项目根>\tei-models` | 目录不存在或文件不全 TEI 起不来 |
| `scripts\local-services.ps1` | `$Python` | `<项目根>\.venv\Scripts\python.exe` | venv 不在默认位置时必改 |
| WeKnora 界面 | MCP 地址 | `http://host.docker.internal:8765/mcp` | MCP 不在宿主时改成容器可达地址 |
| WeKnora 界面 | MCP service_id / 智能体 ID | `<实例内生成>` | 换实例后需重建并重新绑定 |
| MinerU 网络 | `weknora_WeKnora-network` | 由 compose 目录名决定 | 目录改名会变，需设 `COMPOSE_PROJECT_NAME` 或改 yml |
| 硬件 | 显存 | 8 GB 起 | 不要照抄原机器结论，按实际重测 |

**跨服务引用要一起改**：前端 Nginx 代理、`WeKnora/.env`、根 `.env`、WeKnora 界面里的模型/MCP 地址。

---

## 7. 常见问题

| 现象 | 处理 |
| --- | --- |
| 上传 PDF 报 `Document parsing failed` | MinerU 4.0.12 补丁未生效：`docker cp mineru/patch_native.py mineru-api:/tmp/` 后 `docker exec mineru-api python /tmp/patch_native.py` |
| 解析引擎「测试连接」失败 | 检查 `SSRF_WHITELIST_EXTRA` 是否含 `mineru-api`，以及是否同一网络 |
| 智能体空回复 / 一直思考 | 抽取请求需关闭 thinking（本项目已设 `think=false`）；提示词加 `/no_think`，限制输出预算 |
| `I'm sorry, I was unable to generate a response.` | 多为 TEI 8081 不可达，`docker start tei-rerank` |
| MCP 工具列表缺新工具 | 调 `mcp-services/{id}/metadata/refresh` |
| 抽取超时 / OOM | 保持 `keep_alive=0` 串行抽取，必要时停 TEI 或换大显存 |
| `找不到知识库 weknora://kb/<uuid>` | 换机后 `WEKNORA_KB_ID` 未更新 |
| Postgres 认证失败 | `DB_PASSWORD` 与旧数据卷不一致；改回或删卷重建（丢数据） |

---

## 8. 附录：两个智能体的系统提示词

下面两段与数据库 `custom_agents.config.system_prompt` 当前值一致，机器可读版本见 `docs/agent-extract.json`、`docs/agent-qa.json`。修改后请以数据库为准重新导出，页面保存会整体覆盖。

### 8.1 json抽取智能体

````text
你是"防爆合格证字段抽取器"。字段只能来自工具返回结果，不能凭记忆或常识填写。
【固定三步，第 3 步一律通过 call_mcp_tool 执行】
第 1 步（必须真实调用）：discover_mcp_tools(mode="list_tools", server_id="ms1")
第 2 步：从第 1 步的列表里选"从知识库已入库文档抽取"的那个工具
        （它的参数里含 doc_ref / kb_ref；不要选只接受 markdown 正文的那个），
        然后 discover_mcp_tools(mode="describe", server_id="ms1", tool_name="<照抄它的名字>")
第 3 步：一律用 call_mcp_tool(tool_ref="<第2步返回的 tool_ref>", arguments={...})
        按第 2 步给出的参数定义填 arguments；不要直接调用第 1/2 步里出现的裸工具名。
server_id 一律用 "ms1"，不要把文档号、知识库 ID 填进 server_id。
没有真实调用过任何工具就结束回答，视为失败；不要跳过第 1、2 步。

【第 3 步 arguments 怎么填】
- 用户给出的是文件名（例如"6 BM(D)X52-全塑-SYEx23.0885"），开头的数字属于文件名本身，
  不是"第 6 份"。doc_ref 直接填这段文件名，不要换算成 doc://N。
- 只有用户明确说"第 N 份/第一个文档"时，才用 doc://N 序号。
- 标题以"参数卡"开头的文档是入库产物，不是原始报告，禁止作为抽取对象。
- 用户只给 doc://N → doc_ref 填该序号，kb_ref 填 "kb://1"（取文档所属 knowledge_base）。
- 用户直接贴了大段证书正文 → 才用收正文的那个工具，markdown 填正文全文。
- 不要向用户索要正文，也不要说"请上传文件"。

【硬性输出规则】
1. 最终消息 = 第 3 步工具返回的那个 JSON 原文，用 ```json 围栏原样输出：
   第一个字符是 {，最后一个字符是 }，保留 ok / fields / issues / stats 全部结构。
2. 不要把字段摊平，不要改写键名，不要只挑一部分字段，不要补解释或代码块标记。
3. 工具调用本身不是最终消息；工具返回后必须再输出一次最终 JSON。
4. 只要工具返回的 JSON 里有 fields，就算成功拿到结果：
   ok=false 只表示校验有告警，不要重试、不要重复调用工具，直接原样输出。
5. 同一份文档最多调用一次抽取工具；工具报超时/失败时最多再试一次，
   仍失败就把工具返回的错误原文作为最终消息输出，不要自行编造错误信息。
6. 不要抄历史消息里的旧结果，每次都要重新调用工具。
````

### 8.2 证书综合问答（条件+语义）

````text
你是“防爆证书知识库”的双路检索问答助手。回答只能依据本轮工具返回的结果，禁止使用常识
## 一、每轮必须做两路检索
A. 语义检索（WeKnora 知识库）：调用本地知识库检索工具
B. 条件检索（MCP 参数库）：必须先发现工具再调用：
   1) discover_mcp_tools(mode="list_tools", server_id="<系统里列出的 MCP 服务 ID，如 1b99ffa8-…>")
   2) discover_mcp_tools(mode="describe", server_id="<同一个 server_id>", tool_name="structured_search")
   3) 调用 describe 后出现的 structured_search 工具（名称形如 mcp_cert_extractor_structured_search；
      若没有直接出现，用 call_mcp_tool(tool_ref=..., arguments={"question": 用户原问题, "limit": 20})）
   注意：structured_search 在 describe 之前不可用；不要凭空写工具名，否则调用会被静默丢弃。
## 二、绝对规则（违反任何一条都视为错误回答）
1. 每一条结论性陈述都必须紧跟一行“出处”，格式固定为：
   出处：证书编号=<证书编号>｜文件=<文件名或知识标题>｜定位=chunk #<chunk_index>｜原文="<原文片段>"
   并在该行末尾使用系统要求的引用标签，例如 <ref id="chunk://3"/>。
2. 证书编号、文件名、原文片段必须逐字来自同一条检索结果，不得改写编号，不得把不同证书的字段拼在一起。
   证书编号可以来自文件名/知识标题，也可以来自片段正文；两者必须能对应到同一条检索结果。
3. 如果检索片段和文件名里都没有证书编号，写“证书编号=片段未包含”，并且这条不得作为结论输出。
4. 如果检索结果不足以支持结论，只输出：
   “未在知识库中找到可引用的依据，无法给出结论。”
   然后列出缺失的信息（如证书编号、字段名、文件范围），不得给出推测答案。
5. 原文事实和推断必须分开：
   - 原文事实：直接引用，并给出处；
   - 推断：写成“推断（依据：证书编号…原文…）”，且不能当作最终结论。
6. 多个证书对比时，逐条列出处，禁止把多个证书合并成一条出处。
7. 只能引用本次检索结果中的 chunk 句柄；历史回答、记忆、@提及的目录都不能作为出处。
8. 输出前自检：每条结论是否都有“证书编号 + 文件 + 定位 + 原文”？缺任何一项就删除该条。
## 三、回答结构
1. 直接结论：每条一句，紧跟出处行。
2. 依据明细：可以列多条检索片段，但每条都要能对应到上面的出处。
3. 未覆盖/无法确认：明确写出没有找到依据的字段或证书。
## 四、禁止
- 禁止无出处的总结、建议、经验判断；
- 禁止编造页码、条款号、证书编号、检测机构；
- 禁止把“推测”“通常”“一般来说”当作结论；
- 禁止使用 [1] 之类自定义引用格式，只使用系统要求的 <ref id="chunk://N"/>。
```
````

> 当前数据库里的问答提示词末尾多出三个反引号（保存时带入的代码围栏），建议在智能体编辑页删除；同时该版本已不含「文档清单类问题 / `<recent_documents>` 只是样本」规则，若需要按文档数量作答，请把对应规则补回。
