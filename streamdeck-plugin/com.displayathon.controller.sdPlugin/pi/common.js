// Shared Property-Inspector runtime for displayathon. Connects to Stream
// Deck, persists per-action settings, and persists ONE GLOBAL setting for
// the displayathon service base URL — so it's configured once and every
// displayathon key uses the same target.

(() => {
  const DEFAULT_BASE = 'http://127.0.0.1:49696';

  const pi = window.datPI = {
    settings: {},                  // per-action settings
    global: { base: DEFAULT_BASE },
    context: null,
    actionUUID: null,
    ws: null,
    onSettings: () => {},
    onMessage: () => {}
  };

  window.connectElgatoStreamDeckSocket = function (inPort, inUUID, inRegisterEvent, _inInfo, inActionInfo) {
    pi.context = inUUID;
    let parsed = {};
    try { parsed = JSON.parse(inActionInfo); } catch (_) {}
    pi.actionUUID = parsed.action;
    pi.settings = (parsed.payload && parsed.payload.settings) || {};

    pi.ws = new WebSocket('ws://127.0.0.1:' + inPort);
    pi.ws.onopen = () => {
      pi.ws.send(JSON.stringify({ event: inRegisterEvent, uuid: inUUID }));
      pi.ws.send(JSON.stringify({ event: 'getGlobalSettings', context: inUUID }));
      // Fire onSettings now so per-action fields render immediately; the
      // base-URL field updates separately when global settings come back.
      pi.onSettings(pi.settings);
    };
    pi.ws.onmessage = (evt) => {
      let msg;
      try { msg = JSON.parse(evt.data); } catch (_) { return; }
      if (msg.event === 'didReceiveSettings') {
        pi.settings = (msg.payload && msg.payload.settings) || {};
        pi.onSettings(pi.settings);
      } else if (msg.event === 'didReceiveGlobalSettings') {
        const g = (msg.payload && msg.payload.settings) || {};
        if (!g.base) g.base = DEFAULT_BASE;
        pi.global = g;
        const baseEl = document.getElementById('dat-base');
        if (baseEl) baseEl.value = pi.global.base;
      } else if (msg.event === 'sendToPropertyInspector') {
        pi.onMessage(msg.payload || {});
      }
    };
  };

  pi.save = function () {
    if (!pi.ws) return;
    pi.ws.send(JSON.stringify({
      event: 'setSettings',
      context: pi.context,
      payload: pi.settings
    }));
  };

  pi.saveGlobal = function () {
    if (!pi.ws) return;
    pi.ws.send(JSON.stringify({
      event: 'setGlobalSettings',
      context: pi.context,
      payload: pi.global
    }));
  };

  pi.ask = function (cmd, extra) {
    if (!pi.ws) return;
    const payload = Object.assign({ command: cmd, base: pi.global.base }, extra || {});
    pi.ws.send(JSON.stringify({
      event: 'sendToPlugin',
      context: pi.context,
      action: pi.actionUUID,
      payload
    }));
  };

  pi.bind = function (selector, key, transform) {
    const el = document.querySelector(selector);
    if (!el) return;
    const apply = () => {
      let v;
      if (el.type === 'checkbox') v = el.checked;
      else v = el.value;
      if (transform) v = transform(v);
      pi.settings[key] = v;
      pi.save();
    };
    if (el.type === 'range' || el.type === 'color' || el.tagName === 'SELECT') {
      el.addEventListener('input', apply);
      el.addEventListener('change', apply);
    } else if (el.type === 'text' || el.type === 'number') {
      el.addEventListener('change', apply);
      el.addEventListener('blur', apply);
    } else {
      el.addEventListener('change', apply);
    }
  };

  pi.fillOptions = function (selectSelector, items, getValue, getLabel, currentValue) {
    const sel = document.querySelector(selectSelector);
    if (!sel) return;
    sel.innerHTML = '';
    items.forEach((it) => {
      const opt = document.createElement('option');
      opt.value = getValue(it);
      opt.textContent = getLabel(it);
      if (currentValue != null && String(opt.value) === String(currentValue)) opt.selected = true;
      sel.appendChild(opt);
    });
  };

  pi.serverBlock = function () {
    return `
      <h2>Service (global) <span class="status" id="dat-server-status">unknown</span></h2>
      <div class="row">
        <label>Base URL</label>
        <input type="text" id="dat-base" placeholder="http://127.0.0.1:49696">
        <button id="dat-test">Test</button>
      </div>
      <div class="hint">One value shared by <em>every</em> displayathon key. Default <code>http://127.0.0.1:49696</code>; point at another machine like <code>http://192.168.1.42:49696</code>.</div>
      <hr>
    `;
  };

  pi.wireServerBlock = function () {
    const baseEl = document.getElementById('dat-base');
    const status = document.getElementById('dat-server-status');
    if (!baseEl) return;
    baseEl.value = pi.global.base || DEFAULT_BASE;
    baseEl.addEventListener('change', () => {
      pi.global.base = (baseEl.value.trim() || DEFAULT_BASE);
      pi.saveGlobal();
    });
    document.getElementById('dat-test').addEventListener('click', () => {
      status.textContent = 'pinging…'; status.className = 'status';
      pi.ask('ping');
    });
    pi.onMessage = ((prev) => (m) => {
      if (m.command === 'ping') {
        if (m.ok) {
          const v = (m.body && m.body.version) || '?';
          const ble = (m.body && m.body.ble_ready) ? 'BLE ✓' : 'BLE ✗';
          status.textContent = `connected · v${v} · ${ble}`;
          status.className = 'status good';
        } else {
          status.textContent = `unreachable (${m.status || 'no response'})`;
          status.className = 'status bad';
        }
      }
      if (prev) prev(m);
    })(pi.onMessage);
  };

  // Optional helper: load fonts into a <select id="...">.
  pi.loadFonts = function (selectId, currentValue) {
    pi.ask('getFonts');
    pi.onMessage = ((prev) => (m) => {
      if (m.command === 'fonts') {
        const items = (m.fonts || []).map((f) => ({
          name: f.name, label: `${f.name} (${f.source})`,
        }));
        if (items.length === 0) items.push({ name: 'Helvetica', label: 'Helvetica (default)' });
        pi.fillOptions('#' + selectId, items, (i) => i.name, (i) => i.label, currentValue || 'Helvetica');
      }
      if (prev) prev(m);
    })(pi.onMessage);
  };
})();
