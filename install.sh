#!/usr/bin/env bash
# Install the claude-usage-api as a systemd --user service, bound to the LAN
# with a bearer token and a self-signed TLS cert.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC_UNIT="$HERE/claude-usage-api.service"
DEST_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DEST_UNIT="$DEST_DIR/claude-usage-api.service"

CLAUDE_DIR="$HOME/.claude"
TOKEN_FILE="$CLAUDE_DIR/usage-api-token"

mkdir -p "$CLAUDE_DIR"
chmod 700 "$CLAUDE_DIR" || true

# 1) Bearer token (required when binding to LAN)
if [[ ! -s "$TOKEN_FILE" ]]; then
  umask 077
  python3 -c "import secrets; print(secrets.token_urlsafe(32))" > "$TOKEN_FILE"
  chmod 600 "$TOKEN_FILE"
  echo "generated bearer token at $TOKEN_FILE"
else
  echo "reusing bearer token at $TOKEN_FILE"
fi

# 2) Install systemd unit
mkdir -p "$DEST_DIR"
cp "$SRC_UNIT" "$DEST_UNIT"

systemctl --user daemon-reload
systemctl --user enable --now claude-usage-api.service

# Keep the service running after logout
loginctl enable-linger "$USER" 2>/dev/null || true

systemctl --user --no-pager status claude-usage-api.service | head -20

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
LAN_IP="${LAN_IP:-<your-lan-ip>}"
TOKEN="$(cat "$TOKEN_FILE")"

cat <<EOF

────────────────────────────────────────────────────────────
Service URL    : http://${LAN_IP}:7878
Bearer token   : ${TOKEN}
Token file     : ${TOKEN_FILE}   (chmod 600)

Test from another device on the SAME Wi-Fi:
  curl -H "Authorization: Bearer ${TOKEN}" http://${LAN_IP}:7878/usage

────────────────────────────────────────────────────────────
⚠️  PORT 7878 IS NOW OPEN ON ALL NETWORK INTERFACES (0.0.0.0)
────────────────────────────────────────────────────────────
That means every interface this box has: Wi-Fi, Ethernet, Docker
bridges, libvirt, Tailscale / WireGuard / OpenVPN peers, etc.

Before you walk away, check:

  1. Is this machine on a TRUSTED Wi-Fi (your own AP, WPA2/3)?
     If you'll ever take it to a café, hotel, or office Wi-Fi,
     set CLAUDE_USAGE_HOST=127.0.0.1 in the unit and use SSH
     tunnels instead, or stop the service when out.

  2. Are you running a VPN (Tailscale, WireGuard, ...)?
     Your peers can reach this port. Tighten with:
       CLAUDE_USAGE_ALLOW_CIDRS=127.0.0.0/8,<your-lan-cidr>

  3. Is this a cloud VM / VPS / dev container?
     0.0.0.0 = the open Internet. DO NOT run with this default.
     Stop now: systemctl --user stop claude-usage-api.service

  4. Open firewall to JUST your LAN subnet (recommended):
       sudo ufw allow from $(echo "$LAN_IP" | awk -F. '{print $1"."$2"."$3".0/24"}') to any port 7878 proto tcp
       sudo ufw deny 7878

Traffic is plaintext: anyone sniffing your Wi-Fi sees the token.
Defenses in place: bearer-token auth, RFC1918 source-IP allowlist,
120 req/min/IP rate limit. See README.md for the full security model.
────────────────────────────────────────────────────────────
EOF
