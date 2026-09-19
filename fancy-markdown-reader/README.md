# Fancy Markdown Reader

An offline Markdown reader that runs entirely in your browser. No install, no server, no network —
just open `index.html` (double-click it, or `open index.html`).

## Opening documents

- **Open file** (<kbd>⌘/Ctrl</kbd>+<kbd>O</kbd>) — a single `.md` file.
- **Open folder** (<kbd>⌘/Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>O</kbd>) — browse every Markdown file in a file tree; relative
  images and links between documents work.
- **Drag & drop** a file or folder anywhere, or **paste** Markdown text.

In Chromium browsers (Chrome, Brave, Edge) files stay linked to disk: the page **live-reloads** when
you save in your editor (green dot in the toolbar) and recently opened items are listed on the start
screen. Safari and Firefox work too, without those two extras.

## What gets rendered

GitHub-flavored Markdown (tables, task lists, strikethrough, autolinks), syntax-highlighted code with
copy buttons, KaTeX math (`$…$`, `$$…$$`, ` ```math `), Mermaid diagrams (` ```mermaid `), footnotes,
GitHub alerts (`> [!NOTE]`), `==highlight==`, `:emoji:` shortcodes, YAML front matter, heading
anchors, and sanitized inline HTML (`<details>`, `<kbd>`, `<sub>`, `<img width>`, …).
Click **See the feature demo** on the start screen for a tour.

## Reading

Outline sidebar with scroll tracking · light / dark / sepia themes · font, text size and column
width settings · source view (<kbd>⌘/Ctrl</kbd>+<kbd>U</kbd>) · sidebar toggle (<kbd>⌘/Ctrl</kbd>+<kbd>B</kbd>) ·
click-to-zoom images · print / save as PDF.

## Layout

`index.html`, `app.css`, `app.js`, `loader.js` are the app. `vendor/` holds pinned copies of marked,
DOMPurify, highlight.js, KaTeX and Mermaid, so normally nothing is fetched from the internet.

### If `vendor/` is missing or incomplete

`loader.js` resolves every library in this order:

1. **`vendor/`** on disk — no network at all.
2. **This browser's cache** (IndexedDB database `fmr-libs`) — filled by an earlier download.
3. **CDN** (jsDelivr / cdnjs), only for the files that are missing and only when they are needed:
   the parser, sanitizer and highlighter at startup, KaTeX the first time a document contains math,
   Mermaid the first time one contains a diagram. Downloads are pinned to the exact vendored versions,
   verified with SHA-384 integrity hashes, then cached so they work offline from then on.

A browser page cannot write into `vendor/`, so the cache lives in the browser profile: it is per
browser, and clearing site data removes it. With no `vendor/`, no cache and no internet, the start
screen says so instead of failing silently.
