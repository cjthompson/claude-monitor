#!/usr/bin/env bash
# Export Claude Code OAuth token from macOS Keychain.
# Usage: ./export_token.sh [--json] [--refresh]

set -euo pipefail

SERVICE="Claude Code-credentials"
TOKEN_URL="https://platform.claude.com/v1/oauth/token"
CLIENT_ID="9d1c250a-e61b-44d9-88ed-5944d1962f5e"

JSON_OUT=false
DO_REFRESH=false

for arg in "$@"; do
  case "$arg" in
    --json) JSON_OUT=true ;;
    --refresh) DO_REFRESH=true ;;
    -h|--help)
      echo "Usage: $(basename "$0") [--json] [--refresh]"
      echo "  --json     Output as JSON"
      echo "  --refresh  Refresh the access token via OAuth API"
      exit 0
      ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

# Read credentials from Keychain
keychain_out=$(security find-generic-password -s "$SERVICE" -w 2>/dev/null) || {
  echo "Error: No credentials found in Keychain" >&2
  exit 1
}

# Output may be hex-encoded or raw JSON depending on macOS version
if [[ "$keychain_out" =~ ^\{  ]]; then
  raw="$keychain_out"
else
  raw=$(echo "$keychain_out" | xxd -r -p)
fi

access_token=$(echo "$raw" | jq -r '.claudeAiOauth.accessToken // empty')
refresh_token=$(echo "$raw" | jq -r '.claudeAiOauth.refreshToken // empty')
expires_at_ms=$(echo "$raw" | jq -r '.claudeAiOauth.expiresAt // 0')

if [[ -z "$access_token" ]]; then
  echo "Error: No OAuth token found in credentials" >&2
  exit 1
fi

if $DO_REFRESH; then
  if [[ -z "$refresh_token" ]]; then
    echo "Error: No refresh token available" >&2
    exit 1
  fi

  response=$(curl -s --max-time 15 -X POST "$TOKEN_URL" \
    -H "Content-Type: application/json" \
    -d "{\"grant_type\":\"refresh_token\",\"refresh_token\":\"$refresh_token\",\"client_id\":\"$CLIENT_ID\"}")

  new_access=$(echo "$response" | jq -r '.access_token // empty')
  new_refresh=$(echo "$response" | jq -r ".refresh_token // \"$refresh_token\"")
  expires_in=$(echo "$response" | jq -r '.expires_in // 3600')

  if [[ -z "$new_access" ]]; then
    echo "Error: Refresh response missing access_token" >&2
    echo "$response" >&2
    exit 1
  fi

  new_expires_at_ms=$(( ($(date +%s) + expires_in) * 1000 ))

  # Update keychain
  account=$(security find-generic-password -s "$SERVICE" 2>/dev/null \
    | grep '"acct"' | sed 's/.*<blob>="\{0,1\}//' | sed 's/"\{0,1\}$//')

  if [[ -z "$account" ]]; then
    echo "Error: Could not determine keychain account" >&2
    exit 1
  fi

  updated=$(echo "$raw" | jq \
    --arg at "$new_access" \
    --arg rt "$new_refresh" \
    --argjson ea "$new_expires_at_ms" \
    '.claudeAiOauth.accessToken = $at | .claudeAiOauth.refreshToken = $rt | .claudeAiOauth.expiresAt = $ea')

  security add-generic-password -U -a "$account" -s "$SERVICE" -w "$updated"

  access_token="$new_access"
  refresh_token="$new_refresh"
  expires_at_ms="$new_expires_at_ms"
  echo "Token refreshed successfully" >&2
fi

if $JSON_OUT; then
  jq -n \
    --arg at "$access_token" \
    --arg rt "$refresh_token" \
    --argjson ea "$expires_at_ms" \
    '{access_token: $at, refresh_token: $rt, expires_at: $ea}'
else
  expires_local=$(date -r $((expires_at_ms / 1000)) +"%Y-%m-%d %I:%M:%S %p %Z")
  echo "access_token:  $access_token"
  echo "refresh_token: $refresh_token"
  echo "expires_at:    $expires_at_ms"
  echo "expires:       $expires_local"
fi
