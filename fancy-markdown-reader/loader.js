// Library loader: vendor/ first, then this browser's IndexedDB cache, then (only if still missing) the CDN.
// CDN downloads are version-pinned, integrity-checked and cached so they work offline afterwards.
const Libs = (() => {
  'use strict';

  const JSD = 'https://cdn.jsdelivr.net/npm/';
  const HLJS = 'https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/';
  const LIBS = {
    marked: [{ local: 'vendor/marked.min.js', cdn: JSD + 'marked@12.0.2/marked.min.js', sri: 'sha384-/TQbtLCAerC3jgaim+N78RZSDYV7ryeoBCVqTuzRrFec2akfBkHS7ACQ3PQhvMVi' }],
    purify: [{ local: 'vendor/purify.min.js', cdn: JSD + 'dompurify@3.1.6/dist/purify.min.js', sri: 'sha384-+VfUPEb0PdtChMwmBcBmykRMDd+v6D/oFmB3rZM/puCMDYcIvF968OimRh4KQY9a' }],
    hljs: [
      { local: 'vendor/highlight.min.js', cdn: HLJS + 'highlight.min.js', sri: 'sha384-F/bZzf7p3Joyp5psL90p/p89AZJsndkSoGwRpXcZhleCWhd8SnRuoYo4d0yirjJp' },
      { local: 'vendor/hljs-light.css', cdn: HLJS + 'styles/github.min.css', sri: 'sha384-eFTL69TLRZTkNfYZOLM+G04821K1qZao/4QLJbet1pP4tcF+fdXq/9CdqAbWRl/L', id: 'hljs-light' },
      { local: 'vendor/hljs-dark.css', cdn: HLJS + 'styles/github-dark.min.css', sri: 'sha384-wH75j6z1lH97ZOpMOInqhgKzFkAInZPPSPlZpYKYTOqsaizPvhQZmAtLcPKXpLyH', id: 'hljs-dark' },
    ],
    katex: [
      { local: 'vendor/katex/katex.min.js', cdn: JSD + 'katex@0.16.11/dist/katex.min.js', sri: 'sha384-7zkQWkzuo3B5mTepMUcHkMB5jZaolc2xDwL6VFqjFALcbeS9Ggm/Yr2r3Dy4lfFg' },
      { local: 'vendor/katex/katex.min.css', cdn: JSD + 'katex@0.16.11/dist/katex.min.css', sri: 'sha384-nB0miv6/jRmo5UMMR1wu3Gz6NLsoTkbqJghGIsx//Rlm+ZU03BU6SQNC66uf4l5+', inlineFonts: true },
    ],
    mermaid: [{ local: 'vendor/mermaid.min.js', cdn: JSD + 'mermaid@10.9.1/dist/mermaid.min.js', sri: 'sha384-WmdflGW9aGfoBdHc4rRyWzYuAjEmDwMdGdiPNacbwfGKxBW/SO6guzuQ76qjnSlr' }],
  };

  /* ---- IndexedDB cache (key = pinned CDN URL); every failure just means "not cached" ---- */
  let dbp = null;
  const db = () => dbp ||= new Promise((res, rej) => {
    const r = indexedDB.open('fmr-libs', 1);
    r.onupgradeneeded = () => r.result.createObjectStore('files');
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
  const store = async (mode, fn) => {
    const d = await db();
    return new Promise((res, rej) => {
      const t = d.transaction('files', mode), req = fn(t.objectStore('files'));
      t.oncomplete = () => res(req.result);
      t.onerror = t.onabort = () => rej(t.error);
    });
  };
  const cacheGet = url => store('readonly', s => s.get(url)).catch(() => undefined);
  const cachePut = (url, text) => store('readwrite', s => s.put(text, url)).catch(e => console.warn('Library cache unavailable', e));

  /* ---- injection ---- */
  const isCss = f => f.local.endsWith('.css');
  const injectLocal = f => new Promise((res, rej) => {
    const node = isCss(f)
      ? Object.assign(document.createElement('link'), { rel: 'stylesheet', href: f.local })
      : Object.assign(document.createElement('script'), { src: f.local });
    if (f.id) node.id = f.id;
    node.onload = res;
    node.onerror = () => { node.remove(); rej(); };
    document.head.append(node);
  });
  const injectText = (f, text) => {
    const node = document.createElement(isCss(f) ? 'style' : 'script');
    if (f.id) node.id = f.id;
    node.textContent = text;
    document.head.append(node);
  };

  /* ---- CDN ---- */
  async function toDataUri(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status} for ${url}`);
    const bytes = new Uint8Array(await r.arrayBuffer());
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return `data:font/woff2;base64,${btoa(bin)}`;
  }

  async function download(f) {
    const r = await fetch(f.cdn, { integrity: f.sri });
    if (!r.ok) throw new Error(`HTTP ${r.status} for ${f.cdn}`);
    let text = await r.text();
    if (f.inlineFonts) { // relative font URLs die once the CSS is inlined, so embed the woff2 files
      const base = f.cdn.slice(0, f.cdn.lastIndexOf('/') + 1);
      const names = [...new Set([...text.matchAll(/url\((fonts\/[\w-]+\.woff2)\)/g)].map(m => m[1]))];
      const uris = new Map(await Promise.all(names.map(async n => [n, await toDataUri(base + n)])));
      text = text.replace(/src:url\((fonts\/[\w-]+\.woff2)\) format\("woff2"\)[^;}]*/g, (_, n) => `src:url(${uris.get(n)}) format("woff2")`);
    }
    return text;
  }

  let notified = [], notifyTimer;
  function notify(name) {
    if (!notified.includes(name)) notified.push(name);
    clearTimeout(notifyTimer);
    notifyTimer = setTimeout(() => {
      const t = document.getElementById('toast');
      t.textContent = `Downloaded ${notified.join(', ')} (missing from vendor/) — cached in this browser for offline use.`;
      t.hidden = false;
      notified = [];
      setTimeout(() => (t.hidden = true), 5000);
    }, 400);
  }

  async function loadFile(f, name) {
    try { return await injectLocal(f); } catch {}
    let text = await cacheGet(f.cdn);
    if (text === undefined) {
      try { text = await download(f); }
      catch (e) { throw new Error(`${f.local} is missing and could not be downloaded (${e.message}).`); }
      await cachePut(f.cdn, text);
      notify(name);
    }
    injectText(f, text);
  }

  const loading = {};
  function load(name) {
    return loading[name] ||= Promise.all(LIBS[name].map(f => loadFile(f, name)))
      .catch(e => { delete loading[name]; throw e; });
  }

  return { load };
})();

Promise.all(['marked', 'purify', 'hljs'].map(Libs.load)).then(
  () => document.body.append(Object.assign(document.createElement('script'), { src: 'app.js' })),
  e => {
    console.error(e);
    const p = document.querySelector('#welcome .hero p');
    p.textContent = `Can’t start: ${e.message} Restore the vendor/ folder, or open this page once while online so the libraries can be downloaded and cached.`;
    document.querySelector('#welcome .actions').hidden = true;
  },
);
