// Pure-Node PNG generator for displayathon Stream Deck icons.
// Renders an LED-matrix-style icon for each action: a 96×16-ish grid of
// "LEDs" drawn as small rounded squares, with the lit pixels arranged to
// form a glyph that telegraphs the action (solid block, fade gradient,
// scrolling text, GIF frame, heartbeat, refresh arrow).
//
// Each glyph has two variants: an in-app icon (Icon: in manifest) and a
// per-state on-key icon — same drawing, slightly darker background.
//
// Pure Node — no native deps. Run from the plugin root: `node make-icons.js`.

const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

const OUT = path.join(__dirname, 'com.displayathon.controller.sdPlugin', 'images');
fs.mkdirSync(OUT, { recursive: true });

// ---------- PNG plumbing ----------
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
    t[n] = c >>> 0;
  }
  return t;
})();
function crc32(buf) {
  let c = 0xFFFFFFFF;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xFF] ^ (c >>> 8);
  return (c ^ 0xFFFFFFFF) >>> 0;
}
function chunk(type, data) {
  const len = Buffer.alloc(4); len.writeUInt32BE(data.length, 0);
  const t = Buffer.from(type, 'ascii');
  const crc = Buffer.alloc(4); crc.writeUInt32BE(crc32(Buffer.concat([t, data])), 0);
  return Buffer.concat([len, t, data, crc]);
}
function hex2rgb(hex) {
  hex = hex.replace('#', '');
  return [parseInt(hex.slice(0, 2), 16), parseInt(hex.slice(2, 4), 16), parseInt(hex.slice(4, 6), 16)];
}

// ---------- canvas ----------
function makeCanvas(W, H) {
  const raw = Buffer.alloc(H * (1 + W * 4));
  for (let y = 0; y < H; y++) raw[y * (1 + W * 4)] = 0; // filter byte
  return { W, H, raw };
}
function setPx(cv, x, y, rgba) {
  if (x < 0 || x >= cv.W || y < 0 || y >= cv.H) return;
  const off = y * (1 + cv.W * 4) + 1 + x * 4;
  const a = rgba[3] / 255;
  cv.raw[off]     = Math.round(cv.raw[off]     * (1 - a) + rgba[0] * a);
  cv.raw[off + 1] = Math.round(cv.raw[off + 1] * (1 - a) + rgba[1] * a);
  cv.raw[off + 2] = Math.round(cv.raw[off + 2] * (1 - a) + rgba[2] * a);
  cv.raw[off + 3] = Math.max(cv.raw[off + 3], rgba[3]);
}
function fillRect(cv, x, y, w, h, rgba) {
  for (let dy = 0; dy < h; dy++)
    for (let dx = 0; dx < w; dx++)
      setPx(cv, x + dx, y + dy, rgba);
}
function roundedMask(x, y, W, H, radius) {
  const r = radius;
  if (x < r && y < r) return ((r - x) ** 2 + (r - y) ** 2) <= r * r;
  if (x < r && y >= H - r) return ((r - x) ** 2 + (y - (H - r - 1)) ** 2) <= r * r;
  if (x >= W - r && y < r) return ((x - (W - r - 1)) ** 2 + (r - y) ** 2) <= r * r;
  if (x >= W - r && y >= H - r) return ((x - (W - r - 1)) ** 2 + (y - (H - r - 1)) ** 2) <= r * r;
  return true;
}
function fillBackground(cv, bgHex, radius) {
  const [r, g, b] = hex2rgb(bgHex);
  for (let y = 0; y < cv.H; y++) {
    for (let x = 0; x < cv.W; x++) {
      if (!roundedMask(x, y, cv.W, cv.H, radius)) continue;
      const off = y * (1 + cv.W * 4) + 1 + x * 4;
      cv.raw[off] = r; cv.raw[off + 1] = g; cv.raw[off + 2] = b; cv.raw[off + 3] = 255;
    }
  }
}
function emit(cv) {
  const sig = Buffer.from([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]);
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(cv.W, 0); ihdr.writeUInt32BE(cv.H, 4);
  ihdr[8] = 8; ihdr[9] = 6; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0;
  return Buffer.concat([
    sig,
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(cv.raw)),
    chunk('IEND', Buffer.alloc(0))
  ]);
}

// ---------- LED matrix ----------
// Draw an N×M grid of LED-dot squares centered in the canvas, with a per-cell
// color function. dotMask determines which cells are LIT (0..1).
function drawMatrix(cv, cols, rows, colorFn) {
  const pad = Math.round(cv.W * 0.10);
  const gridW = cv.W - 2 * pad;
  const gridH = Math.round(cv.H * (rows / cols) * 0.55);
  const offX = pad;
  const offY = Math.round((cv.H - gridH) / 2);
  const cellW = gridW / cols;
  const cellH = gridH / rows;
  const dotR = Math.max(1, Math.floor(Math.min(cellW, cellH) * 0.40));
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const cx = Math.round(offX + cellW * (c + 0.5));
      const cy = Math.round(offY + cellH * (r + 0.5));
      const rgba = colorFn(c, r);
      if (!rgba) continue;
      drawDot(cv, cx, cy, dotR, rgba);
    }
  }
}
function drawDot(cv, cx, cy, r, rgba) {
  for (let dy = -r; dy <= r; dy++) {
    for (let dx = -r; dx <= r; dx++) {
      const d2 = dx * dx + dy * dy;
      if (d2 > r * r) continue;
      // soft edge
      const t = 1 - Math.sqrt(d2) / (r + 0.5);
      const a = Math.max(0, Math.min(255, Math.round(rgba[3] * t)));
      setPx(cv, cx + dx, cy + dy, [rgba[0], rgba[1], rgba[2], a]);
    }
  }
}

// ---------- glyphs (lit cells per action) ----------
const COLS = 12;  // narrow matrix that fits the icon
const ROWS = 4;   // chunky rows for legibility at SD's tiny sizes

// Each glyph returns [r,g,b,a] for lit cells, null for unlit.
const DIM = [40, 40, 50, 220];

function clampByte(n) { return Math.max(0, Math.min(255, Math.round(n))); }
function withA(rgb, a) { return [rgb[0], rgb[1], rgb[2], a]; }

const GLYPHS = {
  // Plugin chrome: warm gold full matrix (the display lit up)
  plugin(col, row) {
    return [255, 204, 0, 230];
  },
  category(col, row) {
    return [255, 204, 0, 230];
  },

  // Solid: a solid block of one color (orange) — every cell lit.
  solid(col, row) {
    return [255, 130, 30, 235];
  },

  // Fade: left→right gradient between two colors.
  fade(col, row) {
    const t = col / (COLS - 1);
    const a = hex2rgb('#ff3060');
    const b = hex2rgb('#3060ff');
    return [
      clampByte(a[0] * (1 - t) + b[0] * t),
      clampByte(a[1] * (1 - t) + b[1] * t),
      clampByte(a[2] * (1 - t) + b[2] * t),
      235
    ];
  },

  // Text: spell "DAT" across the rows with chunky pixels.
  text(col, row) {
    // 3-letter mask: rows 0..3, cols 0..11 — 3 letters wide w/ 1px gutters
    // Each letter is 3 cols × 4 rows. Letters: D, A, T
    const letters = [
      // D (col 0..2)
      ['XX.', 'X.X', 'X.X', 'XX.'],
      // A (col 4..6)
      ['.X.', 'X.X', 'XXX', 'X.X'],
      // T (col 8..10)
      ['XXX', '.X.', '.X.', '.X.'],
    ];
    const letterIdx = Math.floor(col / 4);
    const inLetterCol = col % 4;
    if (letterIdx >= letters.length || inLetterCol >= 3) return null;
    const ch = letters[letterIdx][row] && letters[letterIdx][row][inLetterCol];
    if (ch !== 'X') return null;
    return [255, 240, 80, 240];
  },

  // GIF: a tiny animation frame, alternating mid-screen dots.
  gif(col, row) {
    // checkerboard-ish blocks, three vibrant hues
    const hue = (col + row * 3) % 3;
    if ((col + row) % 2 === 1) return null;
    if (hue === 0) return [255, 80, 160, 235];
    if (hue === 1) return [80, 220, 255, 235];
    return [255, 220, 80, 235];
  },

  // Health: heartbeat trace across the row.
  health(col, row) {
    const trace = [2, 2, 2, 1, 0, 3, 0, 1, 2, 2, 2, 2];   // row index per col (0=top)
    if (row !== trace[col]) return null;
    return [80, 255, 160, 240];
  },

  // Rewarm: arrow curling around — bottom row going right, then up-right corner.
  rewarm(col, row) {
    // Define a curved arrow shape across cells:
    //   row 0: cols 6..10 lit (top horizontal of curl)
    //   row 1: col 10 lit, col 11 lit (right vertical)
    //   row 2: cols 4..11 lit (bottom horizontal)
    //   row 3: col 4 lit (arrowhead)
    const lit = {
      0: [6, 7, 8, 9, 10],
      1: [10, 11],
      2: [4, 5, 6, 7, 8, 9, 10, 11],
      3: [4, 5],
    };
    if (!(lit[row] || []).includes(col)) return null;
    return [255, 180, 80, 240];
  },
};

// ---------- icon renderer ----------
function makeIcon(size, glyphKey, opts) {
  const cv = makeCanvas(size, size);
  const radius = Math.round(size * 0.18);
  fillBackground(cv, opts.bg, radius);

  // Top label dot — small displayathon mark in the upper-left to brand it.
  // Subtle so it doesn't crowd the glyph at SD's tiny render sizes.
  if (opts.brand !== false) {
    const m = Math.max(2, Math.round(size * 0.045));
    fillRect(cv, Math.round(size * 0.08), Math.round(size * 0.08), m, m, [255, 204, 0, 230]);
  }

  // Draw the LED matrix using the glyph's lit-cell function. Unlit cells
  // get a dim dot so it reads as a real LED panel even on the dim ones.
  const glyph = GLYPHS[glyphKey] || GLYPHS.plugin;
  drawMatrix(cv, COLS, ROWS, (c, r) => {
    const lit = glyph(c, r);
    if (lit) return lit;
    return DIM;
  });

  return emit(cv);
}

// ---------- icon set ----------
// Pairs of in-app + on-key icons per action. Backgrounds match the BoisClub
// plugin's tonal contrast: brighter bg for in-app, darker bg for on-key.
const ICONS = {
  // Plugin chrome (warm gold)
  'plugin':       { bg: '#1a1a1a', glyph: 'plugin' },
  'category':     { bg: '#1a1a1a', glyph: 'category' },

  // Solid (orange block)
  'solid':        { bg: '#1f130a', glyph: 'solid' },
  'solid-key':    { bg: '#150e06', glyph: 'solid' },

  // Fade (gradient)
  'fade':         { bg: '#0e0f1f', glyph: 'fade' },
  'fade-key':     { bg: '#07081a', glyph: 'fade' },

  // Text (yellow letters)
  'text':         { bg: '#1a1604', glyph: 'text' },
  'text-key':     { bg: '#120e02', glyph: 'text' },

  // GIF (multicolor checker)
  'gif':          { bg: '#160d1c', glyph: 'gif' },
  'gif-key':      { bg: '#0d0712', glyph: 'gif' },

  // Health (green heartbeat)
  'health':       { bg: '#06190f', glyph: 'health' },
  'health-key':   { bg: '#03120a', glyph: 'health' },

  // Rewarm (orange refresh arrow)
  'rewarm':       { bg: '#1c1308', glyph: 'rewarm' },
  'rewarm-key':   { bg: '#120b03', glyph: 'rewarm' },
};

function writePair(name, spec) {
  for (const [suffix, size] of [['', 144], ['@2x', 288]]) {
    const png = makeIcon(size, spec.glyph, { bg: spec.bg });
    const p = path.join(OUT, `${name}${suffix}.png`);
    fs.writeFileSync(p, png);
    console.log('wrote', `${name}${suffix}.png`, png.length, 'bytes');
  }
}

for (const [name, spec] of Object.entries(ICONS)) writePair(name, spec);

console.log('done — LEDs rendered.');
