# ade-dedrm

> A single-command CLI that turns Adobe Digital Editions (ADE) `.acsm`
> fulfillment tickets and Adept-DRM-protected EPUB / PDF files into
> clean, readable copies.

**한국어**: [README.ko.md](./README.ko.md)

- DRM removal core is a trimmed port of `ineptepub` and `ineptpdf` from
  [noDRM/DeDRM_tools](https://github.com/noDRM/DeDRM_tools).
- ACSM fulfillment is a trimmed port of libgourou from
  [acsm-calibre-plugin (DeACSM)](https://github.com/Leseratte10/acsm-calibre-plugin).
- No Calibre plugin required. Runs anywhere Python 3.12 runs — scripts,
  cron jobs, headless servers, CI.

## Features

| Task | Subcommand | Output |
|---|---|---|
| Import an existing local ADE activation (macOS) | `import-ade` | 3 state files under `~/.config/ade-dedrm/` |
| Extract your Adobe RSA user key (`.der`) | `extract-key` | `adobekey.der` used by `decrypt` |
| ACSM → encrypted book download | `fulfill` | `.epub` or `.pdf` (still DRM-wrapped) |
| Remove Adept DRM (EPUB/PDF auto-detected) | `decrypt` | DRM-free file |
| **`fulfill` + `decrypt` in one shot** | `process` | DRM-free file (recommended) |

## Installation

```bash
git clone https://github.com/yun-sangho/ade-dedrm.git
cd ade-dedrm
uv sync
```

Python 3.12 is required. `uv` installs and pins it automatically.

## Quick start (macOS)

Assuming Adobe Digital Editions is already installed and authorized with
your Adobe ID:

```bash
# 1. Bootstrap the state directory from your local ADE install (run once)
uv run ade-dedrm import-ade

# 2. Export your user RSA key (used for DRM removal)
uv run ade-dedrm extract-key -o ~/adobekey.der

# 3. Turn a purchased .acsm into a clean EPUB or PDF in one shot
uv run ade-dedrm process ~/Downloads/book.acsm -k ~/adobekey.der
# → ~/Downloads/book.epub (or book.pdf)
```

## Subcommand reference

### `import-ade` — bootstrap state from a local ADE install (macOS only)

Combines `~/Library/Application Support/Adobe/Digital Editions/activation.dat`
with the `DeviceKey` / `DeviceFingerprint` entries stored in the macOS
keychain, producing `~/.config/ade-dedrm/{devicesalt, device.xml, activation.xml}`.

```bash
uv run ade-dedrm import-ade [--force]
```

- **Precondition**: ADE is installed and you have already completed
  Help → Authorize Computer with your Adobe ID.
- macOS may prompt for keychain access when reading the device secrets.
- `--force`: overwrite an existing `~/.config/ade-dedrm/` directory.
- **State directory location** can be overridden with `$ADE_DEDRM_HOME`
  (default: `$XDG_CONFIG_HOME/ade-dedrm`, falling back to `~/.config/ade-dedrm`).

### `extract-key` — export your Adobe RSA user key

```bash
uv run ade-dedrm extract-key [-o PATH] [--force]
```

- Parses `activation.dat` to recover the Adobe RSA private key and writes
  it as a `.der` file.
- Default output: `./adobekey.der`.
- This key is what `decrypt` / `process` need to strip Adept DRM.

### `fulfill` — ACSM → encrypted EPUB / PDF

```bash
uv run ade-dedrm fulfill INPUT.acsm [-o OUTPUT] [--force]
```

- Parses the `.acsm` ticket to extract `operatorURL`, then builds an
  Adobe-style tree-hash + textbook-RSA-signed fulfillment request and
  POSTs it to the ACS4 server.
- Downloads the encrypted file from the response and injects either
  `META-INF/rights.xml` (EPUB) or a patched `/ADEPT_LICENSE` object (PDF)
  so downstream DRM removal sees a complete Adept-protected file.
- **The output extension is chosen automatically** based on what the
  server returned — `.epub` or `.pdf`.
- The output is still DRM-wrapped; you still need `decrypt` to actually
  read it.
- **Precondition**: `import-ade` must have populated the state directory.

### `decrypt` — remove Adept DRM (EPUB / PDF auto-detected)

```bash
uv run ade-dedrm decrypt -k KEY.der INPUT [-o OUTPUT] [--force]
```

- Auto-detects the input format from magic bytes (`PK…` or `%PDF-`).
- **EPUB**: per-entry AES-CBC decrypt → strip PKCS#7 padding → zlib
  inflate → re-pack the ZIP without the Adept bits in `encryption.xml`.
- **PDF**: unwrap the RSA-encrypted book key from `/ADEPT_LICENSE`,
  decrypt every stream/string object, then re-serialize the PDF with
  `/Encrypt` removed.
- Default output: `<input>.nodrm.<ext>`.

### `process` — `fulfill` + `decrypt` in one shot (recommended)

```bash
uv run ade-dedrm process INPUT.acsm -k KEY.der [-o OUTPUT] [--force]
```

- Internally runs `fulfill` into a temp file and immediately `decrypt`s
  it into the final output. No intermediate DRM file is left on disk.
- `-k` is optional — if omitted, the tool will look for
  `<state_dir>/adobekey.der` or fall back to running `extract-key` on
  the fly.

## Usage examples

### Everyday workflow

```bash
# One-time setup
uv run ade-dedrm import-ade
uv run ade-dedrm extract-key -o ~/adobekey.der

# Every future purchase is a single command
uv run ade-dedrm process ~/Downloads/new_book.acsm \
    -k ~/adobekey.der -o ~/Books/new_book.epub
```

### Decrypt files you already downloaded

If you only have the encrypted file (no `.acsm`):

```bash
uv run ade-dedrm decrypt -k ~/adobekey.der encrypted_book.epub
uv run ade-dedrm decrypt -k ~/adobekey.der encrypted_book.pdf
```

### Alternate state directory

Useful for sandboxed tests or multiple activations:

```bash
export ADE_DEDRM_HOME=$(mktemp -d)
uv run ade-dedrm import-ade
uv run ade-dedrm process book.acsm -k /path/to/key.der
```

## Cross-platform status

Roughly 95% of the source is pure Python and works on any platform
`cryptography` + `pycryptodome` + `lxml` do. The only OS-specific piece
is how the initial state files are obtained:

| Area | macOS | Linux | Windows |
|---|---|---|---|
| DRM removal (`decrypt`) | ✅ | ✅ | ✅ |
| ACSM fulfillment (`fulfill` / `process`) | ✅ | ✅ | ✅ |
| State bootstrap (`import-ade`) | ✅ | ❌ | ❌ |
| State bootstrap (`activate`) | 🗓 planned | 🗓 planned | 🗓 planned |

A macOS user can run `import-ade` once and copy the resulting
`~/.config/ade-dedrm/` directory to any Linux or Windows machine —
everything downstream of that state will work without changes.

**Roadmap to true cross-platform**: Tier 3 adds an `ade-dedrm activate
--anonymous` / `--adobe-id` subcommand that registers a fresh ADE device
directly against Adobe's ACS4 servers, removing the dependency on a
local ADE install entirely. Detailed plan:
[`docs/tier3-activate-plan.md`](./docs/tier3-activate-plan.md).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Input file is not Adept-DRM protected |
| 2 | Wrong key / decryption failure |
| 3 | I/O problem (file missing, refuse-to-overwrite, TTY missing, etc.) |
| 4 | ACSM fulfillment failed (network, server error, unsupported response) |

## Troubleshooting

### `E_GOOGLE_DEVICE_LIMIT_REACHED` (Google Play Books)

Your Google Play Books account has hit its per-account ACS4 device slot
limit. This is a **server-side rejection** — the CLI itself is working
correctly. Resolve it on the Google side:

1. `play.google.com/books` → Settings → Devices, deauthorize devices you
   no longer use.
2. If the UI won't let you free a slot, contact Google Play support and
   ask them to "reset the ACS4 device activation count for my Google
   Play Books account".

After freeing a slot, download a fresh `.acsm` from Play Books and retry.

### `E_ADEPT_DISTRIBUTOR_AUTH`

The tool already retries this automatically. If it persists, re-run
`import-ade` to refresh the state directory, or check the Adobe account
device limit at `account.adobe.com`.

### `wrong key` / `decryption failed`

The `.der` file from `extract-key` is from a different ADE activation
than the one that fulfilled the book. Re-extract from the `activation.dat`
that belongs to the same Adobe ID.

### `%PDF-` file but no `EBX_HANDLER` / `ADEPT_LICENSE` inside

That PDF isn't Adept-protected — it's using a different scheme (Apple
FairPlay, Amazon, a password handler, etc.). Out of scope for this tool.

## Testing

```bash
uv run pytest tests/ -q
```

26 cases covering:

- Adobe tree hash + signing (byte-for-byte matching the DeACSM reference).
- pkcs12 unwrap, state directory resolution.
- PDF patch helpers (backward reader, trailer parsing, `/ADEPT_LICENSE`
  injection).
- PDF parser primitives and the "not DRM" branch on a synthetic PDF.
- Synthetic EPUB roundtrip: RSA unwrap → AES-CBC → zlib inflate →
  ZIP rebuild.

Actual ACSM fulfillment and actual DRM removal require Adobe / Google
servers, so they can't run in CI — verify them manually with a real
purchased `.acsm` (see the quick start above).

## License

**GPL v3.** This project ports code from DeDRM_tools and DeACSM, both of
which are GPL v3, so the copyleft is inherited. See [`NOTICE`](./NOTICE)
for the full attribution chain.

## Legal notice

This tool is intended for **personal backups and accessibility** of
EPUB / PDF files you have **legitimately purchased**. Using it on books
you did not buy, library loans, borrowed copies, or anyone else's
content may violate copyright law and anti-circumvention statutes in
your jurisdiction (for example, the Korean Copyright Act §104-2, the
US DMCA §1201, the EU Copyright Directive Article 6). You are solely
responsible for how you use this software.

The authors provide this software as-is, without any warranty, and
accept no liability for any consequences of its use.
