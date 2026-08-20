'use strict';

/* LANplay viewer.
 *
 * Frames arrive as binary: an 8-byte little-endian header [x][y][w][h] followed by
 * a JPEG. v1 only ever sends whole frames, but drawing each payload at its stated
 * offset means the tile/dirty-rect upgrade needs no change here at all.
 *
 * Input goes back as small JSON messages. Coordinates are normalized 0..1 against
 * the canvas's on-screen rectangle, so window resizing, letterboxing and Retina
 * scaling cannot pull the remote pointer out of sync with yours.
 */

const cv = document.getElementById('screen');
const ctx = cv.getContext('2d', { alpha: false, desynchronized: true });
const gate = document.getElementById('gate');
const gateForm = document.getElementById('gateform');
const gateMsg = document.getElementById('gatemsg');
const pinInput = document.getElementById('pin');
const hud = document.getElementById('hud');
const dot = document.getElementById('dot');
const qualityEl = document.getElementById('quality');
const heightEl = document.getElementById('height');
const metaEl = document.getElementById('meta');
const grabBtn = document.getElementById('grab');
const fullBtn = document.getElementById('full');

const PIN_KEY = 'lanplay.pin';

let ws = null;
let pin = '';
let capturing = false;
let retry = null;

let frames = 0, bytes = 0, rtt = 0;
let decoding = false, pendingFrame = null;
let pendingMove = null, wheelX = 0, wheelY = 0;
const held = new Set();

const r3 = (v) => Math.round(v * 1000) / 1000;
const send = (obj) => { if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj)); };
const setDot = (state) => { dot.className = state; };

/* ---- connection --------------------------------------------------------- */

function connect(candidate) {
  clearTimeout(retry);
  pin = candidate;
  setDot('wait');
  hud.textContent = 'connecting';

  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${scheme}://${location.host}/ws`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => send({ t: 'auth', pin });
  ws.onmessage = onMessage;
  ws.onclose = onClose;
  ws.onerror = () => {};   // onclose always follows and carries the useful code
}

function onMessage(ev) {
  if (typeof ev.data !== 'string') {
    bytes += ev.data.byteLength;
    pendingFrame = ev.data;             // only the newest one is worth decoding
    if (!decoding) pump();
    return;
  }
  const msg = JSON.parse(ev.data);
  if (msg.t === 'ready') {
    localStorage.setItem(PIN_KEY, pin);
    document.body.classList.add('connected');
    gate.classList.remove('error');
    setDot('on');
    sendConfig();
  } else if (msg.t === 'pong') {
    rtt = Math.round(performance.now() - msg.ts);
  }
}

function onClose(ev) {
  ws = null;
  setCapture(false);
  held.clear();                          // the host releases its own on disconnect
  setDot('off');
  document.body.classList.remove('connected');

  if (ev.code === 4003) {
    localStorage.removeItem(PIN_KEY);
    showGate('That PIN was not right. Check the other laptop.', true);
    return;
  }
  showGate('Connection lost. Retrying...', false);
  hud.textContent = 'reconnecting';
  if (pin) retry = setTimeout(() => connect(pin), 2000);
}

function showGate(message, isError) {
  gateMsg.textContent = message;
  gate.classList.toggle('error', !!isError);
  pinInput.focus();
  pinInput.select();
}

/* ---- drawing ------------------------------------------------------------ */

async function pump() {
  decoding = true;
  try {
    while (pendingFrame) {
      const buf = pendingFrame;
      pendingFrame = null;

      const head = new DataView(buf, 0, 8);
      const x = head.getUint16(0, true);
      const y = head.getUint16(2, true);
      const w = head.getUint16(4, true);
      const h = head.getUint16(6, true);

      const bmp = await createImageBitmap(new Blob([buf.slice(8)], { type: 'image/jpeg' }));
      // A full frame states the stream's current size, so a quality/scale change on
      // the host resizes the canvas here without needing its own message.
      if (x === 0 && y === 0 && (cv.width !== w || cv.height !== h)) {
        cv.width = w;
        cv.height = h;
      }
      ctx.drawImage(bmp, x, y);
      bmp.close();
      frames++;
    }
  } catch (err) {
    console.warn('frame decode failed', err);
  } finally {
    decoding = false;
  }
}

/* ---- pointer ------------------------------------------------------------ */

function norm(e) {
  const r = cv.getBoundingClientRect();
  if (!r.width || !r.height) return null;
  return {
    x: Math.min(Math.max((e.clientX - r.left) / r.width, 0), 1),
    y: Math.min(Math.max((e.clientY - r.top) / r.height, 0), 1),
  };
}

cv.addEventListener('pointermove', (e) => {
  const p = norm(e);
  if (p) pendingMove = p;                // coalesced to one send per frame, below
});

cv.addEventListener('pointerdown', (e) => {
  e.preventDefault();
  setCapture(true);
  cv.setPointerCapture(e.pointerId);     // keep getting events if the drag leaves
  const p = norm(e);
  if (!p) return;
  pendingMove = null;                    // the press carries its own position
  send({ t: 'd', b: e.button, x: r3(p.x), y: r3(p.y) });
});

cv.addEventListener('pointerup', (e) => {
  if (cv.hasPointerCapture(e.pointerId)) cv.releasePointerCapture(e.pointerId);
  const p = norm(e);
  send(p ? { t: 'u', b: e.button, x: r3(p.x), y: r3(p.y) } : { t: 'u', b: e.button });
});

cv.addEventListener('pointercancel', (e) => send({ t: 'u', b: e.button }));
cv.addEventListener('contextmenu', (e) => e.preventDefault());

cv.addEventListener('wheel', (e) => {
  e.preventDefault();
  // deltaMode 0 = pixels, 1 = lines, 2 = pages. Normalize all three to notches and
  // let the fractions accumulate, so a trackpad's tiny deltas still add up.
  const k = e.deltaMode === 1 ? 1 / 3 : e.deltaMode === 2 ? 3 : 1 / 100;
  wheelX += e.deltaX * k;
  wheelY += e.deltaY * k;
}, { passive: false });

function tick() {
  requestAnimationFrame(tick);
  if (pendingMove) {
    send({ t: 'm', x: r3(pendingMove.x), y: r3(pendingMove.y) });
    pendingMove = null;
  }
  const ix = Math.trunc(wheelX), iy = Math.trunc(wheelY);
  if (ix || iy) {
    send({ t: 'w', dx: ix, dy: iy });
    wheelX -= ix;
    wheelY -= iy;
  }
}
requestAnimationFrame(tick);

/* ---- keyboard ----------------------------------------------------------- */

const isReleaseChord = (e) => e.ctrlKey && e.altKey && e.shiftKey && e.code === 'KeyQ';

addEventListener('keydown', (e) => {
  if (!capturing) return;
  e.preventDefault();
  if (isReleaseChord(e)) { setCapture(false); return; }
  held.add(e.code);
  // Auto-repeat comes from the local keyboard, not from the remote key's state, so
  // repeats must be forwarded too -- otherwise holding Backspace deletes one letter.
  send({ t: 'k', c: e.code, d: 1 });
}, true);

addEventListener('keyup', (e) => {
  if (!capturing) return;
  e.preventDefault();
  held.delete(e.code);
  send({ t: 'k', c: e.code, d: 0 });
}, true);

function setCapture(on) {
  if (on === capturing) return;
  capturing = on;
  document.body.classList.toggle('capturing', on);
  grabBtn.classList.toggle('active', on);
  grabBtn.textContent = on ? 'Release input  ⌃⌥⇧Q' : 'Capture input';

  if (on) {
    if (document.activeElement) document.activeElement.blur();
  } else {
    for (const code of held) send({ t: 'k', c: code, d: 0 });
    held.clear();
    if (navigator.keyboard && navigator.keyboard.unlock) navigator.keyboard.unlock();
  }
}

// Losing focus with keys down would strand them held on the host.
addEventListener('blur', () => setCapture(false));
document.addEventListener('visibilitychange', () => { if (document.hidden) setCapture(false); });

/* ---- controls ----------------------------------------------------------- */

function sendConfig() {
  send({
    t: 'cfg',
    q: Number(qualityEl.value),
    h: Number(heightEl.value),
    meta: !metaEl.checked,      // checkbox says "Cmd = Win"; host flag is meta_as_ctrl
  });
}

qualityEl.addEventListener('change', sendConfig);
heightEl.addEventListener('change', sendConfig);
metaEl.addEventListener('change', sendConfig);
grabBtn.addEventListener('click', () => setCapture(!capturing));

fullBtn.addEventListener('click', async () => {
  if (document.fullscreenElement) { await document.exitFullscreen(); return; }
  await document.documentElement.requestFullscreen();
  // Keyboard Lock is what lets Esc, Tab and friends reach the game rather than
  // being swallowed by the browser. Chrome implements it; Safari does not.
  if (navigator.keyboard && navigator.keyboard.lock) {
    try { await navigator.keyboard.lock(); } catch (err) { console.warn('keyboard lock unavailable', err); }
  }
  setCapture(true);
});

document.addEventListener('fullscreenchange', () => {
  fullBtn.textContent = document.fullscreenElement ? 'Exit fullscreen' : 'Fullscreen';
});

gateForm.addEventListener('submit', (e) => {
  e.preventDefault();
  const value = pinInput.value.trim();
  if (value) connect(value);
});

setInterval(() => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  hud.textContent = `${frames} fps · ${(bytes / 1024).toFixed(0)} KB/s · ${rtt} ms`;
  frames = 0;
  bytes = 0;
  send({ t: 'ping', ts: performance.now() });
}, 1000);

/* ---- start -------------------------------------------------------------- */

const saved = new URLSearchParams(location.search).get('pin') || localStorage.getItem(PIN_KEY) || '';
if (saved) {
  pinInput.value = saved;
  connect(saved);
} else {
  pinInput.focus();
}
