# Local-only Pods MCP audit bridge. Requires a .env (cp .env.example .env).
.PHONY: up down restart rebuild logs ps health shell

up:              ## build + start the bridge on :8100
	docker compose up -d --build

down:            ## stop + remove the bridge
	docker compose down

restart:         ## recreate to pick up .env / spec changes (plain restart won't)
	docker compose up -d --force-recreate

rebuild:         ## force a clean rebuild
	docker compose build --no-cache && docker compose up -d

logs:            ## follow bridge logs
	docker compose logs -f

ps:              ## container status
	docker compose ps

health:          ## curl the health endpoint
	curl -s localhost:8100/health && echo

shell:           ## shell into the running container
	docker compose exec mcp-bridge /bin/bash
