# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Single-file Flask server (`claude_usage.py`) that polls the Anthropic OAuth usage API, caches results, serves them as JSON, and optionally pushes utilization to an AWTRIX 3 LED display (Ulanzi TC001) via MQTT.

## Running

```bash
# Activate venv and start server (default: 127.0.0.1:5000)
source .venv/bin/activate
python3 claude_usage.py
```

Dependencies: `pip install flask paho-mqtt` (see `requirements.txt`). Python 3.11+ venv is already set up in `.venv/`.

## Configuration

All config is via environment variables — see the `README.md` table. Key ones: `USAGE_REFRESH_INTERVAL`, `USAGE_HOST`, `USAGE_PORT`, `MQTT_BROKER`, `AWTRIX_PREFIX`, `CLAUDE_CODE_OAUTH_TOKEN`.

## Architecture

Everything lives in `claude_usage.py`:

- **Token resolution** — tries env var, then `~/.claude/.credentials.json`, then macOS Keychain
- **Background refresh thread** (`_refresh_loop`) — polls `api.anthropic.com/api/oauth/usage` on an interval, updates in-memory cache (`_cache` + `_cache_lock`), persists to `usage_cache.json`, and publishes to MQTT. Exponential backoff on 429s.
- **Flask routes** — `GET /usage` (cached data + metadata) and `GET /health` (status + timing)
- **AWTRIX integration** — builds a combined 32x8 pixel layout with reset timer text and 3 stacked progress bars (5h/7d/extra), published as a single retained MQTT message to `{AWTRIX_PREFIX}/custom/claude_usage`
- **Persistent cache** — `usage_cache.json` is loaded on startup so the display shows real values immediately

No tests, no build system, no linter config. This is a single-purpose utility.
