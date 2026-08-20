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

// The two halves of the wire format must agree.
check('encode.py packs the header as <HHHH', py.includes('struct.Struct("<HHHH")'));
const offsets = [...js.matchAll(/head\.getUint16\((\d+), true\)/g)].map(m => Number(m[1]));
check('viewer.js reads four little-endian u16 at 0,2,4,6',
      JSON.stringify(offsets) === '[0,2,4,6]', `[${offsets}]`);
check('viewer.js skips exactly the 8 header bytes', js.includes('buf.slice(8)'));
check('the DataView is bounded to the header', js.includes('new DataView(buf, 0, 8)'));

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
