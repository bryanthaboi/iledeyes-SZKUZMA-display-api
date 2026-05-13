# displayathon

A two-process driver for the **iledeyes-\*** Bluetooth LED display (96×16 RGB332 panel).

## The hardware

SZKUZMA flexible LED matrix · 96×16 USB-powered Bluetooth panel · advertises as `iledeyes-*`. Sold on Amazon: <https://a.co/d/03oFzrzX>. Stock companion app (iLEDColor) sucked, hence this.

| Piece | What it is |
|---|---|
| **`displayathon-service`** | Long-running HTTP service that owns the Bluetooth connection. Accepts solid colors, gradient fades, GIFs, and scrolling text via a small REST API. Runs forever under `launchd`. Reachable on the LAN. |
| **`displayathon.app`** | Native macOS desktop window built with NiceGUI. A thin client over the service; opens any time you want to drive the display from a UI. |

Both ship as standalone PyInstaller binaries — **no system Python, no venv, no pip** required to run them. The wire-level Bluetooth protocol lives in `displayathon.py` and is reused unchanged.

---

## Quick start

```bash
./build.sh                     # one-time, ~2 min — produces dist/*
./service/install.sh           # installs + starts the launchd agent
open dist/displayathon.app     # launch the desktop UI
```

Once installed, the service runs on every login, restarts automatically on crash, and survives reboots. The `.app` can be opened any time you want a UI; the API is always available regardless.

```bash
# verify it's running:
curl -s http://127.0.0.1:49696/api/health
```

---

## Project layout

```
displayathon.py            Bluetooth protocol + payload builders
display_service.py         FastAPI service — owns the BLE lock
display_client.py          httpx wrapper used by the desktop app
app.py                     NiceGUI native window
build.sh                   builds the two standalone artifacts
packaging/
  service.spec               PyInstaller spec for displayathon-service
  app.spec                   PyInstaller spec for displayathon.app
service/
  install.sh                 installs the service with launchd
  uninstall.sh               removes the service
fonts/
  m6x11plus.ttf              bundled fonts for the Text mode
requirements.txt           pinned Python deps (build-time only)
dist/                      build outputs (generated)
  displayathon-service       standalone CLI binary (the service)
  displayathon.app           standalone macOS app bundle (the UI)
```

---

## Building from source

Prerequisites (build-time only):
- macOS, Apple Silicon or Intel
- Python 3.11+

```bash
./build.sh
```

The build script reuses a `.venv/` directory if present, otherwise it creates a throwaway `.build-venv/`. Either way you don't need to activate anything yourself. The resulting binaries in `dist/` are fully self-contained.

Outputs:
- `dist/displayathon-service` — ~23 MB CLI binary
- `dist/displayathon.app` — ~128 MB Mac app bundle

---

## Installing the service

```bash
./service/install.sh
```

What it does:

1. **Picks the binary mode** if `dist/displayathon-service` exists (preferred). Falls back to `.venv/bin/python display_service.py` for dev iteration if only a venv is present.
2. **Copies the binary** to `~/Library/displayathon/bin/displayathon-service` so the launchd plist points at a stable path (independent of the source tree).
3. **Syncs bundled fonts** from `fonts/*.ttf` into `~/Library/displayathon/fonts/`. The service also reads this directory at runtime — drop new `.ttf`/`.otf`/`.ttc` files in there any time, no reinstall needed.
4. **Writes the launchd plist** to `~/Library/LaunchAgents/com.boisclubgames.displayathon.plist` with `KeepAlive=true` + `RunAtLoad=true` (auto-restart on crash, comes up on login).
5. **Bootstraps + kickstarts** the agent so the service is immediately running.
6. **Strips quarantine xattrs** and ad-hoc re-signs the binary so Gatekeeper doesn't refuse to launch it under launchd.

Verify:

```bash
curl -s http://127.0.0.1:49696/api/health
```

You should see something like:
```json
{"ok":true,"version":"0.1.0","busy":false,"uptime_s":12,"last_upload_ts":0,"port":49696}
```

### Service management

```bash
# logs
tail -f ~/Library/Logs/displayathon/out.log
tail -f ~/Library/Logs/displayathon/err.log

# status
launchctl print gui/$(id -u)/com.boisclubgames.displayathon

# manual restart
launchctl kickstart -k gui/$(id -u)/com.boisclubgames.displayathon

# re-install (after rebuild or path changes)
./service/install.sh

# uninstall
./service/uninstall.sh
```

---

## Using the desktop app

```bash
open dist/displayathon.app             # native window (default)
```

Or in browser-tab fallback mode (useful for debugging):
```bash
dist/displayathon.app/Contents/MacOS/displayathon --web
```

Four tabs:

- **Solid** — pick one color, fill the display
- **Fade** — pick two colors, ship a 40-frame ping-pong gradient
- **GIF** — drop in any `.gif` (the service resizes to 96×16 if needed)
- **Text** — scrolling text via the JavaScript canvas → byte-identical to what the API renders

A status bar at the bottom polls `/api/health` every 2 seconds and disables the Send buttons when the service is unreachable.

To point the app at a different service host: `DISPLAYATHON_URL=http://other-host:49696 open dist/displayathon.app`.

---

## API reference

The service listens on **`http://<host>:49696`** by default, bound to `0.0.0.0` — reachable from any machine on the same LAN. There is no auth.

All endpoints return JSON `{ok: bool, message: str, meta: {...}}` unless noted. Errors return the same shape with a relevant HTTP status code.

### `GET /api/health`

```bash
curl -s http://127.0.0.1:49696/api/health
```
```json
{"ok":true,"version":"0.1.0","busy":false,"uptime_s":42,"last_upload_ts":0,"port":49696}
```

### `GET /api/fonts`

Lists all fonts the service can use for `/api/text`. Sources: `user` (`~/Library/displayathon/fonts/`), `bundled` (inside the binary), `system` (macOS fonts).

```bash
curl -s http://127.0.0.1:49696/api/fonts
```

### `GET /api/fonts/{name}/file`

Returns the raw TTF/OTF bytes for the named font. Used internally by the UI's canvas; you can also fetch it from any browser.

### `POST /api/solid`

```bash
curl -X POST http://127.0.0.1:49696/api/solid \
  -H 'content-type: application/json' \
  -d '{"color":"#ffcc00"}'
# also accepts {"r":255,"g":204,"b":0}, "#fc0" (3-digit hex)
```

### `POST /api/fade`

```bash
curl -X POST http://127.0.0.1:49696/api/fade \
  -H 'content-type: application/json' \
  -d '{"color_a":"#ff0000","color_b":"#00ff00","frames":40}'
```

### `POST /api/gif`

Multipart upload:
```bash
curl -X POST http://127.0.0.1:49696/api/gif -F file=@some.gif
```

Or raw body:
```bash
curl -X POST http://127.0.0.1:49696/api/gif \
  -H 'content-type: image/gif' \
  --data-binary @some.gif
```

Any GIF dimensions are accepted — the service auto-resizes to 96×16.

### `POST /api/text` — full control

```bash
curl -X POST http://127.0.0.1:49696/api/text \
  -H 'content-type: application/json' \
  -d '{
    "text":            "BOIS CLUB GAMES",
    "font":            "Helvetica",
    "size":            17,
    "weight":          900,
    "style":           "normal",
    "letter_spacing":  0,
    "y_offset":        0,
    "fg":              "#ffffff",
    "bg":              "#000000",
    "antialias":       false
  }'
```

Defaults if a field is omitted: font=Helvetica, size=17, weight=900, style=normal, letter_spacing=0, y_offset=0, fg=#ffffff, bg=#000000, antialias=false.

Rendering goes through **CoreText** (the same engine the UI's canvas uses), so the bytes shipped to the display match the desktop app's output byte-for-byte at the RGB332 quantization level.

### `GET /api/text?...` — simple form

The convenient form, three accepted shapes:

```bash
# 1) bare query string (everything after `?` becomes the text)
curl 'http://127.0.0.1:49696/api/text?BOIS%20CLUB%20GAMES'

# 2) named query param (same as above; required if you also want overrides)
curl 'http://127.0.0.1:49696/api/text?text=BOIS%20CLUB%20GAMES'

# 3) URL path
curl 'http://127.0.0.1:49696/api/text/BOIS%20CLUB%20GAMES'

# with overrides
curl 'http://127.0.0.1:49696/api/text?text=HI&size=20&font=Menlo&weight=400&fg=%23ff0&y_offset=-1'
```

**Browser-friendly** — paste into any browser address bar:
```
http://127.0.0.1:49696/api/text?HELLO%20FROM%20BROWSER
```

URL-encoding reminders: space → `%20`, `#` (in colors) → `%23`.

### `POST /api/text/preview`

Same body as `/api/text` but returns an `image/png` of the rendered strip (4× nearest-neighbor upscaled). Tile count is in the `X-Tile-Count` response header. Useful for previews without shipping to the display.

### `POST /api/text/tiles`

Accepts pre-rendered RGB332 tile bytes (base64) — what the `.app`'s canvas path uses internally:

```json
{"tile_pixels_b64": "<base64 of tile_count*96*16 bytes>", "tile_count": 2}
```

### Status codes

| Code | Meaning |
|---|---|
| 200 | success |
| 400 | bad payload (malformed JSON, not a GIF, unknown color, empty text, font missing) |
| 429 | BLE busy — another upload in progress |
| 502 | bleak library error mid-transfer |
| 503 | no `iledeyes-*` device found within 10s |
| 500 | unexpected (logged with traceback) |

---

## What you can do with this

Because the service is just a plain HTTP API bound to your LAN, anything that can make a web request can drive the display. A few ideas:

### Live streaming (Streamer.bot, OBS, etc.)

[Streamer.bot](https://streamer.bot/) has a **Fetch URL** sub-action that fires an HTTP request on any trigger. Point it at this service and you've got a physical LED ticker for your stream.

- **New follower / sub / cheer** → fire `GET http://<mac-ip>:49696/api/text?Welcome%20%user%`
  In Streamer.bot, use the URL-encoded variable: `GET http://192.168.4.22:49696/api/text?text={user}%20just%20subbed`
- **Chat message** → trigger on a `!sign` command and POST `{"text":"%rawInput%"}` to `/api/text`
- **Bits / cheers** → flash a solid color: `POST /api/solid {"color":"#ffcc00"}` then back to a fade or text
- **Stream goes live** → `GET /api/text?LIVE` to scroll a banner
- **Raid incoming** → flash + GIF: `POST /api/gif` with a celebration GIF

Streamer.bot variables URL-encode cleanly into the query-string form, so you don't need a webhook server in the middle — the bot talks straight to the service.

### Other natural fits

- **Twitch / YouTube chat bots** (chatterino plugins, custom bots) — same pattern: chat event → HTTP GET
- **Home Assistant** — `rest_command` integration to show notifications, temperatures, alerts on the LED
- **Shortcuts.app on iOS / macOS** — *"Get Contents of URL"* action with the GET text endpoint; pin to widget or share sheet
- **Pipe stdout** — pipe anything line-based through `xargs -I{} curl "http://.../api/text?{}"` for shell-driven scrolls
- **Webhooks from anywhere** — GitHub releases, CI failures, Cloudflare alerts, Stripe payments, calendar events
- **MIDI / OSC / DAW** — any controller that can fire HTTP can change colors or strobes in time with music
- **Cron** — `crontab` entries that swap content based on time of day

The API is the contract; how you trigger it is wide open.

---

## LAN access

The service binds `0.0.0.0` by default, so any device on the same network can drive the display:

```bash
# find this Mac's LAN IP
ipconfig getifaddr en0     # WiFi, or en1/en2 if wired

# from another machine
curl 'http://192.168.x.y:49696/api/text?HELLO%20FROM%20LAN'
```

**Heads-up about the firewall.** The first time something connects from another host, macOS may pop a "Allow incoming connections?" prompt. If you miss it and connections time out, go to **System Settings → Network → Firewall → Options…**, find `displayathon-service`, set it to **Allow**.

**Security.** No auth on the API. On a trusted home/office LAN that's fine; on shared Wi-Fi anyone can drive your display. To lock to loopback only, set `DISPLAYATHON_HOST=127.0.0.1` in `service/install.sh`'s plist block and reinstall.

---

## Configuration

Environment variables (set in the launchd plist; edit `service/install.sh` and reinstall to change persistently):

| Var | Default | Purpose |
|---|---|---|
| `DISPLAYATHON_HOST` | `0.0.0.0` | bind address — defaults to all interfaces so LAN clients can reach the API |
| `DISPLAYATHON_PORT` | `49696` | service port |
| `DISPLAYATHON_LOG` | `INFO` | log level (`DEBUG` for verbose) |
| `DISPLAYATHON_URL` | `http://127.0.0.1:49696` | where the desktop app looks for the service |

---

## Adding fonts

Drop any `.ttf`, `.otf`, or `.ttc` into either:

- `fonts/` in the repo — copied into the user dir on every `./service/install.sh`
- `~/Library/displayathon/fonts/` — picked up live; no reinstall, no rebuild

`GET /api/fonts` shows what's available. Font names in the API are the file stem (`m6x11plus.ttf` → `"m6x11plus"`).

---

## Troubleshooting

**`service unreachable` in the app** — the service isn't running. Run `./service/install.sh`. For one-shot debug: `./dist/displayathon-service` directly.

**`503 no iledeyes-* device found`** — the display is asleep, out of range, or already paired to another BLE client. Power-cycle the display.

**`429 BLE busy`** — concurrent upload in flight. Wait a beat and retry.

**Service won't start under launchd** — first check `~/Library/Logs/displayathon/err.log`. Common cause: a stale plist after moving the project. Re-run `./service/install.sh` so the plist picks up the current binary path.

**`Bootstrap failed: 5: Input/output error`** during install — usually a half-loaded job from a previous attempt. The install script already waits + retries; if it persists, manually clear with:
```bash
launchctl bootout gui/$(id -u)/com.boisclubgames.displayathon
./service/install.sh
```

**App opens but tabs do nothing** — the status bar will be red and explain why. Almost always the service isn't reachable.

**Text from the API doesn't look like text from the UI** — they go through the same CoreText renderer; if they really diverge, check the `via` field in the `/api/text` response: `coretext` is the canonical path, `pil` is the headless fallback (used only if AppKit fails to import — shouldn't happen on macOS).

---

## Automated builds (GitHub Actions, self-hosted runner)

`.github/workflows/build.yml` builds the two artifacts and publishes a tagged GitHub release on every push to `main`. Because PyInstaller binaries are macOS-architecture-specific and require the full build toolchain, the workflow runs on a **self-hosted runner** — i.e., this Mac.

### One-time runner setup

1. Repo → **Settings → Actions → Runners → New self-hosted runner → macOS**.
2. Follow GitHub's three commands (download, configure, run) — they'll register the runner with the repo and start the listener.
3. Either run it interactively (`./run.sh`) for ad-hoc builds, or install it as a launchd service so it's always available:
   ```bash
   ./svc.sh install
   ./svc.sh start
   ```
4. Make sure `python3` (3.11+) and `gh` (`brew install gh`) are on the runner's `$PATH`. Both are needed by the workflow.

### Triggers

- **Push to `main`** → build + release.
- **Workflow Dispatch** from the Actions tab → manual rebuild.

### What gets published

Each release is tagged `v<UTC-date>-<short-sha>` (e.g. `v2026.05.13-1a2b3c4`) and contains:

- `displayathon.app.zip` — the desktop UI, packaged with `ditto` so codesign and xattrs survive a round-trip through the zip.
- `displayathon-service` — the standalone CLI binary that launchd runs.

Pull either or both from the Releases page on any Mac.

---

## Uninstall

```bash
./service/uninstall.sh
```

Stops the agent, removes the plist, and deletes the installed binary copy. Logs at `~/Library/Logs/displayathon/` remain — delete them yourself if you want them gone. The repo itself, the `dist/` build outputs, and `~/Library/displayathon/fonts/` are untouched.
