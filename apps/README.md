# apps/

This directory holds the two standalone deliverables of the alpha (α) product plan: the backend service in `server/` and the frontend client in `web/`. Each app is independently buildable and deployable; the split keeps API/runtime concerns separate from UI concerns while sharing the same repo for coordinated versioning. The underlying RAG-Anything library lives at the repo root (`raganything/`) and is consumed by `server/` as a dependency.
