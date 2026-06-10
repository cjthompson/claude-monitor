#!/usr/bin/env bash
# Manage the Claude Code OAuth token in the macOS Keychain.
#
# Usage: ./claude-credentials.sh [--raw | --simple | --refresh | --oauth-only]
#        ./claude-credentials.sh --import <file|->
#        ./claude-credentials.sh [--oauth-only] --send <host> [--send-port <port>]

set -euo pipefail

SERVICE="Claude Code-credentials"
TOKEN_URL="https://platform.claude.com/v1/oauth/token"
CLIENT_ID="9d1c250a-e61b-44d9-88ed-5944d1962f5e"

RAW_OUT=false
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
Usage: $(basename "$0") [--raw | --simple | --refresh | --oauth-only]
       $(basename "$0") --import <file|->
       $(basename "$0") [--oauth-only] --send <host> [--send-port <port>]
       $(basename "$0") --receive [--port <port>]

Modes:
  (no args)    Print this help (does not dump credentials by default).

  --raw        Print raw keychain bytes for '$SERVICE' (no decoding).

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

  --send <host>     Encrypt the keychain bytes and send them over a TCP
                    connection to <host> on the configured port. With
                    --oauth-only, send only the claudeAiOauth section.
                    Default port: 47299. Override with --send-port.
                    The receiver must be running --receive first; if nothing
                    is listening, --send fails with a connection error.

  --receive         Listen for ONE TCP connection on the configured port
                    (default 47299, override with --port), verify+decrypt the
                    payload, then write it to the keychain (replacing the
                    existing entry, same write path as --import) and exit.
                    Receiver is one-shot — it is NOT a daemon.
                    Note: macOS may prompt for firewall access the first
                    time you --receive, since python3 is listening on a non-
                    standard port.

  --send-port <port>  Override the destination port for --send (default 47299).
  --port <port>       Override the listening port for --receive (default 47299).

Default port 47299 is "claude credentials" (4+7+2+9+9). It sits in the
IANA registered-port range (1024-49151) but is not assigned to a common
service, so collisions are unlikely.

ENCRYPTION: --send/--receive are end-to-end encrypted (AES-256-CBC +
HMAC-SHA256) with a shared passphrase. Set CLAUDE_CREDENTIALS_PASSPHRASE on
both ends, or you will be prompted. Both ends must use the same passphrase; a
wrong passphrase or a tampered/forged payload is rejected and the keychain is
left untouched.

--raw, --simple, --refresh, --import, --send, and --receive are mutually exclusive.
--oauth-only can be used alone or with --send.
EOF
}

# Resolve the transfer passphrase into $PASS: env first, else an interactive
# no-echo prompt. Never accepted on the command line (would leak via ps/history).
get_passphrase() {
  if [[ -n "${CLAUDE_CREDENTIALS_PASSPHRASE:-}" ]]; then
    PASS="$CLAUDE_CREDENTIALS_PASSPHRASE"
  elif [[ -t 0 ]]; then
    read -rsp "Passphrase: " PASS </dev/tty; echo >&2
  else
    echo "Error: no passphrase: set CLAUDE_CREDENTIALS_PASSPHRASE or run interactively" >&2
    exit 1
  fi
  # An empty shared secret would defeat encryption — reject it (matches the
  # env path, where an unset/empty var is already treated as missing).
  if [[ -z "$PASS" ]]; then
    echo "Error: empty passphrase not allowed" >&2
    exit 1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --raw)        RAW_OUT=true ;;
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

primary_modes=0
$RAW_OUT              && primary_modes=$((primary_modes + 1))
$SIMPLE_OUT           && primary_modes=$((primary_modes + 1))
$DO_REFRESH           && primary_modes=$((primary_modes + 1))
[[ -n "$IMPORT_PATH" ]] && primary_modes=$((primary_modes + 1))
[[ -n "$SEND_HOST" ]]   && primary_modes=$((primary_modes + 1))
$RECEIVE_MODE           && primary_modes=$((primary_modes + 1))
if (( primary_modes > 1 )); then
  echo "Error: --raw, --simple, --refresh, --import, --send, and --receive are mutually exclusive" >&2
  exit 1
fi

if $OAUTH_ONLY && { $RAW_OUT || $SIMPLE_OUT || $DO_REFRESH || [[ -n "$IMPORT_PATH" ]] || $RECEIVE_MODE; }; then
  echo "Error: --oauth-only can only be used by itself or with --send" >&2
  exit 1
fi

keychain_json_from_bytes() {
  local keychain_out="$1"
  if [[ "$keychain_out" =~ ^\{ ]]; then
    printf '%s' "$keychain_out"
  else
    printf '%s' "$keychain_out" | xxd -r -p
  fi
}

oauth_only_from_bytes() {
  local keychain_out="$1"
  keychain_json_from_bytes "$keychain_out" | jq -c '{claudeAiOauth: .claudeAiOauth}'
}

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

# --send: encrypt the keychain bytes, then send the frame over a TCP connection.
# TCP confirms delivery — connect() fails if no receiver is listening. The
# receiver must be running --receive first. Encryption: see transfer_crypto.py
# (AES-256-CBC + HMAC-SHA256, PBKDF2). AES is done by openssl; KDF/HMAC/base64
# by the system python3 stdlib — both stock on macOS, no extra deps.
if [[ -n "$SEND_HOST" ]]; then
  keychain_bytes=$(security find-generic-password -s "$SERVICE" -w 2>/dev/null) || {
    echo "Error: No credentials found in Keychain" >&2; exit 1
  }
  if $OAUTH_ONLY; then
    send_payload=$(oauth_only_from_bytes "$keychain_bytes")
    mode_note=" (oauth-only)"
  else
    send_payload="$keychain_bytes"
    mode_note=""
  fi
  get_passphrase

  # Encrypt -> wire frame (base64(salt||ciphertext)\nhex(hmac)).
  frame=$(printf '%s' "$send_payload" | CRED_PASS="$PASS" python3 -c '
import base64, hashlib, hmac, os, subprocess, sys
pw = os.environ["CRED_PASS"].encode()
data = sys.stdin.buffer.read()
salt = os.urandom(16)
material = hashlib.pbkdf2_hmac("sha256", pw, salt, 600000, 80)
enc_key, iv, mac_key = material[0:32], material[32:48], material[48:80]
ct = subprocess.run(
    ["openssl", "enc", "-aes-256-cbc", "-K", enc_key.hex(), "-iv", iv.hex(), "-nosalt"],
    input=data, capture_output=True, check=True).stdout
blob = salt + ct
tag = hmac.new(mac_key, blob, hashlib.sha256).hexdigest()
sys.stdout.write(base64.b64encode(blob).decode() + "\n" + tag + "\n")
')

  # Send the frame over TCP.
  printf '%s' "$frame" | python3 -c '
import socket, sys
data = sys.stdin.buffer.read()
host = sys.argv[1]
port = int(sys.argv[2])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(10)
try:
    sock.connect((host, port))
    sock.sendall(data)
except OSError as e:
    sys.exit(f"Error: could not connect to {host}:{port} — {e}")
finally:
    sock.close()
' "$SEND_HOST" "$SEND_PORT"

  byte_count=$(printf '%s' "$frame" | wc -c | tr -d ' ')
  echo "Sent $byte_count encrypted bytes to $SEND_HOST:$SEND_PORT via TCP$mode_note" >&2
  exit 0
fi

# --receive: listen for one TCP connection, read it fully, write to keychain.
# One-shot: accept a single connection, write it, exit. Not a daemon.
# Note: macOS will prompt for firewall access the first time python3 listens
# on a non-standard port. Default port 47299 is an unassigned registered port.
if $RECEIVE_MODE; then
  get_passphrase  # up front: prompt (if any) and fail fast before listening
  echo "Listening for one TCP connection on port $RECEIVE_PORT..." >&2
  echo "Note: macOS may prompt for firewall access the first time you --receive" >&2

  received=$(python3 -c '
import os, socket, sys
def _int_env(name, default):
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default
# Bound a hostile/buggy peer: drop an idle connection and refuse a stream larger
# than any real credential blob (~11 KB) so it cannot exhaust memory.
RECV_TIMEOUT = _int_env("CLAUDE_CREDENTIALS_RECV_TIMEOUT", 30)
MAX_PAYLOAD = _int_env("CLAUDE_CREDENTIALS_MAX_PAYLOAD", 1024 * 1024)
port = int(sys.argv[1])
srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", port))
srv.listen(1)
conn, addr = srv.accept()
conn.settimeout(RECV_TIMEOUT)
chunks = []
total = 0
while True:
    try:
        block = conn.recv(65535)
    except socket.timeout:
        sys.exit("Error: connection idle for %ds; aborting" % RECV_TIMEOUT)
    if not block:
        break
    total += len(block)
    if total > MAX_PAYLOAD:
        sys.exit("Error: payload exceeds %d bytes; aborting" % MAX_PAYLOAD)
    chunks.append(block)
conn.close()
srv.close()
sys.stdout.buffer.write(b"".join(chunks))
' "$RECEIVE_PORT") || {
    echo "Error: receive failed on TCP port $RECEIVE_PORT" >&2; exit 1
  }

  if [[ -z "$received" ]]; then
    echo "Error: Received empty connection" >&2; exit 1
  fi

  # Verify+decrypt BEFORE touching the keychain. A wrong passphrase or a
  # forged/tampered frame is rejected here, leaving credentials untouched.
  content=$(printf '%s' "$received" | CRED_PASS="$PASS" python3 -c '
import base64, hashlib, hmac, os, subprocess, sys
pw = os.environ["CRED_PASS"].encode()
lines = [ln for ln in sys.stdin.read().splitlines() if ln.strip()]
if len(lines) != 2:
    sys.exit("Error: malformed transfer frame")
try:
    blob = base64.b64decode(lines[0], validate=True)
except Exception:
    sys.exit("Error: malformed transfer frame")
if len(blob) <= 16:
    sys.exit("Error: malformed transfer frame")
tag_hex = lines[1].strip()
if len(tag_hex) != 64 or any(c not in "0123456789abcdef" for c in tag_hex.lower()):
    sys.exit("Error: malformed transfer frame")
salt, ct = blob[:16], blob[16:]
material = hashlib.pbkdf2_hmac("sha256", pw, salt, 600000, 80)
enc_key, iv, mac_key = material[0:32], material[32:48], material[48:80]
expected = hmac.new(mac_key, blob, hashlib.sha256).hexdigest()
if not hmac.compare_digest(expected, tag_hex):
    sys.exit("Error: authentication failed (wrong passphrase or corrupted/forged data)")
pt = subprocess.run(
    ["openssl", "enc", "-d", "-aes-256-cbc", "-K", enc_key.hex(), "-iv", iv.hex(), "-nosalt"],
    input=ct, capture_output=True, check=True).stdout
sys.stdout.buffer.write(pt)
') || {
    echo "Keychain left unchanged." >&2; exit 1
  }
  content="$(printf '%s' "$content" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"

  # Reuse the --import write path: discover account, write.
  account=$(security find-generic-password -s "$SERVICE" 2>/dev/null \
    | grep '"acct"' | sed 's/.*<blob>="\{0,1\}//' | sed 's/"\{0,1\}$//')

  if [[ -z "$account" ]]; then
    echo "Error: No existing keychain entry for service '$SERVICE'. Run 'claude login' first." >&2
    exit 1
  fi

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
  oauth_only_from_bytes "$keychain_out"
  exit 0
fi

# --raw: pass-through of `security -w` bytes, exactly as Claude Code wrote them.
if $RAW_OUT; then
  security find-generic-password -s "$SERVICE" -w
  exit $?
fi

# No mode selected (e.g. no arguments) → show help instead of dumping the blob.
if ! $SIMPLE_OUT && ! $DO_REFRESH; then
  usage
  exit 0
fi

# --simple and --refresh both need the keychain bytes parsed as JSON.
keychain_out=$(security find-generic-password -s "$SERVICE" -w 2>/dev/null) || {
  echo "Error: No credentials found in Keychain" >&2
  exit 1
}

# Output may be hex-encoded or raw JSON depending on macOS version
raw=$(keychain_json_from_bytes "$keychain_out")

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
