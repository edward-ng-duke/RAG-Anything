# RAG-Anything 作为独立 RAG 服务的 API 设计要点

把 RAG-Anything 包成一个 HTTP 服务，集成进已有 agent 系统（带用户体系，每个用户多个"AI 文件夹"）的设计纪要。

---

## 1. 核心理念：无状态 ≠ 无数据库

REST 里的 stateless 指**没有会话状态**（请求自包含、不依赖前一个请求的 server 内存），不是"服务端没有数据"。

| 例子 | 有持久数据？ | 是无状态 API 吗？ |
|---|---|---|
| AWS S3 | 海量 | ✅ 是（每请求带 bucket+key+签名） |
| Stripe API | 全部交易 | ✅ 是 |
| 我们的 RAG 服务 | 知识图谱+向量 | ✅ **依然是** |

进程内的 LRU 实例缓存只是**性能优化**，不是协议状态 — 缓存清空了，下个请求重新加载，结果完全一样。

---

## 2. 调用契约：传 `kb_id`，不传 `user_id`

```
✅ POST /v1/kbs/abc123/query        {"query": "..."}
❌ POST /v1/query                    {"user_id": "...", "kb_id": "...", "query": "..."}
```

为什么不传 user_id：

| 角色 | 该不该知道 user？ |
|---|---|
| 你的 agent 系统 | ✅ 必须知道 |
| RAG 服务 | ❌ 不需要也不应该 |

理由：
1. **职责单一**：RAG 服务只管"对这个 KB 做 RAG"，user 是谁、有没有权限、要不要限流，全是 agent 系统的事
2. **授权链清晰**：user → agent 系统验权 → 决定能访问哪些 kb_id → 用 kb_id 调 RAG。RAG 服务是被信任的下游，不重复鉴权
3. **未来灵活性**：以后做"团队共享 KB"、"系统预置 KB"、"组织级 KB"，KB 的 owner 模型从 user 变成 team/org 时，RAG 服务不用改一行
4. **测试简单**：RAG 服务的集成测试不需要 mock 用户系统

例外：可以透传**非身份**的可选参数（行为参数），比如 `top_k`、`mode`、`language`、`max_tokens`、`trace_id` —— RAG 服务把它当配置用，不当身份用，依然保持"不知道 user"。

---

## 3. 三层架构（职责分层）

```
┌─────────────────────────────────────────────────┐
│  你的 Agent 系统（已有）                           │
│  • 用户/认证/会话/计费/审计                          │
│  • KB 注册表：kb_id → owner_id, name, tags, ... │
│  • 业务 agent 编排                                │
└──────────────────┬──────────────────────────────┘
                   │  HTTP（内网或 mTLS）
                   │  携带 kb_id（无 session）
                   ▼
┌─────────────────────────────────────────────────┐
│  RAG 服务（要新建）                                │
│  • 不知道用户、不做用户级 auth                       │
│  • 收到 (kb_id, payload) 即工作                  │
│  • 内部 LRU 缓存：kb_id → RAGAnything 实例         │
│  • 异步任务：长摄入 → job_id 轮询                   │
│  • 同 kb_id 摄入排队（pipeline lock）             │
└──────────────────┬──────────────────────────────┘
                   │
        ┌──────────┴──────────┐
        ▼                     ▼
   [mineru.net]        [存储后端]
  云解析 PDF/docx     • MVP: 文件系统（每 KB 独立目录）
                     • Prod: Postgres + pgvector + AGE
                     [LLM/Embedding endpoints]
```

**为什么是这个分层：**
- RAG 服务不持有用户态 → 横向扩容只看 KB 分布，不看用户数
- 用户系统不内嵌 RAG 逻辑 → 换 RAG 实现 / 升级 / A/B 测试不影响业务
- 调用方不感知 KB 实例缓存命中与否 → 客户端代码极简

---

## 4. 四层操作模型

### Level 1: KB 容器本身

| Method | Path | 说明 |
|---|---|---|
| POST | `/v1/kbs/{kb_id}` | 建 KB（幂等） |
| GET | `/v1/kbs/{kb_id}` | 查 KB 状态/统计 |
| DELETE | `/v1/kbs/{kb_id}` | 删 KB（finalize 实例 + 清存储） |

**❌ 没有 `GET /v1/kbs`（列出所有 KB）** — RAG 服务不知道"我"是谁。这个查询在你 agent 系统的 DB 做：
```sql
SELECT kb_id, name, created_at FROM knowledge_bases WHERE owner_id = ?
```

### Level 2: 文档

| Method | Path | 说明 |
|---|---|---|
| POST | `/v1/kbs/{kb_id}/documents` | **异步**摄入 → 返回 `job_id` |
| GET | `/v1/kbs/{kb_id}/jobs/{job_id}` | 查摄入状态/进度 |
| GET | `/v1/kbs/{kb_id}/documents` | 列文档 |
| DELETE | `/v1/kbs/{kb_id}/documents/{doc_id}` | 删单文档（含图谱关联数据） |

**没有"改文档"** — RAG 没"改"，就是删掉重加。

摄入必须异步：mineru 解析 + 实体抽取 = 5–15 分钟，HTTP 30s timeout 撑不住。

### Level 3: 实体/关系（可选）

| Method | Path | 说明 |
|---|---|---|
| POST | `/v1/kbs/{kb_id}/entities` | 增实体 |
| PATCH | `/v1/kbs/{kb_id}/entities/{name}` | 改实体 |
| DELETE | `/v1/kbs/{kb_id}/entities/{name}` | 删实体 |
| 同上 | `/v1/kbs/{kb_id}/relations/...` | 增删改关系 |

LightRAG 都有原生方法 (`adelete_by_entity` 等)，要不要暴露看产品需要。MVP 可以不开。

### Level 4: 查询（最常用）

| Method | Path | 说明 |
|---|---|---|
| POST | `/v1/kbs/{kb_id}/query` | 文本查询（hybrid/local/global/naive/mix） |
| POST | `/v1/kbs/{kb_id}/query/stream` | SSE 流式 |
| POST | `/v1/kbs/{kb_id}/query/multimodal` | 带图/表/公式输入的查询 |

查询是同步的，hybrid 模式 ~5–30s 出答案。

### 其它

| Method | Path | 说明 |
|---|---|---|
| POST | `/v1/kbs/{kb_id}/cache/clear` | 清 LLM 缓存 |
| GET | `/healthz` | 健康检查 + 缓存命中率 |

---

## 5. 数据所有权对照表

| 数据 | 存在哪 | 例子 |
|---|---|---|
| user 表、登录、密码 | **你的 DB** | `users` |
| KB 元数据（name、owner、tags、icon、description） | **你的 DB** | `knowledge_bases` |
| 用户 quota、计费、审计日志 | **你的 DB** | `usage_logs`, `quotas` |
| KB 内部知识图谱、向量、文档原文 | **RAG 服务管理的存储** | `working_dir/{kb_id}/` 或 Postgres workspace |
| 摄入作业进度（job 表） | RAG 服务 | `jobs` 表（SQLite/Postgres） |
| mineru.net token | **你的 DB**（按用户计费）或 RAG 服务（全平台共享） | — |

**心智模型**：
- 你的 agent 系统 = "认识用户 + 判断用户能干啥 + 业务老板"
- RAG 服务 = "面向 kb_id 的纯计算工人，干活不问情由"

---

## 6. 调用方代码模板

你 agent 系统里（伪代码）：

```python
# === 用户态操作（只在你的 DB 里）===
def create_kb_for_user(user_id, name) -> str:
    kb_id = uuid4()
    db.kbs.insert(kb_id=kb_id, owner_id=user_id, name=name)
    rag_client.post(f"/v1/kbs/{kb_id}")          # 通知 RAG 服务建容器
    return kb_id

def list_my_kbs(user_id):
    return db.kbs.where(owner_id=user_id)         # ❌ 不调 RAG 服务

def rename_kb(user_id, kb_id, new_name):
    assert_owner(user_id, kb_id)
    db.kbs.update(kb_id=kb_id, name=new_name)     # ❌ 不调 RAG 服务

def delete_kb(user_id, kb_id):
    assert_owner(user_id, kb_id)
    db.kbs.delete(kb_id)                          # 删你的元数据
    rag_client.delete(f"/v1/kbs/{kb_id}")         # 让 RAG 服务删存储

# === 走 RAG 服务的操作 ===
def add_document(user_id, kb_id, file):
    assert_owner(user_id, kb_id)
    return rag_client.post(f"/v1/kbs/{kb_id}/documents", files={...})

def query(user_id, kb_id, q):
    assert_owner(user_id, kb_id)
    quota.check_and_consume(user_id, "query")     # 你的计费
    return rag_client.post(f"/v1/kbs/{kb_id}/query", json={"query": q})
```

调用流程：

```
用户操作
  │
  ▼
[你的 agent 系统]
  ├─ 验 user JWT → 拿到 user_id
  ├─ 查 DB: 这个 user 能访问哪些 kb_id？
  ├─ 选定 kb_id（或拒绝）
  ├─ （可选）记审计日志、扣额度
  └─ 调 RAG 服务 ──────► POST /v1/kbs/{kb_id}/query
                          (header: X-Internal-Bearer: xxx)
                          ▼
                    [RAG 服务]
                    LRU 找 kb_id 的实例 → 查询 → 返回
```

---

## 7. 异步摄入设计

```
POST /documents     →  202 {"job_id": "j-xxx"}
                          │
                          ▼
                    asyncio.Task on KBRouter:
                    1. 上传文件到 jobs/{job_id}/input.pdf
                    2. mineru.net 解析（1-2 min）
                    3. rag.insert_content_list(...)（5-10 min）
                    4. 写 jobs 表: status=done / failed, error?

GET /jobs/{job_id}  →  {"status": "running"|"done"|"failed",
                        "progress": "parsing"|"extracting:12/30",
                        "doc_id": "...", "error": "..."}
```

可选：webhook callback —— 在 POST 时传 `callback_url`，摄入完成后 RAG 服务 POST 给 agent 系统，省去轮询。

---

## 8. 关键约束（设计前必须知道）

源自 RAG-Anything / LightRAG 源码 review：

| 约束 | 影响 |
|---|---|
| `RAGAnything` 实例化便宜（lazy init），但首次调用会全量加载 NanoVectorDB / 图文件到内存 | 不能每请求都新建实例 → 进程内 LRU 缓存 |
| 每个实例持有 LightRAG + 模态处理器 + LLM/embedding 异步 worker 池 | 实例必须 `finalize_storages()` 才能干净关闭，LRU 淘汰要 await close |
| 多租户隔离机制：LightRAG 的 `workspace` 字段 | 文件后端→子目录前缀；DB 后端→`WHERE workspace=?` 行级过滤 |
| 同一 workspace 的 ingest 是互斥的（pipeline status lock） | 同 KB 不能并发摄入；查询不受影响 |
| 6 类存储后端可插拔 | MVP 用 JSON 文件，规模化切 Postgres+pgvector+AGE |
| LightRAG 没原生"删 KB"方法，但有 `adelete_by_doc_id`/`aget_docs_by_status`/`aclear_cache` | 删 KB = 删 working_dir 目录 / 清 workspace 数据；其余原生支持 |
| 官方 `lightrag-server` 是单 LightRAG 实例 / 单进程 | 不能直接复用做多租户，要自己写 router 包多实例 |

---

## 9. 横向扩容路径

| 阶段 | 规模 | 架构 |
|---|---|---|
| 1 | < 500 KBs | 单实例 + 文件系统 + LRU 32 |
| 2 | 多实例 + 文件系统 | 按 `hash(kb_id)` 在 nginx/envoy 做 sticky 路由，每 KB 只活在一个 pod 上 |
| 3 | 真正分布式 | 所有 pod 共享 Postgres+pgvector+AGE，workspace 字段做隔离，任意 pod 处理任意请求 |

文件系统方案撞墙的信号：
- 单机 KB 数 > 5k（`ls` 都慢）
- 需要 KB 之间共享实体
- 想要跨实例 HA（多副本同时读写）

---

## 10. 关键决策点（实现前要定）

| 决策 | 推荐 |
|---|---|
| 存储后端 | MVP 文件系统每 KB 独立目录；KB > 500 切 Postgres+pgvector+AGE |
| 异步任务驱动 | MVP FastAPI BackgroundTasks；规模化切 ARQ（轻量 asyncio） |
| 鉴权 | shared bearer 起步；公网暴露切 mTLS |
| 删 KB 时机 | 软删除 + 24h 撤回窗口（防误删） |
| 多模态查询暴露 | 先纯文本，后加多模态 |
| Qwen3 thinking 控制 | 默认关；query 参数 `enable_thinking=false` 可覆盖 |
| mineru.net token | 全平台共享 vs 用户自带 → 看你计费策略 |

---

## 11. 接入清单（agent 系统侧）

需要在你自己 DB 里新加的表：

```sql
CREATE TABLE knowledge_bases (
    kb_id      uuid PRIMARY KEY,           -- RAG 服务认这个 ID
    owner_id   uuid NOT NULL,              -- 你的 user FK
    name       text NOT NULL,              -- 用户可见的"AI 文件夹"名
    description text,
    icon       text,
    tags       text[],
    created_at timestamptz DEFAULT now(),
    deleted_at timestamptz,                -- 软删除
    doc_count  int DEFAULT 0,              -- 缓存值，定时同步 RAG 服务
    bytes_used bigint DEFAULT 0,           -- 计费/限额
    settings   jsonb                       -- 用户偏好（默认查询 mode 等）
);
CREATE INDEX ON knowledge_bases(owner_id) WHERE deleted_at IS NULL;
```

需要在 agent 系统加的 service 层：
- `RagClient`：HTTP 客户端，封装对 RAG 服务的调用，自动带内部 bearer
- `KBService`：business logic 层，做权限检查、调 `RagClient`、维护元数据表
- `IngestJobWatcher`：（可选）定时器或 webhook handler，更新 `doc_count` 等缓存值
