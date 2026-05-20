// displayathon — Stream Deck plugin host.
// Runs in the Stream Deck CEF webview. Talks WebSocket to the SD app on
// localhost and dispatches HTTP calls to the displayathon service on the
// user-configured base URL (single global setting — shared by every key).

(() => {
  const DEFAULT_BASE = 'http://127.0.0.1:49696';
  let ws = null;
  let pluginUUID = null;
  // globalSettings.base holds the entire host+port string, e.g.
  // 'http://192.168.1.42:49696'. The PI lets the user edit this once and
  // every key uses it. Per-key settings are stored elsewhere.
  let globalSettings = { base: DEFAULT_BASE };

  // ---------- WebSocket plumbing ----------
  function connectElgatoStreamDeckSocket(inPort, inPluginUUID, inRegisterEvent, _inInfo) {
    pluginUUID = inPluginUUID;
    ws = new WebSocket('ws://127.0.0.1:' + inPort);

    ws.onopen = () => {
      ws.send(JSON.stringify({ event: inRegisterEvent, uuid: inPluginUUID }));
      ws.send(JSON.stringify({ event: 'getGlobalSettings', context: inPluginUUID }));
    };

    ws.onmessage = (evt) => {
      let msg;
      try { msg = JSON.parse(evt.data); } catch (_) { return; }
      handleSDMessage(msg);
    };

    ws.onerror = (e) => log('ws error', e && e.message);
    ws.onclose = () => log('ws closed');
  }
  window.connectElgatoStreamDeckSocket = connectElgatoStreamDeckSocket;

  // ---------- helpers ----------
  function log(...args) {
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({
        event: 'logMessage',
        payload: { message: '[displayathon] ' + args.map((a) => typeof a === 'string' ? a : JSON.stringify(a)).join(' ') }
      }));
    }
  }

  function getBase() {
    return String(globalSettings.base || DEFAULT_BASE).replace(/\/+$/, '');
  }

  function showOk(context) {
    if (!ws) return;
    ws.send(JSON.stringify({ event: 'showOk', context }));
  }
  function showAlert(context) {
    if (!ws) return;
    ws.send(JSON.stringify({ event: 'showAlert', context }));
  }
  function setTitle(context, title) {
    if (!ws) return;
    ws.send(JSON.stringify({
      event: 'setTitle',
      context,
      payload: { title: title == null ? '' : String(title), target: 0 }
    }));
  }

  async function http(base, path, init) {
    const url = base + path;
    try {
      const res = await fetch(url, init);
      let json = null;
      try { json = await res.json(); } catch (_) { json = {}; }
      return { ok: res.ok && (json && json.ok !== false), status: res.status, json };
    } catch (err) {
      return { ok: false, status: 0, json: { ok: false, message: (err && err.message) || 'network_error' } };
    }
  }

  async function postJSON(base, path, body) {
    return http(base, path, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
  }

  // ---------- per-action handlers ----------
  // Each handler takes ({ context, settings }) and returns true on success.

  async function actSolid({ settings }) {
    const color = (settings.hex || '#ffcc00');
    const r = await postJSON(getBase(), '/api/solid', { color });
    return r.ok;
  }

  async function actFade({ settings }) {
    const a = settings.hexA || '#ff0000';
    const b = settings.hexB || '#0000ff';
    const frames = clampInt(settings.frames, 2, 200, 40);
    const r = await postJSON(getBase(), '/api/fade', { color_a: a, color_b: b, frames });
    return r.ok;
  }

  async function actText({ settings }) {
    if (!settings.text) return false;
    const body = {
      text: settings.text,
      fg: settings.fg || '#ffffff',
      bg: settings.bg || '#000000',
      font: settings.font || 'Helvetica',
      size: clampInt(settings.size, 6, 64, 17),
      letter_spacing: clampInt(settings.letter_spacing, -4, 8, 0),
      y_offset: clampInt(settings.y_offset, -8, 8, 0),
      antialias: !!settings.antialias,
      weight: clampInt(settings.weight, 100, 900, 900),
      style: settings.style || 'normal',
    };
    const r = await postJSON(getBase(), '/api/text', body);
    return r.ok;
  }

  async function actGif({ settings, context }) {
    const path = (settings.path || '').trim();
    if (!path) return false;
    // Stream Deck's CEF can read files via fetch('file://...') — load the
    // .gif from disk, then POST it as a raw image/gif body. We don't use
    // multipart here because constructing FormData with a real File object
    // from arbitrary disk paths is awkward in the CEF sandbox, and the
    // service happily accepts a raw body too (see app's /api/gif handler).
    let buf;
    try {
      const fileUrl = path.startsWith('file://') ? path : 'file://' + path;
      const res = await fetch(fileUrl);
      if (!res.ok) { log('gif fetch failed', res.status, path); return false; }
      buf = await res.arrayBuffer();
    } catch (err) {
      log('gif read error', err && err.message, path);
      return false;
    }
    const r = await http(getBase(), '/api/gif', {
      method: 'POST',
      headers: { 'content-type': 'image/gif' },
      body: buf,
    });
    if (!r.ok) log('gif post failed', r.status, (r.json && r.json.message) || '');
    return r.ok;
  }

  async function actHealth({ context, settings }) {
    const r = await http(getBase(), '/api/health');
    const j = r.json || {};
    if (!r.ok) {
      setTitle(context, 'down');
      return false;
    }
    const busy = j.busy ? '⌛' : '✓';
    const ble = j.ble_ready ? '' : '\nble?';
    setTitle(context, `${busy} v${j.version || '?'}\n${formatDur(j.uptime_s || 0)}${ble}`);
    return true;
  }

  async function actRewarm({ context }) {
    const r = await postJSON(getBase(), '/api/ble/rewarm', {});
    return r.ok;
  }

  const HANDLERS = {
    'com.displayathon.controller.solid':   actSolid,
    'com.displayathon.controller.fade':    actFade,
    'com.displayathon.controller.text':    actText,
    'com.displayathon.controller.gif':     actGif,
    'com.displayathon.controller.health':  actHealth,
    'com.displayathon.controller.rewarm':  actRewarm,
  };

  // ---------- SD event dispatch ----------
  function handleSDMessage(msg) {
    const { event, action, context, payload } = msg;
    if (event === 'didReceiveGlobalSettings') {
      const incoming = (payload && payload.settings) || {};
      if (!incoming.base) incoming.base = DEFAULT_BASE;
      globalSettings = incoming;
      log('global settings received: base=' + globalSettings.base);
      return;
    }
    if (event === 'keyDown') {
      const settings = (payload && payload.settings) || {};
      const handler = HANDLERS[action];
      if (!handler) {
        log('no handler for action', action);
        showAlert(context);
        return;
      }
      Promise.resolve(handler({ context, settings, action, payload }))
        .then((ok) => { if (ok) showOk(context); else showAlert(context); })
        .catch((err) => { log('handler error', err && err.message); showAlert(context); });
      return;
    }
    if (event === 'willAppear') {
      const settings = (payload && payload.settings) || {};
      if (settings.label) setTitle(context, settings.label);
      // For the health key, refresh state on appear so users see status
      // without having to press first.
      if (action === 'com.displayathon.controller.health') {
        actHealth({ context, settings }).catch(() => {});
      }
      return;
    }
    if (event === 'sendToPlugin') {
      handleSendToPlugin(action, context, (payload && payload) || {});
      return;
    }
  }

  async function handleSendToPlugin(action, context, payload) {
    const cmd = payload.command;
    // Trust the base URL the PI is currently editing, so a freshly-typed
    // host pings the new URL even before its setGlobalSettings has come back.
    const base = payload.base
      ? String(payload.base).replace(/\/+$/, '')
      : getBase();
    if (cmd === 'ping') {
      const r = await http(base, '/api/health');
      sendToPI(context, action, { command: 'ping', ok: r.ok, status: r.status, body: r.json || {} });
      return;
    }
    if (cmd === 'getFonts') {
      const r = await http(base, '/api/fonts');
      sendToPI(context, action, { command: 'fonts', ok: r.ok, fonts: (r.json && r.json.fonts) || [] });
      return;
    }
    if (cmd === 'rewarm') {
      const r = await postJSON(base, '/api/ble/rewarm', {});
      sendToPI(context, action, { command: 'rewarm', ok: r.ok, body: r.json || {} });
      return;
    }
  }

  function sendToPI(context, action, payload) {
    if (!ws) return;
    ws.send(JSON.stringify({ event: 'sendToPropertyInspector', context, action, payload }));
  }

  // ---------- utils ----------
  function clampInt(v, min, max, fallback) {
    const n = parseInt(v, 10);
    if (!Number.isFinite(n)) return fallback;
    return Math.max(min, Math.min(max, n));
  }
  function formatDur(s) {
    s = Math.max(0, parseInt(s, 10) || 0);
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm';
    if (s < 86400) return Math.floor(s / 3600) + 'h';
    return Math.floor(s / 86400) + 'd';
  }
})();
