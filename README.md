# claude-usage

A lightweight Flask server that periodically fetches your Claude Code usage limits from the Anthropic API, exposes them as JSON endpoints, and publishes utilization to an AWTRIX 3 display (Ulanzi TC001) via MQTT.

## Requirements

- Python 3.13+
- `flask`
- `paho-mqtt` (for AWTRIX integration)
- A Claude Code Pro/Max subscription authenticated via OAuth
- Mosquitto MQTT broker (or any MQTT broker)

## Installation

```bash
pip3 install flask paho-mqtt
```

### MQTT broker (Mosquitto)

```bash
sudo apt-get install -y mosquitto mosquitto-clients
sudo mosquitto_passwd -c /etc/mosquitto/passwd test
sudo systemctl enable --now mosquitto
```

## Usage

```bash
python3 claude_usage.py
```

The server starts on `127.0.0.1:5000` by default, refreshes usage data every 60 seconds, and publishes to AWTRIX via MQTT.

## Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /usage` | Returns the latest cached usage data |
| `GET /health` | Returns server status and last-updated timestamp |

### Example responses

`GET /usage`
```json
{
  "last_updated": "2026-03-11T12:00:00+00:00",
  "last_error": null,
  "data": {
    "five_hour": {"resets_at": "2026-03-11T17:00:00+00:00", "utilization": 90},
    "seven_day": {"resets_at": "2026-03-18T12:00:00+00:00", "utilization": 74},
    "extra_usage": {"is_enabled": true, "monthly_limit": 4000, "used_credits": 2640, "utilization": 66}
  }
}
```

`GET /health`
```json
{
  "status": "ok",
  "last_updated": "2026-03-11T12:00:00+00:00",
  "last_error": null,
  "refresh_interval_seconds": 60
}
```

## Configuration

| Environment Variable | Default | Description |
|----------------------|---------|-------------|
| `USAGE_REFRESH_INTERVAL` | `60` | Seconds between API polls |
| `USAGE_HOST` | `127.0.0.1` | Host to bind the server to |
| `USAGE_PORT` | `5000` | Port to listen on |
| `CLAUDE_CODE_OAUTH_TOKEN` | -- | OAuth token (overrides credential file / keychain) |
| `MQTT_BROKER` | `localhost` | MQTT broker hostname |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USER` | `test` | MQTT username |
| `MQTT_PASS` | `test` | MQTT password |
| `AWTRIX_PREFIX` | `awtrix_a05ff4` | MQTT topic prefix for your AWTRIX device |

## AWTRIX 3 integration

Three custom apps are published via MQTT to cycle on the display:

| App | Topic | Description |
|-----|-------|-------------|
| `claude_5h` | `awtrix_a05ff4/custom/claude_5h` | 5-hour utilization with progress bar |
| `claude_7d` | `awtrix_a05ff4/custom/claude_7d` | 7-day utilization with progress bar |
| `claude_extra` | `awtrix_a05ff4/custom/claude_extra` | Extra usage credits utilization |

Each app is color-coded by utilization: green (<50%), yellow (<80%), red (>=80%).

Apps are published with `retain=True`, so the AWTRIX display picks them up even if it reconnects after the server. When the API is unreachable, default values are published to keep the display active.

### Showing only Claude usage on AWTRIX

Disable all built-in apps via MQTT:

```bash
mosquitto_pub -h localhost -u test -P test \
  -t "awtrix_a05ff4/settings" \
  -m '{"TIM":false,"DAT":false,"TEMP":false,"HUM":false,"BAT":false}'
```

### Rate limiting

On HTTP 429 responses, the refresh interval doubles exponentially (up to 1 hour) and resets on the next successful fetch.

## Token resolution

The server resolves your OAuth token in this order:

1. `CLAUDE_CODE_OAUTH_TOKEN` environment variable
2. `~/.claude/.credentials.json`
3. macOS Keychain (services `Claude Code-credentials` or `Claude Code`)

If no token is found, authenticate first with:

```bash
claude auth login
```

> **Note:** API keys (`sk-ant-api...`) are not supported. The usage-limits endpoint requires a Pro/Max OAuth token.
