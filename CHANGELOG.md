# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## v1.0.0 — 2026-05-12

Initial public release of **claude-usage-exporter** — a single-file Python
HTTP server and Prometheus exporter that wraps Claude Code's undocumented
`/api/oauth/usage` endpoint and makes the data available over your LAN.

### Endpoints

- `GET /usage` — live subscription utilisation for every rate-limit window
  (`five_hour`, `seven_day`, `seven_day_opus`, `seven_day_sonnet`, …) with
  `resets_at` timestamps. 30-second in-memory cache.
- `GET /stats` — raw contents of `~/.claude/stats-cache.json` (total sessions,
  messages, per-model token counters).
- `GET /token` — OAuth access-token expiry state
  (`expires_at`, `expires_in_seconds`, `expired`).
- `GET /metrics` — Prometheus text exposition including
  `claude_usage_utilization_percent`, `claude_usage_resets_at_seconds`,
  `claude_extra_usage_credits_used`, `claude_extra_usage_monthly_limit`,
  `claude_total_sessions`, `claude_total_messages`,
  `claude_model_tokens_total`, `claude_token_expires_in_seconds`, and a
  `claude_usage_scrape_ok` health gauge.
- `GET /health` — unauthenticated liveness probe.

### Security model

- **Bearer-token auth** with constant-time comparison (`hmac.compare_digest`).
- **Source-IP allowlist** — loopback + RFC1918 + link-local + ULA by default;
  override with `CLAUDE_USAGE_ALLOW_CIDRS`.
- **Per-IP rate limit** — 120 req/min, with the tracking dict capped at 1024
  distinct IPs to bound memory under flood.
- **CORS off by default** — opt in via `CLAUDE_USAGE_ALLOW_ORIGINS`. No
  `Allow-Credentials` reflection.
- **Exact-match routing** — `/usagex` and other prefix tricks return 404.
- **Optional TLS** — set `CLAUDE_USAGE_CERT` / `CLAUDE_USAGE_KEY` to wrap the
  listening socket.
- **Fail-safe defaults** — the server refuses to bind a non-loopback address
  without a token unless `CLAUDE_USAGE_INSECURE=1` is explicitly set.
- **Security headers** — `Cache-Control: no-store`,
  `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY` on every response.
- **`/health` info-leak removed** — unauthenticated probes only see
  `{"ok": true}`, not the endpoint list.

### Install

```bash
git clone https://github.com/lahirunirmalx/claude-usage-exporter ~/tools/claude-usage
cd ~/tools/claude-usage
./install.sh
```

The installer generates a 32-byte url-safe bearer token in
`~/.claude/usage-api-token` (mode 600), installs a systemd `--user` unit,
enables linger so the service survives reboot and logout, and prints a
loud port-opening warning before exiting.

### Requirements

- Linux with `systemd --user`
- Python 3.8 or newer (no third-party packages)
- An active Claude Code login (`~/.claude/.credentials.json` exists)

### Tested on

- Ubuntu 20.04, systemd 245, Python 3.8.10 (target / shipped configuration)

### Known caveats

- The upstream endpoint (`/api/oauth/usage`) is **undocumented** by Anthropic
  and may change or disappear without notice. The server will start returning
  upstream errors when that happens.
- The server sends `User-Agent: claude-cli/...` to mimic the official CLI.
  This is how community tools (`ccusage`, `claude-cost`, etc.) work today, but
  it is a policy gray area.
- The default systemd unit binds `0.0.0.0:7878`, which means **every** network
  interface on the host: Wi-Fi, Ethernet, Docker bridges, libvirt, VPN peers,
  cloud-VM public NICs. Read the *Opening ports* section of the README before
  deploying — especially on cloud VMs, VPNs, or untrusted Wi-Fi.
- Traffic is plaintext by default. The bearer token is visible to anyone on
  the LAN segment. Enable TLS or tunnel via SSH on shared networks.

### Not affiliated with Anthropic

This is a community tool. Not endorsed by, sponsored by, or supported by
Anthropic. Your OAuth token, your usage, your responsibility.
