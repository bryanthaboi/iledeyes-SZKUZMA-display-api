"""HTTP service for the displayathon LED driver.

Replaces the Flask UI in ui.py with a long-running FastAPI service that:
  - owns the BLE upload lock (only one process should ever talk to the device)
  - exposes split REST endpoints (/api/solid, /api/fade, /api/gif, /api/text)
  - renders scrolling text server-side via PIL (bundled fonts/ + system fallbacks)
  - returns PNG previews so the NiceGUI app shows exactly what gets shipped
  - is designed to be installed as a launchd agent (see service/install.sh)

Wire-level behavior is delegated to displayathon.py unchanged — this module only
assembles config + payload and drives the same do_upload sequence ui.py used.

Run:
    python display_service.py                  # foreground, default port 49696
    DISPLAYATHON_PORT=9000 python display_service.py
    DISPLAYATHON_LOG=DEBUG python display_service.py
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import re
import struct
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Optional

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field

import displayathon as dat

HERE = Path(__file__).parent

# Fonts are searched in two places, in priority order:
#   1) the user-droppable runtime dir — no rebuild needed to add a new font
#   2) fonts bundled with the binary (or sitting next to the source in dev mode)
USER_FONTS_DIR = Path.home() / "Library" / "displayathon" / "fonts"
if getattr(sys, "frozen", False):
    BUNDLED_FONTS_DIR = Path(getattr(sys, "_MEIPASS", HERE)) / "fonts"
else:
    BUNDLED_FONTS_DIR = HERE / "fonts"
BRIGHTNESS = 10
WIDTH = dat.WIDTH
HEIGHT = dat.HEIGHT
CHUNK_SIZE = dat.CHUNK_SIZE
VERSION = "0.1.0"

PORT = int(os.environ.get("DISPLAYATHON_PORT", "49696"))
HOST = os.environ.get("DISPLAYATHON_HOST", "0.0.0.0")
LOG_LEVEL = os.environ.get("DISPLAYATHON_LOG", "INFO").upper()


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("displayathon")


# ---------------------------------------------------------------------------
# error types
# ---------------------------------------------------------------------------

class BadInputError(ValueError):
    """400 — client sent something we can't act on."""


class DeviceNotFoundError(RuntimeError):
    """503 — couldn't find an iledeyes-* device during scan."""


class BleError(RuntimeError):
    """502 — bleak raised mid-transfer."""


# ---------------------------------------------------------------------------
# color parsing
# ---------------------------------------------------------------------------

_HEX6_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
_HEX3_RE = re.compile(r"^#?([0-9a-fA-F]{3})$")


def parse_color(spec) -> tuple[int, int, int]:
    """Accept '#ffcc00', 'ffcc00', '#fc0', [r,g,b], or {'r':..,'g':..,'b':..}."""
    if isinstance(spec, str):
        s = spec.strip()
        m = _HEX6_RE.match(s)
        if m:
            h = m.group(1)
            return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        m = _HEX3_RE.match(s)
        if m:
            h = m.group(1)
            return int(h[0] * 2, 16), int(h[1] * 2, 16), int(h[2] * 2, 16)
        raise BadInputError(f"bad color hex: {spec!r}")
    if isinstance(spec, dict):
        try:
            return int(spec["r"]), int(spec["g"]), int(spec["b"])
        except (KeyError, TypeError, ValueError) as e:
            raise BadInputError(f"bad color dict: {spec!r}") from e
    if isinstance(spec, (list, tuple)) and len(spec) == 3:
        return int(spec[0]), int(spec[1]), int(spec[2])
    raise BadInputError(f"unrecognized color: {spec!r}")


# ---------------------------------------------------------------------------
# frame builders (solid / fade)
# ---------------------------------------------------------------------------

def _solid_frame(rgb: tuple[int, int, int]) -> bytes:
    return bytes([dat.rgb_to_rgb332(*rgb)]) * (WIDTH * HEIGHT)


def _fade_frames(a: tuple[int, int, int], b: tuple[int, int, int], n: int = 40) -> list[bytes]:
    half = max(1, n // 2)
    if half > 1:
        fwd = [tuple(int(a[k] + (b[k] - a[k]) * i / (half - 1)) for k in range(3))
               for i in range(half)]
    else:
        fwd = [a]
    bwd = list(reversed(fwd))
    palette = (fwd + bwd)[:n]
    return [_solid_frame(palette[i % len(palette)]) for i in range(n)]


# ---------------------------------------------------------------------------
# text rendering (PIL → tile bytes + PNG preview)
# ---------------------------------------------------------------------------

_SYSTEM_FONT_PATHS = {
    "Helvetica": "/System/Library/Fonts/Helvetica.ttc",
    "Menlo": "/System/Library/Fonts/Menlo.ttc",
    "Courier New": "/System/Library/Fonts/Supplemental/Courier New.ttf",
    "Arial": "/System/Library/Fonts/Supplemental/Arial.ttf",
    "Monaco": "/System/Library/Fonts/Monaco.ttf",
    "Impact": "/System/Library/Fonts/Supplemental/Impact.ttf",
}


def list_fonts() -> list[dict]:
    """Discover user, bundled, and system fonts for text-native rendering.

    User-dir fonts win on name collision; bundled fonts win over system. The
    list is deduplicated by lowercase stem.
    """
    out: list[dict] = []
    seen: set[str] = set()

    def _add(name: str, path: str, source: str) -> None:
        key = name.lower()
        if key in seen:
            return
        seen.add(key)
        out.append({"name": name, "path": path, "source": source})

    for dir_path, source in ((USER_FONTS_DIR, "user"), (BUNDLED_FONTS_DIR, "bundled")):
        if dir_path.exists():
            for p in sorted(dir_path.iterdir()):
                if p.suffix.lower() in (".ttf", ".otf", ".ttc"):
                    _add(p.stem, str(p), source)

    for name, path in _SYSTEM_FONT_PATHS.items():
        if Path(path).exists():
            _add(name, path, "system")

    return out


def _resolve_font(name: Optional[str], size: int):
    """Resolve a font name → PIL ImageFont. Falls back to system + default."""
    fonts = list_fonts()
    if name:
        name_l = name.lower()
        for f in fonts:
            if f["name"].lower() == name_l:
                try:
                    return ImageFont.truetype(f["path"], size=size)
                except OSError as e:
                    log.warning("failed to load %s: %s", f["path"], e)
    for f in fonts:
        try:
            return ImageFont.truetype(f["path"], size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_text_strip(
    text: str,
    fg: tuple[int, int, int],
    bg: tuple[int, int, int],
    font_name: Optional[str] = None,
    size: int = 18,
    letter_spacing: int = 0,
    y_offset: int = -1,
    antialias: bool = False,
) -> tuple[Image.Image, int]:
    """Render `text` onto a (tile_count*96, 16) RGB image. Returns (img, tile_count).

    Vertical centering uses the proven bbox recipe from
    displayathon.render_scroll_frames: center the rendered bbox vertically in
    the 16-pixel canvas, then apply `y_offset` as an extra nudge. This matches
    the look of the old JS canvas path (textBaseline='middle' + yOffset=-1)
    for pixel fonts like m6x11plus.
    """
    if not text:
        raise BadInputError("text is empty")
    font = _resolve_font(font_name, size)

    # Measure the full text bbox — needed for both width (tile count) and
    # vertical centering. PIL bboxes are (x0, y0, x1, y1) relative to the
    # text anchor; y0 is often negative (ascender above baseline).
    full_bbox = font.getbbox(text)
    text_h = full_bbox[3] - full_bbox[1]

    if letter_spacing:
        text_w = 0
        for ch in text:
            cb = font.getbbox(ch)
            text_w += (cb[2] - cb[0]) + letter_spacing
        text_w = max(1, text_w - letter_spacing)
    else:
        text_w = full_bbox[2] - full_bbox[0]

    tile_count = max(1, (text_w + WIDTH - 1) // WIDTH)
    canvas_w = tile_count * WIDTH

    img = Image.new("RGB", (canvas_w, HEIGHT), bg)
    draw = ImageDraw.Draw(img)
    draw.fontmode = "L" if antialias else "1"

    # Top y so that the bbox is vertically centered, plus user offset.
    base_y = (HEIGHT - text_h) // 2 - full_bbox[1] + y_offset

    if letter_spacing:
        x = -full_bbox[0]  # remove the per-text x lead-in same as default path
        for ch in text:
            draw.text((x, base_y), ch, fill=fg, font=font)
            cb = font.getbbox(ch)
            x += (cb[2] - cb[0]) + letter_spacing
    else:
        draw.text((-full_bbox[0], base_y), text, fill=fg, font=font)

    return img, tile_count


def strip_to_tile_bytes(img: Image.Image) -> bytes:
    """Quantize the (tile_count*96, 16) RGB strip → row-major RGB332 bytes.

    Matches the formula used by ui.py's JS canvas path:
        rgb332 = (r & 0xE0) | ((g & 0xE0) >> 3) | ((b & 0xC0) >> 6)
    """
    raw = img.tobytes()
    w, h = img.size
    out = bytearray(w * h)
    for i in range(w * h):
        p = i * 3
        r, g, b = raw[p], raw[p + 1], raw[p + 2]
        out[i] = (r & 0xE0) | ((g & 0xE0) >> 3) | ((b & 0xC0) >> 6)
    return bytes(out)


def strip_to_preview_png(img: Image.Image, scale: int = 4) -> bytes:
    """Upscale a tile strip with nearest-neighbor → PNG bytes for the UI preview."""
    w, h = img.size
    big = img.resize((w * scale, h * scale), Image.Resampling.NEAREST)
    buf = io.BytesIO()
    big.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# native text rendering via AppKit/CoreText
#
# WebKit (and therefore the .app's canvas tile renderer) uses CoreText for
# its text rasterization on macOS. PIL/FreeType produces visibly different
# pixels for the same font+size, which is why /api/text used to look "wrong"
# next to the UI. By drawing the text with NSAttributedString into an
# NSBitmapImageRep here, then quantizing the result to RGB332, we get pixel
# output that lines up with what the canvas in the UI produces — same engine
# end-to-end. PIL stays in tree as a non-macOS fallback only.
# ---------------------------------------------------------------------------

_USE_CORETEXT = False
try:
    from AppKit import (  # type: ignore
        NSAffineTransform, NSApplication, NSAttributedString, NSBezierPath,
        NSBitmapImageRep, NSColor, NSDeviceRGBColorSpace, NSFont,
        NSFontAttributeName, NSFontManager, NSForegroundColorAttributeName,
        NSGraphicsContext, NSItalicFontMask, NSKernAttributeName,
    )
    from Foundation import NSMakePoint, NSMakeRect  # type: ignore
    # Initialise NSApplication once — required to use AppKit drawing APIs
    # even when no window is being shown.
    NSApplication.sharedApplication()
    _USE_CORETEXT = True
except Exception as _coretext_err:  # pragma: no cover — non-macOS or missing pyobjc
    log.warning("AppKit/CoreText unavailable (%s); /api/text will use PIL", _coretext_err)


# CSS font-weight → NSFontManager weight slot. NSFM weights run 1..15 with
# 5 being "regular"; 14 maps to NSFontWeightBlack which matches CSS 900.
_CSS_TO_NSFM_WEIGHT = {
    100: 1, 200: 2, 300: 3, 400: 5, 500: 6,
    600: 8, 700: 9, 800: 10, 900: 14,
}


def _resolve_nsfont(family: str, size: int, weight: int, style: str):
    traits = 0
    if (style or "normal").lower() == "italic":
        traits |= NSItalicFontMask
    nsfm_weight = _CSS_TO_NSFM_WEIGHT.get(int(weight), 5)
    fm = NSFontManager.sharedFontManager()
    font = fm.fontWithFamily_traits_weight_size_(family, traits, nsfm_weight, size)
    if font is None:
        font = NSFont.fontWithName_size_(family, size)
    if font is None:
        font = NSFont.systemFontOfSize_(size)
    return font


def _render_text_tiles_coretext(
    text: str,
    fg: tuple[int, int, int],
    bg: tuple[int, int, int],
    font_family: str,
    size: int,
    weight: int,
    style: str,
    letter_spacing: int,
    y_offset: int,
    antialias: bool,
) -> tuple[bytes, int]:
    """Render text via AppKit → quantize → return (tile_bytes, tile_count)."""
    if not text:
        raise BadInputError("text is empty")

    font = _resolve_nsfont(font_family or "Helvetica", size, weight, style)
    fg_color = NSColor.colorWithCalibratedRed_green_blue_alpha_(
        fg[0] / 255.0, fg[1] / 255.0, fg[2] / 255.0, 1.0,
    )
    bg_color = NSColor.colorWithCalibratedRed_green_blue_alpha_(
        bg[0] / 255.0, bg[1] / 255.0, bg[2] / 255.0, 1.0,
    )

    attrs = {NSFontAttributeName: font, NSForegroundColorAttributeName: fg_color}
    if letter_spacing:
        attrs[NSKernAttributeName] = float(letter_spacing)
    attr_str = NSAttributedString.alloc().initWithString_attributes_(text, attrs)

    measured = attr_str.size()
    text_w = max(1, int(measured.width + 0.5))
    tile_count = max(1, (text_w + WIDTH - 1) // WIDTH)
    canvas_w = tile_count * WIDTH

    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, canvas_w, HEIGHT,
        8,    # bitsPerSample
        4,    # samplesPerPixel (RGBA)
        True, False,
        NSDeviceRGBColorSpace,
        canvas_w * 4,
        32,
    )

    ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(ctx)
    ctx.setShouldAntialias_(bool(antialias))

    # Background fill.
    bg_color.setFill()
    NSBezierPath.fillRect_(NSMakeRect(0, 0, canvas_w, HEIGHT))

    # Draw text in Cocoa's native y-up coordinate system. (An earlier version
    # flipped the context with scaleY=-1 to fake y-down "canvas" coords, but
    # flipping the user-coord space also flips the glyph strokes, which made
    # every letter render upside-down.)
    #
    # We want the visual middle of the em-box at canvas-y = HEIGHT/2 + y_offset
    # (canvas's textBaseline='middle' semantics). Converted to Cocoa y-up:
    #     em_middle_cocoa = HEIGHT - (HEIGHT/2 + y_offset) = HEIGHT/2 - y_offset
    # The em-box middle sits (ascender - |descender|)/2 above the baseline,
    # and NSAttributedString.drawAtPoint draws so that point.y is the bottom
    # of the layout rect, which is |descender| below the baseline.
    ascent = float(font.ascender())
    descent_abs = abs(float(font.descender()))
    em_middle_cocoa = HEIGHT / 2.0 - y_offset
    baseline_cocoa = em_middle_cocoa - (ascent - descent_abs) / 2.0
    point_y = baseline_cocoa - descent_abs
    attr_str.drawAtPoint_(NSMakePoint(0, point_y))

    NSGraphicsContext.restoreGraphicsState()

    # NSBitmapImageRep returns RGBA, row-major, top-down — exactly what we
    # need to walk and quantize.
    raw = bytes(rep.bitmapData())[: canvas_w * HEIGHT * 4]
    out = bytearray(tile_count * WIDTH * HEIGHT)
    for t in range(tile_count):
        x0 = t * WIDTH
        for y in range(HEIGHT):
            for x in range(WIDTH):
                p = (y * canvas_w + (x0 + x)) * 4
                r, g, b = raw[p], raw[p + 1], raw[p + 2]
                out[t * WIDTH * HEIGHT + y * WIDTH + x] = (
                    (r & 0xE0) | ((g & 0xE0) >> 3) | ((b & 0xC0) >> 6)
                )
    return bytes(out), tile_count


# ---------------------------------------------------------------------------
# payload assembly per mode
# ---------------------------------------------------------------------------

def build_solid_payload(rgb: tuple[int, int, int]) -> tuple[bytes, dict]:
    frames = [_solid_frame(rgb)]
    header = dat._metadata(WIDTH, HEIGHT, frame_count=1, frame_ms=40)
    payload = header + b"".join(frames)
    return payload, {"mode": "solid", "frames": 1, "bytes": len(payload)}


def build_fade_payload(a: tuple[int, int, int], b: tuple[int, int, int], n: int) -> tuple[bytes, dict]:
    frames = _fade_frames(a, b, n=n)
    header = dat._metadata(WIDTH, HEIGHT, frame_count=len(frames), frame_ms=40)
    payload = header + b"".join(frames)
    return payload, {"mode": "fade", "frames": len(frames), "bytes": len(payload)}


def build_gif_payload(gif_bytes: bytes) -> tuple[bytes, dict]:
    if gif_bytes[:6] not in (b"GIF87a", b"GIF89a"):
        raise BadInputError(f"not a GIF (magic={gif_bytes[:6]!r})")
    gw = int.from_bytes(gif_bytes[6:8], "little")
    gh = int.from_bytes(gif_bytes[8:10], "little")
    if (gw, gh) != (WIDTH, HEIGHT):
        with tempfile.NamedTemporaryFile(suffix=".gif", delete=False) as t:
            t.write(gif_bytes)
            tmp_path = t.name
        try:
            gif_bytes = dat._resize_gif_to_display(tmp_path)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        gw = int.from_bytes(gif_bytes[6:8], "little")
        gh = int.from_bytes(gif_bytes[8:10], "little")
    header = dat._metadata_gif_native(gw, gh)
    payload = header + gif_bytes
    return payload, {"mode": "gif-native", "gif_bytes": len(gif_bytes), "bytes": len(payload)}


def build_text_payload(
    text: str,
    fg: tuple[int, int, int],
    bg: tuple[int, int, int],
    font_name: Optional[str],
    size: int,
    letter_spacing: int,
    y_offset: int,
    antialias: bool,
) -> tuple[bytes, dict, Image.Image]:
    img, tile_count = render_text_strip(
        text, fg, bg,
        font_name=font_name, size=size,
        letter_spacing=letter_spacing, y_offset=y_offset, antialias=antialias,
    )
    tile_bytes = strip_to_tile_bytes(img)
    header = dat._metadata_text_scroll(WIDTH, HEIGHT, tile_count)
    payload = header + tile_bytes
    meta = {"mode": "text-native", "tiles": tile_count, "bytes": len(payload)}
    return payload, meta, img


# ---------------------------------------------------------------------------
# BLE upload — mirrors ui.py's do_upload, but takes a `mode` flag for the
# StartStream vs StartPlayList branch.
# ---------------------------------------------------------------------------

_ble_lock = asyncio.Lock()
_state = {"last_upload_ts": 0.0, "started_at": time.time()}


async def do_upload(mode: str, payload: bytes) -> dict:
    """Connect to iledeyes-*, push the payload, return ack stats. Raises on failure."""
    try:
        from bleak import BleakClient, BleakScanner
        from bleak.exc import BleakError
    except ImportError as e:
        raise BleError(f"bleak not importable: {e}")

    dev = await BleakScanner.find_device_by_filter(
        lambda d, _a: (d.name or "").startswith("iledeyes"), timeout=10.0,
    )
    if dev is None:
        raise DeviceNotFoundError("no iledeyes-* device found within 10s")

    ack_count = {"n": 0}

    def cb(_h, data: bytearray) -> None:
        b = bytes(data)
        if len(b) >= 2 and b[0] == 0x54 and b[1] == 0x00:
            ack_count["n"] += 1

    try:
        async with BleakClient(dev, timeout=10.0) as client:
            await client.start_notify(dat.NOTIFY_CHAR, cb)
            await dat.connect_and_authenticate(client)
            await asyncio.sleep(0.15)
            await dat.set_enabled(client, True)
            await asyncio.sleep(0.05)
            await dat.set_brightness(client, BRIGHTNESS)
            await asyncio.sleep(0.1)

            wrapped, crc, total = dat.wrap_ctn(payload)

            if mode == "gif-native":
                start_pkt = dat.build_start_playlist(1, 0, crc, total)
            else:
                start_pkt = dat.build_start_stream(crc, total)
            await client.write_gatt_char(dat.CMD_CHAR, start_pkt, response=False)
            await asyncio.sleep(0.02)

            ack_count["n"] = 0
            for i, off in enumerate(range(0, len(wrapped), CHUNK_SIZE)):
                piece = wrapped[off:off + CHUNK_SIZE]
                pkt = dat.build_packet(
                    dat.Handle.Continue, piece, sequence=i, data_length=len(piece),
                )
                await client.write_gatt_char(dat.WRITE_CHAR, pkt, response=False)
                deadline = asyncio.get_event_loop().time() + 0.5
                while ack_count["n"] <= i and asyncio.get_event_loop().time() < deadline:
                    await asyncio.sleep(0.003)
            n_chunks = (len(wrapped) + CHUNK_SIZE - 1) // CHUNK_SIZE

            await client.write_gatt_char(
                dat.WRITE_CHAR,
                dat.build_packet(dat.Handle.EndStream, b"\x01"),
                response=False,
            )
            await asyncio.sleep(0.25)

            if mode == "gif-native":
                await client.write_gatt_char(
                    dat.CMD_CHAR, dat.build_play_commit(), response=False,
                )
                await asyncio.sleep(0.15)
    except BleakError as e:
        raise BleError(f"bleak: {e}") from e

    _state["last_upload_ts"] = time.time()
    return {
        "bytes": len(wrapped),
        "chunks": n_chunks,
        "acks": ack_count["n"],
        "summary": f"{len(wrapped)}B · {n_chunks} chunks · {ack_count['n']} ACKs",
    }


async def upload_with_lock(mode: str, payload: bytes) -> dict:
    """Acquire the BLE lock non-blocking; raise 429-style error if held."""
    if _ble_lock.locked():
        raise HTTPException(status_code=429, detail="BLE busy — another upload in progress")
    async with _ble_lock:
        return await do_upload(mode, payload)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "displayathon-service v%s · python %s · port %d · host %s",
        VERSION, sys.executable, PORT, HOST,
    )
    _state["started_at"] = time.time()
    yield
    # Graceful shutdown: give an in-flight upload up to 1s to drain.
    try:
        await asyncio.wait_for(_ble_lock.acquire(), timeout=1.0)
        _ble_lock.release()
    except asyncio.TimeoutError:
        log.warning("shutdown: ble lock still held after 1s — exiting anyway")


app = FastAPI(title="displayathon", version=VERSION, lifespan=lifespan)

# Permissive CORS — service binds to loopback only, so this just lets the
# NiceGUI app on :49697 fetch fonts and POST tile bytes to :49696.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Tile-Count"],
)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    """Translate FastAPI's default 422 shape into our {ok, message} envelope."""
    first = exc.errors()[0] if exc.errors() else {"msg": "invalid request"}
    return JSONResponse(
        {"ok": False, "message": f"{first.get('msg', 'invalid')} at {'.'.join(str(x) for x in first.get('loc', []))}"},
        status_code=400,
    )


# ---- request models -------------------------------------------------------

class SolidReq(BaseModel):
    color: object | None = None
    r: int | None = None
    g: int | None = None
    b: int | None = None

    def rgb(self) -> tuple[int, int, int]:
        if self.color is not None:
            return parse_color(self.color)
        if None not in (self.r, self.g, self.b):
            return parse_color({"r": self.r, "g": self.g, "b": self.b})
        raise BadInputError("provide 'color' (hex/dict) or r/g/b")


class FadeReq(BaseModel):
    color_a: object
    color_b: object
    frames: int = Field(default=40, ge=2, le=200)


class TextReq(BaseModel):
    text: str
    fg: object = "#ffffff"
    bg: object = "#000000"
    font: str | None = "Helvetica"
    size: int = Field(default=17, ge=6, le=64)
    letter_spacing: int = Field(default=0, ge=-4, le=8)
    y_offset: int = Field(default=0, ge=-8, le=8)
    antialias: bool = False
    # Weight + style are passed straight through to the CoreText/AppKit
    # renderer so the API output matches the UI's canvas output. Same
    # numeric scale as CSS font-weight.
    weight: int = Field(default=900, ge=100, le=900)
    style: str = "normal"  # "normal", "italic", "oblique"


class TileReq(BaseModel):
    """Pre-rendered tile bytes — base64-encoded RGB332, tile_count×WIDTH×HEIGHT.

    Used by the NiceGUI UI which renders text tiles in a JS canvas (for
    byte-identical parity with the original Flask ui.py) and ships the
    finished bytes here.
    """
    tile_pixels_b64: str
    tile_count: int = Field(ge=1, le=64)


# ---- error helpers --------------------------------------------------------

def _err_response(status: int, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "message": message}, status_code=status)


async def _run_upload(mode: str, payload: bytes, meta: dict) -> JSONResponse:
    """Common wrapper: lock + upload + uniform JSON response + error mapping."""
    try:
        result = await upload_with_lock(mode, payload)
    except HTTPException:
        raise
    except DeviceNotFoundError as e:
        log.warning("device not found: %s", e)
        return _err_response(503, str(e))
    except BleError as e:
        log.exception("ble error")
        return _err_response(502, str(e))
    except BadInputError as e:
        return _err_response(400, str(e))
    except Exception as e:
        log.exception("unexpected upload error")
        return _err_response(500, f"{type(e).__name__}: {e}")
    return JSONResponse({"ok": True, "message": result["summary"], "meta": {**meta, **result}})


# ---- endpoints ------------------------------------------------------------

@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "version": VERSION,
        "busy": _ble_lock.locked(),
        "uptime_s": int(time.time() - _state["started_at"]),
        "last_upload_ts": int(_state["last_upload_ts"]) if _state["last_upload_ts"] else 0,
        "port": PORT,
    }


@app.get("/api/fonts")
async def fonts() -> dict:
    return {"fonts": list_fonts()}


@app.get("/api/fonts/{name}/file")
async def font_file(name: str):
    """Serve the raw TTF/OTF bytes so the UI can register the font for canvas."""
    for f in list_fonts():
        if f["name"].lower() == name.lower():
            p = Path(f["path"])
            if not p.exists():
                return _err_response(404, f"font file missing: {p}")
            return FileResponse(p, media_type="font/ttf")
    return _err_response(404, f"unknown font: {name}")


@app.post("/api/solid")
async def api_solid(req: SolidReq):
    try:
        payload, meta = build_solid_payload(req.rgb())
    except BadInputError as e:
        return _err_response(400, str(e))
    return await _run_upload("solid", payload, meta)


@app.post("/api/fade")
async def api_fade(req: FadeReq):
    try:
        a = parse_color(req.color_a)
        b = parse_color(req.color_b)
        payload, meta = build_fade_payload(a, b, req.frames)
    except BadInputError as e:
        return _err_response(400, str(e))
    return await _run_upload("fade", payload, meta)


@app.post("/api/gif")
async def api_gif(request: Request, file: Annotated[Optional[UploadFile], File()] = None):
    """Accepts multipart `file=...` OR a raw application/gif body."""
    try:
        if file is not None:
            gif_bytes = await file.read()
        else:
            gif_bytes = await request.body()
        if not gif_bytes:
            return _err_response(400, "no gif data in request")
        payload, meta = build_gif_payload(gif_bytes)
    except BadInputError as e:
        return _err_response(400, str(e))
    except Exception as e:
        log.exception("gif build error")
        return _err_response(400, f"{type(e).__name__}: {e}")
    return await _run_upload("gif-native", payload, meta)


@app.post("/api/text/tiles")
async def api_text_tiles(req: TileReq):
    """Ship pre-rendered RGB332 tile bytes as a native scroll payload.

    Mirrors the original ui.py text-native path: the UI renders the text in a
    JS canvas using @font-face fonts and quantizes to RGB332 client-side, so
    the on-screen pixels are byte-identical to the legacy Flask UI.
    """
    try:
        tile_bytes = base64.b64decode(req.tile_pixels_b64)
    except Exception as e:
        return _err_response(400, f"bad base64: {e}")
    expected = req.tile_count * WIDTH * HEIGHT
    if len(tile_bytes) != expected:
        return _err_response(
            400,
            f"tile_pixels_b64 length mismatch: got {len(tile_bytes)}B, expected {expected}B "
            f"({req.tile_count} tiles × {WIDTH}×{HEIGHT})",
        )
    header = dat._metadata_text_scroll(WIDTH, HEIGHT, req.tile_count)
    payload = header + tile_bytes
    meta = {"mode": "text-native", "tiles": req.tile_count, "bytes": len(payload), "via": "canvas"}
    return await _run_upload("text-native", payload, meta)


async def _build_text_payload_from_req(req: TextReq):
    """Pick the best available text renderer and build the wire payload.

    On macOS this uses AppKit/CoreText — the same engine the UI's canvas
    runs on — so the bytes shipped to the display via /api/text match what
    the .app would ship for the same input. PIL is the headless fallback.
    """
    fg = parse_color(req.fg)
    bg = parse_color(req.bg)
    if _USE_CORETEXT:
        tile_bytes, tile_count = _render_text_tiles_coretext(
            req.text, fg, bg,
            font_family=req.font or "Helvetica",
            size=req.size, weight=req.weight, style=req.style,
            letter_spacing=req.letter_spacing, y_offset=req.y_offset,
            antialias=req.antialias,
        )
        header = dat._metadata_text_scroll(WIDTH, HEIGHT, tile_count)
        payload = header + tile_bytes
        meta = {"mode": "text-native", "tiles": tile_count, "bytes": len(payload), "via": "coretext"}
        return payload, meta
    payload, meta, _img = build_text_payload(
        req.text, fg, bg,
        font_name=req.font, size=req.size,
        letter_spacing=req.letter_spacing, y_offset=req.y_offset,
        antialias=req.antialias,
    )
    meta["via"] = "pil"
    return payload, meta


@app.post("/api/text")
async def api_text(req: TextReq):
    try:
        payload, meta = await _build_text_payload_from_req(req)
    except BadInputError as e:
        return _err_response(400, str(e))
    except Exception as e:
        log.exception("text render error")
        return _err_response(400, f"{type(e).__name__}: {e}")
    return await _run_upload("text-native", payload, meta)


async def _send_text_with_defaults(text: str, **overrides):
    """Shared path for the GET helpers — applies TextReq defaults + overrides."""
    try:
        req = TextReq(text=text, **overrides)
        payload, meta = await _build_text_payload_from_req(req)
    except BadInputError as e:
        return _err_response(400, str(e))
    except Exception as e:
        log.exception("text render error")
        return _err_response(400, f"{type(e).__name__}: {e}")
    return await _run_upload("text-native", payload, meta)


@app.get("/api/text")
async def api_text_get(
    request: Request,
    text: Optional[str] = None,
    font: Optional[str] = None,
    size: Optional[int] = None,
    letter_spacing: Optional[int] = None,
    y_offset: Optional[int] = None,
    fg: Optional[str] = None,
    bg: Optional[str] = None,
    antialias: Optional[bool] = None,
    weight: Optional[int] = None,
    style: Optional[str] = None,
):
    """Send text with defaults via GET. Three accepted shapes:

        GET /api/text?text=BOIS%20CLUB%20GAMES         # normal query param
        GET /api/text?BOIS%20CLUB%20GAMES              # bare query string
        GET /api/text?text=HI&size=20&font=Menlo&fg=%23ff0   # overrides
    """
    from urllib.parse import unquote_plus
    if not text:
        # Bare query string fallback: everything after `?` becomes the text,
        # as long as it doesn't look like key=value (no `=` and no `&`).
        raw = request.url.query
        if raw and "=" not in raw and "&" not in raw:
            text = unquote_plus(raw)
    if not text:
        return _err_response(400, "no text provided — use ?text=... or /api/text/<text>")

    overrides = {k: v for k, v in {
        "font": font, "size": size, "letter_spacing": letter_spacing,
        "y_offset": y_offset, "fg": fg, "bg": bg, "antialias": antialias,
        "weight": weight, "style": style,
    }.items() if v is not None}
    return await _send_text_with_defaults(text, **overrides)


@app.get("/api/text/{text:path}")
async def api_text_get_path(text: str):
    """Send text via path: GET /api/text/BOIS%20CLUB%20GAMES"""
    from urllib.parse import unquote
    return await _send_text_with_defaults(unquote(text))


@app.post("/api/text/preview")
async def api_text_preview(req: TextReq):
    try:
        fg = parse_color(req.fg)
        bg = parse_color(req.bg)
        img, tile_count = render_text_strip(
            req.text, fg, bg,
            font_name=req.font, size=req.size,
            letter_spacing=req.letter_spacing, y_offset=req.y_offset,
            antialias=req.antialias,
        )
    except BadInputError as e:
        return _err_response(400, str(e))
    png = strip_to_preview_png(img, scale=4)
    return Response(
        content=png,
        media_type="image/png",
        headers={"X-Tile-Count": str(tile_count)},
    )


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
        log_level=LOG_LEVEL.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
