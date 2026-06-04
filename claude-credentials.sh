#!/usr/bin/env bash
# Manage the Claude Code OAuth token in the macOS Keychain.
#
# Usage: ./claude-credentials.sh [--simple] [--refresh] --import <file|->

set -euo pipefail

SERVICE="Claude Code-credentials"
TOKEN_URL="https://platform.claude.com/v1/oauth/token"
CLIENT_ID="9d1c250a-e61b-44d9-88ed-5944d1962f5e"

SIMPLE_OUT=false
DO_REFRESH=false
IMPORT_PATH=""

usage() {
  cat <<EOF
Usage: $(basename "$0") [--simple] [--refresh] --import <file|->

Modes:
  (default)    Print raw keychain bytes for '$SERVICE' (no decoding).

  --simple     Print the three OAuth fields in human-readable form:
                 access_token:  <value>
                 refresh_token: <value>
                 expires_at:    <ms since epoch>
                 expires:       <local datetime>

  --refresh    Refresh the access token via OAuth, write the result back
               to the keychain, then print the result in --simple form.

  --import <path>   Read raw keychain JSON from <path> (use '-' for stdin)
                    and write it verbatim to the keychain, replacing the
                    existing entry. Input is expected to be exactly what
                    default-mode export produces — the same shape Claude
                    Code itself stores. Requires the keychain entry to
                    already exist on this Mac (i.e., 'claude login' has
                    been run here at least once) so the account name can
                    be discovered.

--simple, --refresh, and --import are mutually exclusive.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --simple)   SIMPLE_OUT=true ;;
    --refresh)  DO_REFRESH=true ;;
    --import)
      [[ $# -ge 2 ]] || { echo "Error: --import requires a path argument (use '-' for stdin)" >&2; exit 1; }
      IMPORT_PATH="$2"
      shift
      ;;
    -h|--help)  usage; exit 0 ;;
    *)          echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

modes=0
$SIMPLE_OUT && modes=$((modes + 1))
$DO_REFRESH  && modes=$((modes + 1))
[[ -n "$IMPORT_PATH" ]] && modes=$((modes + 1))
if (( modes > 1 )); then
  echo "Error: --simple, --refresh, and --import are mutually exclusive" >&2
  exit 1
fi

# --import: read raw keychain bytes (file or stdin) and write verbatim.
if [[ -n "$IMPORT_PATH" ]]; then
  tmpfile=$(mktemp)
  trap 'rm -f "$tmpfile"' EXIT

  if [[ "$IMPORT_PATH" == "-" ]]; then
    cat > "$tmpfile"
  else
    if [[ ! -r "$IMPORT_PATH" ]]; then
      echo "Error: Cannot read import file: $IMPORT_PATH" >&2
      exit 1
    fi
    cat "$IMPORT_PATH" > "$tmpfile"
  fi

  if [[ ! -s "$tmpfile" ]]; then
    echo "Error: Import input is empty" >&2
    exit 1
  fi

  # Discover account name from existing keychain entry
  account=$(security find-generic-password -s "$SERVICE" 2>/dev/null \
    | grep '"acct"' | sed 's/.*<blob>="\{0,1\}//' | sed 's/"\{0,1\}$//')

  if [[ -z "$account" ]]; then
    echo "Error: No existing keychain entry for service '$SERVICE'. Run 'claude login' first." >&2
    exit 1
  fi

  security add-generic-password -U -a "$account" -s "$SERVICE" -w "$(cat "$tmpfile")"

  bytes=$(wc -c < "$tmpfile" | tr -d ' ')
  echo "Imported $bytes bytes to keychain service '$SERVICE' (account: $account)" >&2
  exit 0
fi

# Default: pass-through of `security -w` bytes, exactly as Claude Code wrote them.
if ! $SIMPLE_OUT && ! $DO_REFRESH; then
  security find-generic-password -s "$SERVICE" -w
  exit $?
fi

# --simple and --refresh both need the keychain bytes parsed as JSON.
keychain_out=$(security find-generic-password -s "$SERVICE" -w 2>/dev/null) || {
  echo "Error: No credentials found in Keychain" >&2
  exit 1
}

# Output may be hex-encoded or raw JSON depending on macOS version
if [[ "$keychain_out" =~ ^\{ ]]; then
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

expires_local=$(date -r $((expires_at_ms / 1000)) +"%Y-%m-%d %I:%M:%S %p %Z")
echo "access_token:  $access_token"
echo "refresh_token: $refresh_token"
echo "expires_at:    $expires_at_ms"
echo "expires:       $expires_local"
