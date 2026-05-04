# rag-service-web (apps/web)

Next.js 15 (App Router) frontend for the RAG-Anything alpha-product. Talks to
the `rag-service` API at `NEXT_PUBLIC_API_BASE_URL`.

## Stack

- **Next.js 15** with the App Router and React 19
- **shadcn/ui** + Tailwind CSS for components/theming (`components.json`)
- **TanStack Query** for server-state caching and mutations
- **Zustand** for lightweight client state
- **react-hook-form + zod** for form schemas / validation
- **sigma + graphology** for the knowledge-graph viewer
- **react-markdown + remark-gfm** for rendering chat responses

## Routes

```
app/
  (auth)/
    login/        Sign in (email + password -> JWT)
    signup/       Account creation
  (app)/          Authenticated layout (sidebar + topbar)
    documents/    Upload, list, status of ingest jobs
    chat/         SSE-streamed conversations against the RAG endpoint
    kg/           Knowledge-graph viewer (entities + relationships)
    settings/     Tenant settings, model + parser config, API key mgmt
  page.tsx        Root landing -> redirects to /documents or /login
```

## Dev quick-start

```bash
cd apps/web
npm install
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 npm run dev
# -> http://localhost:3000
```

The API server (`rag-api`) must be reachable at `NEXT_PUBLIC_API_BASE_URL` for
auth, documents, chat, and KG views to work.

## Build

```bash
npm run build      # production build
npm start          # run the built app on :3000
npm run lint       # eslint
```

## Env vars

| Var                       | Notes                                                |
|---------------------------|------------------------------------------------------|
| `NEXT_PUBLIC_API_BASE_URL`| Origin of `rag-api`. Browser-facing — must be public |

## Docker

The Dockerfile uses a multi-stage Node 20 Alpine build. Build context is
`apps/web` (NOT the repo root):

```bash
docker build -f apps/web/Dockerfile -t rag-web:latest apps/web
```

This is also wired up via `docker-compose.prod.yml` at the repo root. See
`/DEPLOY.md` for full deployment notes.
