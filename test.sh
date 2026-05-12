#!/usr/bin/env bash
# Quick check: hit the undocumented Claude Code /usage endpoint directly.
# Usage: ./test.sh
set -euo pipefail

CREDS="${HOME}/.claude/.credentials.json"
if [[ ! -r "$CREDS" ]]; then
  echo "cannot read $CREDS" >&2
  exit 1
fi

TOKEN="$(python3 -c "import json,sys; print(json.load(open('$CREDS'))['claudeAiOauth']['accessToken'])")"
if [[ -z "$TOKEN" ]]; then
  echo "no accessToken in $CREDS (are you logged in with claude /login?)" >&2
  exit 1
fi

curl -sS \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "anthropic-beta: oauth-2025-04-20" \
  -H "User-Agent: claude-cli/2.1.133 (external, linux)" \
  "https://api.anthropic.com/api/oauth/usage" | python3 -m json.tool
