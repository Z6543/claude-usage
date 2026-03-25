"""Flask web server exposing Claude Code usage limits as JSON and AWTRIX 3.

Token resolution order:
  1. CLAUDE_CODE_OAUTH_TOKEN env var
  2. ~/.claude/.credentials.json  (Linux default; also present on macOS)
  3. macOS Keychain               (service "Claude Code-credentials", fallback "Claude Code")

Endpoints:
  GET /usage  — returns latest cached usage data as JSON
  GET /health — returns server status and last-updated timestamp

AWTRIX 3 integration (optional):
  Set MQTT_BROKER to enable. Publishes custom apps via MQTT:
    claude_5h    — 5-hour utilization
    claude_7d    — 7-day utilization
    claude_extra — extra-usage credits utilization
"""

import json
import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
REFRESH_INTERVAL = int(os.environ.get("USAGE_REFRESH_INTERVAL", "60"))  # seconds
MAX_BACKOFF = 3600  # cap backoff at 1 hour on repeated 429s
CACHE_FILE = Path(__file__).parent / "usage_cache.json"

# MQTT / AWTRIX 3 settings (all optional — MQTT disabled when MQTT_BROKER is unset)
MQTT_BROKER = os.environ.get("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "test")
MQTT_PASS = os.environ.get("MQTT_PASS", "test")
AWTRIX_PREFIX = os.environ.get("AWTRIX_PREFIX", "awtrix_a05ff4")

_DEFAULT_DATA = {
    "extra_usage": {"is_enabled": False, "monthly_limit": 40, "used_credits": 35, "utilization": 70},
    "five_hour": {"resets_at": None, "utilization": 23},
    "iguana_necktie": None,
    "seven_day": {"resets_at": None, "utilization": 87},
    "seven_day_cowork": None,
    "seven_day_oauth_apps": None,
    "seven_day_opus": None,
    "seven_day_sonnet": None,
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

_cache: dict = {}
_cache_lock = threading.Lock()
_last_updated: datetime | None = None
_last_error: str | None = None


# ---------------------------------------------------------------------------
# Persistent cache
# ---------------------------------------------------------------------------


def _save_cache(data: dict, updated: datetime) -> None:
    payload = {"last_updated": updated.isoformat(), "data": data}
    CACHE_FILE.write_text(json.dumps(payload, indent=2))


def _load_cache() -> None:
    global _last_updated
    if not CACHE_FILE.exists():
        return
    try:
        payload = json.loads(CACHE_FILE.read_text())
        data = payload.get("data", {})
        ts = payload.get("last_updated")
        with _cache_lock:
            _cache.clear()
            _cache.update(data)
            if ts:
                _last_updated = datetime.fromisoformat(ts)
        log.info("Loaded cached usage from %s", CACHE_FILE)
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        log.warning("Failed to load cache file: %s", exc)


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------


def _keychain_read(service: str) -> str | None:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
        )
        value = result.stdout.strip()
        return value if value else None
    except FileNotFoundError:
        return None


def get_token_from_env() -> str | None:
    raw = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("{"):
        data = json.loads(raw)
        return data.get("accessToken") or data.get("access_token")
    return raw


def get_token_from_credentials_file() -> str | None:
    if not CREDENTIALS_FILE.exists():
        return None
    data = json.loads(CREDENTIALS_FILE.read_text())
    oauth = data.get("claudeAiOauth") or data.get("claudeAiOAuth")
    if isinstance(oauth, dict):
        token = oauth.get("accessToken") or oauth.get("access_token")
        if token:
            return token
    for key in ("accessToken", "access_token", "oauth_token", "token"):
        if key in data:
            return data[key]
    return None


def get_token_from_keychain() -> tuple[str, str] | None:
    for service in ("Claude Code-credentials", "Claude Code"):
        token = _keychain_read(service)
        if token:
            return token, service
    return None


def get_token() -> tuple[str, str]:
    """Return (token, source) or raise RuntimeError if none found."""
    token = get_token_from_env()
    if token:
        return token, "CLAUDE_CODE_OAUTH_TOKEN env var"

    token = get_token_from_credentials_file()
    if token:
        return token, str(CREDENTIALS_FILE)

    result = get_token_from_keychain()
    if result:
        token, service = result
        return token, f"macOS Keychain ({service})"

    raise RuntimeError(
        "No Claude Code token found. Checked:\n"
        "  • CLAUDE_CODE_OAUTH_TOKEN env var\n"
        f"  • {CREDENTIALS_FILE}\n"
        "  • macOS Keychain ('Claude Code-credentials', 'Claude Code')\n\n"
        "Authenticate first with: claude auth login"
    )


# ---------------------------------------------------------------------------
# API fetch
# ---------------------------------------------------------------------------


def fetch_usage(token: str) -> dict:
    if token.startswith("sk-ant-api"):
        raise RuntimeError(
            "Found an API key, not an OAuth token. "
            "The usage-limits endpoint requires a Pro/Max subscription authenticated via OAuth."
        )
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": BETA_HEADER,
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


# ---------------------------------------------------------------------------
# MQTT / AWTRIX 3
# ---------------------------------------------------------------------------

_mqtt_client = None


def _color_for_utilization(pct: int) -> str:
    """Return a hex color: green < 50, yellow < 80, red >= 80."""
    if pct < 50:
        return "#00FF00"
    if pct < 80:
        return "#FFFF00"
    return "#FF0000"


def _minutes_remaining(resets_at: str | None) -> int | None:
    if not resets_at:
        return None
    try:
        reset_time = datetime.fromisoformat(resets_at)
        delta = reset_time - datetime.now(timezone.utc)
        return max(0, int(delta.total_seconds() / 60))
    except (ValueError, TypeError):
        return None


def _build_awtrix_combined(
    five_pct: int, seven_pct: int, extra_pct: int, reset_mins: int | None,
) -> dict:
    """Build a single AWTRIX app with reset timer + 3 progress bars (32x8 display).

    Layout (left to right):
      x=0..13   minutes remaining until 5h reset (text)
      x=15..31  3 stacked progress bars (top: 5h, mid: 7d, bottom: extra)
    """
    bar_x = 15
    bar_max_w = 32 - bar_x
    draw = []

    mins_text = str(reset_mins) if reset_mins is not None else "--"
    mins_color = _color_for_utilization(five_pct)
    draw.append({"dt": [0, 1, mins_text, mins_color]})

    bars = [(five_pct, 0), (seven_pct, 3), (extra_pct, 6)]
    for pct, y in bars:
        color = _color_for_utilization(pct)
        bar_w = max(1, int(bar_max_w * pct / 100)) if pct > 0 else 0
        draw.append({"df": [bar_x, y, bar_max_w, 2, "#333333"]})
        if bar_w > 0:
            draw.append({"df": [bar_x, y, bar_w, 2, color]})
    return {"draw": draw, "lifetime": REFRESH_INTERVAL * 3}


def _mqtt_connect():
    global _mqtt_client
    if not MQTT_BROKER:
        return
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        log.error("paho-mqtt not installed; MQTT disabled. Install with: uv pip install paho-mqtt")
        return

    _mqtt_client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="claude-usage",
    )
    if MQTT_USER:
        _mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
    _mqtt_client.connect(MQTT_BROKER, MQTT_PORT)
    _mqtt_client.loop_start()
    log.info("MQTT connected to %s:%d (prefix: %s)", MQTT_BROKER, MQTT_PORT, AWTRIX_PREFIX)


def _mqtt_publish(data: dict) -> None:
    if not _mqtt_client:
        return

    five_hour = data.get("five_hour") or {}
    five_pct = five_hour.get("utilization") or 0
    seven_pct = (data.get("seven_day") or {}).get("utilization") or 0
    extra_pct = (data.get("extra_usage") or {}).get("utilization") or 0
    reset_mins = _minutes_remaining(five_hour.get("resets_at"))

    payload = _build_awtrix_combined(five_pct, seven_pct, extra_pct, reset_mins)
    topic = f"{AWTRIX_PREFIX}/custom/claude_usage"
    _mqtt_client.publish(topic, json.dumps(payload), retain=True)
    log.debug("MQTT published %s: %s", topic, payload)

    log.info("AWTRIX app updated (5h=%d%% 7d=%d%% extra=%d%%)",
             five_pct, seven_pct, extra_pct)


# ---------------------------------------------------------------------------
# Background refresh
# ---------------------------------------------------------------------------


def _refresh_loop() -> None:
    global _last_updated, _last_error
    backoff = REFRESH_INTERVAL
    while True:
        try:
            token, source = get_token()
            data = fetch_usage(token)
            with _cache_lock:
                _cache.clear()
                _cache.update(data)
                _last_updated = datetime.now(timezone.utc)
                _last_error = None
            log.info("Usage cache refreshed (token source: %s)", source)
            _save_cache(data, _last_updated)
            backoff = REFRESH_INTERVAL
        except urllib.error.HTTPError as exc:
            _last_error = str(exc)
            if exc.code == 429:
                backoff = min(backoff * 2, MAX_BACKOFF)
                log.warning("Rate limited (429); backing off to %ds", backoff)
            else:
                log.error("Failed to refresh usage: %s", exc)
        except Exception as exc:  # noqa: BLE001
            _last_error = str(exc)
            log.error("Failed to refresh usage: %s", exc)

        with _cache_lock:
            publish_data = dict(_cache) if _cache else _DEFAULT_DATA
        _mqtt_publish(publish_data)
        time.sleep(backoff)


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------


@app.get("/usage")
def usage():
    with _cache_lock:
        data = dict(_cache) if _cache else _DEFAULT_DATA
        return jsonify(
            {
                "last_updated": _last_updated.isoformat() if _last_updated else None,
                "last_error": _last_error,
                "data": data,
            }
        )


@app.get("/health")
def health():
    with _cache_lock:
        return jsonify(
            {
                "status": "ok" if _cache else "degraded",
                "last_updated": _last_updated.isoformat() if _last_updated else None,
                "last_error": _last_error,
                "refresh_interval_seconds": REFRESH_INTERVAL,
            }
        )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    _load_cache()
    _mqtt_connect()

    t = threading.Thread(target=_refresh_loop, daemon=True, name="usage-refresh")
    t.start()

    host = os.environ.get("USAGE_HOST", "127.0.0.1")
    port = int(os.environ.get("USAGE_PORT", "5000"))
    log.info("Starting Claude usage server on %s:%d (refresh every %ds)", host, port, REFRESH_INTERVAL)
    app.run(host=host, port=port)


if __name__ == "__main__":
    main()
