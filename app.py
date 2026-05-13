"""NiceGUI native desktop window for displayathon.

This is a thin client. The only thing it does is render a UI and POST to
displayathon-service over loopback HTTP. All BLE logic lives in
display_service.py + displayathon.py.

Run:
    python app.py              # native window via pywebview
    python app.py --web        # browser tab on http://localhost:49697
    DISPLAYATHON_URL=http://127.0.0.1:9000 python app.py
"""
from __future__ import annotations

import argparse
import base64
import json

from nicegui import ui, app as nicegui_app

from display_client import DEFAULT_BASE_URL, DisplayClient, Result, ServiceUnreachable


client = DisplayClient()


# ---------------------------------------------------------------------------
# shared mutable UI state
# ---------------------------------------------------------------------------

state = {
    "fonts": [],
    "service_ok": False,
    "service_msg": "checking…",
    "send_buttons": [],   # filled below; toggled enabled/disabled by health poll
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _notify_result(r: Result, prefix: str = "") -> None:
    if r.ok:
        ui.notify(f"{prefix}{r.message}", type="positive", position="bottom")
    else:
        ui.notify(f"{prefix}{r.message or 'error'}", type="negative", position="bottom")


def _bytes_to_data_url(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _update_send_buttons() -> None:
    for btn in state["send_buttons"]:
        if state["service_ok"]:
            btn.enable()
        else:
            btn.disable()


# ---------------------------------------------------------------------------
# tab builders
# ---------------------------------------------------------------------------

def build_solid_tab():
    with ui.column().classes("w-full gap-3"):
        color = ui.color_input(label="color", value="#ffcc00").classes("w-64")
        swatch = ui.element("div").classes("rounded-lg shadow-inner") \
            .style("height:80px; width:100%; border:1px solid #333;")

        def refresh() -> None:
            swatch.style(f"height:80px; width:100%; border:1px solid #333; "
                         f"background:{color.value};")
        color.on("update:model-value", lambda _: refresh())
        refresh()

        async def send() -> None:
            try:
                r = await client.solid(color.value)
            except ServiceUnreachable as e:
                ui.notify(f"service unreachable: {e}", type="negative")
                return
            _notify_result(r, "solid: ")

        btn = ui.button("Send to display", on_click=send, icon="send").props("color=primary")
        state["send_buttons"].append(btn)


def build_fade_tab():
    with ui.column().classes("w-full gap-3"):
        with ui.row().classes("gap-3 items-end"):
            a = ui.color_input(label="color A", value="#ff0000").classes("w-48")
            b = ui.color_input(label="color B", value="#00ff00").classes("w-48")
            frames = ui.number(label="frames", value=40, min=2, max=200, step=2).classes("w-28")

        strip = ui.element("div").classes("rounded-lg shadow-inner") \
            .style("height:80px; width:100%; border:1px solid #333;")

        def refresh() -> None:
            strip.style(
                "height:80px; width:100%; border:1px solid #333; "
                f"background: linear-gradient(90deg, {a.value} 0%, {b.value} 50%, {a.value} 100%);"
            )
        a.on("update:model-value", lambda _: refresh())
        b.on("update:model-value", lambda _: refresh())
        refresh()

        async def send() -> None:
            try:
                r = await client.fade(a.value, b.value, frames=int(frames.value or 40))
            except ServiceUnreachable as e:
                ui.notify(f"service unreachable: {e}", type="negative")
                return
            _notify_result(r, "fade: ")

        btn = ui.button("Send to display", on_click=send, icon="send").props("color=primary")
        state["send_buttons"].append(btn)


def build_gif_tab():
    gif = {"bytes": None, "name": None}

    with ui.column().classes("w-full gap-3"):
        ui.label("Upload a GIF — any size; the service will resize to 96×16 if needed.").classes("text-sm text-gray-500")

        info = ui.label("(no file selected)").classes("text-sm")
        preview = ui.image().classes("rounded border w-full").style("max-height:200px; object-fit:contain; background:#111;")
        preview.visible = False

        # Bypass NiceGUI's ui.upload entirely (Quasar shows 100% but the
        # Python on_upload callback never fires reliably). Use a plain
        # <input type=file>, read bytes in JS, stash on window.__datGifPending,
        # poll from Python. JS lives in GIF_PICKER_JS via add_body_html.
        ui.html('''
            <label class="block w-full text-center cursor-pointer py-4 rounded
                          border-2 border-dashed border-gray-500 hover:border-yellow-500
                          transition-colors">
                <input id="dat_gif_input" type="file" accept=".gif,image/gif"
                       style="display:none;">
                <span class="text-sm text-gray-300">
                    Click here or drop a .gif
                </span>
            </label>
        ''')

        async def poll_for_gif() -> None:
            try:
                drained = await ui.run_javascript(
                    "(function(){const p=window.__datGifPending; "
                    "window.__datGifPending=null; return p;})();",
                    timeout=2.0,
                )
            except Exception:
                return
            if not drained:
                return
            try:
                data = base64.b64decode(drained.get("b64", ""))
            except Exception as e:
                ui.notify(f"gif decode error: {e}", type="negative")
                return
            if not data:
                return
            gif["bytes"] = data
            gif["name"] = drained.get("name") or "upload.gif"
            info.text = f"{gif['name']} · {len(data):,} bytes — ready to send"
            preview.set_source(_bytes_to_data_url(data, "image/gif"))
            preview.visible = True
            ui.notify(
                f"loaded {gif['name']} ({len(data):,} bytes)",
                type="positive",
            )

        ui.timer(0.4, poll_for_gif)

        async def send() -> None:
            if not gif["bytes"]:
                ui.notify("pick a GIF first", type="warning")
                return
            try:
                r = await client.gif(gif["bytes"], gif["name"] or "upload.gif")
            except ServiceUnreachable as e:
                ui.notify(f"service unreachable: {e}", type="negative")
                return
            _notify_result(r, "gif: ")

        btn = ui.button("Send to display", on_click=send, icon="send").props("color=primary")
        state["send_buttons"].append(btn)


def build_text_tab():
    """Text tab uses the exact JS canvas tile renderer from the old ui.py so
    the on-screen pixels are byte-identical to the legacy Flask UI.

    Form values are NiceGUI widgets (so the look matches the rest of the app),
    but the actual pixel-quantize-to-RGB332 happens in JS in the page, and we
    POST the resulting bytes to /api/text/tiles unchanged.
    """
    with ui.column().classes("w-full gap-3"):
        text = ui.input(label="text", value="BOIS CLUB GAMES").classes("w-full")
        with ui.row().classes("gap-3 items-end"):
            font_sel = ui.select(
                options={"Helvetica": "Helvetica (loading…)"},
                value="Helvetica", label="font",
            ).classes("w-56")
            size = ui.number(label="size (px)", value=17, min=6, max=64).classes("w-28")
            letter_spacing = ui.number(label="letter-spacing", value=0, min=-4, max=8).classes("w-32")
            y_offset = ui.number(label="y-offset", value=0, min=-8, max=8).classes("w-28")
        with ui.row().classes("gap-3 items-end"):
            fg = ui.color_input(label="foreground", value="#ffffff").classes("w-44")
            bg = ui.color_input(label="background", value="#000000").classes("w-44")
            style_sel = ui.select(
                options={"normal": "normal", "italic": "italic", "oblique": "oblique"},
                value="normal", label="style",
            ).classes("w-32")
            weight_sel = ui.select(
                options={
                    "100": "100", "200": "200", "300": "300", "400": "400",
                    "500": "500", "600": "600", "700": "700", "800": "800", "900": "900",
                },
                value="900", label="weight",
            ).classes("w-28")
            antialias = ui.switch("antialias", value=False)

        # Visible 96×16 preview canvas, scaled 5× via CSS — matches old ui.py.
        ui.html(
            '<canvas id="dat_preview" width="96" height="16" '
            'style="width:480px; height:80px; background:#000; '
            'image-rendering:pixelated; border:1px solid #333; border-radius:6px;"></canvas>'
        )
        tile_label = ui.label("").classes("text-xs text-gray-500 font-mono")

        async def send() -> None:
            if not text.value:
                ui.notify("text is empty", type="warning")
                return
            # Read all values, hand them to the JS renderer, get base64 tiles back.
            opts = {
                "text": text.value,
                "family": font_sel.value or "monospace",
                "style": style_sel.value,
                "weight": weight_sel.value,
                "size": int(size.value or 18),
                "letterSpacing": int(letter_spacing.value or 0),
                "yOffset": int(y_offset.value or 0),
                "antialias": bool(antialias.value),
                "fg": fg.value,
                "bg": bg.value,
            }
            try:
                result = await ui.run_javascript(
                    f"return await window.datRenderTextTiles({json.dumps(opts)});",
                    timeout=5.0,
                )
            except Exception as e:
                ui.notify(f"canvas render error: {e}", type="negative")
                return
            if not result or "tilePixelsB64" not in result:
                ui.notify("canvas render returned nothing", type="negative")
                return
            try:
                r = await client.text_tiles(result["tilePixelsB64"], int(result["tileCount"]))
            except ServiceUnreachable as e:
                ui.notify(f"service unreachable: {e}", type="negative")
                return
            _notify_result(r, "text: ")

        btn = ui.button("Send to display", on_click=send, icon="send").props("color=primary")
        state["send_buttons"].append(btn)

        # ---- live preview wiring (JS-driven, scrolls in the visible canvas) --
        async def kick_preview() -> None:
            opts = {
                "text": text.value or "",
                "family": font_sel.value or "monospace",
                "style": style_sel.value,
                "weight": weight_sel.value,
                "size": int(size.value or 18),
                "letterSpacing": int(letter_spacing.value or 0),
                "yOffset": int(y_offset.value or 0),
                "antialias": bool(antialias.value),
                "fg": fg.value,
                "bg": bg.value,
            }
            await ui.run_javascript(
                f"window.datStartPreview({json.dumps(opts)});",
                timeout=2.0,
            )
            try:
                tile_count = await ui.run_javascript(
                    f"return window.datMeasureTiles({json.dumps(opts)});",
                    timeout=2.0,
                )
                if tile_count:
                    tile_label.text = (
                        f"{int(tile_count)} tile{'s' if int(tile_count) != 1 else ''} · "
                        f"{int(tile_count) * 96}×16 px"
                    )
            except Exception:
                pass

        debounce_timer = {"t": None}

        def schedule_kick() -> None:
            if debounce_timer["t"] is not None:
                debounce_timer["t"].cancel()
            debounce_timer["t"] = ui.timer(0.2, kick_preview, once=True)

        for w in (text, font_sel, size, letter_spacing, y_offset, fg, bg,
                  style_sel, weight_sel, antialias):
            w.on("update:model-value", lambda _: schedule_kick())

        # ---- font-list population + initial preview ---------------------------
        async def populate_fonts() -> None:
            try:
                fonts = await client.fonts()
            except Exception:
                return
            state["fonts"] = fonts
            # Build the dropdown.
            opts = {f["name"]: f"{f['name']} ({f['source']})" for f in fonts}
            font_sel.options = opts
            preferred = next((f["name"] for f in fonts if f["name"].lower() == "helvetica"), None)
            if preferred:
                font_sel.value = preferred
            elif fonts:
                font_sel.value = fonts[0]["name"]
            font_sel.update()
            # Ship the font catalog to JS so @font-face declarations get registered.
            await ui.run_javascript(
                f"window.datRegisterFonts({json.dumps(fonts)}, {json.dumps(client.base_url)});",
                timeout=5.0,
            )
            await kick_preview()

        nicegui_app.on_connect(populate_fonts)


# ---------------------------------------------------------------------------
# status banner — polls /api/health every 2s
# ---------------------------------------------------------------------------

def build_status_bar():
    bar = ui.row().classes("w-full items-center justify-between px-4 py-2 rounded-md") \
        .style("background:#111; color:#ccc; border-top:1px solid #333;")
    with bar:
        dot = ui.element("span").style(
            "display:inline-block; width:10px; height:10px; border-radius:50%; background:#888;"
        )
        msg = ui.label("checking…").classes("text-sm font-mono")
        ui.space()
        details = ui.label("").classes("text-xs font-mono text-gray-500")

    async def poll() -> None:
        try:
            h = await client.health()
        except ServiceUnreachable as e:
            state["service_ok"] = False
            state["service_msg"] = f"service unreachable on {client.base_url}"
            dot.style("display:inline-block; width:10px; height:10px; border-radius:50%; background:#e54;")
            msg.text = state["service_msg"]
            details.text = "run ./service/install.sh — or python display_service.py for foreground"
            _update_send_buttons()
            return
        state["service_ok"] = True
        busy = h.get("busy")
        up_s = h.get("uptime_s", 0)
        last = h.get("last_upload_ts", 0)
        dot.style(
            "display:inline-block; width:10px; height:10px; border-radius:50%; "
            f"background:{'#fb0' if busy else '#5f5'};"
        )
        msg.text = f"service ok · v{h.get('version','?')} · {'BUSY' if busy else 'idle'}"
        last_txt = "no uploads yet" if not last else f"last upload {_fmt_ago(last)}"
        details.text = f"uptime {_fmt_dur(up_s)} · {last_txt} · port {h.get('port','?')}"
        _update_send_buttons()

    ui.timer(2.0, poll)
    nicegui_app.on_connect(poll)


def _fmt_dur(s: int) -> str:
    if s < 60: return f"{s}s"
    if s < 3600: return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _fmt_ago(ts: int) -> str:
    import time as _t
    delta = max(0, int(_t.time()) - ts)
    return f"{_fmt_dur(delta)} ago"


# ---------------------------------------------------------------------------
# layout
# ---------------------------------------------------------------------------

GIF_PICKER_JS = r"""
<script>
// File-picker bridge for the GIF tab. Reads picked file as base64, stashes
// it on window.__datGifPending for the Python poller to drain.
(function(){
  function wireOnce(){
    const inp = document.getElementById('dat_gif_input');
    if (!inp || inp._datWired) return false;
    inp._datWired = true;

    inp.addEventListener('change', (ev) => {
      const f = ev.target.files && ev.target.files[0];
      if (!f) return;
      const r = new FileReader();
      r.onload = () => {
        const s = r.result || '';
        const comma = String(s).indexOf(',');
        window.__datGifPending = {
          name: f.name, size: f.size,
          b64: comma >= 0 ? String(s).slice(comma+1) : String(s),
        };
      };
      r.readAsDataURL(f);
    });
    return true;
  }
  // The input is rendered by NiceGUI after page mount; retry until present.
  if (!wireOnce()) {
    const iv = setInterval(() => { if (wireOnce()) clearInterval(iv); }, 200);
    setTimeout(() => clearInterval(iv), 10000);
  }
})();
</script>
"""

CANVAS_RENDERER_JS = r"""
<script>
// Canvas-based RGB332 tile renderer — ported verbatim from the old ui.py.
// Pixel output is byte-identical to the legacy Flask UI.

(function () {
  const TILE_W = 96, TILE_H = 16;

  function hexToRgb(h){
    h = (h||'').replace('#',''); if (h.length === 3) h = h.split('').map(c => c+c).join('');
    return [parseInt(h.slice(0,2),16)||0, parseInt(h.slice(2,4),16)||0, parseInt(h.slice(4,6),16)||0];
  }

  function composeFont(o){
    // matches old ui.py's CSS font shorthand
    const fam = (o.family && o.family !== '(default)') ? `"${o.family}"` : 'monospace';
    return `${o.style||'normal'} ${o.weight||'400'} ${Math.max(6, parseInt(o.size)||18)}px ${fam}`;
  }

  function drawScrollFrame(canvas, text, cfg, fgCss, bgCss, offsetPx){
    const w = canvas.width, h = canvas.height;
    const ctx = canvas.getContext('2d');
    ctx.imageSmoothingEnabled = cfg.antialias;
    ctx.textRendering = cfg.antialias ? 'optimizeLegibility' : 'geometricPrecision';
    ctx.fillStyle = bgCss; ctx.fillRect(0, 0, w, h);
    ctx.font = cfg.font;
    ctx.textBaseline = 'middle';
    ctx.fillStyle = fgCss;
    if (cfg.letterSpacing) ctx.letterSpacing = cfg.letterSpacing + 'px';
    const y = Math.round(h/2) + (cfg.yOffset||0);
    if (cfg.letterSpacing && !('letterSpacing' in ctx)) {
      let x = offsetPx;
      for (const ch of text) { ctx.fillText(ch, x, y); x += ctx.measureText(ch).width + cfg.letterSpacing; }
    } else {
      ctx.fillText(text, offsetPx, y);
    }
  }

  function measureTextWidth(cfg, text){
    const c = document.createElement('canvas'); const ctx = c.getContext('2d');
    ctx.font = cfg.font;
    if (cfg.letterSpacing && 'letterSpacing' in ctx) ctx.letterSpacing = cfg.letterSpacing + 'px';
    if (cfg.letterSpacing && !('letterSpacing' in ctx)) {
      let w = 0; for (const ch of text) w += ctx.measureText(ch).width + cfg.letterSpacing; return Math.ceil(w);
    }
    return Math.ceil(ctx.measureText(text).width);
  }

  function buildCfg(o){
    return {
      font: composeFont(o),
      antialias: !!o.antialias,
      letterSpacing: parseInt(o.letterSpacing)||0,
      yOffset: parseInt(o.yOffset)||0,
    };
  }

  // ---- public: register fonts from the service catalog ------------------
  const _registered = new Set();
  window.datRegisterFonts = async function(fonts, serviceBaseUrl){
    for (const f of fonts) {
      if (f.source === 'system') continue;          // browser already has these
      if (_registered.has(f.name)) continue;
      const url = `${serviceBaseUrl}/api/fonts/${encodeURIComponent(f.name)}/file`;
      try {
        const ff = new FontFace(f.name, `url(${url})`, {display: 'swap'});
        await ff.load();
        document.fonts.add(ff);
        _registered.add(f.name);
      } catch (e) {
        console.warn('font load failed', f.name, e);
      }
    }
  };

  // ---- public: render tiles → base64 + count (same algorithm as old ui.py)
  window.datRenderTextTiles = async function(opts){
    const cfg = buildCfg(opts);
    const [ar,ag,ab] = hexToRgb(opts.fg);
    const [br,bg,bb] = hexToRgb(opts.bg);
    const fg = `rgb(${ar},${ag},${ab})`, bgCss = `rgb(${br},${bg},${bb})`;
    // ensure the requested font is actually loaded before measuring/drawing
    try { await document.fonts.load(cfg.font); } catch (e) {}

    const text = opts.text || '';
    const textW = measureTextWidth(cfg, text);
    const tileCount = Math.max(1, Math.ceil(textW / TILE_W));
    const bmpW = tileCount * TILE_W;

    const big = document.createElement('canvas');
    big.width = bmpW; big.height = TILE_H;
    drawScrollFrame(big, text, cfg, fg, bgCss, 0);
    const img = big.getContext('2d').getImageData(0, 0, bmpW, TILE_H).data;
    const out = new Uint8Array(tileCount * TILE_W * TILE_H);
    for (let t = 0; t < tileCount; t++) {
      const x0 = t * TILE_W;
      for (let y = 0; y < TILE_H; y++) {
        for (let x = 0; x < TILE_W; x++) {
          const p = (y * bmpW + (x0 + x)) * 4;
          const r = img[p], g = img[p+1], b = img[p+2];
          out[t*TILE_W*TILE_H + y*TILE_W + x] =
            (r & 0xE0) | ((g & 0xE0) >> 3) | ((b & 0xC0) >> 6);
        }
      }
    }
    let bin = ''; for (let i = 0; i < out.length; i++) bin += String.fromCharCode(out[i]);
    return { tilePixelsB64: btoa(bin), tileCount: tileCount };
  };

  // ---- public: tile count for the current opts (preview helper) ---------
  window.datMeasureTiles = function(opts){
    const cfg = buildCfg(opts);
    const w = measureTextWidth(cfg, opts.text || '');
    return Math.max(1, Math.ceil(w / TILE_W));
  };

  // ---- public: scrolling preview in the visible #dat_preview canvas -----
  let _previewTimer = null;
  window.datStartPreview = async function(opts){
    if (_previewTimer) { clearInterval(_previewTimer); _previewTimer = null; }
    const cv = document.getElementById('dat_preview');
    if (!cv) return;
    cv.width = TILE_W; cv.height = TILE_H;
    const cfg = buildCfg(opts);
    try { await document.fonts.load(cfg.font); } catch (e) {}
    const [ar,ag,ab] = hexToRgb(opts.fg);
    const [br,bg,bb] = hexToRgb(opts.bg);
    const fg = `rgb(${ar},${ag},${ab})`, bgCss = `rgb(${br},${bg},${bb})`;
    const text = opts.text || '';
    const tw = measureTextWidth(cfg, text);
    const totalScroll = cv.width + tw;
    let i = 0;
    _previewTimer = setInterval(() => {
      const x = cv.width - (i * 2) % totalScroll;
      drawScrollFrame(cv, text, cfg, fg, bgCss, x);
      i++;
    }, 50);
  };
})();
</script>
"""


@ui.page("/")
def index() -> None:
    ui.add_head_html(
        "<style>"
        "body { background: #0a0a0a; color: #ddd; }"
        ".q-tab--active { color: #ffcc00 !important; }"
        ".q-tab__indicator { background: #ffcc00 !important; }"
        "</style>"
    )
    ui.add_body_html(CANVAS_RENDERER_JS)
    ui.add_body_html(GIF_PICKER_JS)

    with ui.column().classes("w-full max-w-3xl mx-auto p-4 gap-4"):
        ui.label("displayathon").classes("text-2xl font-semibold")
        ui.label(f"→ {client.base_url}").classes("text-xs text-gray-500 font-mono")

        with ui.card().classes("w-full"):
            with ui.tabs().classes("w-full") as tabs:
                t_solid = ui.tab("Solid", icon="palette")
                t_fade  = ui.tab("Fade",  icon="gradient")
                t_gif   = ui.tab("GIF",   icon="movie")
                t_text  = ui.tab("Text",  icon="text_fields")
            with ui.tab_panels(tabs, value=t_text).classes("w-full"):
                with ui.tab_panel(t_solid): build_solid_tab()
                with ui.tab_panel(t_fade):  build_fade_tab()
                with ui.tab_panel(t_gif):   build_gif_tab()
                with ui.tab_panel(t_text):  build_text_tab()

        build_status_bar()


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="displayathon NiceGUI app")
    parser.add_argument("--web", action="store_true",
                        help="run as a browser tab instead of a native window")
    parser.add_argument("--port", type=int, default=49697,
                        help="port for the NiceGUI UI itself (default 49697)")
    args = parser.parse_args()

    ui.run(
        title="displayathon",
        native=not args.web,
        window_size=(760, 820),
        host="127.0.0.1",
        port=args.port,
        reload=False,
        show=args.web,
        dark=True,
    )


if __name__ == "__main__":
    # Required for PyInstaller-bundled multiprocessing: NiceGUI's worker
    # subprocesses re-exec this binary with internal flags, and freeze_support
    # short-circuits before argparse sees them.
    import multiprocessing
    multiprocessing.freeze_support()
    main()
