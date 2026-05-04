# RAG-Anything 子服务实施方案

## Context

用户有一个已经管理用户系统的现有项目，希望把 RAG-Anything 包成一个**独立的内网 HTTP 子服务**，由主服务通过传 `tenant_id` 调用。核心功能：

- 用户上传任意类型文件（PDF/Office/纯文本/图片/音视频）
- 用户对自己上传的文件做问答

**为什么需要这个方案**：RAG-Anything 是 Python 库且生命周期内有可变状态（LightRAG 索引、解析缓存），直接嵌入主服务有两个问题：(1) 如果主服务不是 Python 写的，无法嵌入；(2) MinerU 解析慢（10–60s/文件）会阻塞主服务的 HTTP worker。把它独立成"API + Worker"双进程的子服务，是同时解决这两个问题的最小代价方案。

**预期规模**：小型 SaaS / 初创产品，几十到几百租户，单机部署起步，按需扩展。

## 架构总览

```
主服务 ──(HTTP, X-Tenant-Id + Bearer token)──▶ RAG 子服务
                                                ├── api (FastAPI)
                                                │     - /query (同步)
                                                │     - /ingest (入队)
                                                │     - /jobs/{id}
                                                │     - LRU 缓存 RAGAnything 实例
                                                ├── worker (arq)
                                                │     - 消费 ingest 队列
                                                │     - 调 MinerU + LightRAG 写入
                                                │     - 写完发 Redis pubsub
                                                ├── Postgres (jobs/documents/audit)
                                                ├── Redis (队列 + pubsub + 限流)
                                                └── 本地盘 data/{uploads,working_dirs}
```

**核心数据流**：
1. 上传 → API 落盘 + 入队 → 立即返回 `job_id`
2. Worker 拉任务 → MinerU + RAGAnything.process_document_complete → 落 working_dir
3. Worker 发 `tenant_reload:{tenant_id}` pubsub 信号
4. API 订阅信号 → evict LRU 缓存里该租户的实例
5. 下次查询 → API lazy-rebuild 实例 → `aquery()` → 返回答案

**为什么 API 和 Worker 分进程**：API 永远不被 MinerU 拖慢；Worker 挂了重启从 Redis 续上；同租户写入靠 Redis 锁串行（绕开 LightRAG 文件存储并发问题），跨租户并行。

## 技术栈

| 组件 | 选型 | 理由 |
|---|---|---|
| 语言/框架 | Python 3.11 + FastAPI | RAG-Anything 是 Python 库，零阻抗 |
| 队列 | arq (Redis-based) | 原生 async，比 Celery 轻 10x |
| DB | Postgres 16 | 业务元数据，不放索引 |
| 缓存/队列/pubsub | Redis 7 | 一物三用 |
| 解析 | MinerU（RAG-Anything 默认） | 用户需要 PDF/Office/图片/音视频全覆盖 |
| LLM/Embedding/VLM | OpenAI 兼容 API | 用户偏好；与 RAG-Anything 现有示例一致 |
| 部署 | Docker Compose（单机起步） | 小型 SaaS 阶段够用 |

## 需要新建的代码结构

主服务和 RAG 子服务**完全分离**。子服务建议放新仓库（或主仓库的 `services/rag/`）。**注意：本方案的所有新代码都在子服务仓库内，RAG-Anything 本身保持不动，仅作为依赖引入。**

```
rag-service/
├── pyproject.toml                     # 依赖：raganything, fastapi, arq, asyncpg, redis, structlog
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── alembic/                           # DB migrations
│   └── versions/001_initial.py
├── src/rag_service/
│   ├── __init__.py
│   ├── config.py                      # pydantic-settings 读环境变量
│   ├── api/
│   │   ├── __init__.py
│   │   ├── app.py                     # FastAPI app + 中间件
│   │   ├── auth.py                    # Bearer token + X-Tenant-Id 校验
│   │   ├── deps.py                    # FastAPI 依赖注入
│   │   ├── routers/
│   │   │   ├── ingest.py              # POST /v1/ingest
│   │   │   ├── jobs.py                # GET /v1/jobs/{id}
│   │   │   ├── documents.py           # GET/DELETE /v1/documents
│   │   │   ├── query.py               # POST /v1/query
│   │   │   ├── tenants.py             # GET /v1/tenants/me
│   │   │   └── health.py              # /healthz /readyz /metrics
│   │   └── schemas.py                 # pydantic 请求/响应模型
│   ├── worker/
│   │   ├── __init__.py
│   │   ├── settings.py                # arq WorkerSettings
│   │   ├── tasks.py                   # ingest_document, rebuild_index
│   │   └── locks.py                   # per-tenant Redis 分布式锁
│   ├── core/
│   │   ├── rag_factory.py             # 创建/缓存 RAGAnything 实例
│   │   ├── llm_provider.py            # OpenAI 兼容 LLM/embedding/VLM 工厂
│   │   ├── reload_listener.py         # API 端订阅 tenant_reload pubsub
│   │   └── paths.py                   # working_dir / uploads 路径推导（含白名单校验）
│   ├── db/
│   │   ├── models.py                  # SQLAlchemy 模型
│   │   ├── session.py                 # async session
│   │   └── repositories.py            # CRUD 封装
│   ├── observability/
│   │   ├── logging.py                 # structlog 配置
│   │   └── metrics.py                 # Prometheus 指标定义
│   └── cli.py                         # rag-api / rag-worker 入口
└── tests/
    ├── conftest.py                    # 测试用 Postgres/Redis fixtures
    ├── test_api_query.py
    ├── test_api_ingest.py
    ├── test_worker_ingest.py
    ├── test_dedup.py
    └── test_reload.py
```

**复用的关键 RAG-Anything 接口**（这些都已经存在，无需修改）：
- [raganything/raganything.py](raganything/raganything.py): `RAGAnything(config, llm_model_func, embedding_func, vision_model_func)`
- [raganything/processor.py](raganything/processor.py): `process_document_complete(file_path, output_dir, ...)`
- [raganything/query.py:102](raganything/query.py#L102): `aquery(question, mode, top_k)`
- [raganything/config.py](raganything/config.py): `RAGAnythingConfig(working_dir=...)`
- 现有调用范式参考：[scripts/run_rag.py](scripts/run_rag.py)

## 数据模型（Postgres）

```sql
CREATE TABLE tenants (
  tenant_id        TEXT PRIMARY KEY,
  created_at       TIMESTAMPTZ DEFAULT now(),
  storage_quota_mb INT DEFAULT 1024,
  config_json      JSONB DEFAULT '{}'
);

CREATE TABLE documents (
  document_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      TEXT REFERENCES tenants ON DELETE CASCADE,
  file_name      TEXT NOT NULL,
  file_size      BIGINT,
  content_hash   TEXT NOT NULL,
  mime_type      TEXT,
  storage_path   TEXT NOT NULL,
  status         TEXT NOT NULL,        -- pending | parsing | indexed | failed | deleted
  uploaded_at    TIMESTAMPTZ DEFAULT now(),
  indexed_at     TIMESTAMPTZ,
  error_message  TEXT,
  UNIQUE (tenant_id, content_hash)
);
CREATE INDEX ON documents (tenant_id, status);

CREATE TABLE jobs (
  job_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id      TEXT NOT NULL,
  document_id    UUID REFERENCES documents,
  job_type       TEXT NOT NULL,        -- ingest | reindex | delete
  status         TEXT NOT NULL,        -- queued | running | done | failed
  progress       JSONB DEFAULT '{}',
  created_at     TIMESTAMPTZ DEFAULT now(),
  started_at     TIMESTAMPTZ,
  finished_at    TIMESTAMPTZ,
  error_message  TEXT,
  retries        INT DEFAULT 0
);
CREATE INDEX ON jobs (tenant_id, status, created_at DESC);

CREATE TABLE query_log (
  id           BIGSERIAL PRIMARY KEY,
  tenant_id    TEXT NOT NULL,
  user_id      TEXT,
  question     TEXT NOT NULL,
  mode         TEXT,
  latency_ms   INT,
  token_in     INT,
  token_out    INT,
  cost_usd     NUMERIC(10, 6),
  created_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON query_log (tenant_id, created_at DESC);
```

**关键决策**：
- `content_hash` (sha256) 做同租户去重，命中直接返回 `deduplicated: true`
- 删除 = 软删 + 入队 `rebuild_index` 任务（LightRAG 图谱无法精准删单文档）
- 所有外键带 `ON DELETE CASCADE`，租户注销可级联清理

## 文件系统布局

```
data/
├── uploads/{tenant_id}/{document_id}.{ext}
├── working_dirs/{tenant_id}/...        # LightRAG KV/向量/图谱
└── mineru_output/{tenant_id}/{document_id}/
```

**安全**：`tenant_id` 必须匹配 `^[a-zA-Z0-9_-]{1,64}$`，禁止 `..` 路径穿越。集中在 `core/paths.py` 校验。

## API 表面

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/v1/ingest` | 上传 + 入队（multipart） |
| GET | `/v1/jobs/{job_id}` | 查任务状态 |
| GET | `/v1/documents` | 列文档（游标分页） |
| GET | `/v1/documents/{id}` | 单文档详情 |
| DELETE | `/v1/documents/{id}` | 软删 + 入队重建 |
| POST | `/v1/query` | 同步查询 |
| GET | `/v1/tenants/me` | 配额/用量 |
| GET | `/healthz` `/readyz` `/metrics` | 运维 |

**鉴权**：
- `Authorization: Bearer <INTERNAL_TOKEN>` 服务间共享密钥（常量时间比较）
- `X-Tenant-Id: <tenant_id>` 必填，正则白名单
- `X-User-Id: <user_id>` 可选，仅审计

**示例请求**：
```http
POST /v1/ingest
Authorization: Bearer <INTERNAL_TOKEN>
X-Tenant-Id: acme-corp
X-User-Id: u_42
Content-Type: multipart/form-data

file=@./report.pdf
```
响应：
```json
{ "job_id": "01HFG...", "document_id": "d_8f...", "status": "queued", "deduplicated": false }
```

```json
POST /v1/query
{
  "question": "Q3 的销售额是多少？",
  "mode": "hybrid",
  "top_k": 10,
  "vlm_enhanced": false
}
```
响应：
```json
{
  "answer": "...",
  "sources": [{"document_id": "...", "file_name": "report.pdf", "chunk_id": "c_12", "score": 0.87}],
  "latency_ms": 1843,
  "tokens": {"in": 4200, "out": 380, "cost_usd": 0.012}
}
```

**约束**：
- 上传上限 1000MB（streaming upload，不全量读内存）
- `/query` 超时 60s，`/ingest` 上传部分 30s
- 每租户 QPS 限流 10（Redis 计数器）
- CORS 默认 `*`（**已知风险**：如果未来浏览器要直接调，必须把内部 token 换成主服务签发的短期 JWT）

**错误约定**：`{"error": {"code": "...", "message": "...", "details": {...}}}`，主要错误码：`UNAUTHORIZED` / `TENANT_INVALID` / `DOCUMENT_NOT_FOUND` / `JOB_NOT_FOUND` / `QUOTA_EXCEEDED` / `UPSTREAM_LLM_ERROR` / `PARSE_FAILED` / `RATE_LIMITED`。

## Ingest 流水线

**Worker 处理 `ingest_document`**：
1. 拿 per-tenant Redis 锁（`SET tenant_lock:{tenant_id} NX EX 600`）
2. 标记 job running
3. 调 `RAGAnything.process_document_complete()`（内部含 MinerU + LightRAG 写入；带 progress callback 更新 `jobs.progress`）
4. 标记 document indexed + job done
5. `redis.publish("tenant_reload:{tenant_id}", "1")`
6. 异常分类重试：MinerU 子进程崩溃重试 2 次（指数退避），LLM 错误立即放弃

**`rebuild_index`（删除触发）**：
1. 拿锁（更长 timeout，3600s）
2. 标记目标文档 `status='deleted'`
3. 备份 working_dir 到 `.bak`
4. 重新跑所有 `status='indexed'` 文档（parse_cache 命中则跳过 MinerU，只重跑 LLM 抽取）
5. 清理备份 + 发重载信号

**Reload 信号**：API 进程启动时启 background task 订阅 `tenant_reload:*`，收到就 evict LRU 缓存里该租户的实例（不热重建——避免雪崩）。

## 部署

**Docker Compose**：
```yaml
services:
  api:
    build: .
    command: rag-api
    ports: ["8000:8000"]
    environment: &rag-env
      - INTERNAL_TOKEN=${INTERNAL_TOKEN}
      - DATABASE_URL=postgresql://rag:rag@db:5432/rag
      - REDIS_URL=redis://redis:6379/0
      - LLM_BASE_URL=${LLM_BASE_URL}
      - LLM_API_KEY=${LLM_API_KEY}
      - LLM_MODEL=${LLM_MODEL}
      - EMBEDDING_BASE_URL=${EMBEDDING_BASE_URL}
      - EMBEDDING_API_KEY=${EMBEDDING_API_KEY}
      - EMBEDDING_MODEL=${EMBEDDING_MODEL}
      - VLM_MODEL=${VLM_MODEL}
      - DATA_DIR=/data
      - MAX_UPLOAD_MB=1000
      - CORS_ORIGINS=*
    volumes: ["./data:/data"]
    depends_on: [db, redis]
  worker:
    build: .
    command: rag-worker
    environment: *rag-env
    volumes: ["./data:/data"]
    depends_on: [db, redis]
    deploy: {resources: {limits: {memory: 8G}}}
  db:
    image: postgres:16
    environment: {POSTGRES_USER: rag, POSTGRES_PASSWORD: rag, POSTGRES_DB: rag}
    volumes: ["./pgdata:/var/lib/postgresql/data"]
  redis:
    image: redis:7-alpine
    volumes: ["./redisdata:/data"]
```

**配置三层**（优先级：请求级 > 租户级 > 进程级）：
1. 进程级环境变量：连接串、密钥、默认模型、上限
2. 租户级 `tenants.config_json`：覆盖 LLM 模型、top_k 偏好、租户自带 API key
3. 请求级查询 body：临时覆盖 `mode`、`top_k`、`vlm_enhanced`

**可观测性**：
- structlog JSON 日志，每条带 `tenant_id` / `request_id` / `job_id`
- Prometheus 指标：`rag_query_latency_seconds`、`rag_ingest_duration_seconds`、`rag_llm_tokens_total`、`rag_llm_cost_usd_total`、`rag_active_rag_instances`、`rag_queue_depth`

**护栏**：
- 每租户每日 token 配额（超出 429）
- 每租户存储配额（上传时校验）
- 每租户 QPS 限流
- MinerU 子进程超时 600s
- LLM 超时 30s + 重试 2 次

**备份**：Postgres `pg_dump` 每日 + working_dirs/uploads rsync 到 S3-compat。脚本化 `restore.sh tenant_id` 单租户恢复 30 分钟内。

## 渐进式扩展路径

| 触发 | 动作 |
|---|---|
| Worker CPU 跑满 | `worker` 加副本（per-tenant 锁天然防冲突） |
| 单机磁盘吃紧 | `data/` 挂 NFS / 对象存储 fuse |
| 某租户文档数 > 1000 | 该租户切到 LightRAG + Postgres+pgvector backend |
| 多个大客户 / 上千租户 | 全量迁 K8s + 共享存储后端（方案 C） |

## 端到端验证

按构建顺序，每步都要能独立测：

1. **Skeleton**：`docker compose up` 起来，`/healthz` 200，`/readyz` 检查 PG/Redis/磁盘可写
2. **Auth**：缺 token / 错 token / 缺 tenant-id / 非法 tenant-id 全部正确拒绝
3. **路径安全**：`tenant_id="../etc/passwd"` 这类 payload 被白名单拦下
4. **Ingest 同步部分**：`POST /v1/ingest` 返回 `job_id`、`document_id`，文件落到正确路径
5. **去重**：同租户上传相同文件，第二次 `deduplicated=true` 且不入队
6. **Worker 处理**：起 worker，job 状态变 `running` → `done`，`documents.status='indexed'`
7. **Query**：`POST /v1/query` 返回答案 + 来源；新建 API 副本（不预热），首次查询 lazy-load 实例正常
8. **Reload 信号**：API 进程在 ingest 完成后能感知到，evict 缓存
9. **Per-tenant 串行**：同租户连续两个 ingest，第二个等第一个完成才开始；不同租户并行
10. **删除 = 重建**：删一个文档，job 跑完后查询不再返回该文档的 chunk
11. **限流/配额**：超 QPS 返 429；超存储配额上传被拒
12. **指标**：`/metrics` 含所有定义的指标，租户标签正确

**测试栈**：pytest + pytest-asyncio + testcontainers（拉真 Postgres/Redis），不 mock DB（关键路径要打到真实存储）。LLM 用一个 fake provider 注入，测试时不烧 token。

## 已知风险与未决项

1. **CORS `*` + Bearer token**：如果未来浏览器直连 RAG，需切换到主服务签发的短期 JWT（带 tenant_id claim）。当前内网用没问题。
2. **删除 = 重建** 的成本：N 个文档时是 O(N) LLM 抽取调用，对大租户不友好。需要在产品上限速 + 提示用户。后续可探索"延迟过滤"方案（查询时按 `documents.status` 过滤 chunks，但需要 LightRAG 改造）。
3. **MinerU 模型下载**：首次启动 worker 会下载几个 GB 的模型，应在 Docker build 阶段预下载，或挂 volume 缓存。
4. **共享磁盘的多机扩展**：方案 B 的"加 worker 副本"前提是所有 worker 看到同一个 `data/` 目录（NFS 或 EFS），否则 working_dir 不一致。早期单机不是问题。
5. **重启时 in-flight job**：worker 关停时正在跑的 job 会被 arq 标记失败重试。`rebuild_index` 不幂等（已经备份过 working_dir 再重跑会冲掉），需要在任务开头检查 `.bak` 是否已存在。
