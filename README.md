# Bing Bang

Top-down multiplayer arcade shooter. Up to 4 players per room over WebSockets.
Three modes: co-op vs bots (waves), free-for-all, team deathmatch.

## Run locally

    python3 server.py

Then open <http://localhost:8000>.

## Run in Docker

    docker compose up -d --build

## Deploy

Push to `main`. The `deploy_listener.py` container receives a GitHub webhook,
verifies HMAC against `DEPLOY_SECRET`, `git fetch && git reset --hard origin/main`,
then `docker compose up -d --build bingbang`.

## Files

- `server.py` — authoritative game server (stdlib-only, WebSocket + HTTP)
- `index.html` — client (renders server snapshots, sends input)
- `Dockerfile` — game server container
- `Dockerfile.deploy` + `deploy_listener.py` — webhook receiver
- `docker-compose.yml` — wires both onto the existing `twenty_default` network
- `test_e2e.py` — synthetic protocol test
