/* Behavioural test of the viewer's frame acknowledgement.
 *
 * test_viewer.mjs only reads the files as text. This one actually runs viewer.js,
 * against a stub DOM, because the ack is load-bearing in a way a static check
 * cannot see: the host stops after two un-acked frames, so a viewer that fails to
 * ack does not degrade -- it freezes solid after two frames.
 *
 * The subtle part being pinned down is that frames are counted on *arrival* while
 * the ack is sent after *drawing*. A frame superseded before it could be decoded is
 * never drawn, and if it were not counted its credit would never come back and the
 * stream would wedge.
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

const el = () => ({
  classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
  addEventListener() {}, focus() {}, select() {}, blur() {},
  getContext: () => ({ drawImage() {} }),
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

const frame = () => new Uint8Array([0, 0, 0, 0, 4, 0, 3, 0, 0xff, 0xd8]).buffer;
const settle = () => new Promise((r) => setTimeout(r, 20));
const acks = () => sent.filter((m) => m.t === 'ack');
const lastAck = () => acks().at(-1);

/* ---- drive it ----------------------------------------------------------- */

const ws = sockets[0];
check('a saved PIN opens a socket on load', !!ws, `${sockets.length} socket(s)`);
ws.onopen();
ws.onmessage({ data: JSON.stringify({ t: 'ready', w: 1280, h: 720 }) });
check('no ack before any frame arrives', acks().length === 0);

// One frame, drawn and acknowledged.
ws.onmessage({ data: frame() });
await settle();
check('a drawn frame is acknowledged', lastAck()?.n === 1, JSON.stringify(lastAck()));

// Three back-to-back. The middle one is superseded before it can be decoded and is
// never drawn -- but it must still be counted, or the host loses a credit forever.
ws.onmessage({ data: frame() });
ws.onmessage({ data: frame() });
ws.onmessage({ data: frame() });
await settle();
check('a superseded frame still returns its credit', lastAck()?.n === 4,
      `${JSON.stringify(lastAck())} after 4 frames total`);

// A decode failure must not strand the credit either.
decodeFails = true;
ws.onmessage({ data: frame() });
await settle();
decodeFails = false;
check('a failed decode is still acknowledged', lastAck()?.n === 5, JSON.stringify(lastAck()));

// Acks only ever move forwards.
const ns = acks().map((m) => m.n);
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
