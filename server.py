#!/usr/bin/env python3
"""
Local/LAN HTTP wrapper for Claude Code's /usage data.

Endpoints:
  GET /usage     -> live subscription usage (5h + 7d windows)
  GET /stats     -> local Claude Code stats cache (~/.claude/stats-cache.json)
  GET /metrics   -> Prometheus text exposition of /usage + /stats
  GET /token     -> { expires_at, expires_in_seconds, expired }
  GET /health    -> liveness check (unauthenticated)

Config (env vars; defaults are loopback-only / unauthenticated):
  CLAUDE_USAGE_HOST          bind address       (default 127.0.0.1)
  CLAUDE_USAGE_PORT          bind port          (default 7878; also sys.argv[1])
  CLAUDE_USAGE_TOKEN         bearer token       (overrides token file)
  CLAUDE_USAGE_TOKEN_FILE    file with token    (default ~/.claude/usage-api-token)
  CLAUDE_USAGE_CERT          TLS cert PEM       (enables HTTPS when paired with KEY)
  CLAUDE_USAGE_KEY           TLS key PEM
  CLAUDE_USAGE_ALLOW_CIDRS   comma list of CIDRs allowed to connect
                             (default: loopback + RFC1918 + link-local + ULA)
  CLAUDE_USAGE_ALLOW_ORIGINS comma list of CORS origins to permit
                             (default: empty — browser/JS clients are blocked)
  CLAUDE_USAGE_RATE_LIMIT    requests/min/IP    (default 120; 0 disables)
  CLAUDE_USAGE_INSECURE      "1" to allow non-loopback bind without a token

Run:
  python3 server.py            # 127.0.0.1:7878, no auth
  python3 server.py 9000       # custom port
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
CREDS_PATH = os.path.join(HOME, ".claude", ".credentials.json")
STATS_PATH = os.path.join(HOME, ".claude", "stats-cache.json")
UPSTREAM = "https://api.anthropic.com/api/oauth/usage"
UA = "claude-cli/2.1.133 (external, linux)"

CACHE_TTL = 30  # seconds
_cache = {"data": None, "ts": 0.0}
_cache_lock = threading.Lock()


# ---------- config ----------

def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


HOST = os.environ.get("CLAUDE_USAGE_HOST", "127.0.0.1").strip() or "127.0.0.1"
try:
    PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("CLAUDE_USAGE_PORT", "7878"))
except ValueError:
    PORT = 7878

TOKEN_FILE = os.environ.get("CLAUDE_USAGE_TOKEN_FILE") or os.path.join(HOME, ".claude", "usage-api-token")
CERT_FILE = (os.environ.get("CLAUDE_USAGE_CERT") or "").strip()
KEY_FILE = (os.environ.get("CLAUDE_USAGE_KEY") or "").strip()
INSECURE = _env_bool("CLAUDE_USAGE_INSECURE")
try:
    RATE_LIMIT = max(0, int(os.environ.get("CLAUDE_USAGE_RATE_LIMIT", "120")))
except ValueError:
    RATE_LIMIT = 120

_DEFAULT_CIDRS = (
    "127.0.0.0/8,::1/128,"
    "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,"
    "fc00::/7,fe80::/10"
)
ALLOW_CIDRS_RAW = os.environ.get("CLAUDE_USAGE_ALLOW_CIDRS", _DEFAULT_CIDRS)
try:
    ALLOW_NETWORKS = [
        ipaddress.ip_network(c.strip(), strict=False)
        for c in ALLOW_CIDRS_RAW.split(",") if c.strip()
    ]
except ValueError as exc:
    sys.stderr.write(f"bad CLAUDE_USAGE_ALLOW_CIDRS ({ALLOW_CIDRS_RAW!r}): {exc}\n")
    sys.exit(2)

ALLOWED_ORIGINS = {
    o.strip() for o in os.environ.get("CLAUDE_USAGE_ALLOW_ORIGINS", "").split(",") if o.strip()
}
MAX_RL_IPS = 1024


def _is_loopback_host(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _load_token() -> str | None:
    t = (os.environ.get("CLAUDE_USAGE_TOKEN") or "").strip()
    if t:
        return t
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r") as f:
                t = f.read().strip()
                return t or None
        except OSError:
            return None
    return None


TOKEN = _load_token()
AUTH_REQUIRED = TOKEN is not None or not _is_loopback_host(HOST)

if AUTH_REQUIRED and not TOKEN and not INSECURE:
    sys.stderr.write(
        f"refusing to bind {HOST!r} without an API token.\n"
        f"  - put a secret in {TOKEN_FILE} (chmod 600), or\n"
        f"  - export CLAUDE_USAGE_TOKEN=..., or\n"
        f"  - set CLAUDE_USAGE_INSECURE=1 to override (NOT recommended).\n"
    )
    sys.exit(2)


# ---------- rate limit ----------

_rl_lock = threading.Lock()
_rl_buckets: "dict[str, deque[float]]" = {}


def _allow_rate(ip: str) -> bool:
    if RATE_LIMIT <= 0:
        return True
    now = time.monotonic()
    with _rl_lock:
        if ip not in _rl_buckets and len(_rl_buckets) >= MAX_RL_IPS:
            # Cap distinct-IP tracking to bound memory under flood.
            return False
        q = _rl_buckets.setdefault(ip, deque())
        cutoff = now - 60.0
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            return False
        q.append(now)
        return True


# ---------- upstream helpers ----------

def read_oauth():
    with open(CREDS_PATH, "r") as f:
        creds = json.load(f)
    oauth = creds.get("claudeAiOauth") or {}
    if not oauth.get("accessToken"):
        raise RuntimeError("no claudeAiOauth.accessToken in " + CREDS_PATH)
    return oauth


def token_state():
    oauth = read_oauth()
    expires_at_ms = oauth.get("expiresAt")
    now_ms = int(time.time() * 1000)
    if expires_at_ms is None:
        return {"expires_at": None, "expires_in_seconds": None, "expired": False}
    delta_s = (expires_at_ms - now_ms) // 1000
    return {
        "expires_at": expires_at_ms,
        "expires_in_seconds": delta_s,
        "expired": delta_s <= 0,
    }


def fetch_usage():
    with _cache_lock:
        now = time.time()
        if _cache["data"] is not None and now - _cache["ts"] < CACHE_TTL:
            return _cache["data"], True

        oauth = read_oauth()
        req = urllib.request.Request(
            UPSTREAM,
            headers={
                "Authorization": f"Bearer {oauth['accessToken']}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": UA,
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
        data = json.loads(body)
        _cache["data"] = data
        _cache["ts"] = now
        return data, False


def read_stats():
    with open(STATS_PATH, "r") as f:
        return json.load(f)


# ---------- prometheus ----------

def _metric_lines(name, help_text, mtype, samples):
    out = [f"# HELP {name} {help_text}", f"# TYPE {name} {mtype}"]
    for labels, value in samples:
        if value is None:
            continue
        label_str = ",".join(f'{k}="{v}"' for k, v in labels.items()) if labels else ""
        out.append(f"{name}{{{label_str}}} {value}" if labels else f"{name} {value}")
    return out


def render_prometheus():
    lines = []

    try:
        usage, _ = fetch_usage()
        usage_buckets = [
            "five_hour", "seven_day", "seven_day_oauth_apps",
            "seven_day_opus", "seven_day_sonnet", "seven_day_cowork",
            "seven_day_omelette",
        ]
        util_samples = []
        reset_samples = []
        for bucket in usage_buckets:
            b = usage.get(bucket)
            if isinstance(b, dict):
                util_samples.append(({"window": bucket}, b.get("utilization")))
                resets_at = b.get("resets_at")
                if resets_at:
                    try:
                        from datetime import datetime
                        ts = datetime.fromisoformat(resets_at.replace("Z", "+00:00")).timestamp()
                        reset_samples.append(({"window": bucket}, ts))
                    except Exception:
                        pass
        lines += _metric_lines(
            "claude_usage_utilization_percent",
            "Subscription utilization percent per rate-limit window",
            "gauge", util_samples,
        )
        lines += _metric_lines(
            "claude_usage_resets_at_seconds",
            "Unix timestamp when each rate-limit window resets",
            "gauge", reset_samples,
        )

        extra = usage.get("extra_usage") or {}
        lines += _metric_lines(
            "claude_extra_usage_enabled",
            "1 if pay-as-you-go extra usage is enabled, else 0",
            "gauge", [({}, 1 if extra.get("is_enabled") else 0)],
        )
        if extra.get("used_credits") is not None:
            lines += _metric_lines(
                "claude_extra_usage_credits_used",
                "Extra-usage credits spent this month",
                "gauge", [({}, extra.get("used_credits"))],
            )
        if extra.get("monthly_limit") is not None:
            lines += _metric_lines(
                "claude_extra_usage_monthly_limit",
                "Extra-usage monthly credit cap",
                "gauge", [({}, extra.get("monthly_limit"))],
            )
        lines += _metric_lines("claude_usage_scrape_ok", "1 if /usage fetch succeeded", "gauge", [({}, 1)])
    except Exception as e:
        lines += _metric_lines("claude_usage_scrape_ok", "1 if /usage fetch succeeded", "gauge", [({}, 0)])
        lines.append(f"# usage fetch failed: {e.__class__.__name__}: {e}")

    try:
        stats = read_stats()
        lines += _metric_lines(
            "claude_total_sessions", "Total Claude Code sessions recorded",
            "counter", [({}, stats.get("totalSessions", 0))],
        )
        lines += _metric_lines(
            "claude_total_messages", "Total Claude Code messages recorded",
            "counter", [({}, stats.get("totalMessages", 0))],
        )
        token_samples = []
        for model, m in (stats.get("modelUsage") or {}).items():
            for field in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"):
                token_samples.append(({"model": model, "kind": field}, m.get(field, 0)))
        lines += _metric_lines(
            "claude_model_tokens_total", "Cumulative tokens by model and kind",
            "counter", token_samples,
        )
    except Exception as e:
        lines.append(f"# stats read failed: {e.__class__.__name__}: {e}")

    try:
        tok = token_state()
        if tok.get("expires_in_seconds") is not None:
            lines += _metric_lines(
                "claude_token_expires_in_seconds",
                "Seconds until the OAuth access token expires (negative = expired)",
                "gauge", [({}, tok["expires_in_seconds"])],
            )
    except Exception as e:
        lines.append(f"# token state failed: {e.__class__.__name__}: {e}")

    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------- HTTP handler ----------

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "claude-usage-api/2"
    sys_version = ""

    def _client_ip(self) -> str:
        return self.client_address[0]

    def _ip_allowed(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in ALLOW_NETWORKS)

    def _check_auth(self) -> bool:
        if not AUTH_REQUIRED:
            return True
        h = self.headers.get("Authorization", "")
        if not h.startswith("Bearer "):
            return False
        provided = h[len("Bearer "):].strip()
        return bool(TOKEN) and hmac.compare_digest(provided, TOKEN)

    def _send(self, status, body, content_type="application/json", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in _SECURITY_HEADERS.items():
            self.send_header(k, v)
        origin = self.headers.get("Origin")
        if origin and origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Max-Age", "600")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status, payload, extra=None):
        self._send(status, json.dumps(payload, indent=2).encode("utf-8"), "application/json", extra)

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def do_GET(self):
        ip = self._client_ip()
        if not self._ip_allowed(ip):
            self._send_json(403, {"error": "forbidden", "reason": "source ip not allowed"})
            return
        if not _allow_rate(ip):
            self._send_json(429, {"error": "rate limited"})
            return

        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path in ("/", "/health"):
            self._send_json(200, {"ok": True})
            return

        if not self._check_auth():
            self._send_json(
                401,
                {"error": "unauthorized"},
                {"WWW-Authenticate": 'Bearer realm="claude-usage-api"'},
            )
            return

        try:
            if path == "/usage":
                data, cached = fetch_usage()
                self._send_json(200, data, {"X-Cache": "HIT" if cached else "MISS"})
            elif path == "/stats":
                self._send_json(200, read_stats())
            elif path == "/token":
                self._send_json(200, token_state())
            elif path == "/metrics":
                self._send(200, render_prometheus(), "text/plain; version=0.0.4")
            else:
                self._send_json(404, {"error": "not found"})
        except urllib.error.HTTPError as e:
            payload = {"error": "upstream", "status": e.code, "body": e.read().decode("utf-8", "replace")}
            if e.code == 401:
                payload["hint"] = "OAuth token rejected — run `claude /login` to refresh."
            self._send_json(e.code, payload)
        except FileNotFoundError as e:
            self._send_json(500, {"error": "missing file", "detail": str(e)})
        except Exception as e:
            self._send_json(500, {"error": e.__class__.__name__, "detail": str(e)})

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s %s\n" % (self.log_date_time_string(), self._client_ip(), fmt % args))


def _build_ssl_context():
    if not (CERT_FILE and KEY_FILE):
        return None
    if not (os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)):
        sys.stderr.write(f"warning: CLAUDE_USAGE_CERT/KEY set but file(s) missing — serving plaintext.\n")
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)
    return ctx


def main():
    addr = (HOST, PORT)
    httpd = ThreadingHTTPServer(addr, Handler)

    scheme = "http"
    ctx = _build_ssl_context()
    if ctx is not None:
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    elif not _is_loopback_host(HOST):
        sys.stderr.write(
            "warning: serving PLAINTEXT on a non-loopback address. "
            "Set CLAUDE_USAGE_CERT and CLAUDE_USAGE_KEY for TLS.\n"
        )

    sys.stderr.write(
        f"claude-usage-api listening on {scheme}://{HOST}:{PORT} "
        f"(auth={'on' if AUTH_REQUIRED else 'off'}, "
        f"tls={'on' if ctx else 'off'}, "
        f"rate_limit={RATE_LIMIT}/min, "
        f"allow={ALLOW_CIDRS_RAW})\n"
    )
    sys.stderr.write("  GET /usage    /stats    /token    /metrics    /health\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
