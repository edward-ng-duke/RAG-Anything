# RAG-Anything ↔ ONYX 集成执行指南 (v1)

> 本文件是 ONYX 团队在 ONYX 仓库内实施 RAG-Anything 集成的完整指南。读者：ONYX 后端工程师、前端工程师、运维。
>
> 配套：RAG-Anything 一侧会开通 `/v1/onyx/*` 一组接口（OpenAPI spec 由 RAG-Anything 维护方在 `docs/onyx-integration/openapi.yaml` 落地，并在变更时同步给 ONYX）。
>
> 版本：v1（2026-05）。
> 反馈：rag-anything 维护组。

---

## 1. 背景与目标

ONYX 想在自家 UI 里加一个「Knowledge Graph QA」边栏，让用户：
- 选/建 KB（知识库）
- 上传文档
- 在 KB 内做 KG-增强多轮问答（含引用卡片）
- 浏览 KB 内的实体/关系/子图

后台用 RAG-Anything（独立产品）。RAG-Anything 不动 ONYX 的核心，只对外提供一组服务间接口；ONYX 这边只动**前端** + **一组后端代理路由** + **一张本地表**。

不在 v1 范围：ONYX 既有的 connector（Slack/Confluence/...）拉来的文档暂不自动流入 RAG-Anything。

---

## 2. 架构

```
┌────────── ONYX (产品门面) ────────────┐
│  Browser (onyx Web UI)                 │
│    │ /api/onyx/rag/...   (cookie auth) │
│    │ + 浏览器 EventSource for SSE      │
│    ▼                                   │
│  ONYX FastAPI Backend                  │
│    │ Authorization: Bearer <RAG_TOKEN> │
│    │ X-Onyx-User-Id: <user_uuid>       │
│    │ X-Onyx-KB-Id:   <kb_uuid>         │
│    │ X-Request-Id:   <uuid>            │
│    ▼                                   │
└────────────────┬───────────────────────┘
                 │ REST / SSE
                 ▼
┌────── RAG-Anything 后端 ──────────────┐
│ POST /v1/onyx/kb            (KB CRUD) │
│ POST /v1/onyx/documents     (上传)     │
│ POST /v1/onyx/query         (SSE)      │
│ GET  /v1/onyx/kg/*          (KG 浏览)  │
│ ...                                    │
└────────────────────────────────────────┘
```

关键约束：
- 浏览器**不直连** RAG-Anything；所有调用走 ONYX 后端代理。
- `INTERNAL_TOKEN`（96 字符服务级密钥）**永远不进浏览器**。
- ONYX 在 RAG 里**没有 user 概念**——`X-Onyx-User-Id` 是个不透明字符串，仅用于 RAG 一侧的审计/限流。

---

## 3. 认证与租户模型

### 3.1 三段身份

每一个 ONYX→RAG 请求都要带：

| 头部 | 说明 |
|---|---|
| `Authorization: Bearer <INTERNAL_TOKEN>` | 服务级密钥，96 字符。在 ONYX 后端 env 中，绝不入浏览器/git/日志。 |
| `X-Onyx-User-Id` | onyx 用户 uuid（≤128 字符）；用于审计 + 限流分桶。除 `GET /v1/onyx/kb`（列表）外都必传。 |
| `X-Onyx-KB-Id` | onyx 知识库 id（≤64 字符）= RAG `tenant_id`。除 KB 创建/列表外都必传。 |
| `X-Request-Id` | UUID 或自定义；建议每次都带，便于跨服务追踪。 |

### 3.2 租户映射

**RAG `tenant_id` ≡ ONYX KB `id`**。

| 概念 | 在 ONYX 里 | 在 RAG 里 |
|---|---|---|
| 用户 | `onyx_user` 表行 | 不存在（仅 audit 字段透传） |
| 工作区 | `onyx_workspace` | 不存在（仅 audit 字段透传） |
| 知识库 | `onyx_kb` 表行 | `tenants` 表行（tenant_id 形如 `onyx-<uuid>`，config_json.source='onyx'） |
| ACL（谁能访问哪个 KB）| `onyx_kb_member` 表 | **不存在；ONYX 全权管理** |
| 文档/实体/关系/chunk | 不存（onyx 不持有） | `documents` + `lightrag_*` 表，按 `tenant_id`/`workspace` 隔离 |

### 3.3 KB 生命周期

```
ONYX 用户 alice 在 UI 点「新建 KB」
   ↓ ONYX backend → RAG: POST /v1/onyx/kb {display_name:..., onyx_workspace_id, onyx_owner_user_id}
   ← RAG: 201 {kb_id:"onyx-3f9b...", ...}
ONYX backend 把 kb_id 写入 onyx_kb.rag_tenant_id 列。
此后 alice 上传/查询都带 X-Onyx-KB-Id: onyx-3f9b...

ONYX 用户 alice 删 KB
   ↓ ONYX backend 软删 onyx_kb (deleted_at = now)
   ↓ ONYX backend → RAG: DELETE /v1/onyx/kb/onyx-3f9b...
   ← RAG: 204 (级联删除 documents/jobs/lightrag_* 行/磁盘文件)
   ↓ ONYX backend 硬删 onyx_kb 行
```

---

## 4. ONYX 后端工作清单

### 4.1 新建 ONYX 表

```sql
CREATE TABLE onyx_kb (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  rag_tenant_id   TEXT UNIQUE NOT NULL,           -- RAG 返回的 tenant_id
  display_name    TEXT NOT NULL,
  description     TEXT,
  owner_user_id   UUID REFERENCES onyx_user(id),
  workspace_id    UUID REFERENCES onyx_workspace(id),
  visibility      TEXT NOT NULL DEFAULT 'private',  -- private|workspace|public
  created_at      TIMESTAMPTZ DEFAULT now(),
  updated_at      TIMESTAMPTZ DEFAULT now(),
  deleted_at      TIMESTAMPTZ NULL
);

CREATE TABLE onyx_kb_member (
  kb_id     UUID REFERENCES onyx_kb(id) ON DELETE CASCADE,
  user_id   UUID REFERENCES onyx_user(id) ON DELETE CASCADE,
  role      TEXT NOT NULL DEFAULT 'reader',   -- owner|editor|reader
  granted_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (kb_id, user_id)
);

CREATE INDEX onyx_kb_workspace_idx ON onyx_kb(workspace_id) WHERE deleted_at IS NULL;
CREATE INDEX onyx_kb_owner_idx ON onyx_kb(owner_user_id) WHERE deleted_at IS NULL;
```

### 4.2 配置项

ONYX backend `.env` 增量：

```ini
RAG_ANYTHING_BASE_URL=https://rag.internal.example.com
RAG_ANYTHING_TOKEN=<96 字符>            # 由 RAG 运维提供，绝不入 git
RAG_ANYTHING_TIMEOUT_SEC=120            # 默认请求超时
RAG_ANYTHING_SSE_TIMEOUT_SEC=600        # SSE 长连接
RAG_ANYTHING_MAX_RETRIES=2              # 5xx/429 自动重试上限
```

启动时调一次 `GET <base>/healthz`（不带 token）确认 RAG 通；失败不阻断启动但打告警。

### 4.3 后端代理路由设计

在 ONYX FastAPI 路由树下加 `/api/onyx/rag/*`：

| ONYX 路径 | 转发到 RAG | 备注 |
|---|---|---|
| `POST /api/onyx/rag/kb` | `POST /v1/onyx/kb` | 创建 KB；ONYX 同时插 onyx_kb 行 |
| `GET /api/onyx/rag/kb` | （**不**直接转发；查 onyx 自己的 onyx_kb，仅返回该用户能看到的）| ACL 在 onyx 完成 |
| `GET /api/onyx/rag/kb/{kb_id}` | `GET /v1/onyx/kb/{kb_id}` | 先查 onyx_kb_member 校验该用户对此 KB 有读权限 |
| `DELETE /api/onyx/rag/kb/{kb_id}` | `DELETE /v1/onyx/kb/{kb_id}` | 仅 owner 角色可删；onyx 这边软删 onyx_kb 后调 RAG，再硬删 onyx_kb |
| `POST /api/onyx/rag/documents` | `POST /v1/onyx/documents` | 多 part 转发文件；先查 onyx_kb_member.role ∈ {owner, editor} |
| `GET /api/onyx/rag/documents` | `GET /v1/onyx/documents` | reader+ |
| `GET /api/onyx/rag/documents/{id}` | `GET /v1/onyx/documents/{id}` | reader+ |
| `DELETE /api/onyx/rag/documents/{id}` | `DELETE /v1/onyx/documents/{id}` | editor+ |
| `GET /api/onyx/rag/jobs/{id}` | `GET /v1/onyx/jobs/{id}` | reader+ |
| `POST /api/onyx/rag/query` | `POST /v1/onyx/query` (SSE) | reader+；流式转发 |
| `POST /api/onyx/rag/query/sync` | `POST /v1/onyx/query/sync` | reader+ |
| `GET /api/onyx/rag/kg/*` | `GET /v1/onyx/kg/*` | reader+ |

**SSE 转发要点**：
- 用 `httpx.AsyncClient` 的 `stream()` 调 RAG，按行转发到 ONYX 客户端响应。
- 不缓冲（FastAPI `StreamingResponse(media_type="text/event-stream")`）。
- 反代（如果 ONYX 前面还有 nginx/envoy）配置：
  ```
  proxy_buffering off;
  proxy_read_timeout 600s;
  chunked_transfer_encoding on;
  add_header Cache-Control no-cache;
  ```
- 每 15s 透传 RAG 发来的 `: keepalive\n\n` 注释行。

**ACL 中间件**：在每个代理路由头部加一个 dependency：

```python
async def require_kb_access(kb_id: UUID, user: OnyxUser, role: str = "reader"):
    """raises 403 if user has no access at the requested role"""
    membership = await db.fetch_one(
        "SELECT role FROM onyx_kb_member WHERE kb_id=:kb AND user_id=:u",
        {"kb": kb_id, "u": user.id},
    )
    if not membership:
        raise HTTPException(404, "kb not found")  # 不暴露存在性
    if not _role_allows(membership["role"], role):
        raise HTTPException(403, "insufficient role")
    # 把 onyx kb_id (UUID) 翻译成 RAG tenant_id (字符串)
    kb_row = await db.fetch_one(
        "SELECT rag_tenant_id FROM onyx_kb WHERE id=:k AND deleted_at IS NULL",
        {"k": kb_id},
    )
    return kb_row["rag_tenant_id"]
```

### 4.4 KB 生命周期同步

```python
# 创建
async def create_kb(payload, user):
    rag_resp = await rag_client.post("/v1/onyx/kb", json={
        "display_name": payload.display_name,
        "onyx_workspace_id": str(payload.workspace_id) if payload.workspace_id else None,
        "onyx_owner_user_id": str(user.id),
        "storage_quota_mb": payload.storage_quota_mb or 1024,
    }, headers={"X-Onyx-User-Id": str(user.id)})
    rag_resp.raise_for_status()
    rag_tenant_id = rag_resp.json()["kb_id"]

    # ONYX 本地落地
    kb = await db.execute(
        "INSERT INTO onyx_kb (rag_tenant_id, display_name, owner_user_id, workspace_id, visibility) "
        "VALUES (:rt, :dn, :ou, :ws, :v) RETURNING *",
        {...},
    )
    await db.execute(
        "INSERT INTO onyx_kb_member (kb_id, user_id, role) VALUES (:k, :u, 'owner')",
        {"k": kb.id, "u": user.id},
    )
    return kb

# 删除（建议幂等）
async def delete_kb(kb_id, user):
    kb = await get_owned_kb(kb_id, user)
    await db.execute("UPDATE onyx_kb SET deleted_at=now() WHERE id=:k", {"k": kb_id})
    try:
        await rag_client.delete(f"/v1/onyx/kb/{kb.rag_tenant_id}",
            headers={"X-Onyx-User-Id": str(user.id)},
            timeout=60,
        )
    except (RAGUpstreamError, httpx.TimeoutException) as e:
        # 标记为「待重试删除」，后台 retry job 兜底；deleted_at 已置，用户视角已删
        await db.execute("UPDATE onyx_kb SET delete_retry_count=delete_retry_count+1 WHERE id=:k",
                         {"k": kb_id})
        log.warning("rag_kb_delete_failed", kb_id=kb_id, error=str(e))
        return  # 用户得到 200；后台 worker 后续重试 RAG 删除
    await db.execute("DELETE FROM onyx_kb WHERE id=:k", {"k": kb_id})
```

### 4.5 错误转译

把 RAG 的 `error_code` 映射到 ONYX 错误体系：

| RAG `error_code` | ONYX 行为 |
|---|---|
| `invalid_token` / `ip_not_allowed` | 配置错误，500 + 告警，不暴露 detail 给用户 |
| `kb_not_found` | 404，提示「知识库不存在或您没有权限」 |
| `quota_exceeded` | 413，提示「KB 已达存储上限，请删除旧文档或申请扩容」 |
| `unsupported_media_type` | 415，提示「不支持的文件类型」 |
| `rate_limited` | 429，按 `Retry-After` 暂停按钮 |
| `upstream_*_error` | 502，提示「检索服务暂时不可用，请稍后再试」+ 自动重试（max 2） |
| 其他 5xx | 500，自动重试 |

### 4.6 重试与超时策略

| 端点 | 超时 | 自动重试 |
|---|---|---|
| `/query` (SSE) | SSE 连接 600s，无心跳 30s 超时断开 | 不自动重试（用户体验差，让用户手动 retry） |
| `/query/sync` | 120s | 5xx 重试 1 次，间隔 2s |
| `/documents` POST | 60s（文件流） | 不自动重试（避免重复入队，且 RAG 自带 dedup） |
| `/documents` 列表/详情/删除 | 10s | 5xx 重试 2 次 |
| `/jobs/{id}` | 5s | 5xx 重试 2 次 |
| `/kb/*` | 30s | 5xx 重试 1 次 |
| `/kg/*` | 10s | 5xx 重试 2 次 |

### 4.7 观测

ONYX backend metrics：
- `onyx_rag_proxy_requests_total{path, status}`
- `onyx_rag_proxy_latency_seconds{path}`
- `onyx_rag_proxy_errors_total{path, error_code}`
- `onyx_rag_proxy_sse_dropped_total`（连接异常断开）

日志带：`request_id`（透传 RAG 那边的 X-Request-Id 响应头）、`onyx_user_id`、`rag_tenant_id`、`path`。

### 4.8 安全清单

- [ ] `RAG_ANYTHING_TOKEN` 仅在 ONYX 后端 env + 内存，不入 git/日志/浏览器/前端 bundle
- [ ] 浏览器调代理路由必须已登录（onyx 既有 session 中间件）
- [ ] 调代理前一定校验 `onyx_kb_member`（reader/editor/owner 三档）
- [ ] CSRF：按 onyx 既有策略（cookie + double-submit / SameSite Strict）
- [ ] 代理路径**白名单**——只接受本文件 §4.3 表格里的具体路径，禁止任意 `/v1/onyx/*` 透传，防 SSRF。
- [ ] 代理转发时**不要**把 onyx 用户 cookie 带到 RAG（`httpx.AsyncClient` 默认不带，确认一下）
- [ ] RAG 错误响应 `detail` 含敏感信息时，ONYX 这边过滤后再展示给用户

---

## 5. ONYX 前端工作清单

### 5.1 路由

```
/sidebar/rag                    KB 选择器入口（侧边栏入口）
/sidebar/rag/{kb_id}            进入特定 KB（默认显示 chat tab）
/sidebar/rag/{kb_id}?tab=docs   文档管理 tab
/sidebar/rag/{kb_id}?tab=kg     知识图谱 tab
/sidebar/rag/new                新建 KB 流程
```

### 5.2 关键页面

#### 5.2.1 KB 选择器（首屏）

- 卡片列表展示用户能访问的 KB（来自 `GET /api/onyx/rag/kb`）
- 每张卡片：display_name、文档数、最后活跃时间、所有者头像
- 顶部「+ 新建 KB」按钮
- 空态：引导新建

#### 5.2.2 Chat tab（核心，最大工作量）

```
┌── KB: Engineering ────────────── [Settings] ─┐
│ ┌─────────────────────────────────────────┐ │
│ │ 历史会话侧栏 (本地存或 onyx 现有 chat)   │ │
│ ├─────────────────────────────────────────┤ │
│ │  alice: how do I deploy fooservice?     │ │
│ │  ai:    Based on docs, fooservice...    │ │
│ │         [engineering.pdf · p.12]        │ │
│ │         [deploy-guide.md · §3]          │ │
│ │  alice: what about staging?             │ │
│ │  ai:    For staging, you need to...     │ │
│ ├─────────────────────────────────────────┤ │
│ │ [输入框                            发送]│ │
│ │  [模式: hybrid▾] [VLM增强 ☐]            │ │
│ └─────────────────────────────────────────┘ │
└─────────────────────────────────────────────┘
```

技术要点：

- **流式渲染**：用 `fetch` + `ReadableStream` reader 比 `EventSource` 灵活（可带 cookie auth + custom headers）：

  ```ts
  const resp = await fetch("/api/onyx/rag/query", {
    method: "POST",
    headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
    body: JSON.stringify({ question, history, mode, top_k }),
    credentials: "include",
  });
  const reader = resp.body!.getReader();
  // 解析 SSE 格式（按 \n\n 分块，逐 event 处理）
  ```

- **history 拼接**：从 onyx chat 历史里取最近 N 轮（建议 5），转成 `[{role:"user"|"assistant", content:string}]`，作为 body.history 透传。
- **引用卡片**：渲染 `done` 事件 sources 数组，每张卡片显示 `file_name` + page + snippet 摘要；点击跳到 Documents tab 对应文档详情。
- **错误事件**：收到 `event: error` 后停止追加 chunk，显示错误条 + 「重试」按钮。
- **取消**：用户点「停止」取消 fetch（abort controller）；ONYX 后端代理收到 client disconnect 关闭 RAG 流。

#### 5.2.3 Documents tab

- 拖拽区（多文件，单文件 1GB 上限）
- 表格：file_name / size / status (badge: pending/processing/indexed/failed) / uploaded_at / actions
- 上传后：调 `POST /api/onyx/rag/documents`，返回 `job_id` → 后台轮询 `GET /api/onyx/rag/jobs/{id}` 每 5s 直到 status=`done` 或 `failed`
- 删除：二次确认对话框，文案：「删除将触发知识图谱重建，可能持续数分钟到几十分钟」
- 单文档详情页：file_name / metadata / chunks 列表 / KG 中关联实体（调 `/v1/onyx/kg/entities?search=...`，可选）

#### 5.2.4 Knowledge Graph tab

```
┌── 实体列表 ──────────┬── 详情面板 ────────────┐
│ Type ▾  搜索 ___    │  FooService            │
│ ─────────           │  Type: service         │
│ FooService    87    │  Description: ...      │
│ PostgreSQL    42    │  ─────────             │
│ Alice (User)  21    │  ┌─ 邻居子图 ─┐        │
│ ...                 │  │  (sigma.js)  │        │
│                     │  │  深度 [1▾]   │        │
│                     │  └──────────────┘        │
│                     │  关联文档：              │
│                     │   • engineering.pdf      │
└─────────────────────┴───────────────────────┘
```

- 实体表：`GET /api/onyx/rag/kg/entities?type=&search=&cursor=&limit=50`，懒加载分页
- 类型过滤来自 `GET /api/onyx/rag/kg/stats`
- 选中实体 → 右侧调 `GET /api/onyx/rag/kg/entities/{id}` + `/neighbors?depth=1`
- 子图渲染：sigma.js + graphology；节点拖拽展开（点击节点调 `/neighbors?depth=1` 增量加载邻居）
- 节点 > 200 时启用 WebGL 渲染器；> 1000 时降级到 force-atlas2 静态布局

#### 5.2.5 状态管理

- 推荐 React Query / SWR：每个端点一个 hook（`useKbList`、`useKbDocuments`、`useEntity` 等）
- 全局 store（zustand）保存：当前选中 kb_id、当前用户对该 KB 的 role
- KB 切换时清缓存（防数据串）

### 5.3 性能要求

- KB 列表首屏 < 500ms
- chat 第一个 chunk 出现 < 2s（取决于 LLM 上游，无法保证；提供加载指示）
- 文档列表（< 500 行）< 800ms
- KG 实体表分页加载 < 1s
- 子图渲染 100 节点 < 1s 流畅交互

### 5.4 引用跳转

chat 引用卡片点击 → 跳到 `/sidebar/rag/{kb_id}?tab=docs&document=<doc_id>&chunk=<chunk_id>`，文档详情页打开后高亮该 chunk（PDF preview v2 再做）。

---

## 6. RAG-Anything API 端点参考

> ONYX 调用的就是这些。完整 OpenAPI spec 见 `docs/onyx-integration/openapi.yaml`（由 RAG 维护方提供）。

### 6.1 共同请求头规范

所有端点：

- `Authorization: Bearer <INTERNAL_TOKEN>` 必传
- `X-Request-Id` 强烈建议
- `Content-Type: application/json`（除 multipart 上传外）
- 响应头：`X-Request-Id`、`X-RAG-Version`、`X-RAG-API-Version`

### 6.2 KB 生命周期

| 方法 | 路径 | 必传头 | 说明 |
|---|---|---|---|
| POST | `/v1/onyx/kb` | Auth, X-Onyx-User-Id | 创建 KB；body 见 §6.2.1 |
| GET | `/v1/onyx/kb` | Auth, X-Onyx-User-Id | 列出本 RAG 实例上 source=onyx 的所有 KB；query: `cursor`, `limit`, `onyx_workspace_id?`, `onyx_owner_user_id?` |
| GET | `/v1/onyx/kb/{kb_id}` | Auth, X-Onyx-User-Id, X-Onyx-KB-Id | 单 KB 详情 |
| DELETE | `/v1/onyx/kb/{kb_id}` | Auth, X-Onyx-User-Id, X-Onyx-KB-Id | 硬删 + 级联 |

#### 6.2.1 创建 KB

Request:

```json
{
  "display_name": "Engineering Docs",
  "onyx_workspace_id": "ws_xxx",
  "onyx_owner_user_id": "u_alice",
  "storage_quota_mb": 2048
}
```

字段约束：
- `display_name` 必传，1-200 字符
- `onyx_workspace_id` 可选，≤64 字符
- `onyx_owner_user_id` 可选，≤128 字符
- `storage_quota_mb` 可选，默认 1024，min 64，max 102400

Response 201:

```json
{
  "kb_id": "onyx-3f9b2d8a-1234-5678-9abc-def012345678",
  "display_name": "Engineering Docs",
  "storage_quota_mb": 2048,
  "storage_used_mb": 0,
  "document_count": 0,
  "created_at": "2026-05-04T10:00:00Z"
}
```

错误：409 `kb_already_exists`（同 owner_user_id + display_name 已存在；可幂等）

### 6.3 文档管理

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/onyx/documents` | multipart 上传，202 + `{document_id, job_id, status, deduplicated}` |
| GET | `/v1/onyx/documents` | 列表；query: `cursor`, `limit`, `status?` |
| GET | `/v1/onyx/documents/{document_id}` | 详情 |
| DELETE | `/v1/onyx/documents/{document_id}` | 软删 + 入队 reindex（耗时） |
| GET | `/v1/onyx/jobs/{job_id}` | 作业进度 |

#### 6.3.1 上传

```
POST /v1/onyx/documents
Content-Type: multipart/form-data
Authorization: Bearer ...
X-Onyx-User-Id: ...
X-Onyx-KB-Id: onyx-3f9b...

[file=...]                         # 必传
[display_name=自定义名]             # 可选 form field
```

Response 202:

```json
{
  "document_id": "doc_abc123",
  "job_id": "job_xyz789",
  "status": "queued",
  "deduplicated": false,
  "file_name": "engineering-handbook.pdf",
  "file_size": 12345678,
  "content_hash": "sha256:..."
}
```

支持的 MIME：`application/pdf`、`image/png`、`image/jpeg`、`application/msword`、`application/vnd.openxmlformats-officedocument.wordprocessingml.document`、`text/plain`、`text/markdown`。其他返回 415。

#### 6.3.2 作业

`progress.stage` 取值序列：

```
uploaded → parsing → embedding → entity_extraction → graph_writing → done
                                                                       ↓
                                                                    failed
```

`status`：`queued | running | done | failed`

### 6.4 问答（无状态）

#### 6.4.1 SSE 流式

```
POST /v1/onyx/query
Content-Type: application/json
Authorization: Bearer ...
X-Onyx-User-Id: ...
X-Onyx-KB-Id: ...
Accept: text/event-stream
```

Body:

```json
{
  "question": "How do I deploy fooservice?",
  "history": [
    { "role": "user",      "content": "..." },
    { "role": "assistant", "content": "..." }
  ],
  "mode": "hybrid",
  "top_k": 10,
  "vlm_enhanced": false,
  "include_sources": true,
  "max_history_turns": 5
}
```

字段约束：
- `question` 必传，1-4000 字符
- `history` 可选，最多 50 条
- `mode` 默认 `hybrid`，可选 `local|global|naive|mix`
- `top_k` 默认 10，范围 1-50
- `vlm_enhanced` 默认 false（启用图文检索；需 RAG 部署时配置 `VLM_MODEL`）
- `include_sources` 默认 true（done 事件是否带 sources）
- `max_history_turns` 默认 5，范围 0-20（RAG 二次截断防超长 prompt）

SSE 事件序列：

```
event: meta
data: {"request_id":"req_abc","kb_id":"onyx-...","mode":"hybrid","top_k":10}

event: chunk
data: {"text":"Based"}

event: chunk
data: {"text":" on"}

...

event: done
data: {
  "answer":"Based on the provided context, fooservice deploys...",
  "sources":[
    {
      "document_id":"doc_abc",
      "file_name":"engineering-handbook.pdf",
      "chunk_id":"chunk_8c2f",
      "score":0.87,
      "snippet":"To deploy fooservice to staging...",
      "modality":"text",
      "page":12,
      "bbox":null
    }
  ],
  "latency_ms":2341,
  "tokens":{"prompt":2105,"completion":287,"total":2392},
  "warnings":[]
}
```

错误事件（流中）：

```
event: error
data: {"code":"upstream_llm_error","message":"...","retryable":true}
```

之后流终止；HTTP 状态仍是 200。

心跳：每 15s `: keepalive\n\n` 注释行。

#### 6.4.2 Sync 降级

```
POST /v1/onyx/query/sync
```

Body 同上。Response 200 普通 JSON：

```json
{
  "request_id":"req_abc",
  "answer":"...",
  "sources":[...],
  "latency_ms":2341,
  "tokens":{...}
}
```

### 6.5 知识图谱

镜像 α 既有 `/v1/kg/*` 7 个端点：

| 方法 | 路径 | Query 参数 |
|---|---|---|
| GET | `/v1/onyx/kg/entities` | `type?`, `search?`, `cursor?`, `limit` (1-200) |
| GET | `/v1/onyx/kg/entities/{entity_id}` | — |
| GET | `/v1/onyx/kg/entities/{entity_id}/neighbors` | `depth` (1-3) |
| GET | `/v1/onyx/kg/relations` | `source?`, `target?`, `type?`, `cursor?`, `limit` |
| GET | `/v1/onyx/kg/subgraph` | `entities` (逗号分隔，最多 50), `depth` (1-3) |
| GET | `/v1/onyx/kg/chunks/{chunk_id}` | — |
| GET | `/v1/onyx/kg/stats` | — |

返回 `KGNode` / `KGEdge` 结构，可直接喂给 sigma.js / cytoscape：

```json
{
  "nodes": [
    {"id":"e_42","label":"FooService","type":"service","properties":{"description":"..."}},
    {"id":"e_77","label":"PostgreSQL","type":"database","properties":{}}
  ],
  "edges": [
    {"source":"e_42","target":"e_77","type":"depends_on","weight":0.9,"properties":{}}
  ]
}
```

---

## 7. 错误模型

统一格式：

```json
{
  "detail": "human-readable",
  "error_code": "machine_readable_constant",
  "request_id": "req_abc"
}
```

| HTTP | error_code | 含义 |
|---|---|---|
| 400 | `invalid_request` | 请求体/参数不合法 |
| 401 | `missing_token` / `invalid_token` | Token 缺/错 |
| 403 | `ip_not_allowed` | IP 白名单拒绝 |
| 404 | `kb_not_found` / `document_not_found` / `entity_not_found` | 资源不存在 |
| 409 | `kb_already_exists` | 幂等键冲突 |
| 413 | `payload_too_large` / `quota_exceeded` | 文件超限或 KB 配额满 |
| 415 | `unsupported_media_type` | 不支持的 MIME |
| 429 | `rate_limited` | 限流，header `Retry-After: <秒>` |
| 500 | `internal_error` | 内部错 |
| 502 | `upstream_llm_error` / `upstream_embedding_error` / `upstream_mineru_error` | 外部依赖失败，可重试 |
| 503 | `service_unavailable` | RAG 自身不健康 |
| 507 | `storage_full` | 磁盘满 |

ONYX 重试只对 5xx + 429；4xx 视作业务错。429 严格按 `Retry-After`。

---

## 8. 限流（默认值）

| 路径 | 限额 | 身份 |
|---|---|---|
| `/v1/onyx/query`, `/query/sync` | 30/min | per `X-Onyx-User-Id` |
| `/v1/onyx/documents` POST | 10/min | per `X-Onyx-User-Id` |
| `/v1/onyx/kg/*` | 120/min | per `X-Onyx-User-Id` |
| 其他 `/v1/onyx/*` | 60/min | per `X-Onyx-User-Id` |
| 整个 token 聚合 | 1000/min | per token |

ONYX 自己也建议加一层 per-user 限流，避免单用户拖累整体配额。

---

## 9. 数据生命周期与运维

| 触发 | ONYX 行为 |
|---|---|
| 用户离职 | onyx 把该用户的 KB 转 owner 或调本指南 §4.4 删除流程 |
| 工作区注销 | onyx 列出该 ws 下所有 KB → 逐个调 RAG DELETE → 删 onyx_kb 行 |
| 用户撤回敏感文档 | onyx 提示用户使用 DELETE document（耗时数分钟） |
| RAG 端 KB 被遗忘（onyx 软删但忘了调 RAG DELETE）| 后台 worker 周期对账重试 |
| LightRAG 升级停机（小版本，一般 < 1h）| RAG 运维提前 24h 广播；ONYX 可禁用 chat 输入框，显示「检索服务维护中」 |

---

## 10. 部署核对清单

ONYX 团队上线前确认：

- [ ] `RAG_ANYTHING_TOKEN` 与 RAG 运维交换完毕，写入 ONYX 后端 secret 管理（vault / sealed-secret）
- [ ] ONYX 后端能访问 `RAG_ANYTHING_BASE_URL`（防火墙 / Service mesh policy）
- [ ] ONYX 反代（如有）配置 SSE 长连接（§4.3 引用）
- [ ] `onyx_kb` + `onyx_kb_member` 表迁移已执行
- [ ] 代理路由白名单覆盖 §4.3 全表，SSRF 防护测试通过
- [ ] 调用 `/healthz` 探活 ✅
- [ ] 端到端 E2E 跑通：建库 → 上传 PDF → 等 indexed → chat 多轮 → KG 浏览 → 删库
- [ ] 错误注入：故意拔 RAG 的 LLM 上游，确认 onyx 显示降级提示
- [ ] 观测面板（Grafana）：`onyx_rag_proxy_*` 指标可见
- [ ] 安全审计：`grep -r 'RAG_ANYTHING_TOKEN' onyx_logs/` 应为空

---

## 11. v1 不做、v2 再说

- ONYX connector 文档自动同步（v2 加 `POST /v1/onyx/documents/from-connector`）
- 跨 KB 联邦查询（需 LightRAG 上游支持）
- KG 编辑（实体合并/标注；目前 KG 只读）
- 引用跳转到 PDF preview（带 page+bbox 高亮）
- OIDC 替代 INTERNAL_TOKEN（用户量上来再说）
- 计费：query token 用量已在 `done.tokens.total` 暴露，ONYX 可以累计；正式计费体系等产品决策

---

## 12. E2E 验证脚本

ONYX 实施完后，跑一遍：

```python
# tests/e2e/test_rag_integration.py
import httpx, time

base = "http://onyx-backend:8080"
session = onyx_login(alice)

# 1. 建库
kb = httpx.post(f"{base}/api/onyx/rag/kb", json={"display_name":"E2E Test KB"}, cookies=session).json()
assert kb["rag_tenant_id"].startswith("onyx-")

# 2. 上传
with open("samples/test.pdf","rb") as f:
    upload = httpx.post(f"{base}/api/onyx/rag/documents",
                        files={"file": f}, cookies=session,
                        params={"kb_id": kb["id"]}, timeout=60).json()

# 3. 等 indexed
job_id = upload["job_id"]
for _ in range(120):
    job = httpx.get(f"{base}/api/onyx/rag/jobs/{job_id}", cookies=session).json()
    if job["status"] in ("done","failed"):
        break
    time.sleep(5)
assert job["status"] == "done"

# 4. Query 流式
with httpx.stream("POST", f"{base}/api/onyx/rag/query",
                  json={"question":"what is in this doc?","kb_id":kb["id"]},
                  cookies=session, timeout=60) as resp:
    events = []
    for line in resp.iter_lines():
        if line.startswith("event:"):
            events.append(line)
assert any(e.startswith("event: done") for e in events)

# 5. KG 浏览
stats = httpx.get(f"{base}/api/onyx/rag/kg/stats?kb_id={kb['id']}", cookies=session).json()
assert stats["entities"] > 0

# 6. 删库
httpx.delete(f"{base}/api/onyx/rag/kb/{kb['id']}", cookies=session)
```

---

## 13. 联系方式

- RAG-Anything 维护：本仓库 issue 区
- API spec 变更通知：当 RAG 提供 `/v1/onyx/*` 任何不兼容变更时，ONYX 团队会提前 2 周收到通知；老版本路径至少保留 6 个月
- 紧急问题：（部署后填）
