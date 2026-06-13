# Encrypted credential transfer (--send / --receive)

## Context

`claude-credentials.sh` and the `claude-monitor-credentials` Python CLI both
move the Claude OAuth keychain blob between machines over a one-shot TCP
connection (`--send` / `--receive`, with `--oauth-only` to strip
machine-specific keys). Today that transfer is **plaintext and
unauthenticated**: a LAN peer can sniff the tokens, and `--receive` writes the
first connection it accepts into the keychain, so a peer can race the intended
sender and overwrite credentials. An independent review flagged this as
critical.

This spec adds confidentiality + authentication via a shared passphrase, closing
both holes. Encryption becomes **mandatory** for `--send`/`--receive` — there is
no plaintext path after this change, and the interim plaintext warning shipped
alongside the TCP work is removed.

### Hard constraint: two independent implementations

`claude-credentials.sh` is treated as a **fully standalone file**. It may rely
only on stock-macOS tooling — `bash`, `openssl` (which on macOS is LibreSSL),
the system `python3` (3.9, **stdlib only** — no `cryptography`), and `security`.
It must NOT import anything from the `claude_monitor` package or assume a venv.

The `claude_monitor.cli_credentials` CLI lives inside the package and may use the
`cryptography` library (a new dependency).

The two implementations interoperate by agreeing on one **wire format** — the
same standard, two libraries — so `bash --send` ↔ `python --receive` (and the
reverse) both work.

## Crypto construction

Authenticated encryption via **AES-256-CBC + HMAC-SHA256, encrypt-then-MAC**.
GCM is deliberately avoided: the `openssl enc` CLI cannot safely emit/consume the
GCM auth tag, and the bash side has only the openssl CLI for AES.

### Key derivation (self-managed, identical on both sides)

```
salt        = 16 random bytes
material    = PBKDF2-HMAC-SHA256(passphrase, salt, iterations=600000, dklen=80)
enc_key     = material[0:32]    # AES-256 key
iv          = material[32:48]   # AES-CBC IV
mac_key     = material[48:80]   # HMAC-SHA256 key
```

One KDF call; the three keys are independent slices of its output (domain
separation by position). We derive key material ourselves rather than relying on
openssl's internal `-pbkdf2`/`Salted__` derivation, which varies across
LibreSSL/OpenSSL versions and is the fragile part of polyglot AES interop.

### Encrypt (send)

```
ciphertext  = AES-256-CBC(enc_key, iv, PKCS7-pad(plaintext))
tag         = HMAC-SHA256(mac_key, salt || ciphertext)
```

- Python CLI: `cryptography` (`Cipher(AES, CBC)`, `padding.PKCS7`).
- bash: `openssl enc -aes-256-cbc -K <hex(enc_key)> -iv <hex(iv)> -nosalt`
  (PKCS7 is openssl's default block padding). PBKDF2, HMAC, and the base64
  framing are done by the embedded system `python3` (`hashlib.pbkdf2_hmac`,
  `hmac`, `base64.b64encode` — all stdlib), which emits **single-line**
  (unwrapped) base64 so the two-line frame is unambiguous. openssl is used
  only for the AES-CBC step.

### Wire format

Two newline-separated lines, ASCII-safe so the existing line-oriented TCP path
is unchanged:

```
line 1:  base64( salt(16) || ciphertext )
line 2:  hex( HMAC-SHA256 tag )           (64 hex chars)
```

### Decrypt (receive)

1. Split the received bytes into the two lines; base64-decode line 1 → `salt`
   (first 16 bytes) + `ciphertext`; hex-decode line 2 → `tag`.
2. Re-derive `enc_key`/`iv`/`mac_key` from passphrase + `salt`.
3. Recompute `HMAC-SHA256(mac_key, salt || ciphertext)` and compare to `tag`
   with a **constant-time** comparison (`hmac.compare_digest`).
4. **Mismatch → reject**: print a clear error, exit non-zero, **leave the
   keychain untouched.** A wrong passphrase, a tampered payload, or a garbage
   connection from a racing peer all fail here, before any decrypt or write.
5. Only on a valid tag: AES-CBC-decrypt, PKCS7-unpad, strip, write to keychain
   via the existing import path.

This closes both the interception risk (confidentiality) and the overwrite-race
(an unauthenticated peer cannot produce a valid tag).

## Passphrase handling

- Source: env `CLAUDE_CREDENTIALS_PASSPHRASE` if set; otherwise prompt
  interactively with no echo (`read -s` in bash, `getpass.getpass` in Python).
- **Never** a CLI flag (would leak via `ps` and shell history).
- The passphrase is passed to the embedded `python3` in bash via the
  environment, never as an argv argument. (The derived AES key *is* passed to
  `openssl` via `-K` in argv and is briefly visible in `ps` to other local users
  — an accepted tradeoff for this personal-LAN tool; the passphrase itself never
  hits argv.)
- Missing passphrase (env unset and non-interactive stdin) → clear error, exit
  non-zero.

## Scope of change

- `claude_monitor/cli_credentials.py`: encrypt on `--send`, decrypt+verify on
  `--receive`; require passphrase. The existing TCP transport, idle timeout, and
  payload cap are unchanged (the encrypted frame is marginally larger and stays
  well under the cap). `--oauth-only` still selects the plaintext payload that is
  then encrypted. While rewriting `_do_send`, wrap `connect()` errors with
  host:port context (e.g. `Error: could not connect to {host}:{port} — {e}`) for
  parity with the bash script, which already does this — the current Python path
  lets the raw `OSError` bubble to the generic handler and drops the host:port.
- `claude-credentials.sh`: same, using `openssl` + embedded `python3`.
- `pyproject.toml`: add `cryptography` to `dependencies`.
- Remove the interim "plaintext / unauthenticated" warning from both help texts
  (no longer true); update CHANGELOG.
- Help text documents the passphrase env var and that both ends must share it.

## Testing

- **Python unit (in-process):** round-trip encrypt→decrypt; wrong passphrase
  rejected; single-byte tamper of ciphertext/tag rejected; missing passphrase
  errors; `--oauth-only` payload survives the round-trip.
- **Bash:** real-loopback `--send`→`--receive` round-trip with a passphrase
  (extends the existing real-TCP tests); wrong-passphrase receiver rejects and
  leaves the keychain shim uncalled.
- **Interop (first-class — the one spot that can silently break):** a test that
  encrypts with the **real macOS `openssl` binary** and decrypts with
  `cryptography`, and the reverse (encrypt with `cryptography`, decrypt via the
  bash path). Asserts byte-level interoperability against the actual LibreSSL
  binary rather than assuming it.
- Known-answer vectors for PBKDF2 (fixed passphrase+salt → expected 80-byte
  material) shared by both test suites to prove identical key derivation.

## Out of scope

- GCM / AEAD ciphers (CLI limitation; see above).
- Key exchange, certificates, or a daemon — this stays a one-shot,
  passphrase-symmetric transfer ("not a full HTTP server").
- Changing the TCP transport, port, idle timeout, or payload cap from PR2.

## Build sequence

Single PR (stacked on the credentials-helper PR). Suggested commit order:
1. Shared crypto helpers + unit tests (Python) and the bash crypto block, with
   the interop + KDF-vector tests — proves the format before wiring it in.
2. Wire encryption into `--send`/`--receive` on both sides; make passphrase
   mandatory; remove the plaintext warning; update CHANGELOG + help.
