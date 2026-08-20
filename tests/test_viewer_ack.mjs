/* Behavioural test of the viewer's frame acknowledgement.
 *
 * test_viewer.mjs only reads the files as text. This one actually runs viewer.js,
 * against a stub DOM, because the ack is load-bearing in a way a static check
 * cannot see: the host stops after two un-acked frames, so a viewer that fails to
 * ack does not degrade -- it freezes solid after two frames.
 *
 * The subtle part being pinned down is that updates are counted on *arrival* while
 * the ack is sent after *drawing*. It also checks that no update is ever dropped:
 * updates are incremental now, so the old keep-only-the-newest behaviour would
 * leave stale pixels on the canvas until the next full refresh.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const dir = fileURLToPath(new URL('../host', import.meta.url));
const src = readFileSync(`${dir}/static/viewer.js`, 'utf8');

let ok = true;
const check = (label, cond, detail = '') => {
  ok = ok && cond;
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${label}${detail ? '   ' + detail : ''}`);
};

/* ---- the smallest DOM viewer.js will run against ------------------------ */

const sent = [];            // every JSON message the viewer emits
let sockets = [];           // every WebSocket it opens
let decodeFails = false;
let draws = 0;             // every tile the viewer paints

const el = () => ({
  classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
  addEventListener() {}, focus() {}, select() {}, blur() {},
  getContext: () => ({ drawImage() { draws++; } }),
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 450 }),
  textContent: '', value: '65', checked: false, width: 0, height: 0, className: '',
});

class FakeSocket {
  static OPEN = 1;
  constructor() { this.readyState = 1; sockets.push(this); }
  send(s) { sent.push(JSON.parse(s)); }
  close() { this.readyState = 3; }
}

const store = { 'lanplay.pin': '123456' };
const ctx = vm.createContext({
  console,
  document: {
    getElementById: el, addEventListener() {}, body: el(),
    activeElement: null, fullscreenElement: null,
    documentElement: { requestFullscreen: async () => {} },
  },
  addEventListener() {}, navigator: {},
  localStorage: {
    getItem: (k) => store[k] ?? null,
    setItem: (k, v) => { store[k] = v; },
    removeItem: (k) => { delete store[k]; },
  },
  location: { protocol: 'http:', host: '127.0.0.1:8000', search: '' },
  WebSocket: FakeSocket,
  requestAnimationFrame() {}, setInterval() {}, setTimeout() {}, clearTimeout() {},
  performance, Blob, DataView, Uint8Array, JSON, Math, Set, URLSearchParams,
  createImageBitmap: async () => {
    if (decodeFails) throw new Error('synthetic decode failure');
    return { close() {} };
  },
});
vm.runInContext(src, ctx);

// One update carrying a single 4x3 tile at the origin, in the real wire format:
// [u16 count][u16 sw][u16 sh] then [u16 x][u16 y][u16 w][u16 h][u32 len][jpeg]
const frame = (count = 1) => {
  const jpeg = [0xff, 0xd8, 0xff, 0xd9];
  const buf = new ArrayBuffer(6 + count * (12 + jpeg.length));
  const dv = new DataView(buf);
  dv.setUint16(0, count, true); dv.setUint16(2, 4, true); dv.setUint16(4, 3, true);
  for (let i = 0, at = 6; i < count; i++, at += 12 + jpeg.length) {
    dv.setUint16(at, 0, true); dv.setUint16(at + 2, 0, true);
    dv.setUint16(at + 4, 4, true); dv.setUint16(at + 6, 3, true);
    dv.setUint32(at + 8, jpeg.length, true);
    new Uint8Array(buf, at + 12, jpeg.length).set(jpeg);
  }
  return buf;
};
const settle = () => new Promise((r) => setTimeout(r, 20));
const acks = () => sent.filter((m) => m.t === 'ack');
const lastAck = () => acks().at(-1);

/* ---- drive it ----------------------------------------------------------- */

const ws = sockets[0];
check('a saved PIN opens a socket on load', !!ws, `${sockets.length} socket(s)`);
ws.onopen();
ws.onmessage({ data: JSON.stringify({ t: 'ready', w: 1280, h: 720 }) });
check('no ack before any frame arrives', acks().length === 0);

// One update, drawn and acknowledged.
draws = 0;
ws.onmessage({ data: frame() });
await settle();
check('a drawn update is acknowledged', lastAck()?.n === 1, JSON.stringify(lastAck()));
check('its tile is painted', draws === 1, `${draws} draw(s)`);

// A multi-tile update must paint every rectangle it carries.
draws = 0;
ws.onmessage({ data: frame(5) });
await settle();
check('every tile of an update is painted', draws === 5, `${draws} draw(s)`);

// Three back-to-back, arriving while the first is still decoding. None may be
// dropped: an update is a patch now, so skipping one leaves the canvas stale.
draws = 0;
ws.onmessage({ data: frame(2) });
ws.onmessage({ data: frame(2) });
ws.onmessage({ data: frame(2) });
await settle();
check('a queued update is never dropped', draws === 6, `${draws} of 6 tiles drawn`);
check('all three are acknowledged', lastAck()?.n === 5,
      `${JSON.stringify(lastAck())} after 5 updates total`);

// A decode failure must not strand the credit either.
decodeFails = true;
sent.length = 0;
ws.onmessage({ data: frame() });
await settle();
decodeFails = false;
check('a failed decode is still acknowledged', lastAck()?.n === 6, JSON.stringify(lastAck()));
check('...and asks for a repaint, since a lost patch cannot self-repair',
      sent.some((m) => m.t === 'refresh'), JSON.stringify(sent.map((m) => m.t)));

// Acks only ever move forwards.
const ns = acks().map((m) => m.n);   // note: sent[] was cleared above, so this is the tail
check('the ack total never goes backwards',
      ns.every((n, i) => i === 0 || n >= ns[i - 1]), `[${ns}]`);

// A reconnect meets a fresh pacer on the host, whose sent count starts at zero.
sent.length = 0;
ws.onclose({ code: 1006 });
vm.runInContext("connect('123456')", ctx);
const ws2 = sockets.at(-1);
check('reconnecting opens a new socket', ws2 && ws2 !== ws);
ws2.onopen();
ws2.onmessage({ data: JSON.stringify({ t: 'ready', w: 1280, h: 720 }) });
ws2.onmessage({ data: frame() });
await settle();
check('the counter restarts with the connection', lastAck()?.n === 1,
      JSON.stringify(lastAck()));

console.log('\n  ' + (ok ? 'ALL CHECKS PASSED' : 'SOME CHECKS FAILED'));
process.exit(ok ? 0 : 1);
