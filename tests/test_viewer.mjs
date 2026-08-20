/* Static consistency check across the three viewer files: every getElementById in
   viewer.js must exist in index.html, every class the JS toggles must be styled,
   and the binary header must be parsed exactly as encode.py packs it. */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

// fileURLToPath, not URL.pathname -- a space in the folder name arrives percent-encoded.
const dir = fileURLToPath(new URL('../host', import.meta.url));
const js = readFileSync(`${dir}/static/viewer.js`, 'utf8');
const html = readFileSync(`${dir}/static/index.html`, 'utf8');
const css = readFileSync(`${dir}/static/style.css`, 'utf8');
const py = readFileSync(`${dir}/encode.py`, 'utf8');

let ok = true;
const check = (label, cond, detail = '') => {
  ok = ok && cond;
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${label}${detail ? '   ' + detail : ''}`);
};

const ids = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]));
const looked = [...js.matchAll(/getElementById\('([^']+)'\)/g)].map(m => m[1]);
const missing = looked.filter(id => !ids.has(id));
check('every getElementById target exists in the HTML',
      missing.length === 0, `${looked.length} lookups, missing: [${missing}]`);

const unused = [...ids].filter(id => !looked.includes(id) && !css.includes('#' + id));
check('no orphan ids in the HTML', unused.length === 0, `orphans: [${unused}]`);

const toggled = new Set([...js.matchAll(/classList\.(?:toggle|add|remove)\('([^']+)'/g)].map(m => m[1]));
const unstyled = [...toggled].filter(c => !css.includes('.' + c));
check('every class the JS toggles is styled', unstyled.length === 0,
      `[${[...toggled]}] -> unstyled: [${unstyled}]`);

// The two halves of the wire format must agree. Nothing at runtime checks this --
// a mismatch just draws garbage -- so it is pinned down here.
check('encode.py packs a <HHH frame header', py.includes('FRAME = struct.Struct("<HHH")'));
check('encode.py packs a <HHHHI tile header', py.includes('TILE = struct.Struct("<HHHHI")'));

const frameOffsets = [...js.matchAll(/head\.getUint16\((\d+), true\)/g)].map(m => Number(m[1]));
check('viewer.js reads count, w, h at 0,2,4',
      JSON.stringify(frameOffsets) === '[0,2,4]', `[${frameOffsets}]`);

// <HHH is 6 bytes, so tiles start there; <HHHHI is 12, with the u32 length at +8.
check('viewer.js starts the tile walk after the 6-byte frame header',
      /\bat = 6\b/.test(js));
check('viewer.js reads the tile length as a u32 at +8',
      js.includes('t.getUint32(8, true)'));
check('viewer.js bounds each tile DataView to its 12-byte header',
      js.includes('new DataView(buf, at, 12)'));
check('viewer.js advances by header plus payload',
      /at \+= 12 \+ len/.test(js));

// Message types the client emits must all be handled by the server.
const server = readFileSync(`${dir}/server.py`, 'utf8');
const sent = new Set([...js.matchAll(/\bt:\s*'([a-z]+)'/g)].map(m => m[1]));
const unhandled = [...sent].filter(t => !server.includes(`"${t}"`));
check('every message type the viewer sends is handled server-side',
      unhandled.length === 0, `sends [${[...sent].sort()}] -> unhandled: [${unhandled}]`);

check('viewer.js has no leftover debugger statement', !/\bdebugger\b/.test(js));
check('scripts and styles are same-origin (CSP-free, no CDN)',
      !/https?:\/\//.test(html.replace(/<!--[\s\S]*?-->/g, '')));

console.log('\n  ' + (ok ? 'ALL CHECKS PASSED' : 'SOME CHECKS FAILED'));
process.exit(ok ? 0 : 1);
