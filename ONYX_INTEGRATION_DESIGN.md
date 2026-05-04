# RAG-Anything × onyx 集成技术方案

> 文档版本：v0.1
> 日期：2026-05-03
> 状态：技术评审稿（暂不实施）

---

## 1. 引言

### 1.1 背景

用户手上有两个开源项目：

- **onyx**（前身 Danswer，`/home/edward/research/onyx`）：一个成熟的企业级 AI 搜索/Chat 平台。Python + Celery 后端，Next.js 前端，自带连接器框架（Slack/GDrive/GitHub/...）、Vespa 检索、用户/RBAC/SSO、LLM provider 抽象、Persona 编排、Tool 框架、多租户支持。
- **RAG-Anything**（`/home/edward/research/RAG-Anything`）：一个聚焦多模态 RAG 的 Python 库。核心是 MinerU 解析器（PDF/Office/图/表/公式/音视频）+ LightRAG（图谱+向量混合 RAG）+ 多模态 KG（图/表/公式作为实体进知识图谱）+ VLM 增强查询。

用户已经为 RAG-Anything 单独包过一层 HTTP 服务的设计（[RAG_SERVICE_DESIGN.md](/home/edward/research/RAG-Anything/RAG_SERVICE_DESIGN.md)）。本方案讨论的是**用 onyx 当那个"主服务"**，让 onyx 提供 GUI/DB/LLM 编排/用户认证，RAG-Anything 提供解析+RAG 引擎。

### 1.2 动机

为什么不分别用？为什么要集成？

1. **能力互补**：onyx 的检索是 Vespa 的 BM25+向量混合，对结构化文档和短问答很强；RAG-Anything 的 LightRAG 是图谱+向量混合，对跨文档归纳、多模态引用、复杂推理很强。但两边都不擅长对方的场景。
2. **onyx 解析弱**：onyx 默认用 pdfplumber 一类的传统提取器，对带图/表/公式的复杂 PDF 损失大。MinerU 是显著更好的解析器。
3. **重复造轮子代价高**：onyx 已经有完整的用户/auth/会话/审计/前端体系；RAG-Anything 已经有完整的解析+多模态 KG。把两边都完整重写一遍是浪费。
4. **保留各自演进自由**：onyx 与 RAG-Anything 是两个独立项目，独立升级。集成应该用稳定的契约（HTTP API）连接，而不是源代码级耦合。

### 1.3 目标

- 在 onyx 中**新增一种"多模态 KG 模式"**（Multimodal KG Mode），与原有的标准模式平行存在；用户在 UI 上能选择某个连接器走哪种模式。
- 多模态 KG 模式下：文档解析走 MinerU，索引走 LightRAG（保留图谱/多模态实体），查询走 LightRAG 的 hybrid/local/global/mix 模式。
- 标准模式下：onyx 现有逻辑完全不变。
- 引用回链：用户在 chat 里点引用，能跳回 onyx 的文档卡片。
- 单租户部署优先（自己/团队内部用），命名空间为多租户预留。

### 1.4 非目标

- **不**重写 onyx 的检索栈（不替换 Vespa）。
- **不**把 MinerU 模型搬进 onyx 主进程（避免 GB 级模型污染主容器）。
- **不**做真正的双写（避免存储和成本翻倍；模式选择是排他的）。
- **不**改 RAG-Anything 库本身（仅通过 sidecar 对外暴露）。
- **不**在 v1 支持用户级文档 ACL 在 LightRAG 端的过滤（仅 docset 级隔离）。

---

## 2. 现状分析

### 2.1 onyx 提供的能力

| 能力 | 关键文件/位置 |
|---|---|
| Web GUI（Next.js） | [web/src/app/](/home/edward/research/onyx/web/src/app/) |
| 用户/认证（OIDC/Email/SSO） | [backend/onyx/main.py:49](/home/edward/research/onyx/backend/onyx/main.py#L49) |
| 多租户（schema 隔离） | `CURRENT_TENANT_ID_CONTEXTVAR` ([backend/shared_configs/contextvars.py](/home/edward/research/onyx/backend/shared_configs/contextvars.py)) |
| 连接器框架 | [backend/onyx/connectors/interfaces.py:43](/home/edward/research/onyx/backend/onyx/connectors/interfaces.py#L43) |
| 文档/Section 模型 | [backend/onyx/connectors/models.py:348](/home/edward/research/onyx/backend/onyx/connectors/models.py#L348) |
| 索引流水线（chunker→embedder→Vespa） | [backend/onyx/indexing/indexing_pipeline.py:1289](/home/edward/research/onyx/backend/onyx/indexing/indexing_pipeline.py#L1289) |
| 文件存储（FileStore） | `backend/onyx/file_store/` |
| LLM provider 抽象 | [backend/onyx/llm/factory.py](/home/edward/research/onyx/backend/onyx/llm/factory.py) |
| Tool 框架 | [backend/onyx/tools/interface.py:17](/home/edward/research/onyx/backend/onyx/tools/interface.py#L17), [backend/onyx/tools/built_in_tools.py:32](/home/edward/research/onyx/backend/onyx/tools/built_in_tools.py#L32) |
| Persona / DocumentSet | [backend/onyx/db/models.py:3655](/home/edward/research/onyx/backend/onyx/db/models.py#L3655)（Persona）、[3432](/home/edward/research/onyx/backend/onyx/db/models.py#L3432)（DocumentSet）、[660](/home/edward/research/onyx/backend/onyx/db/models.py#L660)（Persona__Tool） |
| Chat 流程 / RAG 组装 | `backend/onyx/chat/process_message.py` |
| `ProcessingMode` 枚举（已存在！） | [backend/onyx/db/enums.py](/home/edward/research/onyx/backend/onyx/db/enums.py) + [models.py:793](/home/edward/research/onyx/backend/onyx/db/models.py#L793) |
| Celery 后台任务 | `backend/onyx/background/celery/` |

**关键发现**：onyx 的 `ConnectorCredentialPair` 上**已经有一个 `processing_mode` 字段**，目前枚举值是 `REGULAR` 和 `FILE_SYSTEM`（后者是给 CLI agent sandbox 用的）。这是一个**天然的扩展点**——加一个 `MULTIMODAL_KG` 值就行，无需新建字段。

### 2.2 RAG-Anything 提供的能力

| 能力 | 关键文件/位置 | 是否独立可用 |
|---|---|---|
| MinerU 解析器（标准库式调用） | [raganything/parser.py:65](/home/edward/research/RAG-Anything/raganything/parser.py#L65)（`Parser` 基类、`MineruParser`） | ✅ 独立可用，输入文件输出 `List[Dict]` |
| Docling 解析器（备选） | 同上 | ✅ |
| `RAGAnything` 主类（编排器） | [raganything/raganything.py:50](/home/edward/research/RAG-Anything/raganything/raganything.py#L50) | 需要 LLM/embedding/VLM 函数注入 |
| `process_document_complete` | [raganything/processor.py](/home/edward/research/RAG-Anything/raganything/processor.py) | 端到端：解析 + 多模态处理 + 写 LightRAG |
| `insert_content_list` | 同上 | 已解析的内容直接入库 |
| 查询 `aquery` | [raganything/query.py:102](/home/edward/research/RAG-Anything/raganything/query.py#L102) | 模式 local/global/hybrid/naive/mix |
| 多模态查询 `aquery_with_multimodal` | [raganything/query.py:195](/home/edward/research/RAG-Anything/raganything/query.py#L195) | 输入带图/表/公式的查询 |
| VLM 增强查询 `aquery_vlm_enhanced` | [raganything/query.py:349](/home/edward/research/RAG-Anything/raganything/query.py#L349) | 自动把图片路径替换为 VLM 描述 |
| 多模态处理器（图/表/公式作为实体） | [raganything/modalprocessors.py](/home/edward/research/RAG-Anything/raganything/modalprocessors.py) | 通过 RAGAnything 编排 |
| 存储后端（KV/向量/图） | LightRAG 内部，可通过 `lightrag_kwargs` 注入 | 默认文件 JSON；可换 Postgres+pgvector / Neo4j |

**关键约束**（来自 [api_summary.md](/home/edward/research/RAG-Anything/api_summary.md) 第 8 节）：
- `RAGAnything` 实例化便宜但首次调用全量加载 NanoVectorDB / 图文件到内存 → 不能每请求新建实例 → 进程内 LRU 缓存。
- 实例必须 `finalize_storages()` 干净关闭。
- 同一 workspace（`kb_id`）的摄入互斥（pipeline status lock）；查询不互斥。
- 存储后端可插拔（6 类）。

### 2.3 重叠与冲突

| 维度 | onyx | RAG-Anything | 说明 |
|---|---|---|---|
| 解析 | pdfplumber + 内建图像 caption | MinerU + Docling + VLM | RAG-Anything 强很多 |
| 检索 | Vespa 混合 | LightRAG 图+向量 | 两种思路，无替代关系 |
| 知识图谱 | 已有 KG 表 + 未启用的 `KnowledgeGraphTool` stub | LightRAG 原生 KG | onyx 自己的 KG 还没做完；RAG-Anything 是另一套 KG |
| LLM 抽象 | onyx LLM provider | RAG-Anything LLM func 注入 | 需要桥接 |
| 多租户 | 内建 | 通过 `working_dir` / `workspace` | 单租户场景 trivially 兼容 |

**命名冲突警告**：onyx 已有一个 `KnowledgeGraphTool`（[backend/onyx/tools/tool_implementations/knowledge_graph/knowledge_graph_tool.py](/home/edward/research/onyx/backend/onyx/tools/tool_implementations/knowledge_graph/knowledge_graph_tool.py)），但是 stub（构造函数直接 `raise NotImplementedError`）。新工具必须叫别的名字（建议 `MultimodalKGTool`，display name "Multimodal KG Search (RAG-Anything)"）。

---

## 3. 总体架构

### 3.1 物理拓扑

```
┌──────────────────────── onyx 部署单元 ─────────────────────────┐
│                                                                │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐     │
│  │  Web (Next)  │    │  API (FastAPI)│    │  Celery       │     │
│  └──────┬───────┘    └───────┬──────┘    │  Workers      │     │
│         │ HTTP               │           └───────┬──────┘     │
│         └─────────┬──────────┘                   │            │
│                   ▼                              │            │
│        ┌────────────────────┐                    │            │
│        │  Postgres          │  ◄─────────────────┘            │
│        │  - 用户/auth       │                                  │
│        │  - cc_pair         │                                  │
│        │  - documents       │                                  │
│        │  - multimodal_kg_jobs (新)                            │
│        └────────────────────┘                                  │
│                   │                                            │
│        ┌──────────┴───────────┐                                │
│        ▼                      ▼                                │
│  ┌──────────┐         ┌──────────────┐                         │
│  │  Vespa   │         │  FileStore   │                         │
│  │  (向量)  │         │  (原始文件)  │                         │
│  └──────────┘         └──────────────┘                         │
│                                                                │
└────────────────────────────┬───────────────────────────────────┘
                             │ HTTP (内网)
                             │ Authorization: Bearer <internal_token>
                             ▼
┌──────────────────── RAG-Anything sidecar ─────────────────────┐
│                                                                │
│  ┌──────────────┐    ┌──────────────┐                         │
│  │  API         │    │  Worker      │                         │
│  │  (FastAPI)   │    │  (arq)       │                         │
│  │  /v1/kbs/    │    │              │                         │
│  └──────┬───────┘    └───────┬──────┘                         │
│         │                    │                                │
│         └────────┬───────────┘                                │
│                  ▼                                            │
│       ┌─────────────────────┐                                 │
│       │  Postgres (sidecar) │  jobs/documents/audit          │
│       └─────────────────────┘                                 │
│       ┌─────────────────────┐                                 │
│       │  Redis              │  队列 + pubsub + 限流          │
│       └─────────────────────┘                                 │
│       ┌─────────────────────┐                                 │
│       │  data/working_dirs/ │  LightRAG KV/向量/图           │
│       │  data/uploads/      │                                 │
│       └─────────────────────┘                                 │
│       ┌─────────────────────┐                                 │
│       │  MinerU 模型缓存     │  几个 GB                       │
│       └─────────────────────┘                                 │
└────────────────────────────────────────────────────────────────┘
```

**说明**：
- onyx 和 sidecar 是**两个独立部署单元**，各自有 DB/缓存/存储。它们之间用 HTTP + Bearer token 通信，仅在 onyx 内网可达。
- 单台机器部署起步：两个 docker compose 起在同一台机器上，通过 host network 或 docker network 互通。
- sidecar 存储**不**入 onyx 的 Postgres/Vespa。这是关键：sidecar 是真正"自管"的，onyx 不需要理解 LightRAG 的存储格式。

### 3.2 逻辑分层

```
┌────────────────────────── 用户交互层 ──────────────────────────┐
│  Web UI: 选 Connector mode、上传文件、Persona 配置、Chat        │
└──────────────────────────────┬──────────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────────┐
│                       onyx 业务编排层                            │
│                                                                 │
│  • 用户/auth/会话/审计                                          │
│  • Connector 管理 + processing_mode 路由                        │
│  • Persona ↔ DocumentSet ↔ cc_pair 关系                        │
│  • Tool 编排（LLM 选择 SearchTool 还是 MultimodalKGTool）       │
│  • 引用渲染（统一 Citation 模型）                               │
└──────────────────────────────┬──────────────────────────────────┘
                               │
              ┌────────────────┼─────────────────┐
              │                │                 │
┌─────────────▼─────────┐ ┌────▼──────────┐ ┌───▼─────────────────┐
│ onyx 标准管道          │ │ onyx 文件存储  │ │ RAG-Anything 引擎   │
│ • Chunker             │ │ • 原始字节    │ │ • MinerU 解析       │
│ • Embedder            │ │ • metadata   │ │ • LightRAG KG/向量  │
│ • Vespa               │ │              │ │ • 多模态实体        │
│ • SearchTool 检索     │ │              │ │ • VLM 增强查询      │
└───────────────────────┘ └──────────────┘ └─────────────────────┘
   processing_mode=REGULAR                processing_mode=MULTIMODAL_KG
```

### 3.3 设计原则

1. **复用 onyx 已有抽象**：`ProcessingMode` 枚举 / `Tool` 接口 / `cc_pair` 关系 / `Citation` 模型 / Celery 队列。**不**新增 DocumentSet 字段，**不**新建用户态表（除了一张 `multimodal_kg_jobs` 状态表）。
2. **单一存储真相**：每份文档要么进 Vespa 要么进 LightRAG，**不**双写。模式由 cc_pair.processing_mode 决定。引用回链通过 onyx documents 表元数据 + sidecar 透传 `onyx_document_id` 实现。
3. **HTTP 契约稳定**：onyx 与 sidecar 之间是 RESTful，可独立演进。
4. **降级显式**：sidecar 不可达时 MultimodalKGTool 不可用且 UI 明示，**不**静默回退到 SearchTool。
5. **单租户优先，多租户预留**：kb_id 命名 `kb_id = f"ccpair-{cc_pair.id}"` 起步；将来切多租户改成 `kb_id = f"t{tenant_id}-cc{cc_pair.id}"`，迁移可平滑。

---

## 4. 核心数据流

### 4.1 创建一个 Multimodal KG 连接器

```
用户在 Web UI [admin/connectors] 点 "Add Connector"
  │
  ▼
选 Connector 类型（File / GDrive / ...）
  │
  ▼
表单出现新字段 "Processing Mode" (radio):
  ◉ Standard
  ◯ Multimodal KG (RAG-Anything)        ← 仅当 RAG_ANYTHING_BASE_URL 已配置
  │
  ▼
用户选 Multimodal KG → 提交
  │
  ▼
onyx 后端：
  • 在 connector_credential_pair 表插一行，processing_mode='MULTIMODAL_KG'
  • 计算 kb_id = f"ccpair-{cc_pair.id}"
  • 调 sidecar POST /v1/kbs/{kb_id} (幂等创建容器)
  │
  ▼
返回成功 → UI 跳到 cc_pair 详情页
```

### 4.2 文档摄入

```
用户在 onyx Web UI 上传 PDF 到一个 MULTIMODAL_KG 模式的 cc_pair
  │
  ▼
onyx FileConnector：
  • 把原始文件存入 FileStore，返回 file_id
  • yield Document(id=..., source=FILE, semantic_identifier=..., sections=[])
    （sections 为空，因为我们不让 onyx 自己解析）
  │
  ▼
Celery 拉起 indexing 任务：run_indexing_pipeline(document_batch, cc_pair, ...)
  │
  ▼
分流（新加）：
  if cc_pair.processing_mode == ProcessingMode.MULTIMODAL_KG:
      _run_multimodal_kg_pipeline(document_batch, cc_pair, file_store, db_session)
      return
  # else: 原有 Vespa 流水线
  │
  ▼
_run_multimodal_kg_pipeline：
  for doc in document_batch:
      • 写 onyx documents 表（仅元数据 + 关联 file_id）
      • 从 FileStore 取原始字节
      • POST /v1/kbs/{kb_id}/documents
        multipart: file=<bytes>, metadata={onyx_document_id, title, ...}
        Sidecar 立即返回 {job_id}
      • INSERT INTO multimodal_kg_jobs (cc_pair_id, document_id, kb_id, sidecar_job_id, status='queued')
  │
  ▼
Sidecar Worker:
  • 拿 per-kb Redis 锁
  • 调 RAGAnything.process_document_complete(file)
    - MinerU 解析（10–60s）
    - 多模态处理器跑：图 → VLM 描述 → KG entity；表 → 结构化 → KG entity；公式 → LaTeX → entity
    - LightRAG 写入：chunks 进向量库；entity/relation 进图库
  • 标记 sidecar 端 job=done
  • Redis publish kb_reload:{kb_id} → sidecar API 端 evict LRU 缓存里的 RAGAnything 实例
  │
  ▼
onyx 端：
  • Celery beat 周期任务 poll_multimodal_kg_jobs：
    GET /v1/kbs/{kb_id}/jobs/{sidecar_job_id} → 更新 multimodal_kg_jobs.status
  • UI 显示进度条
  • status 变 done 后 Web UI 文档卡片显示 "已索引（多模态 KG）"
```

### 4.3 查询

```
用户在 Chat 页面用一个 Persona 提问 "公司这一年的财报里出现的所有客户名单"
  │
  ▼
onyx process_message:
  • 拿 persona.tools 列表，构造 LLM 工具数组
  • LLM 选 tool（SearchTool 或 MultimodalKGTool）
  │
  ▼
LLM 选 MultimodalKGTool（描述里写了"跨文档归纳/含图问题用这个"）：
  • 工具参数: {query, mode='hybrid', vlm_enhanced=false}
  │
  ▼
MultimodalKGTool.run():
  • 解析当前 persona 绑定的 docsets
  • 找出每个 docset 关联的 MULTIMODAL_KG cc_pairs（一个 docset 可能含多个 cc_pair，可能 mix 模式）
  • 跳过 REGULAR cc_pair（那些走 SearchTool）
  • 对每个 MULTIMODAL_KG cc_pair 计算 kb_id
  • 并发调 sidecar POST /v1/kbs/{kb_id}/query
  • 合并返回的 sources（按 score 排序，MVP 不做 RRF）
  │
  ▼
Sidecar:
  • LRU 找 kb_id 实例 → 没有就 lazy 加载
  • 调 RAGAnything.aquery(query, mode='hybrid')
    - 内部走 LightRAG 图谱+向量混合检索
    - 取 top-k chunks + 关联实体
    - LLM 生成最终答案
  • 返回 {answer, sources: [{onyx_document_id, snippet, score, modality}]}
  │
  ▼
MultimodalKGTool 把答案 + sources 包成 ToolResponse:
  • sources 转 onyx Citation（document_id 用 onyx_document_id）
  • 文档标题/链接从 onyx documents 表查
  │
  ▼
Chat 流式渲染：
  • LLM 把 ToolResponse 当工具结果继续生成
  • 答案里带 [1][2] 引用 → 前端渲染成卡片，点击跳 onyx 文档详情页
```

### 4.4 删除

```
单文档删除：
  onyx DELETE /document/{id}
    • 软删 onyx documents 表
    • 异步 Celery：DELETE /v1/kbs/{kb_id}/documents/{onyx_document_id}
    • Sidecar 端：LightRAG.adelete_by_doc_id(onyx_document_id)

cc_pair 删除：
  onyx DELETE /cc_pair/{id}
    • 现有逻辑（清 Vespa）外加：DELETE /v1/kbs/{kb_id}
    • Sidecar 端：finalize 实例 + rm -rf working_dir/{kb_id}
    • 删 onyx multimodal_kg_jobs 表关联行（外键 ON DELETE CASCADE）

DocumentSet 删除：
  现有逻辑遍历 cc_pairs；每个 cc_pair 自己处理删除链路。
```

---

## 5. 数据模型

### 5.1 onyx 侧

**枚举扩展**（[backend/onyx/db/enums.py](/home/edward/research/onyx/backend/onyx/db/enums.py)）：
```python
class ProcessingMode(str, Enum):
    REGULAR = "REGULAR"
    FILE_SYSTEM = "FILE_SYSTEM"
    MULTIMODAL_KG = "MULTIMODAL_KG"   # 新增
```

**新表**：
```sql
CREATE TABLE multimodal_kg_jobs (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  cc_pair_id      INT NOT NULL REFERENCES connector_credential_pair(id) ON DELETE CASCADE,
  document_id     TEXT NOT NULL REFERENCES document(id) ON DELETE CASCADE,
  kb_id           TEXT NOT NULL,
  sidecar_job_id  TEXT NOT NULL,
  status          VARCHAR(16) NOT NULL,  -- queued | running | done | failed
  created_at      TIMESTAMPTZ DEFAULT now(),
  updated_at      TIMESTAMPTZ,
  error_message   TEXT,
  retries         INT DEFAULT 0,
  UNIQUE (cc_pair_id, document_id, sidecar_job_id)
);
CREATE INDEX ON multimodal_kg_jobs (status, updated_at) WHERE status IN ('queued','running');
CREATE INDEX ON multimodal_kg_jobs (kb_id);
```

**alembic migration** 一条：
- 把 `ProcessingMode` 枚举的 check constraint 加上 `'MULTIMODAL_KG'`（onyx 现在用 `Enum(native_enum=False)` 实际是 VARCHAR + check）。
- 创建 `multimodal_kg_jobs` 表。

### 5.2 sidecar 侧

沿用 [RAG_SERVICE_DESIGN.md](/home/edward/research/RAG-Anything/RAG_SERVICE_DESIGN.md) 第"数据模型"节。三处调整：
- `tenants` 表改成 `kbs` 表，主键 `kb_id`（保留 `config_json`、`storage_quota_mb`）。
- `documents.tenant_id` 改成 `documents.kb_id`。
- `documents` 表新增 `onyx_document_id TEXT NOT NULL UNIQUE` 字段（作为 onyx 与 sidecar 的稳定关联键）。
- 文件系统：`data/working_dirs/{kb_id}/`、`data/uploads/{kb_id}/{document_id}.{ext}`。

### 5.3 文件系统布局

```
sidecar 节点：
data/
├── uploads/{kb_id}/{document_id}.{ext}        # 原始文件副本（sidecar 自留一份）
├── working_dirs/{kb_id}/                      # LightRAG KV/向量/图
│   ├── kv_storage/
│   ├── vector_storage/
│   └── graph_storage/
└── mineru_output/{kb_id}/{document_id}/       # MinerU 中间产物（解析后可删）
```

**安全**：`kb_id` 必须匹配 `^[a-zA-Z0-9_-]{1,64}$`（onyx 那边 cc_pair.id 是 int，前缀 `ccpair-` 后是数字，天然安全；多租户场景类似）。

---

## 6. API 契约

### 6.1 onyx → sidecar

所有请求都带 `Authorization: Bearer ${RAG_ANYTHING_INTERNAL_TOKEN}` 和 `X-Onyx-Request-Id: <uuid>`（用于跨服务追踪）。

#### 6.1.1 KB 容器

```http
POST /v1/kbs/{kb_id}                  幂等创建 KB 容器
  Body: {}                            （未来可放 KB 级配置）
  200/201 → { "kb_id": "...", "created_at": "..." }

GET /v1/kbs/{kb_id}                   查 KB 状态
  200 → { "kb_id": "...", "doc_count": N, "size_bytes": ..., "last_updated": "..." }

DELETE /v1/kbs/{kb_id}                删 KB（finalize 实例 + rm working_dir）
  204 (no content)
```

#### 6.1.2 文档

```http
POST /v1/kbs/{kb_id}/documents        异步摄入
  Body (multipart):
    file=<bytes>
    metadata=<json>
  metadata = {
    "onyx_document_id": "doc-abc...",   必填，作为唯一键
    "title": "Q3 Report.pdf",
    "source": "file|slack|...",
    "doc_updated_at": "2026-04-01T...",  ISO-8601
    "owners": ["alice@..."]            可选
  }
  202 → { "job_id": "...", "kb_id": "...", "deduplicated": false }

  幂等：同 (kb_id, onyx_document_id, doc_updated_at) 重复请求返回 deduplicated=true 不入队。

GET /v1/kbs/{kb_id}/jobs/{job_id}     查摄入状态
  200 → {
    "job_id": "...",
    "status": "queued|running|done|failed",
    "progress": { "stage": "parsing|extracting|writing", "percent": 60 },
    "doc_id": "doc-abc...",
    "error": null | "..."
  }

GET /v1/kbs/{kb_id}/documents          列文档（游标分页）
  ?cursor=&limit=50
  200 → { "items": [...], "next_cursor": "..." }

DELETE /v1/kbs/{kb_id}/documents/{onyx_document_id}
  204
```

#### 6.1.3 查询

```http
POST /v1/kbs/{kb_id}/query
  Body: {
    "query": "...",
    "mode": "hybrid|local|global|naive|mix",   default hybrid
    "top_k": 8,
    "vlm_enhanced": false                       触发 aquery_vlm_enhanced
  }
  200 → {
    "answer": "...",
    "sources": [
      {
        "onyx_document_id": "...",              回链键
        "snippet": "...",
        "score": 0.83,
        "modality": "text|image|table|equation",
        "extra": { "page": 12, "bbox": [...] } 可选
      }
    ],
    "trace": {
      "entities_used": ["Q3 Sales", "Customer X"],
      "graph_hops": 2,
      "latency_ms": 1843
    }
  }
```

#### 6.1.4 错误约定

```json
{ "error": { "code": "...", "message": "...", "details": {...} } }
```
主要错误码：`UNAUTHORIZED` / `KB_NOT_FOUND` / `DOCUMENT_NOT_FOUND` / `JOB_NOT_FOUND` / `QUOTA_EXCEEDED` / `UPSTREAM_LLM_ERROR` / `PARSE_FAILED` / `RATE_LIMITED`。

### 6.2 LLM/Embedding/VLM 提供给 sidecar

**v1 方案**：sidecar 通过 env 配自己的 LLM/embedding/VLM 端点（OpenAI 兼容）。这要求用户**两边各配一遍**相同模型。简单但有冗余。

**v2 改进**（不在 v1 实施）：onyx 在每次 `/v1/kbs/{kb_id}/query` 调用时透传当前 persona 选用的 LLM 配置：
```json
"llm_override": {
  "provider": "openai",
  "model": "gpt-4o",
  "api_key": "<onyx 解密后的 key>",
  "base_url": "..."
}
```
sidecar 收到后用 `llm_override` 临时构造 llm_func（LRU cache 按 (kb_id, llm_signature) 二级缓存）。这样 onyx 是 LLM 配置的单一源头，删 sidecar 配置面板。安全前提：sidecar 仅在 onyx 内网，token 即使在请求体里也不外泄。

---

## 7. onyx 侧具体改动

### 7.1 后端

| 路径 | 性质 | 内容 |
|---|---|---|
| `backend/onyx/db/enums.py` | 修改 | `ProcessingMode` 加 `MULTIMODAL_KG` |
| `backend/onyx/db/models.py` | 修改 | 新增 `MultimodalKGJob` SQLAlchemy 模型 |
| `backend/alembic/versions/xxxx_multimodal_kg.py` | 新建 | 迁移脚本 |
| `backend/onyx/configs/app_configs.py` | 修改 | 加 4 个 env：`RAG_ANYTHING_BASE_URL` / `RAG_ANYTHING_INTERNAL_TOKEN` / `RAG_ANYTHING_INGEST_TIMEOUT_S` / `RAG_ANYTHING_QUERY_TIMEOUT_S` |
| `backend/onyx/integrations/raganything/__init__.py` | 新建 | 模块入口 |
| `backend/onyx/integrations/raganything/client.py` | 新建 | 异步 httpx 客户端，封装所有 sidecar 调用，重试+超时 |
| `backend/onyx/integrations/raganything/pipeline.py` | 新建 | `_run_multimodal_kg_pipeline()` 实现，被 `indexing_pipeline.py` 调用 |
| `backend/onyx/integrations/raganything/tasks.py` | 新建 | Celery 任务：`forward_to_raganything_task` / `delete_from_raganything_task` / `poll_multimodal_kg_jobs` |
| `backend/onyx/indexing/indexing_pipeline.py` | 修改 | `run_indexing_pipeline` 入口加 `cc_pair.processing_mode` 分流（约第 1289 行） |
| `backend/onyx/tools/tool_implementations/multimodal_kg/__init__.py` | 新建 |  |
| `backend/onyx/tools/tool_implementations/multimodal_kg/multimodal_kg_tool.py` | 新建 | 实现 `Tool[None]`，名 `MultimodalKGTool`，LLM-facing name `multimodal_kg_query` |
| `backend/onyx/tools/built_in_tools.py` | 修改 | 注册到 `BUILT_IN_TOOL_MAP`、`BUILT_IN_TOOL_TYPES`、`CITEABLE_TOOLS_NAMES` |
| `backend/onyx/server/documents/connector.py` | 修改 | cc_pair create/update API 接受 `processing_mode='MULTIMODAL_KG'` |
| `backend/onyx/server/documents/document_set.py`（如有） | 修改 | docset 删除时连带 sidecar 清理 |

### 7.2 前端

| 路径（具体名按 onyx 现有约定） | 内容 |
|---|---|
| `web/src/app/admin/connectors/[connector]/AddConnectorPage.tsx`（或类似） | 表单加 "Processing Mode" radio 组；当 env 暴露 `NEXT_PUBLIC_RAG_ANYTHING_AVAILABLE=true` 时显示第二选项 |
| `web/src/app/admin/connectors/[connector]/[id]/page.tsx` | cc_pair 详情页显示模式 + 摄入 job 状态轮询（调 onyx 后端 `GET /api/cc_pair/{id}/multimodal_kg_jobs`） |
| `web/src/app/admin/assistants/AssistantEditor.tsx`（或类似） | Persona 编辑器：tools 列表加 "Multimodal KG Search" 开关；当 persona 绑定的 docset 含 MULTIMODAL_KG cc_pair 时默认开启 |
| Chat 页 | 不需要改（Tool 结果走现有 Citation 渲染） |

### 7.3 部署

`docker-compose.yml` 新增 sidecar 服务（沿用现有设计稿）+ onyx 主服务环境变量注入 sidecar URL/token。

---

## 8. 关键技术决策

### 8.1 为什么是 sidecar 而不是把 RAG-Anything 作为库内嵌进 onyx？

- **MinerU 模型几个 GB**，进 onyx 主容器会让镜像膨胀 + 启动变慢。
- **LightRAG 是有状态的**，每个 working_dir 一份 KV/向量/图，需要自己的生命周期管理（finalize_storages）和 LRU 缓存逻辑。塞进 onyx 的 Celery worker 会和 onyx 自己的索引流水线状态打架。
- **依赖冲突风险**：RAG-Anything 拉一堆 ML 库（torch/whisper/paddleocr...），有可能和 onyx 现有 stack 冲突。
- **独立演进**：sidecar 升级 RAG-Anything 版本不影响 onyx 主服务。
- **现成的 sidecar 设计**：用户已经有 [RAG_SERVICE_DESIGN.md](/home/edward/research/RAG-Anything/RAG_SERVICE_DESIGN.md)，复用即可。

**备选方案**（已否决）：把 RAG-Anything 的 Parser 单独拉进 onyx，用 onyx 自己的 chunker/embedder/Vespa 当存储。优点是单存储；缺点是丢失 LightRAG 的图谱/多模态实体能力，与用户"要全部 RAG-Anything 能力"的诉求不符。

### 8.2 为什么是 cc_pair 级模式开关而不是 docset 级？

- **cc_pair 是数据来源**，决定文档怎么来怎么解析；docset 是数据归类，决定文档怎么聚合给 persona 用。模式属于"怎么处理"，自然在 cc_pair 上。
- **onyx 已经有 `processing_mode` 字段在 cc_pair 上**，复用比新建字段更优。
- **更细的隔离粒度**：同一 docset 可以混合模式（例如一个 cc_pair 走 MULTIMODAL_KG 处理重要的复杂 PDF，另一个 cc_pair 走 REGULAR 处理普通文本），Persona 的 MultimodalKGTool 自动只查 MULTIMODAL_KG 那部分。

### 8.3 为什么不双写（同一文档同时进 Vespa 和 LightRAG）？

- **存储成本翻倍**：大文档存两份。
- **嵌入成本翻倍**：两个系统各跑一遍 embedding。
- **索引延迟翻倍**：两条管道串行/并行都要等。
- **失败模式复杂**：一边成功一边失败时怎么办？
- **业务上不需要**：用户的查询要么走 SearchTool 要么走 MultimodalKGTool，不会同时查两边。

如果未来确实出现"同一份文档既要走快速文本检索又要走图谱推理"的需求，再以 docset 级"投递到多个 cc_pair"的方式实现，比双写更可控。

### 8.4 kb_id 命名

| 部署形态 | kb_id 形式 |
|---|---|
| 单租户 | `ccpair-{cc_pair.id}` |
| 多租户（未来） | `t{tenant_id}-cc{cc_pair.id}` |

为何不用 UUID？`cc_pair.id` 是 onyx 内部 int 主键，自带唯一性，省一张映射表，且日志里看着清楚。给字符串前缀确保是合法的目录名片段。

### 8.5 引用回链怎么做？

- onyx 摄入时往 sidecar 传 `metadata.onyx_document_id`。
- sidecar 把它存进 LightRAG 的 chunk metadata（LightRAG 的 chunk 是 dict，可以挂任意字段）。
- 查询响应 `sources[].onyx_document_id` 原样回传。
- onyx MultimodalKGTool 用这个 id 查 onyx documents 表，构造 Citation。

这样**onyx 仍是文档元数据/UI 渲染的唯一权威**，sidecar 的 KG 只存语义内容。

### 8.6 Tool 描述该怎么写才能让 LLM 选对？

LLM 路由的关键是工具描述。建议：

```
SearchTool:
  "Search the indexed corpus for documents matching the query. Best for direct
   factual lookup: 'when was X released', 'who is the author of Y'. Returns top
   passages with citations."

MultimodalKGTool (新):
  "Query the multimodal knowledge graph built from documents. Best for:
   1) Cross-document reasoning ('list all customers across these reports')
   2) Questions about charts/tables/figures/equations
   3) Multi-hop questions ('which projects mention companies that are competitors of X')
   4) When SearchTool's flat passages aren't enough.
   Returns answer + sources with modality (text|image|table|equation)."
```

把"最适合干什么"写清楚，LLM 大概率能选对。万一选错了，用户可以在 persona 里手动只开一个工具。

---

## 9. 安全

### 9.1 跨服务认证

- onyx → sidecar：共享 Bearer token (`RAG_ANYTHING_INTERNAL_TOKEN`)，sidecar 用常量时间比较验证。token 32+ 字节随机。
- sidecar 仅监听内网（docker network 或 127.0.0.1），不开公网。
- 未来如果 sidecar 要给浏览器直连：换成 onyx 签发的短期 JWT（claim 含 kb_id 和过期时间），**不能**把 onyx-sidecar 共享 token 暴露给浏览器。

### 9.2 路径/输入安全

- `kb_id` 必须正则白名单 `^[a-zA-Z0-9_-]{1,64}$`，禁止 `..` 路径穿越。校验集中在 sidecar 的 `core/paths.py`。
- 上传文件：sidecar 上限 1000MB（streaming，不全量读内存）；MIME 白名单。

### 9.3 多租户/RBAC（v1 限制）

- v1 单租户：所有 onyx 用户共享一套 sidecar，docset 级隔离够用。
- v1 限制：sidecar 端**不**做用户级 ACL 过滤。如果一个 persona 能访问某个 MULTIMODAL_KG cc_pair，它就能看到该 cc_pair 下所有 LightRAG 内容。这与"用户级文档可见性"细粒度需求不兼容。
- v2 方案（不实施）：onyx 调 query 时附带 `allowed_doc_ids` 列表；sidecar 端在返回 sources 前后过滤掉不在列表里的。

### 9.4 LLM 配置泄露风险

如果走 v2 LLM 透传方案，onyx 把 API key 发给 sidecar。前提：sidecar 内网部署；不打日志；不持久化。审计要能证明这条传递路径被认证、加密、不落盘。

---

## 10. 可观测性

### 10.1 onyx 侧

- onyx 现有 IndexAttempt 事件 + Sentry 复用：MULTIMODAL_KG 路径加 stage 标签 `multimodal_kg_dispatch` / `multimodal_kg_poll`。
- 新加 Prometheus 指标（`backend/onyx/observability/`）：
  - `onyx_multimodal_kg_jobs_total{status}`
  - `onyx_multimodal_kg_dispatch_latency_seconds`
  - `onyx_multimodal_kg_query_latency_seconds`
  - `onyx_multimodal_kg_sidecar_unavailable_total`

### 10.2 sidecar 侧

沿用 [RAG_SERVICE_DESIGN.md](/home/edward/research/RAG-Anything/RAG_SERVICE_DESIGN.md) 第"可观测性"节：structlog JSON 日志、prom 指标 `rag_query_latency_seconds` / `rag_ingest_duration_seconds` / `rag_llm_tokens_total` / `rag_active_rag_instances` / `rag_queue_depth`。所有日志带 `kb_id` / `onyx_request_id`。

### 10.3 跨服务追踪

`X-Onyx-Request-Id` 在两边日志里都出现，能 grep 串起一次完整调用。

---

## 11. 性能与成本

### 11.1 摄入

| 阶段 | 耗时（一份 50 页带图 PDF） | 备注 |
|---|---|---|
| onyx FileConnector + 落盘 | 1–2s | 现有 |
| onyx → sidecar 传输 | 1–3s | 取决于网络/文件大小 |
| sidecar 排队 | 0–N s | 看队列深度 |
| MinerU 解析 | 10–30s | CPU 密集，可上 GPU 加速 |
| LightRAG 写入（含 LLM 抽实体） | 30–120s | LLM 调用密集，有重试 |
| 总计 | 1–3 分钟/份 |

并发：sidecar worker 数量限制；单 kb 内串行。

### 11.2 查询

| 模式 | 耗时（典型） |
|---|---|
| naive | 1–3s |
| local | 2–5s |
| global | 5–15s |
| hybrid | 5–10s |
| mix | 10–20s |

策略：
- onyx 那边超时设 60s，前端流式渲染 "正在查询多模态知识图谱..." 状态。
- 大量 query 命中同一 kb 时 LRU 命中率高，单实例热数据 < 1s。

### 11.3 成本

- **解析**：MinerU 自托管，无 LLM 调用（除非用 paddleocr 之类）。零 token。
- **索引**：LightRAG 抽实体/关系大量调 LLM。一份 50 页 PDF 估算 100–500K tokens。这是大头。
- **VLM**：每张图一次 vision call，成本中等。
- **查询**：每次查询 1–3 次 LLM 调用。

成本控制：sidecar 每 kb 配 token 配额（[RAG_SERVICE_DESIGN.md](/home/edward/research/RAG-Anything/RAG_SERVICE_DESIGN.md) 第"护栏"节），超出 429。

---

## 12. 风险与未决项

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| 1 | sidecar 挂掉 | MULTIMODAL_KG cc_pair 摄入/查询全部失败 | Tool `is_available` 加健康探活；前端显式提示；摄入 job 进入 `queued` 等重试不丢 |
| 2 | LLM 配置不一致（onyx 配 A，sidecar 配 B） | 查询用的是 sidecar 的模型，可能和用户预期不符 | v1：env 强制对齐；UI 提示；v2：transparent 透传 |
| 3 | 重索引风暴 | onyx 重新拉一次 connector → sidecar 重复处理 N 文档 | sidecar 端按 (onyx_document_id, doc_updated_at) 幂等去重 |
| 4 | LightRAG 没原生用户级 ACL | docset 共享 → 无法做用户级文档可见性 | v1 文档化限制；v2 加 allowed_doc_ids 后过滤 |
| 5 | MinerU 模型下载（几 GB） | sidecar 首启动慢 | Docker build 阶段预下载或 volume 缓存 |
| 6 | onyx 已有 `KnowledgeGraphTool` stub | 命名/概念混淆 | 新工具叫 `MultimodalKGTool`；display name 加 "(RAG-Anything)" |
| 7 | onyx 升级破坏 `processing_mode` 分流点 | 集成断 | 索引流水线分流封一层 wrapper，受 onyx upstream 改动影响最小 |
| 8 | sidecar 升级 LightRAG 破坏存储格式 | 已有 working_dir 数据需要迁移 | RAG-Anything 升级前评审；备份 working_dir |
| 9 | 用户级文档 ACL（多用户场景） | 信息泄露风险 | v1 单租户场景不发生；多用户切多租户时一并解决 |
| 10 | 多 cc_pair fan-out 性能 | 一个 docset 含 10 个 MULTIMODAL_KG cc_pair 时，并发 10 个查询 | MVP 限制每 docset 至多 3 个 MULTIMODAL_KG cc_pair；超过的提示用户合并 |
| 11 | 引用渲染时 sidecar 没回 onyx_document_id | 引用断 | 契约里强制 metadata 必带；sidecar 回包必带；不带就报错而不是降级 |

---

## 13. 渐进式扩展路径

| 触发条件 | 演进动作 |
|---|---|
| 用户量上升，单 sidecar worker 不够 | sidecar worker 加副本（per-kb Redis 锁防冲突） |
| 单机磁盘吃紧 | sidecar `data/` 挂 NFS 或对象存储 fuse |
| 某 KB 文档数 > 1000 | 该 KB 切到 LightRAG + Postgres+pgvector+AGE 后端（透明） |
| 多个组织共用 onyx 实例 | onyx 开 MULTI_TENANT；kb_id 加 tenant 前缀；sidecar 加 KB 删除接口；onyx 租户删除级联 |
| 用户要带图问 | onyx chat 输入区加图片附件；MultimodalKGTool.run 走 sidecar `/query/multimodal` |
| LLM 想用不同模型/key | 切 v2 LLM 透传方案 |
| 公网暴露需求 | 共享 Bearer token 改为 onyx 签发的 JWT |

---

## 14. 实施路线图（不在本方案范围内执行）

> 以下仅为后续执行参考。当前文档目标到达本节即结束，**不写任何代码**。

| Phase | 内容 | 预估工作量 |
|---|---|---|
| 0 | 评审本方案、对齐目标 | 1 周 |
| 1 | sidecar 单独跑通（按现有设计稿） | 2–3 周 |
| 2 | onyx 加 ProcessingMode + alembic + 客户端 + Celery 任务（无 UI） | 1–2 周 |
| 3 | 索引流水线分流 + multimodal_kg_jobs 状态轮询 | 1 周 |
| 4 | MultimodalKGTool 实现 + 注册 + 查询链路打通 | 1–2 周 |
| 5 | 前端：connector 模式选择 + persona 编辑器 tool 开关 + cc_pair 详情页 | 1–2 周 |
| 6 | 引用回链 + 删除链路 + 异常路径 | 1 周 |
| 7 | 可观测性 + 文档 + Demo | 1 周 |

**总计**：约 8–12 周（单人；视前端经验加减）。

---

## 15. 附录

### 15.1 关键代码引用

**onyx 复用点**：
- Tool 接口：[backend/onyx/tools/interface.py:17](/home/edward/research/onyx/backend/onyx/tools/interface.py#L17)
- 内置工具注册：[backend/onyx/tools/built_in_tools.py:32](/home/edward/research/onyx/backend/onyx/tools/built_in_tools.py#L32)
- ProcessingMode 字段：[backend/onyx/db/models.py:793](/home/edward/research/onyx/backend/onyx/db/models.py#L793)
- 索引流水线入口：[backend/onyx/indexing/indexing_pipeline.py:1289](/home/edward/research/onyx/backend/onyx/indexing/indexing_pipeline.py#L1289)
- DocumentSet：[backend/onyx/db/models.py:3432](/home/edward/research/onyx/backend/onyx/db/models.py#L3432)
- Persona__Tool（M:N）：[backend/onyx/db/models.py:660](/home/edward/research/onyx/backend/onyx/db/models.py#L660)
- 已有但未启用的 `KnowledgeGraphTool` stub（避免撞名）：[backend/onyx/tools/tool_implementations/knowledge_graph/knowledge_graph_tool.py](/home/edward/research/onyx/backend/onyx/tools/tool_implementations/knowledge_graph/knowledge_graph_tool.py)

**RAG-Anything 复用点**：
- 主类：[raganything/raganything.py:50](/home/edward/research/RAG-Anything/raganything/raganything.py#L50)
- 解析器（独立可调用）：[raganything/parser.py:65](/home/edward/research/RAG-Anything/raganything/parser.py#L65)
- 端到端处理：[raganything/processor.py](/home/edward/research/RAG-Anything/raganything/processor.py) `process_document_complete`、`insert_content_list`
- 查询：[raganything/query.py:102](/home/edward/research/RAG-Anything/raganything/query.py#L102) `aquery`
- VLM 增强查询：[raganything/query.py:349](/home/edward/research/RAG-Anything/raganything/query.py#L349) `aquery_vlm_enhanced`
- 现成调用范式：[scripts/run_rag.py](/home/edward/research/RAG-Anything/scripts/run_rag.py)

**用户既有相关文档**：
- [RAG_SERVICE_DESIGN.md](/home/edward/research/RAG-Anything/RAG_SERVICE_DESIGN.md) ——sidecar 详细设计
- [api_summary.md](/home/edward/research/RAG-Anything/api_summary.md) ——RAG-Anything 作为独立服务的 API 设计纪要

### 15.2 术语表

| 术语 | 释义 |
|---|---|
| cc_pair | onyx 的 ConnectorCredentialPair，连接器+凭证的组合，是 onyx 数据摄入的最小单元 |
| DocumentSet | onyx 中一组文档的集合，Persona 通过它选择能访问的文档 |
| Persona | onyx 的 Chat 助手配置，绑定 DocumentSets + Tools + LLM |
| Tool（onyx） | LLM 在 chat 中可调用的功能单元，如 SearchTool、WebSearchTool |
| kb_id | RAG-Anything sidecar 中一个 KB 的唯一标识，对应一个 LightRAG `working_dir` |
| LightRAG | RAG-Anything 用的底层 RAG 库，提供图谱+向量混合检索 |
| MinerU | RAG-Anything 默认的多模态文档解析器 |
| ProcessingMode | onyx cc_pair 上的字段，本方案扩展 `MULTIMODAL_KG` 值 |
| MultimodalKGTool | 本方案新增的 onyx Tool，调用 sidecar 做图谱查询 |

### 15.3 文档版本

- v0.1（2026-05-03）：初稿。

---
