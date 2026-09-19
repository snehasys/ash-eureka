(() => {
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const MD_EXT = /\.(md|markdown|mdown|mkd|mdx|txt)$/i;
const SKIP_DIRS = new Set(['node_modules', '.git', '.svn', '.hg', '.venv', '__pycache__']);
const MAX_FILES = 20000;
const HAS_FS_API = 'showOpenFilePicker' in window;

const el = {
  viewer: $('#viewer'), welcome: $('#welcome'), content: $('#content'), source: $('#source'), stats: $('#stats'),
  banner: $('#banner'), outline: $('#panel-outline'), tree: $('#tree'), filter: $('#file-filter'),
  docName: $('#doc-name'), docPath: $('#doc-path'), liveDot: $('#live-dot'), progress: $('#progress div'),
  settings: $('#settings'), lightbox: $('#lightbox'), dropmask: $('#dropmask'), toast: $('#toast'),
  fileInput: $('#file-input'), dirInput: $('#dir-input'), recents: $('#recents'),
};

// ws:  { name, files: Map<path, { getFile(), live }> }   (null when a lone file is open)
// doc: { path, name, getFile, live, text, stamp, size }
const state = { ws: null, doc: null, sourceView: false, renderId: 0, blobs: [], bannerDismissed: false };

/* ================= Settings ================= */

const settings = Object.assign({ theme: 'auto', font: 'sans', width: 'medium', size: 16, sidebar: true, tab: 'outline' }, loadSettings());
function loadSettings() { try { return JSON.parse(localStorage.getItem('fmr-settings')) || {}; } catch { return {}; } }
function saveSettings() { try { localStorage.setItem('fmr-settings', JSON.stringify(settings)); } catch {} }

const darkQuery = matchMedia('(prefers-color-scheme: dark)');
const WIDTHS = { narrow: '680px', medium: '820px', wide: '1100px', full: 'none' };
const effectiveTheme = () => settings.theme === 'auto' ? (darkQuery.matches ? 'dark' : 'light') : settings.theme;

function applySettings() {
  const root = document.documentElement, theme = effectiveTheme();
  const themeChanged = root.dataset.theme !== theme;
  root.dataset.theme = theme;
  root.dataset.font = settings.font;
  root.style.setProperty('--prose-font', `var(--font-${settings.font})`);
  root.style.setProperty('--prose-size', settings.size + 'px');
  root.style.setProperty('--prose-width', WIDTHS[settings.width]);
  $('#hljs-light').disabled = theme === 'dark';
  $('#hljs-dark').disabled = theme !== 'dark';
  document.body.classList.toggle('no-sidebar', !settings.sidebar);
  $('#btn-sidebar').classList.toggle('active', settings.sidebar);
  $('#set-theme').value = settings.theme;
  $('#set-font').value = settings.font;
  $('#set-width').value = settings.width;
  $('#set-size').value = settings.size;
  $('#set-size-out').textContent = settings.size + 'px';
  for (const b of $$('.tabs button')) b.classList.toggle('active', b.dataset.tab === settings.tab);
  $('#panel-outline').hidden = settings.tab !== 'outline';
  $('#panel-files').hidden = settings.tab !== 'files';
  saveSettings();
  if (themeChanged && state.doc) renderMermaid(state.renderId);
}

/* ================= Markdown pipeline ================= */

const EMOJI = {
  smile: '😄', grin: '😁', joy: '😂', rofl: '🤣', wink: '😉', blush: '😊', heart_eyes: '😍', thinking: '🤔', sunglasses: '😎',
  cry: '😢', sob: '😭', angry: '😠', scream: '😱', sweat_smile: '😅', upside_down_face: '🙃', neutral_face: '😐', sleeping: '😴',
  '+1': '👍', thumbsup: '👍', '-1': '👎', thumbsdown: '👎', clap: '👏', wave: '👋', pray: '🙏', muscle: '💪', ok_hand: '👌',
  point_right: '👉', point_left: '👈', point_up: '☝️', point_down: '👇', raised_hands: '🙌', eyes: '👀', brain: '🧠',
  heart: '❤️', broken_heart: '💔', star: '⭐', sparkles: '✨', fire: '🔥', zap: '⚡', boom: '💥', tada: '🎉', rocket: '🚀',
  '100': '💯', bulb: '💡', warning: '⚠️', x: '❌', white_check_mark: '✅', heavy_check_mark: '✔️', question: '❓', exclamation: '❗',
  bug: '🐛', wrench: '🔧', hammer: '🔨', gear: '⚙️', lock: '🔒', unlock: '🔓', key: '🔑', mag: '🔍', link: '🔗', pushpin: '📌',
  memo: '📝', pencil: '✏️', book: '📖', books: '📚', bookmark: '🔖', clipboard: '📋', calendar: '📅', package: '📦',
  computer: '💻', iphone: '📱', email: '📧', bell: '🔔', construction: '🚧', recycle: '♻️', hourglass: '⌛', clock: '🕐',
  chart_with_upwards_trend: '📈', chart_with_downwards_trend: '📉', moneybag: '💰', gift: '🎁', trophy: '🏆', art: '🎨',
  coffee: '☕', beer: '🍺', pizza: '🍕', cake: '🍰', sunny: '☀️', cloud: '☁️', snowflake: '❄️', rainbow: '🌈', earth_americas: '🌎',
  dog: '🐶', cat: '🐱', penguin: '🐧', snake: '🐍', crab: '🦀', whale: '🐳', robot: '🤖', ghost: '👻', skull: '💀', poop: '💩',
  arrow_right: '➡️', arrow_left: '⬅️', arrow_up: '⬆️', arrow_down: '⬇️', information_source: 'ℹ️', no_entry: '⛔', checkered_flag: '🏁',
};

let fn = null; // footnote state for the render in progress: { defs: Map, order: [], refs: {} }

marked.use({
  gfm: true,
  extensions: [
    {
      name: 'mathBlock', level: 'block',
      start(src) { const m = /^ {0,3}\$\$/m.exec(src); return m ? m.index : undefined; },
      tokenizer(src) {
        const m = /^ {0,3}\$\$([\s\S]+?)\$\$[ \t]*(?:\n|$)/.exec(src);
        if (m) return { type: 'mathBlock', raw: m[0], text: m[1].trim() };
      },
      renderer: t => `<div class="math-block">${esc(t.text)}</div>\n`,
    },
    {
      name: 'mathInline', level: 'inline',
      start(src) { const i = src.indexOf('$'); return i < 0 ? undefined : i; },
      tokenizer(src) {
        let m = /^\$\$([^$]+?)\$\$/.exec(src);
        if (m) return { type: 'mathInline', raw: m[0], text: m[1].trim(), display: true };
        m = /^\$(?!\s)((?:\\[^\n]|[^$\\\n])+?)(?<!\s)\$(?![\d$])/.exec(src);
        if (m) return { type: 'mathInline', raw: m[0], text: m[1] };
      },
      renderer: t => `<span class="math-inline"${t.display ? ' data-display="1"' : ''}>${esc(t.text)}</span>`,
    },
    {
      name: 'mark', level: 'inline',
      start(src) { const i = src.indexOf('=='); return i < 0 ? undefined : i; },
      tokenizer(src) {
        const m = /^==(?=\S)([^\n]*?\S)==/.exec(src);
        if (m) return { type: 'mark', raw: m[0], tokens: this.lexer.inlineTokens(m[1]) };
      },
      renderer(t) { return `<mark>${this.parser.parseInline(t.tokens)}</mark>`; },
    },
    {
      name: 'emoji', level: 'inline',
      start(src) { const m = /:[a-z0-9_+-]+:/.exec(src); return m ? m.index : undefined; },
      tokenizer(src) {
        const m = /^:([a-z0-9_+-]+):/.exec(src);
        if (m && Object.hasOwn(EMOJI, m[1])) return { type: 'emoji', raw: m[0], char: EMOJI[m[1]] };
      },
      renderer: t => t.char,
    },
    {
      name: 'footnoteRef', level: 'inline',
      start(src) { const i = src.indexOf('[^'); return i < 0 ? undefined : i; },
      tokenizer(src) {
        const m = /^\[\^([^\]\s]+)\]/.exec(src);
        if (m && fn && fn.defs.has(m[1])) return { type: 'footnoteRef', raw: m[0], id: m[1] };
      },
      renderer(t) {
        let n = fn.order.indexOf(t.id) + 1;
        if (!n) n = fn.order.push(t.id);
        const k = fn.refs[n] = (fn.refs[n] || 0) + 1;
        return `<sup class="fn-ref" id="fnref-${n}${k > 1 ? '-' + k : ''}"><a href="#fn-${n}">[${n}]</a></sup>`;
      },
    },
  ],
});

function splitFrontMatter(src) {
  const m = /^---[ \t]*\r?\n([\s\S]*?)\r?\n(?:---|\.\.\.)[ \t]*(?:\r?\n|$)/.exec(src);
  return m ? { front: m[1], body: src.slice(m[0].length) } : { front: null, body: src };
}

function frontMatterHtml(front) {
  const rows = [];
  let simple = true;
  for (const line of front.split(/\r?\n/)) {
    const m = /^([A-Za-z_][\w .-]*):\s*(.*)$/.exec(line);
    if (m) rows.push([m[1], m[2]]);
    else if (rows.length && /^\s+\S/.test(line)) rows[rows.length - 1][1] += '\n' + line.trim();
    else if (line.trim() && !line.trim().startsWith('#')) { simple = false; break; }
  }
  const body = simple && rows.length
    ? `<table>${rows.map(([k, v]) => `<tr><th>${esc(k)}</th><td>${esc(v.replace(/^(["'])(.*)\1$/, '$2')).replace(/\n/g, '<br>')}</td></tr>`).join('')}</table>`
    : `<pre><code>${esc(front)}</code></pre>`;
  return `<details class="frontmatter"><summary>Front matter</summary>${body}</details>`;
}

// Pull `[^id]: text` definitions (with indented continuation lines) out of the source, ignoring fenced code.
function extractFootnotes(src) {
  const lines = src.split('\n'), out = [], defs = new Map();
  let fence = null, cur = null;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const f = /^ {0,3}(`{3,}|~{3,})/.exec(line);
    if (fence) {
      if (f && f[1][0] === fence[0] && f[1].length >= fence.length && !line.slice(f[0].length).trim()) fence = null;
      out.push(line);
      continue;
    }
    if (f) { fence = f[1]; cur = null; out.push(line); continue; }
    const m = /^\[\^([^\]\s]+)\]:[ \t]*(.*)$/.exec(line);
    if (m) { cur = m[1]; defs.set(cur, [m[2]]); continue; }
    if (cur !== null) {
      const indented = /^(?: {4}|\t)/;
      if (indented.test(line)) { defs.get(cur).push(line.replace(indented, '')); continue; }
      if (!line.trim() && indented.test(lines[i + 1] || '')) { defs.get(cur).push(''); continue; }
      cur = null;
    }
    out.push(line);
  }
  return { src: out.join('\n'), defs };
}

function markdownToHtml(text) {
  const { front, body } = splitFrontMatter(text.replace(/^﻿/, ''));
  const { src, defs } = extractFootnotes(body.replace(/\r\n?/g, '\n'));
  fn = { defs, order: [], refs: {} };
  let html = (front ? frontMatterHtml(front) : '') + marked.parse(src);
  if (fn.order.length) {
    html += '<section class="footnotes"><ol>';
    for (let i = 0; i < fn.order.length; i++) { // order may grow while footnotes reference other footnotes
      const back = ` <a class="fn-back" href="#fnref-${i + 1}" title="Back to text">↩</a>`;
      const inner = marked.parse(defs.get(fn.order[i]).join('\n')).trim();
      html += `<li id="fn-${i + 1}">${inner.endsWith('</p>') ? inner.slice(0, -4) + back + '</p>' : inner + back}</li>`;
    }
    html += '</ol></section>';
  }
  fn = null;
  return html;
}

const slugify = s => s.trim().toLowerCase().replace(/[^\p{L}\p{N}\p{M}_\- ]/gu, '').replace(/ /g, '-');
const ALERT_TITLES = { note: 'ℹ️ Note', tip: '💡 Tip', important: '💬 Important', warning: '⚠️ Warning', caution: '🛑 Caution' };


function buildFragment(text) {
  const frag = DOMPurify.sanitize(markdownToHtml(text), {
    RETURN_DOM_FRAGMENT: true,
    FORBID_TAGS: ['style', 'form', 'dialog'],
    FORBID_ATTR: ['autofocus'],
  });

  // Headings: GitHub-style ids + hover anchors
  const seen = new Map();
  for (const h of $$('h1,h2,h3,h4,h5,h6', frag)) {
    const base = slugify(h.textContent) || 'section';
    const n = seen.get(base) || 0;
    seen.set(base, n + 1);
    if (!h.id) h.id = n ? `${base}-${n}` : base;
    const a = document.createElement('a');
    a.className = 'anchor'; a.href = '#' + h.id; a.textContent = '#'; a.setAttribute('aria-hidden', 'true');
    h.prepend(a);
  }

  // Code blocks: mermaid, math, or highlighted code with a header
  for (const code of $$('pre > code', frag)) {
    const pre = code.parentElement;
    const lang = (/(?:^|\s)language-(\S+)/.exec(code.className) || [])[1] || '';
    if (lang === 'mermaid') {
      const div = document.createElement('div');
      div.className = 'mermaid-block';
      div.dataset.src = code.textContent;
      pre.replaceWith(div);
      continue;
    }
    if (lang === 'math' || lang === 'katex') {
      const div = document.createElement('div');
      div.className = 'math-block';
      div.textContent = code.textContent.trim();
      pre.replaceWith(div);
      continue;
    }
    if (lang && hljs.getLanguage(lang)) {
      code.innerHTML = hljs.highlight(code.textContent.replace(/\n$/, ''), { language: lang, ignoreIllegals: true }).value;
      code.classList.add('hljs');
    }
    const wrap = document.createElement('div');
    wrap.className = 'code-block';
    wrap.innerHTML = `<div class="code-head"><span>${esc(lang || 'text')}</span><button type="button" class="copy">Copy</button></div>`;
    pre.replaceWith(wrap);
    wrap.append(pre);
  }

  // GitHub alerts: > [!NOTE]
  for (const bq of $$('blockquote', frag)) {
    const p = bq.firstElementChild;
    if (!p || p.tagName !== 'P') continue;
    const m = /^\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*(?:<br\s*\/?>)?\s*/i.exec(p.innerHTML);
    if (!m) continue;
    const type = m[1].toLowerCase();
    const div = document.createElement('div');
    div.className = `alert alert-${type}`;
    p.innerHTML = p.innerHTML.slice(m[0].length);
    if (!p.innerHTML.trim()) p.remove();
    const title = document.createElement('p');
    title.className = 'alert-title';
    title.textContent = ALERT_TITLES[type];
    div.append(title, ...bq.childNodes);
    bq.replaceWith(div);
  }

  for (const box of $$('li > input[type=checkbox], li > p:first-child > input[type=checkbox]', frag)) {
    const li = box.closest('li');
    li.classList.add('task-item');
    li.parentElement.classList.add('task-list');
  }

  for (const t of $$('table', frag)) {
    if (t.closest('.frontmatter')) continue;
    const wrap = document.createElement('div');
    wrap.className = 'table-wrap';
    t.replaceWith(wrap);
    wrap.append(t);
  }
  return frag;
}

/* ================= Paths & assets ================= */

const isExternal = u => /^[a-z][a-z0-9+.-]*:/i.test(u) || u.startsWith('//');
const dirname = p => p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '';

function resolvePath(baseDir, rel) {
  const parts = rel.startsWith('/') ? [] : baseDir.split('/').filter(Boolean);
  for (const seg of rel.split('/')) {
    if (seg === '..') parts.pop();
    else if (seg && seg !== '.') parts.push(seg);
  }
  return parts.join('/');
}

function lookup(href) {
  if (!state.ws || !state.doc) return null;
  let p = href.split(/[?#]/)[0];
  try { p = decodeURIComponent(p); } catch {}
  const path = resolvePath(dirname(state.doc.path), p);
  const entry = state.ws.files.get(path);
  return entry ? { path, entry } : null;
}

// Swap relative image/media sources for blob URLs from the open folder. Returns the count left unresolved.
async function resolveAssets(frag) {
  let missing = 0;
  await Promise.all($$('img[src], video[src], audio[src], source[src], video[poster]', frag).map(async node => {
    for (const attr of ['src', 'poster']) {
      const val = node.getAttribute(attr);
      if (!val || isExternal(val) || val.startsWith('#')) continue;
      const hit = lookup(val);
      if (!hit) { missing++; node.removeAttribute(attr); if (attr === 'src' && node.alt === '') node.alt = val; continue; }
      try {
        const url = URL.createObjectURL(await hit.entry.getFile());
        state.blobs.push(url);
        node.setAttribute(attr, url);
      } catch { missing++; }
    }
  }));
  return missing;
}

/* ================= Rendering ================= */

async function render({ keepScroll = false, hash = '' } = {}) {
  const doc = state.doc, id = ++state.renderId;
  const oldBlobs = state.blobs;
  state.blobs = [];
  let frag;
  try {
    frag = buildFragment(doc.text);
  } catch (e) {
    console.error(e);
    frag = document.createDocumentFragment();
    const pre = document.createElement('pre');
    pre.textContent = 'Failed to render this document:\n' + e.message;
    frag.append(pre);
  }
  const missing = await resolveAssets(frag);
  if (id !== state.renderId) return;

  const scroll = el.viewer.scrollTop;
  el.content.replaceChildren(frag);
  oldBlobs.forEach(URL.revokeObjectURL);
  $('code', el.source).innerHTML = hljs.highlight(doc.text, { language: 'markdown', ignoreIllegals: true }).value;

  el.welcome.hidden = true;
  el.stats.hidden = false;
  applyView();
  el.docName.textContent = doc.name;
  el.docPath.textContent = state.ws ? `${state.ws.name}/${doc.path}` : '';
  el.liveDot.hidden = !doc.live;
  document.title = `${doc.name} — Fancy Markdown Reader`;

  const words = (doc.text.match(/[\p{L}\p{N}]+(?:['’-][\p{L}\p{N}]+)*/gu) || []).length;
  el.stats.textContent = `${words.toLocaleString()} words · ${Math.max(1, Math.round(words / 220))} min read · ${doc.text.split('\n').length.toLocaleString()} lines`;

  showBanner(missing);
  buildOutline();
  markActiveFile();

  el.viewer.style.scrollBehavior = 'auto';
  if (keepScroll) el.viewer.scrollTop = scroll;
  else if (hash) goHash(hash);
  else el.viewer.scrollTop = 0;
  el.viewer.style.scrollBehavior = '';
  onScroll();
  renderMath(id);
  renderMermaid(id);
}

function applyView() {
  const hasDoc = !!state.doc;
  el.content.hidden = !hasDoc || state.sourceView;
  el.source.hidden = !hasDoc || !state.sourceView;
  $('#btn-source').classList.toggle('active', state.sourceView);
}

function showBanner(missing) {
  if (!missing || state.ws || state.bannerDismissed) { el.banner.hidden = true; return; }
  el.banner.hidden = false;
  el.banner.innerHTML = `<span>${missing} relative image${missing > 1 ? 's' : ''} can’t be shown for a single file.</span>
    <button type="button" data-act="folder">Open the containing folder</button><button type="button" class="x" data-act="x" title="Dismiss">✕</button>`;
}
el.banner.addEventListener('click', e => {
  const act = e.target.dataset.act;
  if (act === 'folder') pickFolder();
  if (act === 'x') { state.bannerDismissed = true; el.banner.hidden = true; }
});

// KaTeX and Mermaid are only loaded once a document actually needs them.
async function renderMath(id) {
  const nodes = $$('.math-block, .math-inline', el.content);
  if (!nodes.length) return;
  try { await Libs.load('katex'); } catch (e) { toast(e.message, 6000); return; }
  if (id !== state.renderId) return;
  const scroll = el.viewer.scrollTop;
  for (const node of nodes) {
    try {
      katex.render(node.textContent, node, { displayMode: node.matches('.math-block') || !!node.dataset.display, throwOnError: true, strict: false, trust: false });
    } catch (e) {
      node.classList.add('math-error');
      node.title = e.message;
    }
  }
  el.viewer.scrollTop = scroll;
}

async function renderMermaid(id) {
  const blocks = $$('.mermaid-block', el.content);
  if (!blocks.length) return;
  try { await Libs.load('mermaid'); } catch (e) { toast(e.message, 6000); return; }
  if (id !== state.renderId) return;
  mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: effectiveTheme() === 'dark' ? 'dark' : 'default', fontFamily: 'inherit' });
  for (const [i, block] of blocks.entries()) {
    const gid = `mmd-${id}-${i}-${Date.now()}`;
    try {
      const { svg } = await mermaid.render(gid, block.dataset.src);
      if (id !== state.renderId) return;
      block.innerHTML = svg;
    } catch (e) {
      document.getElementById('d' + gid)?.remove();
      document.getElementById(gid)?.remove();
      block.innerHTML = `<pre class="math-error"></pre>`;
      block.firstChild.textContent = 'Mermaid error: ' + (e.message || e) + '\n\n' + block.dataset.src;
    }
  }
}

/* ================= Outline & scrolling ================= */

let headings = [];
function buildOutline() {
  headings = $$('h1,h2,h3,h4,h5,h6', el.content).filter(h => !h.closest('.frontmatter'));
  if (!headings.length) { el.outline.innerHTML = '<p class="empty">No headings in this document.</p>'; return; }
  const min = Math.min(...headings.map(h => +h.tagName[1]));
  el.outline.replaceChildren(...headings.map(h => {
    const a = document.createElement('a');
    a.href = '#' + h.id;
    a.dataset.level = +h.tagName[1] - min + 1;
    a.textContent = a.title = h.textContent.replace(/^#/, '').trim();
    return a;
  }));
}

el.outline.addEventListener('click', e => {
  const a = e.target.closest('a');
  if (!a) return;
  e.preventDefault();
  if (state.sourceView) { state.sourceView = false; applyView(); }
  goHash(a.getAttribute('href').slice(1));
});

function goHash(hash) {
  let id = hash;
  try { id = decodeURIComponent(hash); } catch {}
  const target = el.content.querySelector('#' + CSS.escape(id)) || el.content.querySelector(`a[name="${CSS.escape(id)}"]`)
    || el.content.querySelector('#' + CSS.escape(slugify(id)));
  if (target) target.scrollIntoView({ block: 'start' });
}

let scrollQueued = false;
function onScroll() {
  if (scrollQueued) return;
  scrollQueued = true;
  requestAnimationFrame(() => {
    scrollQueued = false;
    const v = el.viewer, max = v.scrollHeight - v.clientHeight;
    el.progress.style.width = (state.doc && max > 0 ? Math.min(100, v.scrollTop / max * 100) : 0) + '%';
    if (!headings.length || state.sourceView) return;
    const top = v.getBoundingClientRect().top + 90;
    let current = 0;
    for (const [i, h] of headings.entries()) { if (h.getBoundingClientRect().top <= top) current = i; else break; }
    for (const [i, a] of [...el.outline.children].entries()) {
      const on = i === current;
      if (on !== a.classList.contains('active')) {
        a.classList.toggle('active', on);
        if (on) a.scrollIntoView({ block: 'nearest' });
      }
    }
  });
}
el.viewer.addEventListener('scroll', onScroll, { passive: true });
addEventListener('resize', onScroll);

/* ================= Opening things ================= */

async function openDoc(path, entry, hash = '') {
  try {
    const file = await entry.getFile();
    state.doc = { path, name: path.split('/').pop(), getFile: entry.getFile, live: !!entry.live, text: await file.text(), stamp: file.lastModified, size: file.size };
  } catch (e) {
    toast(`Could not read ${path}: ${e.message}`);
    return;
  }
  await render({ hash });
  el.viewer.focus({ preventScroll: true });
}

function openText(name, text) {
  setWorkspace(null);
  state.doc = { path: name, name, getFile: null, live: false, text, stamp: 0, size: text.length };
  return render();
}

function setWorkspace(ws) {
  state.ws = ws;
  state.bannerDismissed = false;
  el.filter.value = '';
  renderTree();
}

const handleEntry = h => ({ getFile: () => h.getFile(), live: true });
const fileEntry = f => ({ getFile: async () => f, live: false });

async function openSingle(name, entry) {
  setWorkspace(null);
  await openDoc(name, entry);
}

async function openWorkspace(name, files) {
  const docs = [...files.keys()].filter(p => MD_EXT.test(p));
  if (!docs.length) { toast(`No Markdown files found in “${name}”.`); return; }
  setWorkspace({ name, files });
  settings.sidebar = true; settings.tab = 'files';
  applySettings();
  const rootDocs = docs.filter(p => !p.includes('/'));
  const first = rootDocs.find(p => /^readme\.(md|markdown)$/i.test(p)) || rootDocs.find(p => /^index\.(md|markdown)$/i.test(p))
    || docs.find(p => /(^|\/)readme\.(md|markdown)$/i.test(p)) || docs.sort(comparePaths)[0];
  await openDoc(first, files.get(first));
}

async function walkHandle(dir, prefix, files) {
  for await (const [name, h] of dir.entries()) {
    if (files.size >= MAX_FILES) return;
    if (h.kind === 'directory') { if (!SKIP_DIRS.has(name)) await walkHandle(h, prefix + name + '/', files); }
    else if (name !== '.DS_Store') files.set(prefix + name, handleEntry(h));
  }
}

async function walkEntry(dirEntry, prefix, files) {
  const reader = dirEntry.createReader();
  for (;;) {
    const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
    if (!batch.length) return;
    for (const e of batch) {
      if (files.size >= MAX_FILES) return;
      if (e.isDirectory) { if (!SKIP_DIRS.has(e.name)) await walkEntry(e, prefix + e.name + '/', files); }
      else if (e.name !== '.DS_Store') {
        let cached;
        files.set(prefix + e.name, { getFile: () => cached ||= new Promise((res, rej) => e.file(res, rej)), live: false });
      }
    }
  }
}

async function openDirHandle(h, remember = true) {
  toast('Scanning folder…');
  const files = new Map();
  await walkHandle(h, '', files);
  hideToast();
  await openWorkspace(h.name, files);
  if (remember && state.ws) recents.add(h);
}

async function openFileHandle(h, remember = true) {
  await openSingle(h.name, handleEntry(h));
  if (remember) recents.add(h);
}

async function pickFile() {
  if (!HAS_FS_API) { el.fileInput.click(); return; }
  try {
    const [h] = await showOpenFilePicker({ types: [{ description: 'Markdown', accept: { 'text/markdown': ['.md', '.markdown', '.mdown', '.mkd', '.mdx'], 'text/plain': ['.txt'] } }] });
    await openFileHandle(h);
  } catch (e) { if (e.name !== 'AbortError') toast(e.message); }
}

async function pickFolder() {
  if (!window.showDirectoryPicker) { el.dirInput.click(); return; }
  try { await openDirHandle(await showDirectoryPicker({ mode: 'read' })); }
  catch (e) { if (e.name !== 'AbortError') toast(e.message); }
}

function openFileList(list, name = 'Dropped files') {
  const mds = list.filter(f => MD_EXT.test(f.name));
  if (!mds.length) { toast('That doesn’t look like a Markdown file.'); return; }
  if (list.length === 1) return openSingle(mds[0].name, fileEntry(mds[0]));
  return openWorkspace(name, new Map(list.map(f => [f.name, fileEntry(f)])));
}

el.fileInput.addEventListener('change', () => { openFileList([...el.fileInput.files]); el.fileInput.value = ''; });
el.dirInput.addEventListener('change', () => {
  const list = [...el.dirInput.files];
  el.dirInput.value = '';
  if (!list.length) return;
  const root = list[0].webkitRelativePath.split('/')[0] || 'Folder';
  const files = new Map();
  for (const f of list) {
    const rel = f.webkitRelativePath ? f.webkitRelativePath.split('/').slice(1) : [f.name];
    if (rel.some(seg => SKIP_DIRS.has(seg)) || f.name === '.DS_Store') continue;
    files.set(rel.join('/'), fileEntry(f));
  }
  openWorkspace(root, files);
});

/* ---- drag & drop, paste ---- */

let dragDepth = 0;
addEventListener('dragenter', e => { if ([...e.dataTransfer.types].includes('Files')) { dragDepth++; el.dropmask.hidden = false; } });
addEventListener('dragleave', () => { if (--dragDepth <= 0) { dragDepth = 0; el.dropmask.hidden = true; } });
addEventListener('dragover', e => e.preventDefault());
addEventListener('drop', async e => {
  e.preventDefault();
  dragDepth = 0;
  el.dropmask.hidden = true;
  const items = [...e.dataTransfer.items].filter(i => i.kind === 'file');
  if (!items.length) return;
  try {
    if (HAS_FS_API && items[0].getAsFileSystemHandle) {
      const handles = (await Promise.all(items.map(i => i.getAsFileSystemHandle()))).filter(Boolean);
      const dir = handles.find(h => h.kind === 'directory');
      if (dir) return await openDirHandle(dir);
      const mds = handles.filter(h => MD_EXT.test(h.name));
      if (!mds.length) return toast('That doesn’t look like a Markdown file.');
      if (handles.length === 1) return await openFileHandle(mds[0]);
      return await openWorkspace('Dropped files', new Map(handles.map(h => [h.name, handleEntry(h)])));
    }
    const entries = items.map(i => i.webkitGetAsEntry?.()).filter(Boolean);
    const plain = [...e.dataTransfer.files];
    const dir = entries.find(en => en.isDirectory);
    if (dir) {
      toast('Scanning folder…');
      const files = new Map();
      await walkEntry(dir, '', files);
      hideToast();
      return await openWorkspace(dir.name, files);
    }
    await openFileList(plain);
  } catch (err) {
    console.error(err);
    toast('Could not open the dropped item: ' + err.message);
  }
});

addEventListener('paste', e => {
  if (e.target.closest?.('input, textarea, select')) return;
  const text = e.clipboardData.getData('text/plain');
  if (text.trim()) { e.preventDefault(); openText('Pasted text', text); }
});

/* ================= File tree ================= */

function comparePaths(a, b) { return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }); }

function renderTree() {
  const ws = state.ws;
  el.filter.hidden = !ws;
  if (!ws) {
    el.tree.innerHTML = '<p class="empty">Open a folder to browse its Markdown files. Relative links and images will work too.</p>';
    return;
  }
  const q = el.filter.value.trim().toLowerCase();
  const paths = [...ws.files.keys()].filter(p => MD_EXT.test(p) && (!q || p.toLowerCase().includes(q))).sort(comparePaths);
  const title = document.createElement('div');
  title.className = 'ws-name';
  title.textContent = ws.name;
  el.tree.replaceChildren(title);

  const fileBtn = (path, label) => {
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'file'; b.dataset.path = path; b.textContent = label; b.title = path;
    return b;
  };
  if (q) {
    el.tree.append(...paths.map(p => fileBtn(p, p)));
    if (!paths.length) el.tree.insertAdjacentHTML('beforeend', '<p class="empty">No matches.</p>');
  } else {
    const root = { dirs: new Map(), files: [] };
    for (const p of paths) {
      const segs = p.split('/');
      let node = root;
      for (const s of segs.slice(0, -1)) {
        if (!node.dirs.has(s)) node.dirs.set(s, { dirs: new Map(), files: [] });
        node = node.dirs.get(s);
      }
      node.files.push(p);
    }
    const build = (node, parent) => {
      for (const [name, child] of [...node.dirs].sort((a, b) => comparePaths(a[0], b[0]))) {
        const d = document.createElement('details');
        const s = document.createElement('summary');
        s.textContent = name;
        const kids = document.createElement('div');
        kids.className = 'children';
        d.append(s, kids);
        build(child, kids);
        parent.append(d);
      }
      for (const p of node.files) parent.append(fileBtn(p, p.split('/').pop()));
    };
    build(root, el.tree);
  }
  markActiveFile();
}

function markActiveFile() {
  for (const b of $$('button.file', el.tree)) {
    const on = !!state.doc && b.dataset.path === state.doc.path;
    b.classList.toggle('active', on);
    if (on) for (let d = b.closest('details'); d; d = d.parentElement.closest('details')) d.open = true;
  }
}

el.tree.addEventListener('click', e => {
  const b = e.target.closest('button.file');
  if (b && state.ws) openDoc(b.dataset.path, state.ws.files.get(b.dataset.path));
});
el.filter.addEventListener('input', renderTree);

/* ================= Content interactions ================= */

el.content.addEventListener('click', async e => {
  const copy = e.target.closest('.code-head .copy');
  if (copy) {
    const text = $('pre', copy.closest('.code-block')).textContent;
    try { await navigator.clipboard.writeText(text); }
    catch {
      const ta = Object.assign(document.createElement('textarea'), { value: text });
      document.body.append(ta); ta.select(); document.execCommand('copy'); ta.remove();
    }
    copy.textContent = 'Copied ✓';
    setTimeout(() => (copy.textContent = 'Copy'), 1400);
    return;
  }

  const a = e.target.closest('a[href]');
  if (a) {
    const href = a.getAttribute('href');
    if (href.startsWith('#')) { e.preventDefault(); goHash(href.slice(1)); return; }
    if (isExternal(href)) { a.target = '_blank'; a.rel = 'noopener noreferrer'; return; }
    e.preventDefault();
    const hit = lookup(href);
    if (!hit) { toast(state.ws ? `Not found in this folder: ${href}` : 'Open the containing folder to follow relative links.'); return; }
    if (MD_EXT.test(hit.path)) { openDoc(hit.path, hit.entry, href.split('#')[1] || ''); return; }
    const url = URL.createObjectURL(await hit.entry.getFile());
    state.blobs.push(url);
    window.open(url, '_blank', 'noopener');
    return;
  }

  const img = e.target.closest('img');
  if (img && img.src) {
    $('img', el.lightbox).src = img.src;
    el.lightbox.hidden = false;
  }
});
el.lightbox.addEventListener('click', () => (el.lightbox.hidden = true));

/* ================= Live reload ================= */

let polling = false;
setInterval(async () => {
  const doc = state.doc;
  if (!doc || !doc.live || polling) return;
  polling = true;
  try {
    const f = await doc.getFile();
    if (doc === state.doc && (f.lastModified !== doc.stamp || f.size !== doc.size)) {
      doc.text = await f.text();
      doc.stamp = f.lastModified;
      doc.size = f.size;
      await render({ keepScroll: true });
      el.liveDot.classList.remove('pulse');
      void el.liveDot.offsetWidth;
      el.liveDot.classList.add('pulse');
    }
  } catch { /* file temporarily unavailable (mid-save, moved) — try again next tick */ }
  polling = false;
}, 1000);

/* ================= Recents (File System Access API only) ================= */

const recents = {
  db: null,
  open() {
    return this.db ||= new Promise((res, rej) => {
      const r = indexedDB.open('fmr', 1);
      r.onupgradeneeded = () => r.result.createObjectStore('recents', { keyPath: 'id' });
      r.onsuccess = () => res(r.result);
      r.onerror = () => rej(r.error);
    });
  },
  async tx(mode, fn) {
    const db = await this.open();
    return new Promise((res, rej) => {
      const t = db.transaction('recents', mode), out = fn(t.objectStore('recents'));
      t.oncomplete = () => res(out.result);
      t.onerror = () => rej(t.error);
    });
  },
  async add(handle) {
    try {
      const all = await this.tx('readonly', s => s.getAll());
      for (const r of all) if (r.handle.kind === handle.kind && await r.handle.isSameEntry(handle)) await this.tx('readwrite', s => s.delete(r.id));
      await this.tx('readwrite', s => s.put({ id: `${Date.now()}-${handle.name}`, name: handle.name, kind: handle.kind, handle, ts: Date.now() }));
      const sorted = (await this.tx('readonly', s => s.getAll())).sort((a, b) => b.ts - a.ts);
      for (const r of sorted.slice(8)) await this.tx('readwrite', s => s.delete(r.id));
    } catch (e) { console.warn('recents unavailable', e); }
  },
  async show() {
    if (!HAS_FS_API) return;
    let all = [];
    try { all = (await this.tx('readonly', s => s.getAll())).sort((a, b) => b.ts - a.ts); } catch { return; }
    el.recents.hidden = !all.length;
    $('ul', el.recents).replaceChildren(...all.map(r => {
      const li = document.createElement('li');
      li.innerHTML = `<button type="button" class="open">${r.kind === 'directory' ? '📁' : '📄'} ${esc(r.name)}<small>${new Date(r.ts).toLocaleDateString()}</small></button><button type="button" class="rm" title="Remove from list">✕</button>`;
      $('.open', li).onclick = async () => {
        try {
          if (await r.handle.queryPermission({ mode: 'read' }) !== 'granted' && await r.handle.requestPermission({ mode: 'read' }) !== 'granted') return;
          await (r.kind === 'directory' ? openDirHandle(r.handle) : openFileHandle(r.handle));
        } catch (e) { toast(`Could not reopen “${r.name}” — it may have moved.`); }
      };
      $('.rm', li).onclick = async () => { await this.tx('readwrite', s => s.delete(r.id)); this.show(); };
      return li;
    }));
  },
};

/* ================= Chrome: toolbar, shortcuts, toast ================= */

let toastTimer;
function toast(msg, ms = 3200) {
  el.toast.textContent = msg;
  el.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(hideToast, ms);
}
function hideToast() { el.toast.hidden = true; }

function toggleSource() {
  if (!state.doc) return;
  state.sourceView = !state.sourceView;
  applyView();
  el.viewer.scrollTop = 0;
}
function toggleSidebar() { settings.sidebar = !settings.sidebar; applySettings(); }
function showDemo() { openText('Feature demo.md', $('#demo-md').textContent); }

$('#btn-open').onclick = $('#w-open').onclick = pickFile;
$('#btn-folder').onclick = $('#w-folder').onclick = pickFolder;
$('#w-demo').onclick = showDemo;
$('#btn-sidebar').onclick = toggleSidebar;
$('#btn-source').onclick = toggleSource;
$('#btn-print').onclick = () => print();
$('#btn-theme').onclick = () => {
  const order = ['auto', 'light', 'dark', 'sepia'];
  settings.theme = order[(order.indexOf(settings.theme) + 1) % order.length];
  applySettings();
  toast(`Theme: ${settings.theme}`, 1200);
};
$('#btn-settings').onclick = e => { e.stopPropagation(); el.settings.hidden = !el.settings.hidden; };
addEventListener('click', e => { if (!el.settings.hidden && !e.target.closest('#settings')) el.settings.hidden = true; });
for (const key of ['theme', 'font', 'width', 'size']) {
  $('#set-' + key).addEventListener('input', e => { settings[key] = key === 'size' ? +e.target.value : e.target.value; applySettings(); });
}
for (const b of $$('.tabs button')) b.onclick = () => { settings.tab = b.dataset.tab; applySettings(); };
darkQuery.addEventListener('change', applySettings);

addEventListener('keydown', e => {
  if (e.key === 'Escape') { el.lightbox.hidden = true; el.settings.hidden = true; return; }
  if (!(e.metaKey || e.ctrlKey) || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === 'o') { e.preventDefault(); e.shiftKey ? pickFolder() : pickFile(); }
  else if (k === 'b' && !e.shiftKey) { e.preventDefault(); toggleSidebar(); }
  else if (k === 'u' && !e.shiftKey) { e.preventDefault(); toggleSource(); }
});

applySettings();
recents.show();
if (location.hash === '#demo') showDemo();
})();
