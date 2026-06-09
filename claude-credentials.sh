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
OAUTH_ONLY=false
IMPORT_PATH=""
SEND_HOST=""
SEND_PORT="47299"
RECEIVE_MODE=false
RECEIVE_PORT="47299"

usage() {
  cat <<EOF
Usage: $(basename "$0") [--simple] [--refresh] [--oauth-only] --import <file|->
       $(basename "$0") --send <host> [--send-port <port>]
       $(basename "$0") --receive [--port <port>]

Modes:
  (default)    Print raw keychain bytes for '$SERVICE' (no decoding).

  --simple     Print the three OAuth fields in human-readable form:
                 access_token:  <value>
                 refresh_token: <value>
                 expires_at:    <ms since epoch>
                 expires:       <local datetime>

  --oauth-only  Print only the claudeAiOauth section as JSON (omits mcpOAuth
                and any other keys). Useful for sharing credentials between
                machines without transferring machine-specific OAuth tokens.

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

  --send <host>     Read keychain bytes (same as default mode) and send them
                    as a single UDP datagram to <host> on the configured
                    port. Default port: 47299. Override with --send-port.
                    Sender is fire-and-forget — the receiver does not need
                    to be running first (UDP is connectionless). Exit 0
                    even if no receiver is listening.

  --receive         Bind a UDP socket on the configured port (default 47299,
                    override with --port), wait for ONE datagram, write the
                    received bytes to the keychain (replacing the existing
                    entry, same write path as --import), then exit.
                    Receiver is one-shot — it is NOT a daemon.
                    Note: macOS may prompt for firewall access the first
                    time you --receive, since python3 is binding a non-
                    standard port.

  --send-port <port>  Override the destination port for --send (default 47299).
  --port <port>       Override the listening port for --receive (default 47299).

Default port 47299 is "claude credentials" (4+7+2+9+9). It is in the
IANA dynamic/private range (49152-65535) so collisions with common
services are unlikely. Note that keychain bytes can be larger than the
theoretical 65507-byte UDP limit only if the credential blob is unusually
large; in practice the OAuth blob is well under 4 KB. macOS default
socket buffer is 9216 bytes — a brief comment near the send/receive
points will call this out.

--simple, --oauth-only, --refresh, --import, --send, and --receive are mutually exclusive.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --simple)     SIMPLE_OUT=true ;;
    --oauth-only) OAUTH_ONLY=true ;;
    --refresh)    DO_REFRESH=true ;;
    --import)
      [[ $# -ge 2 ]] || { echo "Error: --import requires a path argument (use '-' for stdin)" >&2; exit 1; }
      IMPORT_PATH="$2"
      shift
      ;;
    --send)
      [[ $# -ge 2 ]] || { echo "Error: --send requires a <host> argument" >&2; exit 1; }
      SEND_HOST="$2"
      shift
      ;;
    --send-port)
      [[ $# -ge 2 ]] || { echo "Error: --send-port requires a <port> argument" >&2; exit 1; }
      SEND_PORT="$2"
      shift
      ;;
    --receive)    RECEIVE_MODE=true ;;
    --port)
      [[ $# -ge 2 ]] || { echo "Error: --port requires a <port> argument" >&2; exit 1; }
      RECEIVE_PORT="$2"
      shift
      ;;
    -h|--help)  usage; exit 0 ;;
    *)          echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
  shift
done

modes=0
$SIMPLE_OUT   && modes=$((modes + 1))
$OAUTH_ONLY   && modes=$((modes + 1))
$DO_REFRESH   && modes=$((modes + 1))
[[ -n "$IMPORT_PATH" ]] && modes=$((modes + 1))
[[ -n "$SEND_HOST" ]]   && modes=$((modes + 1))
$RECEIVE_MODE           && modes=$((modes + 1))
if (( modes > 1 )); then
  echo "Error: --simple, --oauth-only, --refresh, --import, --send, and --receive are mutually exclusive" >&2
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

  content="$(cat "$tmpfile")"
  content="$(printf '%s' "$content" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  security add-generic-password -U -a "$account" -s "$SERVICE" -w "$content"

  bytes=$(printf '%s' "$content" | wc -c | tr -d ' ')
  echo "Imported $bytes bytes to keychain service '$SERVICE' (account: $account)" >&2
  exit 0
fi

# --send: read keychain bytes and send as a single UDP datagram.
# UDP is connectionless — we just fire and forget. Exit 0 regardless of
# whether a receiver is listening. The credential blob is typically < 4 KB,
# well under the macOS 9216-byte default socket buffer and the 65507-byte
# UDP theoretical max.
if [[ -n "$SEND_HOST" ]]; then
  keychain_bytes=$(security find-generic-password -s "$SERVICE" -w 2>/dev/null) || {
    echo "Error: No credentials found in Keychain" >&2; exit 1
  }
  byte_count=$(printf '%s' "$keychain_bytes" | wc -c | tr -d ' ')

  printf '%s' "$keychain_bytes" | python3 -c '
import socket, sys
data = sys.stdin.buffer.read()
host = sys.argv[1]
port = int(sys.argv[2])
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.sendto(data, (host, port))
sock.close()
' "$SEND_HOST" "$SEND_PORT"

  echo "Sent $byte_count bytes to $SEND_HOST:$SEND_PORT via UDP" >&2
  exit 0
fi

# --receive: bind a UDP socket, accept one datagram, write to keychain.
# One-shot: read a single datagram, write it, exit. Not a daemon.
# Note: macOS will prompt for firewall access the first time python3 binds
# a non-standard port. Default port 47299 is in the IANA dynamic range.
if $RECEIVE_MODE; then
  echo "Listening for one UDP datagram on port $RECEIVE_PORT..." >&2
  echo "Note: macOS may prompt for firewall access the first time you --receive" >&2

  received=$(python3 -c '
import socket, sys
port = int(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", port))
data, addr = sock.recvfrom(65535)
sock.close()
sys.stdout.buffer.write(data)
' "$RECEIVE_PORT") || {
    echo "Error: Failed to bind UDP socket on port $RECEIVE_PORT" >&2; exit 1
  }

  if [[ -z "$received" ]]; then
    echo "Error: Received empty datagram" >&2; exit 1
  fi

  # Reuse the --import write path: discover account, trim, write.
  account=$(security find-generic-password -s "$SERVICE" 2>/dev/null \
    | grep '"acct"' | sed 's/.*<blob>="\{0,1\}//' | sed 's/"\{0,1\}$//')

  if [[ -z "$account" ]]; then
    echo "Error: No existing keychain entry for service '$SERVICE'. Run 'claude login' first." >&2
    exit 1
  fi

  content="$(printf '%s' "$received" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  security add-generic-password -U -a "$account" -s "$SERVICE" -w "$content"

  bytes=$(printf '%s' "$content" | wc -c | tr -d ' ')
  echo "Received and imported $bytes bytes to keychain service '$SERVICE' (account: $account)" >&2
  exit 0
fi

# --oauth-only: extract and output just the claudeAiOauth section.
if $OAUTH_ONLY; then
  keychain_out=$(security find-generic-password -s "$SERVICE" -w 2>/dev/null) || {
    echo "Error: No credentials found in Keychain" >&2; exit 1
  }
  if [[ "$keychain_out" =~ ^\{ ]]; then
    raw="$keychain_out"
  else
    raw=$(echo "$keychain_out" | xxd -r -p)
  fi
  echo "$raw" | jq -c '{claudeAiOauth: .claudeAiOauth}'
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
