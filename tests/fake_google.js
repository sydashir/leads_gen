// A stand-in for Google Apps Script + Sheets, strict about the real API.
// It loads itleads/appscript/Code.gs.tpl, runs it against an in-memory spreadsheet, and serves it over
// HTTP exactly like a deployed web app (POST -> 302 -> JSON). Any method the real Apps Script reference
// does not list makes the script throw, so a typo here would be a typo there.
const fs = require('fs'), http = require('http'), path = require('path'), vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const OFFICIAL = JSON.parse(fs.readFileSync(path.join(__dirname, 'apps_script_methods.json'), 'utf8'));
const TOKEN = process.env.FAKE_TOKEN || 'test-token';
const src = fs.readFileSync(process.env.CODE_GS || path.join(ROOT, 'itleads/appscript/Code.gs.tpl'), 'utf8').replace('__TOKEN__', TOKEN);

const calls = {};
const drive = { access: {}, blockLink: false };       // book id -> sharing access                       // method usage counters, for assertions
function strict(obj, cls, official) {
  const names = new Set(OFFICIAL[official] || []);
  return new Proxy(obj, {
    get(t, p, r) {
      if (typeof p !== 'string' || p.startsWith('_') || p in t && typeof t[p] !== 'function') return Reflect.get(t, p, r);
      if (typeof t[p] === 'function' && !names.has(p) && !['then', 'toJSON', 'constructor'].includes(p)) {
        throw new Error(`Apps Script has no method ${cls}.${p}`);
      }
      if (typeof t[p] !== 'function') {
        if (names.has(p)) { return function () { calls[cls + '.' + p] = (calls[cls + '.' + p] || 0) + 1; return r; }; }
        if (['then', 'toJSON'].includes(p)) return undefined;
        throw new Error(`Apps Script has no method ${cls}.${p}`);
      }
      calls[cls + '.' + p] = (calls[cls + '.' + p] || 0) + 1;
      return t[p].bind(r);
    }
  });
}

// ------------------------------------------------------------------ model
class Cell { constructor() { this.value = ''; this.rich = null; this.note = ''; this.fmt = ''; } }
const key = (r, c) => r + ',' + c;

class SheetImpl {
  constructor(book, name, id) {
    this._book = book; this._name = name; this._id = id; this._cells = new Map();
    this._maxRows = 1000; this._maxCols = 26; this._hidden = false; this._frozen = [0, 0]; this._group = null;
    this._filter = false; this._merges = []; this._heights = {}; this._widths = {};
  }
  _cell(r, c) { const k = key(r, c); if (!this._cells.has(k)) this._cells.set(k, new Cell()); return this._cells.get(k); }
  _peek(r, c) { return this._cells.get(key(r, c)); }
  getName() { return this._name; }
  setName(n) { this._name = n; return this._self; }
  getSheetId() { return this._id; }
  getMaxRows() { return this._maxRows; }
  getMaxColumns() { return this._maxCols; }
  getLastRow() {
    let last = 0;
    for (const [k, cell] of this._cells) {
      const nonEmpty = (cell.value !== '' && cell.value !== null && cell.value !== undefined) || (cell.rich && cell.rich.getText() !== '');
      if (nonEmpty) last = Math.max(last, parseInt(k.split(',')[0], 10));
    }
    return last;
  }
  getRange(a, b, c, d) {
    if (typeof a === 'string') {
      const m = a.match(/^([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?$/);
      if (!m) throw new Error('Bad A1 ' + a);
      const col = s => s.split('').reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0);
      const r1 = +m[2], c1 = col(m[1]), r2 = m[4] ? +m[4] : r1, c2 = m[3] ? col(m[3]) : c1;
      return wrapRange(new RangeImpl(this, r1, c1, r2 - r1 + 1, c2 - c1 + 1));
    }
    const nr = c === undefined ? 1 : c, nc = d === undefined ? 1 : d;
    if (a < 1 || b < 1 || nr < 1 || nc < 1) throw new Error('Range out of bounds');
    if (a + nr - 1 > this._maxRows || b + nc - 1 > this._maxCols) throw new Error(`Range ${a},${b},${nr},${nc} is outside the ${this._maxRows}x${this._maxCols} grid`);
    return wrapRange(new RangeImpl(this, a, b, nr, nc));
  }
  insertRowsAfter(after, n) { this._maxRows += n; return this._self; }
  insertRowsBefore(pos, n) {
    const moved = new Map();
    for (const [k, cell] of this._cells) {
      const [r, c] = k.split(',').map(Number);
      moved.set(key(r >= pos ? r + n : r, c), cell);
    }
    this._cells = moved; this._maxRows += n; return this._self;
  }
  insertColumnsAfter(after, n) { this._maxCols += n; return this._self; }
  deleteColumns(from, n) { this._maxCols -= n; return this._self; }
  setColumnWidth(c, w) { this._widths[c] = w; return this._self; }
  setRowHeight(r, h) { this._heights[r] = h; return this._self; }
  setRowHeights(r, n, h) { for (let i = 0; i < n; i++) this._heights[r + i] = h; return this._self; }
  setFrozenRows(n) { this._frozen[0] = n; return this._self; }
  setFrozenColumns(n) { this._frozen[1] = n; return this._self; }
  setHiddenGridlines(b) { this._gridHidden = b; return this._self; }
  hideSheet() { this._hidden = true; return this._self; }
  activate() { this._book._active = this; return this._self; }
  getFilter() { return this._filter ? {} : null; }
  getColumnGroup(c, depth) {
    if (!this._group) return null;
    return strict({ collapse: () => { this._group.collapsed = true; } }, 'Group', 'group');
  }
  clear() { this._cells.clear(); this._merges = []; return this._self; }
  appendRow(arr) { const r = this.getLastRow() + 1; arr.forEach((v, i) => { this._cell(r, i + 1).value = v; }); return this._self; }
}

class RangeImpl {
  constructor(sheet, r, c, nr, nc) { this._s = sheet; this._r = r; this._c = c; this._nr = nr; this._nc = nc; }
  _each(fn) { for (let i = 0; i < this._nr; i++) for (let j = 0; j < this._nc; j++) fn(this._s._cell(this._r + i, this._c + j), i, j); }
  getValues() {
    const out = [];
    for (let i = 0; i < this._nr; i++) { const row = []; for (let j = 0; j < this._nc; j++) { const c = this._s._peek(this._r + i, this._c + j); row.push(c ? (c.rich ? c.rich.getText() : c.value) : ''); } out.push(row); }
    return out;
  }
  setValues(v) {
    if (v.length !== this._nr || v[0].length !== this._nc) throw new Error(`setValues: data is ${v.length}x${v[0] && v[0].length}, range is ${this._nr}x${this._nc}`);
    this._each((c, i, j) => { c.value = v[i][j]; c.rich = null; });
    return this._self;
  }
  setValue(v) { this._each(c => { c.value = v; c.rich = null; }); return this._self; }
  setFormula(f) { if (!String(f).startsWith('=')) throw new Error('formula must start with ='); this._each(c => { c.value = f; }); return this._self; }
  setRichTextValues(v) {
    if (v.length !== this._nr || v[0].length !== this._nc) throw new Error(`setRichTextValues: ${v.length}x${v[0] && v[0].length} vs ${this._nr}x${this._nc}`);
    this._each((c, i, j) => { c.rich = v[i][j]; c.value = v[i][j].getText(); });
    return this._self;
  }
  setRichTextValue(v) { this._each(c => { c.rich = v; c.value = v.getText(); }); return this._self; }
  setNotes(v) {
    if (v.length !== this._nr || v[0].length !== this._nc) throw new Error(`setNotes: ${v.length}x${v[0] && v[0].length} vs ${this._nr}x${this._nc}`);
    this._each((c, i, j) => { c.note = v[i][j]; }); return this._self;
  }
  setNote(n) { this._each(c => { c.note = n; }); return this._self; }
  setNumberFormat(f) { this._each(c => { c.fmt = f; }); return this._self; }
  merge() { this._s._merges.push([this._r, this._c, this._nr, this._nc]); return this._self; }
  breakApart() { this._s._merges = []; return this._self; }
  clearContent() { this._each(c => { c.value = ''; c.rich = null; }); return this._self; }
  createFilter() { this._s._filter = true; return {}; }
  shiftColumnGroupDepth(d) { this._s._group = { depth: (this._s._group ? this._s._group.depth : 0) + d, collapsed: false }; return this._self; }
  sort(specs) {
    specs = Array.isArray(specs) ? specs : [specs];
    const rows = [];
    for (let i = 0; i < this._nr; i++) { const row = []; for (let j = 0; j < this._nc; j++) row.push(this._s._cell(this._r + i, this._c + j)); rows.push(row); }
    const val = c => { const v = c.value; return v instanceof Date ? v.getTime() : v; };
    rows.sort((a, b) => { for (const s of specs) { const x = val(a[s.column - 1]), y = val(b[s.column - 1]); if (x === y) continue; const lt = (x === '' ? -Infinity : x) < (y === '' ? -Infinity : y); return (lt ? -1 : 1) * (s.ascending ? 1 : -1); } return 0; });
    rows.forEach((row, i) => row.forEach((cell, j) => this._s._cells.set(key(this._r + i, this._c + j), cell)));
    return this._self;
  }
}
// formatting setters we only need to see being called
['setFontFamily', 'setFontSize', 'setFontColor', 'setFontWeight', 'setBackground', 'setVerticalAlignment', 'setHorizontalAlignment', 'setWrapStrategy', 'setBorder']
  .forEach(m => { RangeImpl.prototype[m] = function () { this._fmtLog = (this._fmtLog || 0) + 1; return this._self; }; });

function wrapRange(impl) { const p = strict(impl, 'Range', 'range'); impl._self = p; return p; }
function wrapSheet(impl) { const p = strict(impl, 'Sheet', 'sheet'); impl._self = p; return p; }

class BookImpl {
  constructor(title) { this._title = title; this._id = 'BOOK' + Math.random().toString(36).slice(2, 8); this._sheets = []; this._tz = 'GMT'; this._editors = []; this._next = 1; this._active = null; this._add('Sheet1', 0); }
  _add(name, idx) { const s = wrapSheet(new SheetImpl(this, name, this._next++)); this._sheets.splice(idx === undefined ? this._sheets.length : idx, 0, s); return s; }
  getId() { return this._id; }
  getUrl() { return 'https://docs.google.com/spreadsheets/d/' + this._id + '/edit'; }
  getSheets() { return this._sheets; }
  getSheetByName(n) { return this._sheets.find(s => s.getName() === n) || null; }
  insertSheet(name, idx) { if (this.getSheetByName(name)) throw new Error('A sheet with the name "' + name + '" already exists'); return this._add(name, idx); }
  deleteSheet(s) { this._sheets = this._sheets.filter(x => x !== s); }
  setSpreadsheetTimeZone(tz) { this._tz = tz; }
  getSpreadsheetTimeZone() { return this._tz; }
  getEditors() { return this._editors.map(e => ({ getEmail: () => e })); }
  addEditor(e) { if (!/^[^@\s]+@[^@\s]+$/.test(e)) throw new Error('bad email ' + e); this._editors.push(e); return this._self; }
  setActiveSheet(s) { this._active = s; return s; }
  moveActiveSheet(p) { return this._self; }
}
const books = {};
function wrapBook(impl) { const p = strict(impl, 'Spreadsheet', 'spreadsheet'); impl._self = p; return p; }

class RichImpl {
  constructor() { this._text = ''; this._url = ''; this._style = null; }
  setText(t) { this._text = t; return this._self; }
  setLinkUrl(u) { this._url = u; return this._self; }
  setTextStyle(s) { this._style = s; return this._self; }
  build() { const t = this._text, u = this._url, st = this._style; return { getText: () => t, getLinkUrl: () => u, getTextStyle: () => st }; }
}
class StyleImpl {
  constructor() { this._o = {}; }
  setForegroundColor(c) { if (!/^#[0-9a-f]{6}$/i.test(c)) throw new Error('bad colour ' + c); this._o.fg = c; return this._self; }
  setUnderline(b) { this._o.u = b; return this._self; }
  build() { return Object.assign({}, this._o); }
}
const SpreadsheetApp = strict({
  create(title) { const b = wrapBook(new BookImpl(title)); books[b.getId()] = b; return b; },
  openById(id) { if (!books[id]) throw new Error('No spreadsheet with id ' + id); return books[id]; },
  newRichTextValue() { const i = new RichImpl(); const p = strict(i, 'RichTextValueBuilder', 'rich-text-value-builder'); i._self = p; return p; },
  newTextStyle() { const i = new StyleImpl(); const p = strict(i, 'TextStyleBuilder', 'text-style-builder'); i._self = p; return p; },
  WrapStrategy: { CLIP: 'CLIP', WRAP: 'WRAP', OVERFLOW: 'OVERFLOW' },
  BorderStyle: { SOLID: 'SOLID', SOLID_MEDIUM: 'SOLID_MEDIUM', DOTTED: 'DOTTED' },
}, 'SpreadsheetApp', 'spreadsheet-app');

const DriveApp = strict({
  getFileById(id) {
    if (!books[id]) throw new Error('No file with id ' + id);
    const f = { _id: id,
      setSharing(access, perm) { if (!['ANYONE_WITH_LINK', 'PRIVATE', 'ANYONE'].includes(access)) throw new Error('bad access ' + access); if (drive.blockLink && access !== 'PRIVATE') throw new Error('Access denied: DriveApp.'); drive.access[id] = access; return this._self; },
      getSharingAccess() { return drive.access[id] || 'PRIVATE'; },
      getUrl() { return books[id].getUrl(); } };
    const p = strict(f, 'File', 'file'); f._self = p; return p;
  },
  Access: { ANYONE_WITH_LINK: 'ANYONE_WITH_LINK', PRIVATE: 'PRIVATE', ANYONE: 'ANYONE' },
  Permission: { VIEW: 'VIEW', NONE: 'NONE', EDIT: 'EDIT' },
}, 'DriveApp', 'drive-app');
const propStore = {};
const ctx = {
  SpreadsheetApp, DriveApp,
  PropertiesService: { getScriptProperties: () => ({ getProperty: k => propStore[k] || null, setProperty: (k, v) => { propStore[k] = v; } }) },
  LockService: { getScriptLock: () => ({ waitLock: () => {}, releaseLock: () => {} }) },
  ContentService: { MimeType: { JSON: 'JSON' }, createTextOutput: s => ({ _s: s, setMimeType() { return this; }, getContent() { return this._s; } }) },
  Utilities: {
    parseDate: (s, tz, f) => { if (f !== 'yyyy-MM-dd' || !/^\d{4}-\d{2}-\d{2}$/.test(s)) throw new Error('parseDate: unsupported ' + s); return new Date(s + 'T00:00:00Z'); },
    formatDate: (d, tz, f) => d.toISOString().slice(0, 16).replace('T', ' '),
  },
  JSON, Object, Array, Math, String, Date, Error,
};
vm.createContext(ctx);
vm.runInContext(src, ctx);

// ----------------------------------------------------------------- server
function snapshot() {
  const out = [];
  for (const id in books) {
    const b = books[id];
    out.push({
      id, title: b._title, tz: b._tz, editors: b._editors, url: b.getUrl(), sharing: drive.access[id] || 'PRIVATE',
      sheets: b.getSheets().map(s => {
        const rows = []; const last = s.getLastRow();
        for (let r = 1; r <= Math.min(last, 500); r++) {
          const row = [];
          for (let c = 1; c <= 20; c++) {
            const cell = s._peek ? s._peek(r, c) : null;
            row.push(cell ? { v: cell.value instanceof Date ? cell.value.toISOString().slice(0, 10) : cell.value, link: cell.rich ? cell.rich.getLinkUrl() : '', note: cell.note, fmt: cell.fmt } : { v: '' });
          }
          rows.push(row);
        }
        return { name: s.getName(), hidden: s._hidden, frozen: s._frozen, filter: s._filter, group: s._group, merges: s._merges.length, rows };
      }),
    });
  }
  return { books: out, calls };
}

const results = {}; let seq = 0;
const server = http.createServer((req, res) => {
  let body = '';
  req.on('data', d => body += d);
  req.on('end', () => {
    try {
      if (req.method === 'POST' && req.url.startsWith('/macros/s/FAKE/exec')) {
        const out = ctx.doPost({ postData: { contents: body } }).getContent();
        const id = ++seq; results[id] = out;
        res.writeHead(302, { Location: '/echo/' + id }); return res.end();
      }
      if (req.method === 'GET' && req.url.startsWith('/echo/')) {
        res.writeHead(200, { 'Content-Type': 'application/json' }); return res.end(results[req.url.split('/')[2]] || '{}');
      }
      if (req.method === 'GET' && req.url.startsWith('/macros/s/FAKE/exec')) {
        res.writeHead(200, { 'Content-Type': 'application/json' }); return res.end(ctx.doGet().getContent());
      }
      if (req.method === 'GET' && req.url.startsWith('/debug/set')) {
        const q = new URL(req.url, 'http://x').searchParams; const b = books[Object.keys(books)[0]];
        const sh = b.getSheetByName(q.get('sheet')); sh._cell(+q.get('row'), +q.get('col')).value = q.get('value');
        res.writeHead(200); return res.end('ok');
      }
      if (req.method === 'GET' && req.url.startsWith('/debug/block_link')) {
        drive.blockLink = new URL(req.url, 'http://x').searchParams.get('on') === '1';
        res.writeHead(200); return res.end('ok');
      }
      if (req.method === 'GET' && req.url === '/debug') {
        res.writeHead(200, { 'Content-Type': 'application/json' }); return res.end(JSON.stringify(snapshot()));
      }
      res.writeHead(404); res.end('not found');
    } catch (e) {
      // like Apps Script: a crash outside our try/catch returns an HTML error page
      res.writeHead(200, { 'Content-Type': 'text/html' }); res.end('<html><body>Error: ' + String(e.message) + '</body></html>');
    }
  });
});
server.listen(parseInt(process.env.PORT || '0', 10), '127.0.0.1', () => {
  console.log('PORT ' + server.address().port);
});
process.on('SIGTERM', () => server.close(() => process.exit(0)));
