# claude-usage-exporter

A tiny local HTTP server + Prometheus exporter for Claude Code's `/usage` data,
so your dashboards, status bars, phone widgets, or Prometheus scrapers can read
your subscription utilisation, 5h / 7d rate-limit windows, OAuth token expiry,
and cumulative session stats over your LAN.

Single-file Python 3.8+ server with **no third-party dependencies**.

```
GET /usage     subscription usage (5h + 7d windows)
GET /stats     local Claude Code stats cache
GET /token     OAuth access-token expiry state
GET /metrics   Prometheus exposition of the above
GET /health    liveness probe (unauthenticated)
```

---

## Disclaimers (read first)

- This calls an **undocumented** Claude CLI endpoint (`/api/oauth/usage`) and
  sends `User-Agent: claude-cli/...` to mimic the official CLI. Anthropic can
  change or remove this endpoint at any time and the server will break.
- It uses **your** OAuth token from `~/.claude/.credentials.json`. Anyone with
  the bearer token this server issues can read your usage. Treat it as a
  password.
- Not affiliated with or endorsed by Anthropic.

---

## Security model (TL;DR)

| Control                     | Default                                          |
|----------------------------|--------------------------------------------------|
| Bind address               | `127.0.0.1` (manual run) / `0.0.0.0` (systemd)   |
| Bearer-token auth          | required if non-loopback bind                    |
| Constant-time token check  | yes (`hmac.compare_digest`)                      |
| Source-IP allowlist        | loopback + RFC1918 + link-local + ULA            |
| Rate limit                 | 120 req/min per IP, capped at 1024 tracked IPs   |
| CORS                       | disabled unless `CLAUDE_USAGE_ALLOW_ORIGINS` set |
| Transport                  | **plain HTTP** — token visible on the wire       |
| OAuth credentials          | read at runtime from `~/.claude/.credentials.json` |

The bearer token, allowlist, and rate limit are the three defenses. Lose any
one and the others should still hold.

---

## ⚠️ Opening ports — read this before installing

The `systemd` unit binds the server to `0.0.0.0:7878`, which makes it reachable
on **every network interface your machine has**, not just your Wi-Fi. That
matters if you have:

- **Docker, Podman, libvirt, k3s, etc.** — containers can usually reach
  `host.docker.internal` and any port the host opens on `0.0.0.0`.
- **VPN clients (Tailscale, WireGuard, OpenVPN, Zerotier)** — your peer mesh
  is "the same LAN" as far as the OS is concerned. Tailscale in particular
  will share this port with every device on your tailnet.
- **Public Wi-Fi or hotel networks** — your "LAN" is now everyone on that AP.
  Don't run this there. Set `CLAUDE_USAGE_HOST=127.0.0.1` until you're home.
- **Cloud VMs, dev containers, jump hosts** — `0.0.0.0` likely means the
  open Internet. **Never run this with the default bind on a public host.**
- **Router port-forwards / UPnP** — if your router forwards 7878 (it almost
  certainly doesn't by default), this becomes reachable from the open web.

**Hardening options, weakest to strongest:**

1. **Bind to a specific interface IP** (recommended on multi-homed hosts):
   ```ini
   Environment=CLAUDE_USAGE_HOST=192.168.1.42
   ```
2. **Tighten the source-IP allowlist** to your exact subnet:
   ```ini
   Environment=CLAUDE_USAGE_ALLOW_CIDRS=127.0.0.0/8,192.168.1.0/24
   ```
3. **Loopback-only + SSH tunnel** when you don't actively need LAN access:
   ```ini
   Environment=CLAUDE_USAGE_HOST=127.0.0.1
   ```
   From another machine: `ssh -L 7878:127.0.0.1:7878 you@host`.
4. **Host firewall** — only allow your LAN subnet at the OS level:
   ```bash
   sudo ufw allow from 192.168.1.0/24 to any port 7878 proto tcp
   sudo ufw deny 7878
   ```

If you don't understand which of the above applies to your setup, **don't open
the port** — run loopback-only and tunnel.

The server refuses to start on a non-loopback bind unless a token is configured
(`CLAUDE_USAGE_INSECURE=1` overrides this, and you almost never want that).

---

## Setup

### Prerequisites

- Linux with `systemd --user`
- Python 3.8+
- An active Claude Code login (`~/.claude/.credentials.json` exists)

### Install (systemd user service)

```bash
git clone <this-repo> ~/tools/claude-usage
cd ~/tools/claude-usage
./install.sh
```

The installer:

1. Generates `~/.claude/usage-api-token` (32-byte url-safe, `chmod 600`) if absent.
2. Copies the unit to `~/.config/systemd/user/`.
3. Enables + starts the service.
4. Enables linger so it survives reboots and logout.
5. Prints the bearer token and the LAN URL.

### Test from another device on the same Wi-Fi

```bash
# from your laptop / phone (Termux, Shortcuts, curl-app, etc.)
curl -H "Authorization: Bearer <token>" http://<host-ip>:7878/usage
```

### Manual run (no systemd)

```bash
# loopback only, no auth — for local scripts
python3 server.py

# custom port
python3 server.py 9000

# LAN bind with a token
CLAUDE_USAGE_HOST=0.0.0.0 \
CLAUDE_USAGE_TOKEN_FILE=~/.claude/usage-api-token \
python3 server.py
```

### Uninstall

```bash
systemctl --user disable --now claude-usage-api.service
rm ~/.config/systemd/user/claude-usage-api.service
rm ~/.claude/usage-api-token
loginctl disable-linger "$USER"   # optional
```

---

## Configuration (environment variables)

| Variable                    | Default                                  | Notes |
|----------------------------|------------------------------------------|-------|
| `CLAUDE_USAGE_HOST`        | `127.0.0.1`                              | bind address |
| `CLAUDE_USAGE_PORT`        | `7878`                                   | bind port |
| `CLAUDE_USAGE_TOKEN`       | _(unset)_                                | bearer token (overrides token file) |
| `CLAUDE_USAGE_TOKEN_FILE`  | `~/.claude/usage-api-token`              | file containing the token |
| `CLAUDE_USAGE_CERT`        | _(unset)_                                | TLS cert PEM (enables HTTPS with `KEY`) |
| `CLAUDE_USAGE_KEY`         | _(unset)_                                | TLS key PEM |
| `CLAUDE_USAGE_ALLOW_CIDRS` | loopback + RFC1918 + link-local + ULA    | comma-separated CIDRs allowed to connect |
| `CLAUDE_USAGE_ALLOW_ORIGINS` | _(empty)_                              | comma-separated CORS origins; off by default |
| `CLAUDE_USAGE_RATE_LIMIT`  | `120`                                    | requests / minute / IP; `0` disables |
| `CLAUDE_USAGE_INSECURE`    | `0`                                      | `1` lets non-loopback bind run without a token — don't |

### Enabling HTTPS (optional)

Plain HTTP exposes the bearer token to anyone watching your Wi-Fi traffic.
On a trusted home network with WPA2/3 it's an acceptable trade-off; on shared
Wi-Fi it isn't. To enable TLS:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout ~/.claude/usage-api.key -out ~/.claude/usage-api.crt \
  -subj "/CN=claude-usage-api" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:$(hostname -I | awk '{print $1}')"
chmod 600 ~/.claude/usage-api.{crt,key}
```

Then add to the unit:

```ini
Environment=CLAUDE_USAGE_CERT=%h/.claude/usage-api.crt
Environment=CLAUDE_USAGE_KEY=%h/.claude/usage-api.key
```

Clients will need to pin or trust the self-signed cert (`curl -k`, install on
the device, etc.).

---

## Endpoints

All endpoints except `/health` require `Authorization: Bearer <token>` when
auth is enabled.

### `GET /usage`

Live subscription usage. 30-second in-memory cache.

```json
{
  "five_hour":  { "utilization": 9.0,  "resets_at": "2026-05-12T15:50:01Z" },
  "seven_day":  { "utilization": 54.0, "resets_at": "2026-05-14T11:00:00Z" },
  "seven_day_opus":   null,
  "seven_day_sonnet": { "utilization": 0.0, "resets_at": null },
  ...
}
```

### `GET /stats`

Raw contents of `~/.claude/stats-cache.json` (sessions, messages, per-model
token counters).

### `GET /token`

OAuth access-token expiry state.

```json
{ "expires_at": 1778594119027, "expires_in_seconds": 6962, "expired": false }
```

### `GET /metrics`

Prometheus text exposition of the above. Add a scrape job:

```yaml
scrape_configs:
  - job_name: claude_usage
    scheme: http
    static_configs:
      - targets: ["192.168.1.42:7878"]
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/claude-usage-token
```

### `GET /health`

Always returns `{"ok": true}`. Unauthenticated. Use for liveness probes.

---

## Troubleshooting

**`401 unauthorized`** — the token is missing or wrong. The token is in
`~/.claude/usage-api-token` on the server host.

**`403 forbidden ... source ip not allowed`** — your client isn't in the
allowlist. Either you're not actually on the LAN, or you're connecting through
a NAT/VPN that hides your real IP. Check `journalctl --user -u
claude-usage-api` for the IP the server saw.

**`upstream` error with `OAuth token rejected`** — your Claude credentials
expired. Run `claude /login` on the host.

**Service won't start (status=218/CAPABILITIES)** — your systemd is too old
for some `Protect*` directives in a user unit. The shipped unit has been
pruned for systemd 245; if you see this on a different setup, drop more
directives until it starts.

**Token rotation** —

```bash
rm ~/.claude/usage-api-token
./install.sh         # regenerates and restarts
```

---

## License

MIT — see [LICENSE](LICENSE).
