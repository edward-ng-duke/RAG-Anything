# RAG-Anything dev workflow.
#   make dev          docker compose up --build (foreground, streams logs)
#   make dev-bg       same as dev but detached
#   make dev-down     docker compose down (containers stopped, volumes kept)
#   make dev-logs     tail -f api/worker/web/pg/redis logs
#   make dev-migrate  alembic upgrade head against the dev pg container
#   make dev-rebuild  rebuild api/worker/web images (after Dockerfile changes)
#   make dev-nuke     down -v (DROPS all dev volumes — pg/redis/rag-data)
#
# Host-published ports (kept disjoint from onyx's dev stack):
#   api  http://localhost:8800   web  http://localhost:3300
#   pg   localhost:5532          redis  localhost:6479

SHELL    := /bin/bash
COMPOSE  := docker compose -f docker-compose.dev.yml

.PHONY: help dev dev-bg dev-down dev-logs dev-migrate dev-rebuild dev-nuke

help:
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk -F ':.*?## ' '{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

dev:          ## bring the dev stack up (foreground, --build)
	$(COMPOSE) up --build

dev-bg:       ## bring the dev stack up detached
	$(COMPOSE) up --build -d

dev-down:     ## stop the dev stack (volumes preserved)
	$(COMPOSE) down

dev-logs:     ## follow logs for all services
	$(COMPOSE) logs -f

dev-migrate:  ## alembic upgrade head against the dev pg container
	$(COMPOSE) exec api uv run alembic upgrade head

dev-rebuild:  ## rebuild api/worker/web images
	$(COMPOSE) build api worker web

dev-nuke:     ## down -v (DESTROYS pg-data / redis-data / rag-data volumes)
	$(COMPOSE) down -v
