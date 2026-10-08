/* Console Kit — owner console panel (spec: architect/40-specs/owner-console.md)
   Plain ES2020, IIFE, no external resources, WCAG 2.2 AA compliant */

(function() {
  'use strict';

  // Glyphs for states (spec 4.6): color is never the only indicator
  const GLYPH = {
    awaiting_agent: '●',  // ● filled circle
    awaiting_you: '◐',    // ◐ half-filled
    unlocked: '◑',        // ◑ half-filled other
    locked: '○',          // ○ empty circle
    stale: '◌',           // ◌ dotted circle
    withdrawn: '⊘',       // ⊘ the owner withdrew a stale ruling (CONSOLE-kit/Q30)
    superseded: '⤳'       // ⤳ a replacement was locked (CONSOLE-kit/Q32)
  };

  let config = null;
  let view = null;
  let items = null;
  let cursor = null;
  let panelEl = null;
  let backdropEl = null;
  let liveRegion = null;
  let inboxBtn = null;
  let dockStrip = null;
  // AB-2/Q2 (owner): at 1024px and wider the panel is a column docked on the
  // right, not an overlay; it starts collapsed to a strip with an unread badge.
  const DOCK_QUERY = '(min-width: 1024px)';
  let dockMedia = null;
  let lastFocused = null;
  let currentItem = null;
  let currentMode = null; // 'item' or 'inbox'
  // 0.8.5: true while the item on show was opened from the inbox, so its header can lead back there.
  let fromInbox = false;
  let pendingNonces = {};
  let draftTexts = {};
  const draftSeats = {};  // seat picker key -> {seats, other, roar}: kept across a re-render, like draftTexts
  const draftModes = {};  // form key -> the mode radio picked: kept across a re-render
  let currentFork = null; // the answers sheet's fork filter (spec §7.6), or the round the form walks
  const openForms = new Set(); // disclosure keys the owner left open
  let lockingAll = null;        // 0.8.5: the item whose answers are being locked in turn, or null
  const lockAllError = {};      // 0.8.5: item -> why its last 'Lock all' stopped
  // CONSOLE-kit/Q33: one tap locks one answer after a countdown this page holds. Nothing is sent before it
  // ends; Undo, leaving the question or leaving the page sends nothing. There is no server record of it.
  // The 5-second lock countdown was removed; lock is immediate. These globals are retired.
  const lockErrors = {};          // qid -> why its last lock was refused
  const justLocked = new Set();   // qids locked from this page and not yet drawn rolled up (Q37's roll-up)
  // 0.7.0: the inbox's tabs, the round form, and the live loop.
  let currentTab = 'inbox';     // 'inbox' | 'feed' | 'prs' | 'chat', inside inbox mode
  let arrived = new Set();      // qids and message ids that arrived with the latest live update
  let knownIds = null;          // every qid and message id the page has seen; null before the first view
  let knownVisualIds = null;    // 0.8.19: every visual record id the page has seen
  let seenAtOpen = 0;           // the seen seq when the inbox was opened: what the Feed marks "new"
  const CHAT_ITEM = '@chat';    // mirrors schema.CHAT_ITEM
  const MAX_CHAT = 4000;        // mirrors schema.MAX_CHAT
  const reducedMotion = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;

  // Per-viewer memory in localStorage (0.7.0): what this browser last saw, and
  // the round form's drafts. It is a convenience of THIS browser only: another
  // browser starts afresh, and a private window may refuse it, so every read
  // and write is guarded and the page works without it.
  function storeKey(name) { return 'ck:' + ((config && config.project) || location.pathname) + ':' + name; }
  function memGet(name) {
    try { const v = localStorage.getItem(storeKey(name)); return v === null ? null : JSON.parse(v); } catch (e) { return null; }
  }
  function memSet(name, value) {
    try {
      if (value === null) localStorage.removeItem(storeKey(name));
      else localStorage.setItem(storeKey(name), JSON.stringify(value));
    } catch (e) { /* storage refused: the page carries on without memory */ }
  }

  // Mirrors schema.py: the fork fields and the D13 roster. The server refuses
  // anything else by name, so these only shape the form.
  const FOCUSES = ['whole', 'code', 'design', 'ui', 'backend'];
  const MODES = ['explore', 'tighten'];
  const ROSTER = ['devops', 'ux', 'adversarial', 'security', 'architect', 'analyst'];
  const ROSTER_LABEL = { devops: 'DevOps', ux: 'UX', adversarial: 'Adversarial (red team)',
    security: 'Security', architect: 'Architect', analyst: 'Analyst', roar: 'Roar panel' };
  // 0.8.0, mirrors schema.py: the roar seat (alone, on one answer, once per lock)
  // and the two other kinds of fork. The server refuses anything else by name.
  const ROAR = 'roar';
  // What one seat of a deliberation costs, as measured: about 100k tokens. Shown before
  // a deliberation on an open question is sent, so the owner sees the price first.
  const SEAT_TOKENS_K = 100;
  const STEP_WORDS = {
    refine: { title: 'Refine', mode: 'tighten',
      what: 'revise the spec or document this answer rests on, to match what you decided' },
    drill: { title: 'Drill', mode: 'explore',
      what: 'drill into what this leaves unspecified and draft the questions a spec for it needs' }
  };
  const OTHER_ROLE = /^[A-Za-z0-9][A-Za-z0-9 \-]{0,39}$/;
  const MAX_ROLES = 3;
  const STATE_WORDS = { awaiting_you: 'unanswered', unlocked: 'answered, not locked',
    locked: 'locked', stale: 'stale', withdrawn: 'withdrawn', superseded: 'superseded' };

  // The item and every item under it (D14), safe against a parent cycle. Mirrors view.subtree.
  function subtree(root) {
    const out = new Set();
    for (const id of Object.keys(items || {})) {
      const seen = new Set();
      let node = id;
      while (node != null && items[node] && !seen.has(node)) {
        if (node === root) { out.add(id); break; }
        seen.add(node);
        node = items[node].parent;
      }
    }
    return out;
  }

  // Items depth first from the roots, in the adapter's order. Mirrors view.tree_order.
  function treeOrder() {
    const ids = Object.keys(items || {});
    const kids = new Map();
    for (const id of ids) {
      const p = items[id].parent;
      const key = (p != null && items[p]) ? p : null;
      if (!kids.has(key)) kids.set(key, []);
      kids.get(key).push(id);
    }
    const out = [];
    const seen = new Set();
    const stack = [...(kids.get(null) || [])].reverse();
    while (stack.length) {
      const id = stack.pop();
      if (seen.has(id)) continue;
      seen.add(id);
      out.push(id);
      stack.push(...[...(kids.get(id) || [])].reverse());
    }
    for (const id of ids) if (!seen.has(id)) out.push(id);
    return out;
  }

  // Every question in a scope with every answer it got (spec §7.6). Mirrors view.answers_sheet.
  function answersSheet(itemId, forkId) {
    const order = new Map(treeOrder().map((id, n) => [id, n]));
    const scope = itemId ? subtree(itemId) : null;
    const wanted = forkId && view.forks[forkId] ? new Set(view.forks[forkId].questions) : null;
    const rows = [];
    for (const qid of Object.keys(view.questions)) {
      const q = view.questions[qid];
      const it = q.question.item;
      if (scope && !scope.has(it)) continue;
      if (wanted && !wanted.has(qid)) continue;
      rows.push(q);
    }
    const n = qid => parseInt(qid.split('/Q').pop(), 10);
    rows.sort((a, b) => {
      const oa = order.has(a.question.item) ? order.get(a.question.item) : order.size;
      const ob = order.has(b.question.item) ? order.get(b.question.item) : order.size;
      return (oa - ob) || a.question.item.localeCompare(b.question.item) || (n(a.question.qid) - n(b.question.qid));
    });
    const counts = { awaiting_you: 0, unlocked: 0, locked: 0, stale: 0 };
    let answers = 0;
    for (const q of rows) { counts[q.state] += 1; answers += q.answers.length; }
    return { item: itemId, fork: forkId, rows, counts, answers };
  }

  // Mirrors view.condition_words.
  function conditionWords(c) {
    if (c.kind === 'item_status') return 'item ' + c.item + ' has status ' + c.status;
    if (c.kind === 'excerpt') return c.path + ' still contains the text the question cites';
    return c.path + ' is unchanged (a whole-file check)';
  }

  function optionLabels(qData) {
    const m = {};
    for (const o of qData.options || []) m[o.id] = o.label;
    return m;
  }

  // The sheet as Markdown, for a PR or a chat. Mirrors view.sheet_markdown.
  function sheetMarkdown(sheet) {
    const c = sheet.counts;
    let scope = sheet.item ? '`' + sheet.item + '` and all under it' : 'the whole console';
    if (sheet.fork) scope += ', fork `' + sheet.fork + '`';
    const out = ['# Answers: ' + scope, '',
      sheet.rows.length + ' questions: ' + c.awaiting_you + ' unanswered, ' + c.unlocked + ' answered, ' +
      c.locked + ' locked, ' + c.stale + ' stale. ' + sheet.answers + ' answer' + (sheet.answers === 1 ? '' : 's') + ' in all.', ''];
    for (const q of sheet.rows) {
      const r = q.question;
      const labels = optionLabels(r);
      out.push('## ' + r.qid + ': ' + STATE_WORDS[q.state] + (items[r.item] ? '' : ' (item no longer in the register)'));
      for (const line of r.text.split('\n')) out.push('> ' + line);
      out.push('');
      if (r.star) out.push('★' + (r.star_by ? ', ' + r.star_by + "'s" : '') + ': ' + (labels[r.star] || r.star));
      for (const cnd of q.failing) out.push('Stale because this no longer holds: ' + conditionWords(cnd));
      if (!q.answers.length) out.push('No answer yet.');
      q.answers.forEach((a, i) => {
        const tag = i === q.answers.length - 1 ? 'current' : 'earlier';
        const picks = a.picks.map(p => labels[p] || p).join(', ') || '(no pick)';
        out.push((i + 1) + '. ' + a.ts + ' (' + tag + (a.locked ? ', locked' : '') + '): ' + picks);
        if (a.own_text.trim()) for (const line of a.own_text.split('\n')) out.push('   > ' + line);
        if (a.reason) out.push('   Replaced the answer before it, because: ' + a.reason);
      });
      out.push('');
    }
    return out.join('\n').replace(/\s+$/, '') + '\n';
  }

  // Generate a cryptographically random nonce
  function genNonce() {
    const arr = new Uint8Array(12);
    crypto.getRandomValues(arr);
    const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-';
    let s = '';
    for (let i = 0; i < arr.length; i++) s += chars[arr[i] % chars.length];
    return s;
  }

  // Announce to screen readers via live region
  function announce(msg) {
    if (!liveRegion) return;
    liveRegion.textContent = '';
    setTimeout(() => { liveRegion.textContent = msg; }, 50);
  }

  // A brief "this changed" pop (0.7.0). CSS runs it only without reduced motion.
  function pulse(e) {
    e.classList.remove('ck-pulse');
    void e.offsetWidth; // restart the animation
    e.classList.add('ck-pulse');
  }

  // A progress ring (0.7.0): `done` of `total`, drawn as an SVG arc. It only
  // echoes a count that is always said in words beside it, so it is hidden
  // from assistive tech.
  function ring(done, total, extraClass) {
    const ns = 'http://www.w3.org/2000/svg';
    const r = 7, c = 2 * Math.PI * r;
    const frac = total > 0 ? Math.max(0, Math.min(1, done / total)) : 0;
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('class', 'ck-ring' + (extraClass ? ' ' + extraClass : '') + (total > 0 && done >= total ? ' ck-ring-full' : ''));
    svg.setAttribute('viewBox', '0 0 18 18');
    svg.setAttribute('width', '18');
    svg.setAttribute('height', '18');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    const track = document.createElementNS(ns, 'circle');
    track.setAttribute('class', 'ck-ring-track');
    const arc = document.createElementNS(ns, 'circle');
    arc.setAttribute('class', 'ck-ring-arc');
    for (const e of [track, arc]) {
      e.setAttribute('cx', '9'); e.setAttribute('cy', '9'); e.setAttribute('r', String(r));
      svg.appendChild(e);
    }
    arc.setAttribute('stroke-dasharray', c.toFixed(2));
    arc.setAttribute('stroke-dashoffset', (c * (1 - frac)).toFixed(2));
    svg.setAttribute('data-done', String(done));
    svg.setAttribute('data-total', String(total));
    return svg;
  }

  // Create element helper
  function el(tag, attrs, children) {
    const e = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        if (k === 'className') e.className = attrs[k];
        else if (k.startsWith('data')) e.setAttribute(k.replace(/[A-Z]/g, c => '-' + c.toLowerCase()), attrs[k]);
        else if (k === 'textContent') e.textContent = attrs[k];
        else e.setAttribute(k, attrs[k]);
      }
    }
    if (children) {
      for (const c of children) {
        if (typeof c === 'string') e.appendChild(document.createTextNode(c));
        else if (c) e.appendChild(c);
      }
    }
    return e;
  }

  // Truncate text for aria-labels
  function truncateText(text, maxLen) {
    if (!text) return '';
    if (text.length <= maxLen) return text;
    return text.substring(0, maxLen - 1) + '…';
  }

  // Format relative time
  function relTime(ts) {
    if (!ts) return 'never';
    const d = new Date(ts);
    const now = Date.now();
    const diff = Math.floor((now - d.getTime()) / 1000);
    if (diff < 60) return 'just now';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    return Math.floor(diff / 86400) + 'd ago';
  }

  // Fetch view from API
  async function fetchView() {
    if (!config || !config.api) return null;
    try {
      const resp = await fetch(config.api + '/view', { credentials: 'same-origin' });
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      const data = await resp.json();
      view = data.view;
      items = data.items;
      if (liveVer === null && typeof data.ver === 'string') liveVer = data.ver;  // Q24: the first wait has a baseline
      checkPromise = null; // a new view: "why stale" is checked afresh
      cursor = data.cursor;
      noteArrivals();
      updateInboxButton();
      updateItemButtons();
      updateSectionBadges();   // Overlay a tiny count on any [data-ck-item] section on the host page.
      return data;
    } catch (e) {
      cursor = cursor || {};
      cursor.last_error = "Can't reach the console server — the machine may be asleep. Nothing you typed was lost.";
      return null;
    }
  }

  // POST helper with nonce
  async function apiPost(endpoint, body, nonceKey) {
    if (!config || !config.api) return { error: 'No API configured' };
    // Reuse nonce for retry of the same submit
    let nonce = pendingNonces[nonceKey];
    if (!nonce) {
      nonce = genNonce();
      pendingNonces[nonceKey] = nonce;
    }
    body.nonce = nonce;
    try {
      const resp = await fetch(config.api + endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify(body)
      });
      const data = await resp.json();
      if (resp.ok) {
        delete pendingNonces[nonceKey];
        announce('Sent');
        await fetchView();
      }
      return data;
    } catch (e) {
      return { error: e.message || 'Network error' };
    }
  }

  // "Agent active" (owner, 2026-09-29) only on a real signal: the agent marks the
  // items it is working on, the server drops a mark once the agent syncs or an
  // hour passes, and `cursor.working` carries what is left. Without a mark the
  // honest word is still "awaiting agent": the owner wrote last, and nothing
  // says a session has picked it up.
  function agentActive(itemId) {
    return !!(cursor && cursor.working && Object.prototype.hasOwnProperty.call(cursor.working, itemId));
  }
  // 0.8.2: the named agents holding a mark on this item (the unnamed sessions' bucket is "agent").
  function activeNames(itemId) {
    const by = (cursor && cursor.working_by) || {};
    return Object.keys(by).filter(n => n !== 'agent' && by[n] &&
      Object.prototype.hasOwnProperty.call(by[n], itemId)).sort();
  }
  function agentWords(itemId) {
    if (!agentActive(itemId)) return 'awaiting agent';
    const names = activeNames(itemId);
    return names.length ? names.join(', ') + ' active' : 'agent active';
  }
  // 0.8.2: who wrote an agent record: its name when it gave one, else the word it always had.
  function agentLabel(rec, fallback) { return (rec && rec.by === 'agent' && rec.agent) ? rec.agent : fallback; }

  // What arrived with this view that the page had not seen (0.7.0): new rows are
  // marked so they can be eased in, and a screen reader is told how many came.
  function noteArrivals() {
    const ids = new Set(Object.keys(view.questions || {}));
    for (const msgs of Object.values(view.threads || {})) for (const m of msgs) ids.add(m.id);
    // 0.8.19: visuals too. A visual drawn after the owner's last look is announced + beeped (seenVisuals).
    const vids = new Set();
    for (const vs of Object.values(view.visuals || {})) for (const v of vs) vids.add(v.id);
    if (knownIds === null) { knownIds = ids; knownVisualIds = vids; arrived = new Set(); return; }
    arrived = new Set([...ids].filter(i => !knownIds.has(i)));
    const newVisuals = [...vids].filter(i => !knownVisualIds.has(i));
    knownIds = ids;
    knownVisualIds = vids;
    if (newVisuals.length) {
      beep();
      announce(newVisuals.length === 1 ? 'A new visual was drawn.' : newVisuals.length + ' new visuals were drawn.');
    }
  }

  // 0.8.19: iframe-to-parent clicks from a chart node. Only our own chart iframes talk this shape,
  // and we re-check the fields; anything else is ignored. The parent decides what to do (scroll vs. open).
  window.addEventListener('message', e => {
    const d = e && e.data;
    if (!d || typeof d !== 'object' || !d.type) return;
    if (d.type === 'ck-chart-export') {
      // The iframe serialised its rendered SVG; the parent builds a blob and triggers the download.
      if (typeof d.svg !== 'string' || !d.svg.length) return;
      const name = (typeof d.filename === 'string' && /^[\w.-]{1,80}$/.test(d.filename)) ? d.filename : 'chart';
      const blob = new Blob([d.svg], { type: 'image/svg+xml' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = name + '.svg';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      announce('SVG saved as ' + name + '.svg');
      return;
    }
    if (d.type !== 'ck-chart-click' || typeof d.target !== 'string' || typeof d.kind !== 'string') return;
    if (!panelEl || panelEl.getAttribute('data-open') !== 'true') return;
    if (d.kind === 'item') {
      openFromInbox(d.target);
      return;
    }
    if (d.kind === 'question') {
      // Already on the item view? scroll + focus. Not? open the item first, then scroll + focus.
      const inItem = currentMode === 'item' && currentItem === qidItem(d.target);
      const land = () => {
        const sel = '[data-qid="' + cssEscape(d.target) + '"]';
        const node = panelEl.querySelector('.ck-question' + sel) || panelEl.querySelector(sel);
        if (node) focusCard(node);
      };
      if (inItem) { land(); return; }
      const owner = qidItem(d.target);
      if (owner) { openFromInbox(owner, d.target); setTimeout(land, 120); }
      return;
    }
    if (d.kind === 'fork') {
      // Scroll + focus the fork's own section inside the item.
      const sel = '[data-fork="' + cssEscape(d.target) + '"], [data-request="' + cssEscape(d.target) + '"]';
      const node = panelEl.querySelector(sel);
      if (node) focusCard(node);
    }
  });

  // scrollIntoView + focus for keyboard follow-through; adds tabindex=-1 so a plain <div> can receive focus.
  function focusCard(node) {
    node.scrollIntoView({ behavior: 'smooth', block: 'center' });
    pulse(node);
    if (!node.hasAttribute('tabindex')) node.setAttribute('tabindex', '-1');
    try { node.focus({ preventScroll: true }); } catch (e) { node.focus(); }
  }

  function qidItem(qid) {
    return (typeof qid === 'string' && qid.indexOf('/') > 0) ? qid.split('/')[0] : null;
  }

  function cssEscape(s) {
    return (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/[^\w-]/g, c => '\\' + c);
  }

  // 0.8.19: a short tone when a visual arrives. Suspended AudioContexts are resumed on first click.
  let audioCtx = null;
  function beep() {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      if (!audioCtx) audioCtx = new Ctx();
      if (audioCtx.state === 'suspended') { audioCtx.resume().catch(() => {}); }
      const t0 = audioCtx.currentTime;
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = 'triangle';
      osc.frequency.setValueAtTime(660, t0);
      osc.frequency.exponentialRampToValueAtTime(880, t0 + 0.08);
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.exponentialRampToValueAtTime(0.18, t0 + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.25);
      osc.connect(gain).connect(audioCtx.destination);
      osc.start(t0);
      osc.stop(t0 + 0.27);
    } catch (e) { /* audio blocked or unsupported; the live region still announces */ }
  }

  // Unread (0.7.0): what the AGENT wrote after the store seq this browser last
  // saw with the inbox open: questions asked and messages it wrote, the chat's
  // replies included. The owner's own writes are never "unread". The seq lives
  // in this browser's localStorage, so it is per viewer; a browser that has
  // never looked starts at "nothing unread", not at the whole history.
  function seenSeq() {
    const v = memGet('seen');
    return typeof v === 'number' && v >= 0 ? v : null;
  }
  function unreadCount() {
    if (!view || typeof view.seq !== 'number') return 0;
    let seen = seenSeq();
    if (seen === null) { memSet('seen', view.seq); seen = view.seq; }
    let n = 0;
    for (const q of Object.values(view.questions || {})) if (q.question.seq > seen && q.question.by === 'agent') n++;
    for (const msgs of Object.values(view.threads || {})) for (const m of msgs) if (m.seq > seen && m.by === 'agent') n++;
    // 0.8.19: visuals drawn after the last look count as unread too.
    for (const vs of Object.values(view.visuals || {})) for (const v of vs) if (v.seq > seen) n++;
    return n;
  }

  // 0.8.19: visuals the owner has not seen yet, grouped by item (seq-gated per viewer).
  function unseenVisualsByItem() {
    if (!view || typeof view.seq !== 'number') return {};
    const seen = seenSeq();
    if (seen === null) return {};
    const out = {};
    for (const [item, vs] of Object.entries(view.visuals || {})) {
      const unseen = vs.filter(v => v.seq > seen);
      if (unseen.length) out[item] = unseen;
    }
    return out;
  }
  // The owner is looking at the inbox now: everything up to this view is seen.
  function markSeen() {
    if (!view || typeof view.seq !== 'number' || document.visibilityState === 'hidden') return;
    if (!panelEl || panelEl.getAttribute('data-open') !== 'true' || currentMode !== 'inbox') return;
    if ((seenSeq() || 0) < view.seq) memSet('seen', view.seq);
  }

  // Update inbox button badge — use view.inbox.length to avoid double-counting rolled-up totals
  function updateInboxButton() {
    if (!inboxBtn || !view) return;
    markSeen();
    const unread = unreadCount();
    // Fix #1: inbox.length is the true count, not summed totals which double-count children
    const total = (view.inbox ? view.inbox.length : 0);
    // A second, separate count: threads where the owner wrote last and an agent
    // owes the answer. Kept apart from the first so "waiting for you" never
    // includes work that is not the owner's to do.
    const agent = (view.awaiting_agent ? view.awaiting_agent.length : 0);
    const active = agent ? view.awaiting_agent.filter(agentActive).length : 0;
    let label = total ? 'Open inbox, ' + total + ' waiting for you' : 'Open inbox';
    if (agent - active) label += ', ' + (agent - active) + ' waiting on an agent';
    if (active) label += ', ' + active + ' with an agent at work';
    if (unread) label += ', ' + unread + ' new since you last looked';
    for (const b of [inboxBtn, dockStrip]) {
      if (!b) continue;
      const countEl = b.querySelector('.ck-inbox-count');
      if (countEl) countEl.textContent = total || '';
      const newEl = b.querySelector('.ck-new-count');
      if (newEl) {
        const was = newEl.textContent;
        newEl.textContent = unread ? unread + ' new' : '';
        if (unread && was !== newEl.textContent) pulse(newEl);
      }
      b.setAttribute('data-new-empty', unread ? 'false' : 'true');
      const agentEl = b.querySelector('.ck-agent-count');
      if (agentEl) agentEl.textContent = agent ? GLYPH.awaiting_agent + ' ' + agent : '';
      b.setAttribute('data-empty', total === 0 ? 'true' : 'false');
      b.setAttribute('data-agent-empty', agent === 0 ? 'true' : 'false');
      b.setAttribute('aria-label', label);
    }
  }

  // Docked (desktop) or overlay (narrower): one panel, two presentations.
  function isDocked() { return !!(dockMedia && dockMedia.matches); }

  // Put the panel's attributes and the page's reserved space in step with the
  // current width and open state. The page keeps the room through classes on
  // <html>; console.css turns them into a right margin on <body>.
  function applyDock() {
    const docked = isDocked();
    const open = panelEl.getAttribute('data-open') === 'true';
    const root = document.documentElement;
    root.classList.toggle('ck-dock', docked);
    root.classList.toggle('ck-dock-open', docked && open);
    // A docked column sits beside the board: not modal, no backdrop, no trap.
    // An overlay is a modal dialog; a docked column stays on screen beside the
    // board, so it is a landmark ("Inbox") a screen reader can jump to.
    panelEl.setAttribute('role', docked ? 'complementary' : 'dialog');
    // aria-modal is absent, not "false", when docked: "false" is the default
    // and some readers announce any aria-modal attribute as a dialog.
    if (docked) panelEl.removeAttribute('aria-modal');
    else panelEl.setAttribute('aria-modal', 'true');
    // Closed, the panel is only slid off-screen: inert keeps its controls out
    // of the Tab order and away from assistive tech until it opens again.
    panelEl.inert = !open;
    backdropEl.setAttribute('data-open', open && !docked ? 'true' : 'false');
    if (open && !docked) panelEl.addEventListener('keydown', trapFocus);
    else panelEl.removeEventListener('keydown', trapFocus);
    if (dockStrip) dockStrip.setAttribute('aria-expanded', open ? 'true' : 'false');
    // Narrowing the window turns an open column into a modal overlay: focus
    // left out on the board would sit behind the backdrop, so bring it in.
    if (open && !docked && !panelEl.contains(document.activeElement)) {
      const closeBtn = panelEl.querySelector('.ck-close-btn');
      if (closeBtn) closeBtn.focus();
    }
  }

  // Locked questions of all questions, per item and everything under it (0.7.0):
  // what an item's progress ring shows. One walk up from each question's item,
  // safe against a parent cycle.
  function lockedCounts() {
    const out = {};
    for (const q of Object.values(view.questions || {})) {
      const seen = new Set();
      let node = q.question.item;
      while (node != null && items && items[node] && !seen.has(node)) {
        seen.add(node);
        const c = out[node] || (out[node] = { locked: 0, total: 0 });
        c.total += 1;
        if (q.state === 'locked') c.locked += 1;
        node = items[node].parent;
      }
    }
    return out;
  }

  // Update item indicator buttons in the dashboard
  function updateItemButtons() {
    if (!view) return;
    const rings = lockedCounts();
    document.querySelectorAll('details[id^="item-"]').forEach(det => {
      const id = det.id.replace('item-', '');
      const btn = det.querySelector('.ck-item-btn');
      if (!btn) return;
      const data = view.items[id];
      if (!data) return;
      const rc = rings[id];
      const t = data.total;
      const parts = [];
      if (t.awaiting_you > 0) parts.push(GLYPH.awaiting_you + ' ' + t.awaiting_you + ' you');
      if (t.awaiting_agent > 0) parts.push(GLYPH.awaiting_agent + ' ' + t.awaiting_agent + (agentActive(id) ? ' agent active' : ' agent'));
      if (t.unlocked > 0) parts.push(GLYPH.unlocked + ' ' + t.unlocked + ' unlocked');
      if (t.stale > 0) parts.push(GLYPH.stale + ' ' + t.stale + ' stale');
      const hasItems = parts.length > 0;
      btn.setAttribute('data-has-items', hasItems ? 'true' : 'false');
      // Clear and rebuild
      const before = btn.textContent;
      btn.textContent = '';
      if (rc && rc.total) {
        btn.appendChild(ring(rc.locked, rc.total));
        btn.title = rc.locked + ' of ' + rc.total + ' question' + (rc.total === 1 ? '' : 's') + ' locked';
      }
      if (hasItems) {
        btn.appendChild(document.createTextNode(parts.join('  ')));
      } else {
        btn.appendChild(document.createTextNode('discuss'));
      }
      if (btn.hasAttribute('data-drawn') && before !== btn.textContent) pulse(btn);  // a live change, not the first draw
      btn.setAttribute('data-drawn', 'true');
    });
  }

  // Dashboard section overlay: for every element on the HOST page carrying `data-ck-item="X"`, inject a tiny
  // badge summarising X's state so a section shows what questions and answers it carries, inline. The badge
  // is a button: clicking it opens the console panel focused on X. Idempotent — re-runs just update the text.
  function updateSectionBadges() {
    if (!view || !items) return;
    document.querySelectorAll('[data-ck-item]').forEach(el => {
      const id = el.getAttribute('data-ck-item');
      if (!id || !items[id]) return;
      const t = view.items && view.items[id] && view.items[id].total;
      if (!t) return;
      const parts = [];
      if (t.awaiting_you > 0) parts.push(GLYPH.awaiting_you + ' ' + t.awaiting_you);
      if (t.unlocked > 0) parts.push(GLYPH.unlocked + ' ' + t.unlocked);
      if (t.stale > 0) parts.push(GLYPH.stale + ' ' + t.stale);
      if (t.locked > 0) parts.push(GLYPH.locked + ' ' + t.locked);
      let badge = el.querySelector(':scope > .ck-section-badge');
      if (!parts.length) { if (badge) badge.remove(); return; }
      if (!badge) {
        badge = document.createElement('button');
        badge.className = 'ck-section-badge';
        badge.type = 'button';
        badge.setAttribute('aria-label', 'Open ' + id + ' in the console');
        badge.addEventListener('click', () => openPanel(id, 'item'));
        el.insertBefore(badge, el.firstChild);
      }
      const words = id + '  ' + parts.join('  ');
      if (badge.textContent !== words) {
        badge.textContent = words;
        if (badge.hasAttribute('data-drawn')) pulse(badge);
      }
      badge.setAttribute('data-drawn', 'true');
    });
  }

  // Inject indicator buttons into all item summaries
  function injectItemButtons() {
    document.querySelectorAll('details[id^="item-"]').forEach(det => {
      const summary = det.querySelector('summary');
      if (!summary || summary.querySelector('.ck-item-btn')) return;
      const btn = el('button', {
        className: 'ck-item-btn',
        type: 'button',
        'aria-label': 'Open console panel'
      }, ['discuss']);
      btn.addEventListener('click', e => {
        e.preventDefault();
        e.stopPropagation();
        const id = det.id.replace('item-', '');
        openPanel(id, 'item');
      });
      summary.appendChild(btn);
    });
    updateItemButtons();
  }

  // Create the panel structure
  function createPanel() {
    // Backdrop
    backdropEl = el('div', { className: 'ck-backdrop', 'aria-hidden': 'true' });
    backdropEl.addEventListener('click', closePanel);
    document.body.appendChild(backdropEl);

    // Panel
    panelEl = el('div', {
      className: 'ck-panel',
      id: 'ck-panel',
      role: 'dialog',
      'aria-modal': 'true',
      'aria-label': 'Console panel'
    });
    panelEl.innerHTML = ''; // ensure empty
    document.body.appendChild(panelEl);

    // Live region for announcements
    liveRegion = el('div', {
      className: 'ck-live',
      'aria-live': 'polite',
      'aria-atomic': 'true'
    });
    document.body.appendChild(liveRegion);

    // Until /view has loaded once the count is unknown: "?" says so, where "0"
    // would read as "nothing waiting". updateInboxButton replaces it.
    const UNKNOWN = 'Open inbox (not loaded yet)';

    // Inbox button
    inboxBtn = el('button', {
      className: 'ck-inbox-btn',
      type: 'button',
      'aria-label': UNKNOWN
    }, [
      el('span', {}, ['Inbox']),
      el('span', { className: 'ck-inbox-count' }, ['?']),
      el('span', { className: 'ck-agent-count', 'aria-hidden': 'true' }, ['']),
      el('span', { className: 'ck-new-count', 'aria-hidden': 'true' }, [''])
    ]);
    inboxBtn.setAttribute('data-new-empty', 'true');
    inboxBtn.addEventListener('click', () => openPanel(null, 'inbox'));
    document.body.appendChild(inboxBtn);

    // Docked strip (1024px and wider): the collapsed form of the inbox column
    dockStrip = el('button', {
      className: 'ck-dock-strip',
      type: 'button',
      'aria-label': UNKNOWN,
      'aria-expanded': 'false',
      'aria-controls': 'ck-panel'
    }, [
      el('span', { className: 'ck-dock-label' }, ['Inbox']),
      el('span', { className: 'ck-inbox-count' }, ['?']),
      el('span', { className: 'ck-agent-count', 'aria-hidden': 'true' }, ['']),
      el('span', { className: 'ck-new-count', 'aria-hidden': 'true' }, [''])
    ]);
    dockStrip.setAttribute('data-new-empty', 'true');
    dockStrip.addEventListener('click', () => openPanel(null, 'inbox'));
    document.body.appendChild(dockStrip);

    dockMedia = window.matchMedia ? window.matchMedia(DOCK_QUERY) : null;
    if (dockMedia) {
      const onChange = () => applyDock();
      if (dockMedia.addEventListener) dockMedia.addEventListener('change', onChange);
      else if (dockMedia.addListener) dockMedia.addListener(onChange);
    }
    applyDock();

    // Escape key handler. An overlay closes from anywhere; a docked column sits
    // beside the board, so Escape collapses it only when focus is inside it.
    document.addEventListener('keydown', e => {
      if (e.key !== 'Escape' || panelEl.getAttribute('data-open') !== 'true') return;
      if (isDocked() && !panelEl.contains(document.activeElement)) return;
      closePanel();
    });

    // Keyboard shortcuts: `g <letter>` tab-hop, `?` help, `.` Delegate bar focus, `j/k` row walk.
    // Never fires in a text input; `g` is a sticky prefix for 1500ms.
    document.addEventListener('keydown', handleShortcut);
  }

  let shortcutGpfx = 0;
  function handleShortcut(e) {
    // Cmd+K / Ctrl+K opens the command palette. Captured before the ctrl/meta skip
    // so it works everywhere, including inside a text input — the standard app shortcut.
    if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      openPalette();
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    const tag = t && t.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (t && t.isContentEditable)) return;
    const now = Date.now();
    if (now - shortcutGpfx < 1500) {
      shortcutGpfx = 0;
      const tab = ({ i: 'inbox', f: 'feed', p: 'prs', s: 'favorite', o: 'portfolio', c: 'chat' })[e.key];
      if (tab) {
        e.preventDefault();
        if (panelEl.getAttribute('data-open') !== 'true') openPanel(null, 'inbox');
        currentMode = 'inbox';
        currentTab = tab;
        renderPanel();
        return;
      }
      if (e.key === 'b') { e.preventDefault(); if (panelEl.getAttribute('data-open') === 'true') closePanel(); return; }
      return;
    }
    if (e.key === 'g') { shortcutGpfx = now; return; }
    if (e.key === '?') { e.preventDefault(); openShortcutHelp(); return; }
    if (e.key === '.') {
      e.preventDefault();
      if (panelEl.getAttribute('data-open') !== 'true') openPanel(null, 'inbox');
      const bar = panelEl.querySelector('.ck-delegate-bar');
      if (bar) {
        if (!bar.open) bar.open = true;
        const input = bar.querySelector('input, textarea, select');
        if (input && input.focus) requestAnimationFrame(() => input.focus());
      }
      return;
    }
    if ((e.key === 'j' || e.key === 'k') && panelEl.getAttribute('data-open') === 'true') {
      e.preventDefault();
      walkFocus(e.key === 'j' ? 1 : -1);
    }
  }

  function walkFocus(dir) {
    const rows = Array.from(panelEl.querySelectorAll('[tabindex="0"]'))
      .filter(n => n.getClientRects().length > 0);
    if (!rows.length) return;
    const cur = document.activeElement;
    let i = rows.indexOf(cur);
    if (i < 0) i = dir > 0 ? -1 : rows.length;
    const next = rows[(i + dir + rows.length) % rows.length];
    if (next && next.focus) next.focus();
  }

  function openShortcutHelp() {
    if (document.getElementById('ck-shortcut-help')) return;
    const dlg = el('div', { id: 'ck-shortcut-help', className: 'ck-shortcut-help',
      role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Keyboard shortcuts' });
    dlg.appendChild(el('h2', { className: 'ck-shortcut-h' }, ['Keyboard shortcuts']));
    const rows = [
      ['Ctrl/Cmd + K', 'Open the command palette'],
      ['g i', 'Open Inbox tab'], ['g f', 'Open Feed tab'], ['g p', 'Open PRs tab'],
      ['g s', 'Open Favorite tab'], ['g o', 'Open Portfolio tab'], ['g c', 'Open Chat tab'],
      ['g b', 'Close panel (back to board)'],
      ['.',   'Focus the Delegate bar'],
      ['j / k', 'Next / previous row'],
      ['Enter / Space', 'Open the focused row'],
      ['?',   'Show this help'],
      ['Esc', 'Close panel or this help']
    ];
    const dl = el('dl', { className: 'ck-shortcut-list' });
    for (const [k, v] of rows) {
      dl.appendChild(el('dt', {}, [k]));
      dl.appendChild(el('dd', {}, [v]));
    }
    dlg.appendChild(dl);
    const close = el('button', { className: 'ck-btn ck-shortcut-close', type: 'button' }, ['Close (Esc)']);
    close.addEventListener('click', closeShortcutHelp);
    dlg.appendChild(close);
    dlg.addEventListener('keydown', e => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeShortcutHelp(); }
    });
    document.body.appendChild(dlg);
    close.focus();
  }
  function closeShortcutHelp() {
    const dlg = document.getElementById('ck-shortcut-help');
    if (dlg) dlg.remove();
  }

  // Command palette: one search box over items, playbooks, peers, tabs, actions.
  // Fuzzy filter is a simple case-insensitive subsequence; ranking favours start-of-label hits
  // and then prefers item > question > playbook > peer > tab > action, so a typed id wins.
  const PALETTE_KINDS_ORDER = ['item', 'question', 'playbook', 'peer', 'tab', 'action'];
  let paletteState = { items: [], idx: 0, query: '', open: false };

  function buildPaletteCommands() {
    const cmds = [];
    const tabs = [['inbox', 'Inbox', 'g i'], ['feed', 'Feed', 'g f'], ['prs', 'PRs', 'g p'],
                  ['favorite', 'Favorite', 'g s'], ['portfolio', 'Portfolio', 'g o'], ['chat', 'Chat', 'g c']];
    for (const [id, label, hint] of tabs) cmds.push({ kind: 'tab', id, label, hint });
    for (const id of Object.keys(items || {})) {
      const it = items[id] || {};
      cmds.push({ kind: 'item', id, label: id + (it.title ? ' · ' + it.title : ''), hint: 'open' });
    }
    const qs = (view && view.questions) || {};
    for (const qid of Object.keys(qs)) {
      const q = qs[qid] && qs[qid].question;
      if (!q || !q.item) continue;
      cmds.push({ kind: 'question', id: qid, label: qid + (q.text ? ' · ' + q.text.slice(0, 60) : ''),
        hint: (qs[qid].state || 'unlocked'), item: q.item });
    }
    for (const pb of (view && view.playbooks) || []) {
      cmds.push({ kind: 'playbook', id: pb.name, label: 'Playbook: ' + pb.name,
        hint: pb.description ? pb.description.slice(0, 60) : (pb.steps + ' step' + (pb.steps === 1 ? '' : 's')) });
    }
    const peers = (portfolioState.data && portfolioState.data.peers) || [];
    for (const p of peers) {
      if (!p || !p.name) continue;
      cmds.push({ kind: 'peer', id: p.name, label: 'Peer: ' + (p.project || p.name),
        hint: p.ok === false ? (p.error || 'unreachable') : (p.url || '') });
    }
    cmds.push({ kind: 'action', id: 'close-panel', label: 'Close panel', hint: 'g b' });
    cmds.push({ kind: 'action', id: 'show-shortcuts', label: 'Show keyboard shortcuts', hint: '?' });
    cmds.push({ kind: 'action', id: 'open-delegate', label: 'Focus Delegate bar', hint: '.' });
    return cmds;
  }

  function filterPalette(cmds, query) {
    const q = (query || '').trim().toLowerCase();
    if (!q) {
      return cmds.slice().sort(byPaletteKind).slice(0, 40);
    }
    const scored = [];
    for (const c of cmds) {
      const lab = c.label.toLowerCase();
      let score;
      if (lab.startsWith(q)) score = 0;
      else if (lab.includes(q)) score = 1;
      else if (subsequence(lab, q)) score = 2;
      else continue;
      scored.push({ c, score });
    }
    scored.sort((a, b) => a.score - b.score
      || PALETTE_KINDS_ORDER.indexOf(a.c.kind) - PALETTE_KINDS_ORDER.indexOf(b.c.kind)
      || a.c.label.length - b.c.label.length);
    return scored.slice(0, 40).map(s => s.c);
  }
  function byPaletteKind(a, b) {
    return PALETTE_KINDS_ORDER.indexOf(a.kind) - PALETTE_KINDS_ORDER.indexOf(b.kind)
      || a.label.localeCompare(b.label);
  }
  function subsequence(hay, needle) {
    let i = 0; for (const ch of hay) if (ch === needle[i]) i++; return i === needle.length;
  }

  function openPalette() {
    if (document.getElementById('ck-palette')) return;
    paletteState = { items: filterPalette(buildPaletteCommands(), ''), idx: 0, query: '', open: true };
    const dlg = el('div', { id: 'ck-palette', className: 'ck-palette',
      role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Command palette' });
    const input = el('input', { className: 'ck-palette-input', type: 'text',
      placeholder: 'Jump to item, playbook, peer, tab, action…',
      'aria-label': 'Search command palette', autocomplete: 'off', spellcheck: 'false' });
    const list = el('ul', { className: 'ck-palette-list', role: 'listbox' });
    const foot = el('div', { className: 'ck-palette-foot ck-muted' },
      ['↑↓ walk · Enter run · Esc close']);
    dlg.appendChild(input);
    dlg.appendChild(list);
    dlg.appendChild(foot);
    document.body.appendChild(dlg);
    paintPalette(list);
    input.addEventListener('input', () => {
      paletteState.query = input.value;
      paletteState.items = filterPalette(buildPaletteCommands(), input.value);
      paletteState.idx = 0;
      paintPalette(list);
    });
    input.addEventListener('keydown', e => {
      if (e.key === 'Escape') { e.preventDefault(); closePalette(); return; }
      if (e.key === 'ArrowDown') { e.preventDefault(); paletteState.idx = Math.min(paletteState.items.length - 1, paletteState.idx + 1); paintPalette(list); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); paletteState.idx = Math.max(0, paletteState.idx - 1); paintPalette(list); return; }
      if (e.key === 'Enter') { e.preventDefault(); runPaletteSelected(); return; }
    });
    dlg.addEventListener('click', e => { if (e.target === dlg) closePalette(); });
    requestAnimationFrame(() => input.focus());
  }

  function paintPalette(list) {
    list.textContent = '';
    const items = paletteState.items || [];
    if (!items.length) {
      list.appendChild(el('li', { className: 'ck-palette-empty ck-muted' }, ['no match']));
      return;
    }
    items.forEach((c, i) => {
      const li = el('li', { className: 'ck-palette-row' + (i === paletteState.idx ? ' ck-palette-row-sel' : ''),
        role: 'option', 'aria-selected': i === paletteState.idx ? 'true' : 'false', dataKind: c.kind });
      li.appendChild(el('span', { className: 'ck-palette-kind', dataKind: c.kind }, [c.kind]));
      li.appendChild(el('span', { className: 'ck-palette-label' }, [c.label]));
      if (c.hint) li.appendChild(el('span', { className: 'ck-palette-hint ck-muted' }, [c.hint]));
      li.addEventListener('click', () => { paletteState.idx = i; runPaletteSelected(); });
      list.appendChild(li);
    });
  }

  function runPaletteSelected() {
    const c = paletteState.items[paletteState.idx];
    if (!c) return;
    closePalette();
    if (c.kind === 'tab') {
      if (panelEl.getAttribute('data-open') !== 'true') openPanel(null, 'inbox');
      currentMode = 'inbox';
      currentTab = c.id;
      renderPanel();
      return;
    }
    if (c.kind === 'item') { openPanel(c.id, 'item'); return; }
    if (c.kind === 'question') { openPanel(c.item, 'item'); return; }
    if (c.kind === 'playbook') {
      apiPost('/playbook', { name: c.id }, 'palette-pb-' + c.id + '-' + Date.now())
        .then(r => {
          if (r && r.error) announce('Playbook not run: ' + r.error);
          else if (r && Array.isArray(r.skipped) && r.skipped.length)
            announce((r.records || []).length + ' step(s) ran; ' + r.skipped.length + ' skipped.');
          else announce('Playbook ran.');
        })
        .catch(e => announce('Playbook error: ' + (e.message || 'network')));
      return;
    }
    if (c.kind === 'peer') {
      const peer = ((portfolioState.data && portfolioState.data.peers) || []).find(p => p && p.name === c.id);
      if (peer && peer.url) { try { window.open(peer.url, '_blank', 'noopener'); } catch (e) {} }
      return;
    }
    if (c.kind === 'action') {
      if (c.id === 'close-panel') { if (panelEl.getAttribute('data-open') === 'true') closePanel(); return; }
      if (c.id === 'show-shortcuts') { openShortcutHelp(); return; }
      if (c.id === 'open-delegate') {
        if (panelEl.getAttribute('data-open') !== 'true') openPanel(null, 'inbox');
        const bar = panelEl.querySelector('.ck-delegate-bar');
        if (bar) {
          if (!bar.open) bar.open = true;
          const input = bar.querySelector('input, textarea, select');
          if (input) requestAnimationFrame(() => input.focus());
        }
        return;
      }
    }
  }

  function closePalette() {
    const dlg = document.getElementById('ck-palette');
    if (dlg) dlg.remove();
    paletteState.open = false;
  }

  // Focus trapping inside panel
  function trapFocus(e) {
    if (panelEl.getAttribute('data-open') !== 'true') return;
    // Only controls that are rendered: the ← Back button is display:none above
    // 399px, and counting it as `first` let Shift+Tab out of the modal and left
    // Tab from the last control focusing nothing.
    const focusable = Array.from(panelEl.querySelectorAll(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
    )).filter(e => e.getClientRects().length > 0 && !e.disabled);
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }

  // Open panel
  function openPanel(itemId, mode) {
    lastFocused = document.activeElement;
    currentItem = itemId;
    currentMode = mode;
    fromInbox = false;
    if (mode === 'inbox') seenAtOpen = seenSeq() || 0;
    panelEl.setAttribute('data-open', 'true');
    panelEl.setAttribute('aria-label', mode === 'inbox' ? 'Inbox' : 'Console: ' + (itemId || ''));
    applyDock();
    renderPanel();
    // Fix #2: Focus the close button reliably after render
    requestAnimationFrame(() => {
      const closeBtn = panelEl.querySelector('.ck-close-btn');
      if (closeBtn) closeBtn.focus();
    });
    // Fetch fresh view
    fetchView().then(() => {
      renderPanel();
      // Re-focus after re-render if panel still open
      requestAnimationFrame(() => {
        // Docked, the owner may already be back on the board: only recover focus
        // the re-render dropped (onto <body>), never pull it out of the page.
        const lost = !isDocked() || document.activeElement === document.body;
        if (panelEl.getAttribute('data-open') === 'true' && lost && !panelEl.contains(document.activeElement)) {
          const closeBtn = panelEl.querySelector('.ck-close-btn');
          if (closeBtn) closeBtn.focus();
        }
      });
    });
  }

  // Close panel
  function closePanel() {
    panelEl.setAttribute('data-open', 'false');
    dropLocksNotOnShow();
    applyDock();
    // Default-open stays dismissed for the session once the owner closes it.
    try { sessionStorage.setItem('ck-inbox-closed', '1'); } catch (e) { /* storage blocked */ }
    // Back to what opened it; when that was the strip, it is the strip again.
    if (lastFocused && lastFocused.focus && document.contains(lastFocused)) lastFocused.focus();
  }

  // Render panel content
  function renderPanel() {
    dropLocksNotOnShow();
    panelEl.textContent = '';
    pendingLive = false;
    if (currentMode === 'inbox') {
      renderInbox();
      markSeen();
      updateInboxButton();
    } else if (currentMode === 'round') {
      renderRound(currentFork);
    } else if (currentMode === 'sheet') {
      renderSheet(currentItem, currentFork);
    } else if (currentItem) {
      renderItem(currentItem);
    }
  }

  // Render inbox mode
  function renderInbox() {
    panelEl.appendChild(makeHeader(['Inbox'], closePanel, 'Back to board'));
    panelEl.appendChild(renderTabs());
    panelEl.appendChild(renderStatusBar(null));
    panelEl.appendChild(renderDelegateBar());   // Delegate ▾ — orchestrate from anywhere, not only on an item.
    const trgs = view && Array.isArray(view.triggers) ? view.triggers : [];
    const log = view && Array.isArray(view.trigger_log) ? view.trigger_log : [];
    if (trgs.length || log.length) panelEl.appendChild(renderTriggerLog(trgs, log));

    // One tab panel; the tab bar above says which view it holds (0.7.0).
    const body = el('div', { className: 'ck-body', role: 'tabpanel', id: 'ck-tabpanel',
      'aria-labelledby': 'ck-tab-' + currentTab });
    if (!view) {
      body.appendChild(renderOffline());
    } else if (currentTab === 'feed') {
      renderFeed(body);
    } else if (currentTab === 'prs') {
      renderPRs(body);
    } else if (currentTab === 'favorite') {
      renderFavorites(body);
    } else if (currentTab === 'portfolio') {
      renderPortfolio(body);
    } else if (currentTab === 'chat') {
      renderChat(body);
    } else {
      renderInboxList(body);
    }
    panelEl.appendChild(body);
    if (currentTab === 'chat') {
      const log = body.querySelector('.ck-chat-log');
      if (log) log.scrollTop = log.scrollHeight;
    }
  }

  // The inbox's tabs. Order: Inbox Feed PRs Favorite Portfolio Chat.
  const TABS = [['inbox', 'Inbox'], ['feed', 'Feed'], ['prs', 'PRs'],
                ['favorite', 'Favorite'], ['portfolio', 'Portfolio'], ['chat', 'Chat']];

  function renderTabs() {
    const bar = el('div', { className: 'ck-tabs', role: 'tablist', 'aria-label': 'Inbox views' });
    const unread = unreadCount();
    TABS.forEach(([id, label]) => {
      const selected = currentTab === id;
      const b = el('button', { className: 'ck-tab', type: 'button', role: 'tab', id: 'ck-tab-' + id,
        'aria-selected': selected ? 'true' : 'false', 'aria-controls': 'ck-tabpanel', tabindex: selected ? '0' : '-1' },
      [label]);
      let note = '';
      if (id === 'inbox' && view && view.inbox && view.inbox.length) note = String(view.inbox.length);
      if (id === 'feed' && unread) note = unread + ' new';
      if (id === 'favorite' && view && view.favorites && view.favorites.length) note = String(view.favorites.length);
      if (id === 'portfolio') {
        const n = portfolioAwaitingTotal();
        if (n) note = n + ' ?you';
      }
      if (id === 'chat' && view && view.chat && view.chat.awaiting_agent) note = '●';
      if (note) b.appendChild(el('span', { className: 'ck-tab-note' }, [' ' + note]));
      if (id === 'chat' && note) b.setAttribute('aria-label', 'Chat, waiting on an agent');
      b.addEventListener('click', () => selectTab(id));
      b.addEventListener('keydown', e => {
        const n = TABS.findIndex(t => t[0] === id);
        let to = null;
        if (e.key === 'ArrowRight') to = TABS[(n + 1) % TABS.length][0];
        else if (e.key === 'ArrowLeft') to = TABS[(n + TABS.length - 1) % TABS.length][0];
        else if (e.key === 'Home') to = TABS[0][0];
        else if (e.key === 'End') to = TABS[TABS.length - 1][0];
        if (to) { e.preventDefault(); selectTab(to); }
      });
      bar.appendChild(b);
    });
    return bar;
  }

  function selectTab(id) {
    currentTab = id;
    renderPanel();
    const t = panelEl.querySelector('#ck-tab-' + id);
    if (t) t.focus();
  }

  // The Inbox tab: rounds to answer as one form each, then loose questions, then what waits on an agent.
  function renderInboxList(body) {
    // Q24: before the steward's first items-push the server has no items, and says why (F63): never a blank.
    if (view.items_note) {
      body.appendChild(el('p', { className: 'ck-items-note', role: 'status',
        style: 'color: var(--c-fg-muted); padding: 12px 20px;' }, [view.items_note]));
    }
    // 0.8.19: a map of every item (collapsible, lazy). Clicking a node opens that item.
    if (items && Object.keys(items).length > 1) body.appendChild(renderProjectMap());
    // 0.8.19: items whose visuals arrived since the last look, so a drawn flowchart is impossible to miss.
    const newVisuals = unseenVisualsByItem();
    const newVisualIds = Object.keys(newVisuals);
    if (newVisualIds.length) {
      body.appendChild(el('div', { className: 'ck-section-heading' }, ['New visuals']));
      const list = el('div', { className: 'ck-inbox-list' });
      for (const it of newVisualIds.sort()) {
        const itemData = items[it];
        const vs = newVisuals[it];
        const row = el('div', { className: 'ck-inbox-item ck-inbox-item-visual', tabindex: '0',
          'aria-label': 'New visual on ' + it + (itemData ? ', ' + itemData.title : '') }, [
          el('span', { className: 'ck-q-state', dataState: 'visual' }, ['◫ ', vs.length === 1 ? 'drawn' : vs.length + ' drawn']),
          el('span', { className: 'ck-inbox-item-id' }, [it]),
          el('span', { className: 'ck-inbox-item-title' }, [itemData ? itemData.title : '']),
          renderStarButton('item:' + it, 'this item')
        ]);
        row.addEventListener('click', e => {
          if (e.target.closest('.ck-star')) return;
          openFromInbox(it);
        });
        row.addEventListener('keydown', e => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openFromInbox(it); }
        });
        list.appendChild(row);
      }
      body.appendChild(list);
    }
    const rounds = new Map();
    const loose = [];
    for (const qid of view.inbox || []) {
      const q = view.questions[qid];
      if (!q) continue;
      const f = q.question.forked_from;
      // A round's form walks only open questions (roundSteps), so a ruling from a round that has gone stale
      // would vanish inside its card. It is listed as a loose row instead, which opens its stale card.
      if (f && view.forks[f] && OPEN_STATES.includes(q.state)) {
        if (!rounds.has(f)) rounds.set(f, []);
        rounds.get(f).push(q);
      } else {
        loose.push(q);
      }
    }
    if (rounds.size) {
      body.appendChild(el('div', { className: 'ck-section-heading' }, ['Rounds to answer']));
      const list = el('div', { className: 'ck-inbox-list' });
      for (const fid of rounds.keys()) list.appendChild(renderRoundCard(fid));
      body.appendChild(list);
    }
    // 0.8.19: for every item with open questions here, nest its locked rulings underneath as a collapsible,
    // so the owner sees direction (what was asked → what you answered) without leaving the Inbox.
    const rulingsByItem = {};
    for (const qid of Object.keys(view.questions)) {
      const q = view.questions[qid];
      if (q.state !== 'locked') continue;
      const it = q.question.item;
      if (!rulingsByItem[it]) rulingsByItem[it] = [];
      rulingsByItem[it].push(q);
    }
    for (const it of Object.keys(rulingsByItem)) {
      rulingsByItem[it].sort((a, b) => (b.locked_at || b.question.ts || '').localeCompare(a.locked_at || a.question.ts || ''));
    }
    const footerShown = new Set();

    if (loose.length) {
      body.appendChild(el('div', { className: 'ck-section-heading' }, ['Questions for you']));
      const list = el('div', { className: 'ck-inbox-list' });
      const clusters = looseClusters(loose);
      const inCluster = new Set(clusters.flatMap(c => c.qs.map(q => q.question.qid)));
      // Walk in the order that produced the inbox; after the last row for an item, append its "N answered".
      const emit = (itemId, node) => {
        list.appendChild(node);
        if (!footerShown.has(itemId) && rulingsByItem[itemId] && rulingsByItem[itemId].length) {
          list.appendChild(renderAnsweredFooter(itemId, rulingsByItem[itemId]));
          footerShown.add(itemId);
        }
      };
      for (const c of clusters) emit(c.item, renderClusterCard(c));
      for (const q of loose) if (!inCluster.has(q.question.qid)) emit(q.question.item, looseRow(q));
      body.appendChild(list);
    }

    // Items whose questions are all locked (nothing open), surfaced within the last 24h: shows "direction" too.
    const RECENT_MS = 24 * 60 * 60 * 1000;
    const now = Date.now();
    const recentlyAnswered = Object.keys(rulingsByItem)
      .filter(it => !footerShown.has(it))
      .filter(it => rulingsByItem[it].some(q => {
        const t = Date.parse(q.locked_at || q.question.ts || '');
        return Number.isFinite(t) && (now - t) <= RECENT_MS;
      }))
      .sort();
    if (recentlyAnswered.length) {
      body.appendChild(el('div', { className: 'ck-section-heading' }, ['Recently answered']));
      const list = el('div', { className: 'ck-inbox-list' });
      for (const it of recentlyAnswered) {
        const itemData = items[it];
        const row = el('div', { className: 'ck-inbox-item ck-inbox-item-answered', tabindex: '0' }, [
          el('span', { className: 'ck-q-state', dataState: 'locked' }, [GLYPH.locked || '✓', ' locked']),
          el('span', { className: 'ck-inbox-item-id' }, [it]),
          el('span', { className: 'ck-inbox-item-title' }, [itemData ? itemData.title : '']),
          renderStarButton('item:' + it, 'this item')
        ]);
        row.addEventListener('click', e => {
          if (e.target.closest('.ck-star')) return;
          openFromInbox(it);
        });
        list.appendChild(row);
        list.appendChild(renderAnsweredFooter(it, rulingsByItem[it]));
        footerShown.add(it);
      }
      body.appendChild(list);
    }
    const scanAll = renderScanAll();
    if (scanAll) body.appendChild(scanAll);

    // Items awaiting agent
    if (view.awaiting_agent && view.awaiting_agent.length > 0) {
      const allActive = view.awaiting_agent.every(agentActive);
      body.appendChild(el('div', { className: 'ck-section-heading' }, [allActive ? 'Agent active' : 'Awaiting agent']));
      const list = el('div', { className: 'ck-inbox-list' });
      for (const itemId of view.awaiting_agent) {
        const itemData = items[itemId];
        const item = el('div', { className: 'ck-inbox-item', tabindex: '0' }, [
          el('span', { className: 'ck-q-state', dataState: agentActive(itemId) ? 'agent_active' : 'awaiting_agent' }, [
            GLYPH.awaiting_agent, ' ' + agentWords(itemId)
          ]),
          el('span', { className: 'ck-inbox-item-id' }, [itemId]),
          el('span', { className: 'ck-inbox-item-title' }, [itemData ? itemData.title : ''])
        ]);
        item.addEventListener('click', () => openFromInbox(itemId));
        list.appendChild(item);
      }
      body.appendChild(list);
    }

    const chatWaits = !!(view.chat && view.chat.awaiting_agent);
    if (chatWaits) {
      const row = el('button', { className: 'ck-inbox-item ck-inbox-chat', type: 'button' }, [
        el('span', { className: 'ck-q-state', dataState: 'awaiting_agent' }, [GLYPH.awaiting_agent, ' awaiting agent']),
        el('span', { className: 'ck-inbox-item-title' }, ['Your chat message'])
      ]);
      row.addEventListener('click', () => selectTab('chat'));
      body.appendChild(el('div', { className: 'ck-section-heading' }, ['Chat']));
      body.appendChild(row);
    }

    if (!rounds.size && !loose.length && (!view.awaiting_agent || view.awaiting_agent.length === 0) && !chatWaits) {
      body.appendChild(el('p', { style: 'color: var(--c-fg-muted); text-align: center; padding: 20px;' },
        ['No pending items.']));
    }
  }

  // What waits on the owner about a stale ruling, said on its row: a steward's proposal or advice.
  function staleWaiting(q) {
    const rx = q.state === 'stale' ? q.refactor : null;
    if (!rx) return '';
    const w = [];
    if (rx.proposal) w.push('a new anchor is proposed');
    if (rx.advice) w.push('advice is waiting');
    return w.join(', ');
  }

  function looseRow(q) {
    const itemData = items[q.question.item];
    const qid = q.question.qid;
    const waiting = staleWaiting(q);
    const item = el('div', { className: 'ck-inbox-item' + (arrived.has(qid) ? ' ck-arrived' : ''),
      tabindex: '0', dataQid: qid,
      'aria-label': q.state.replace('_', ' ') + ': ' + qid + (itemData ? ', ' + itemData.title : '')
        + (waiting ? '. ' + waiting : '') }, [
      el('span', { className: 'ck-q-state', dataState: q.state }, [
        GLYPH[q.state] || '', ' ', q.state.replace('_', ' ')
      ]),
      el('span', { className: 'ck-inbox-item-id' }, [q.question.item]),
      el('span', { className: 'ck-inbox-item-qnum', title: qid }, ['Q' + qNum(qid)]),
      el('span', { className: 'ck-inbox-item-title' }, [itemData ? itemData.title : '']),
      waiting ? el('span', { className: 'ck-inbox-item-waiting' }, [waiting]) : ''
    ]);
    const go = () => openFromInbox(q.question.item, qid);
    item.addEventListener('click', go);
    item.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
    });
    return item;
  }

  // 0.8.19: a collapsible "N answered" footer under an item in the Inbox. Each row is a locked ruling
  // on this item; clicking it opens the item and scrolls to that question. Shows direction without leaving.
  function renderAnsweredFooter(itemId, locked) {
    const n = locked.length;
    const wrap = el('details', { className: 'ck-answered-footer', dataItem: itemId });
    const sum = el('summary', { className: 'ck-answered-summary' }, [
      n + ' answered on ' + itemId
    ]);
    wrap.appendChild(sum);
    const list = el('div', { className: 'ck-answered-list' });
    for (const q of locked.slice(0, 8)) {
      const row = el('div', { className: 'ck-answered-row', tabindex: '0',
        'aria-label': 'Open answer for ' + q.question.qid }, [
        el('span', { className: 'ck-q-state', dataState: 'locked' }, [GLYPH.locked || '✓', ' locked']),
        el('span', { className: 'ck-inbox-item-qnum' }, ['Q' + qNum(q.question.qid)]),
        el('span', { className: 'ck-answered-pick' }, [answerSummary(q)])
      ]);
      const go = () => openFromInbox(itemId, q.question.qid);
      row.addEventListener('click', go);
      row.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
      });
      list.appendChild(row);
    }
    if (n > 8) list.appendChild(el('div', { className: 'ck-muted ck-answered-more' }, [
      '…and ' + (n - 8) + ' more; open the item to see them all.'
    ]));
    wrap.appendChild(list);
    return wrap;
  }

  // One short phrase describing a locked ruling: the picked option label, or free text, or the state.
  function answerSummary(q) {
    const a = (q.answers && q.answers.length) ? q.answers[q.answers.length - 1] : null;
    if (!a) return 'locked';
    if (a.pick_labels && a.pick_labels.length) return a.pick_labels.join(' · ');
    if (a.picks && a.picks.length) return a.picks.join(' · ');
    if (a.text) return truncateText(a.text, 80);
    return 'locked';
  }

  // CONSOLE-kit/Q35 (owner, 2026-10-02, the ★): loose questions about one item, asked by one agent within a
  // few minutes, are offered as one round: "Answer these N together" opens the round form on them.
  // "One agent" is the name the asking session gave (names.jsonl, kit 0.8.2): it is the only thing the view
  // carries about who asked. A question asked with no name can't be shown to be the same agent's as another,
  // so it is never grouped. The run breaks at a gap of more than CLUSTER_GAP_MS between two asks.
  const CLUSTER_GAP_MS = 5 * 60 * 1000;

  function askedAt(q) { const t = Date.parse(q.question.ts); return Number.isFinite(t) ? t : 0; }

  function looseClusters(loose) {
    const byKey = new Map();
    for (const q of loose) {
      const who = q.question.agent;
      if (!who || !OPEN_STATES.includes(q.state)) continue;   // a group opens as a round, which walks open ones only
      const k = q.question.item + '\u0000' + who;
      if (!byKey.has(k)) byKey.set(k, []);
      byKey.get(k).push(q);
    }
    const out = [];
    const flush = run => {
      if (run.length < 2) return;
      out.push({ id: 'loose:' + run[0].question.qid, item: run[0].question.item, agent: run[0].question.agent,
        qs: run });
    };
    for (const qs of byKey.values()) {
      qs.sort((a, b) => askedAt(a) - askedAt(b) || qNum(a.question.qid) - qNum(b.question.qid));
      let run = [qs[0]];
      for (const q of qs.slice(1)) {
        if (askedAt(q) - askedAt(run[run.length - 1]) <= CLUSTER_GAP_MS) run.push(q);
        else { flush(run); run = [q]; }
      }
      flush(run);
    }
    return out;
  }

  function renderClusterCard(c) {
    const n = c.qs.length;
    const btn = el('button', { className: 'ck-btn ck-btn-primary ck-cluster-go', type: 'button', dataFocusKey: c.id,
      'aria-label': 'Answer these ' + n + ' questions from ' + c.agent + ' on ' + c.item + ' together' },
    ['Answer these ' + n + ' together']);
    btn.addEventListener('click', () => openLooseRound(c));
    const card = el('div', { className: 'ck-cluster', dataCluster: c.id }, [
      el('div', { className: 'ck-cluster-head' }, [
        el('span', { className: 'ck-inbox-item-id' }, [c.item]),
        ' ' + n + ' questions from ' + c.agent + ' · asked ' + relTime(c.qs[n - 1].question.ts)]),
      btn
    ]);
    const rows = el('div', { className: 'ck-cluster-rows' });
    for (const q of c.qs) rows.appendChild(looseRow(q));
    card.appendChild(rows);
    return card;
  }

  // A group is fixed when it is opened: answering one of its questions mid-way never reshapes the round under
  // the owner (it is kept with the drafts, so a reload resumes it).
  const looseGroups = {};   // 'loose:<first qid>' -> [qid, ...]
  function isLoose(id) { return typeof id === 'string' && id.startsWith('loose:'); }
  function looseQids(id) {
    if (!looseGroups[id]) {
      const saved = memGet('loose-group:' + id);
      looseGroups[id] = Array.isArray(saved) ? saved.filter(x => typeof x === 'string') : [];
    }
    return looseGroups[id];
  }
  function openLooseRound(c) {
    looseGroups[c.id] = c.qs.map(q => q.question.qid);
    memSet('loose-group:' + c.id, looseGroups[c.id]);
    openRound(c.id);
  }

  // 0.8.5: the inbox opens an item in its own panel, so the item must offer the way back.
  function openFromInbox(itemId, qid) {
    currentItem = itemId;
    currentMode = 'item';
    fromInbox = true;
    renderPanel();
    const back = panelEl.querySelector('.ck-back-btn');
    if (back) back.focus();
    const card = qid && panelEl.querySelector('.ck-question[data-qid="' + CSS.escape(qid) + '"]');
    if (card && card.scrollIntoView) card.scrollIntoView({ block: 'nearest' });
  }

  function backToInbox() {
    const left = currentItem;
    currentItem = null;
    currentMode = 'inbox';
    fromInbox = false;
    renderPanel();
    // Back onto the row that was opened, when it is still listed.
    const rows = Array.from(panelEl.querySelectorAll('.ck-inbox-item'));
    const row = rows.find(r => {
      const id = r.querySelector('.ck-inbox-item-id');
      return id && id.textContent === left;
    });
    const target = row || panelEl.querySelector('.ck-close-btn');
    if (target) target.focus();
  }

  // Render item view
  function renderItem(itemId) {
    const itemData = items ? items[itemId] : null;

    const header = el('div', { className: 'ck-header' }, [
      el('button', {
        className: fromInbox ? 'ck-back-btn ck-back-always' : 'ck-back-btn',
        type: 'button',
        'aria-label': fromInbox ? 'Back to inbox' : 'Back to board'
      }, [fromInbox ? '← Inbox' : '← Back']),
      el('span', { className: 'ck-title' }, [
        el('span', { className: 'ck-title-id' }, [itemId]),
        itemData ? ' — ' + itemData.title : ''
      ]),
      renderStarButton('item:' + itemId, 'this item'),
      el('button', {
        className: 'ck-close-btn',
        type: 'button',
        'aria-label': 'Close panel'
      }, ['×'])
    ]);
    header.querySelector('.ck-back-btn').addEventListener('click', fromInbox ? backToInbox : closePanel);
    header.querySelector('.ck-close-btn').addEventListener('click', closePanel);
    panelEl.appendChild(header);
    const sendBar = view ? renderSendBar(itemId) : null;
    if (sendBar) panelEl.appendChild(sendBar);

    // Status bar with item counts
    const statusBar = renderStatusBar(itemId);
    panelEl.appendChild(statusBar);

    // Body
    const body = el('div', { className: 'ck-body' });
    if (!view) {
      body.appendChild(renderOffline());
    } else {
      // Questions for this item
      const qs = Object.values(view.questions).filter(q => q.question.item === itemId);
      // Sort: awaiting_you, unlocked, stale, locked
      const order = ['awaiting_you', 'unlocked', 'stale', 'locked', 'superseded', 'withdrawn'];
      qs.sort((a, b) => order.indexOf(a.state) - order.indexOf(b.state));

      // Dashboard section links: `.console-kit.json` can map this item to one or more #anchors on the host page.
      const sectionLinks = renderSectionLinks(itemId);
      if (sectionLinks) body.appendChild(sectionLinks);
      body.appendChild(renderItemTools(itemId));
      // 0.8.19: a status flowchart for the whole item, lazy-loaded on expand, re-rendered on live wake.
      if (qs.length > 0) body.appendChild(renderItemChart(itemId));
      // 0.14.0: interactive impact graph (Cytoscape), collapsed by default.
      if (qs.length > 0) body.appendChild(renderItemImpact(itemId));

      if (qs.length > 0) {
        body.appendChild(el('div', { className: 'ck-section-heading ck-questions-heading' }, ['Questions']));
        const ready = qs.filter(q => q.state === 'unlocked' && q.answers && q.answers.length > 0);
        if (ready.length > 1 || lockAllError[itemId] || lockingAll === itemId) {
          body.appendChild(renderLockAnswered(itemId, ready));
        }
        for (const q of qs) {
          body.appendChild(renderQuestion(q));
        }
      }

      body.appendChild(renderForks(itemId));
      body.appendChild(renderVisuals(itemId));

      // Thread
      body.appendChild(renderThread(itemId));
    }
    panelEl.appendChild(body);
  }

  // 0.8.5 (owner, 2026-09-30: "there should be a 'lock all items' button rather than one for
  // each question being answered"): one confirmation locks every answered question on the
  // item. Each lock is the POST the single button sends, one after another, so the server's
  // checks are unchanged. A refusal stops there and names its question; what was locked
  // before it stays locked, because a lock is never undone.
  function renderLockAnswered(itemId, ready) {
    const key = 'lockall-' + itemId;
    const wrap = el('div', { className: 'ck-lock-answered' });
    if (lockAllError[itemId]) {
      // No role=alert: it re-announced on every live redraw. announce() says it once, and
      // focus lands here after the run.
      const err = el('div', { className: 'ck-error-msg ck-lock-answered-error' }, [lockAllError[itemId]]);
      err.setAttribute('tabindex', '-1');
      const dismiss = el('button', { className: 'ck-btn ck-btn-quiet', type: 'button' }, ['Dismiss']);
      dismiss.addEventListener('click', () => {
        delete lockAllError[itemId];
        renderPanel();
        const h = panelEl.querySelector('.ck-questions-heading');
        if (h) { h.setAttribute('tabindex', '-1'); h.focus(); }
      });
      err.appendChild(dismiss);
      wrap.appendChild(err);
    }
    if (lockingAll === itemId) {
      wrap.appendChild(el('p', { className: 'ck-muted' }, ['Locking answers…']));
      return wrap;
    }
    if (ready.length < 2) return wrap;
    if (!openForms.has(key)) {
      const open = el('button', { className: 'ck-btn ck-btn-primary ck-lock-answered-open', type: 'button' },
        ['Lock all ' + ready.length + ' answers…']);
      open.addEventListener('click', () => {
        delete lockAllError[itemId];
        openForms.add(key);
        renderPanel();
        const h = panelEl.querySelector('.ck-lock-answered .ck-confirm-heading');
        if (h) { h.setAttribute('tabindex', '-1'); h.focus(); }
      });
      wrap.appendChild(open);
      return wrap;
    }
    const box = el('div', { className: 'ck-confirm' }, [
      el('div', { className: 'ck-confirm-heading' }, ['Lock these ' + ready.length + ' answers?'])
    ]);
    for (const q of ready) {
      box.appendChild(el('div', { className: 'ck-lock-answered-qid' }, [q.question.qid]));
      box.appendChild(renderReceipt(q.question, q.answers[q.answers.length - 1]));
    }
    const actions = el('div', { className: 'ck-actions', style: 'margin-top: 12px;' });
    const go = el('button', { className: 'ck-btn ck-btn-primary ck-lock-answered-go', type: 'button' },
      ['Lock all ' + ready.length + ' answers']);
    const cancel = el('button', { className: 'ck-btn', type: 'button' }, ['Cancel']);
    cancel.addEventListener('click', () => {
      openForms.delete(key);
      renderPanel();
      const again = panelEl.querySelector('.ck-lock-answered-open');
      if (again) again.focus();
    });
    go.addEventListener('click', async () => {
      // The answers shown are the ones locked: taken now, not re-read after each reply.
      const todo = ready.map(q => ({ qid: q.question.qid, answer: q.answers[q.answers.length - 1].id }));
      lockingAll = itemId;
      openForms.delete(key);
      renderPanel();
      let done = 0;
      let failed = null;
      for (const t of todo) {
        const r = await apiPost('/lock', { qid: t.qid, answer: t.answer }, 'lock-' + t.qid);
        if (r.error) { failed = t.qid + ' was not locked: ' + r.error; break; }
        done += 1;
      }
      lockingAll = null;
      if (failed) {
        lockAllError[itemId] = 'Locked ' + done + ' of ' + todo.length + '. ' + failed;
        announce(lockAllError[itemId]);
      } else {
        announce('Locked ' + done + ' answers.');
      }
      renderPanel();
      // The owner may have moved on while the run was in flight: only then leave focus alone.
      if (currentMode !== 'item' || currentItem !== itemId) return;
      // The button that was pressed is gone: land on what the run left behind.
      const land = panelEl.querySelector('.ck-lock-answered-error') || panelEl.querySelector('.ck-questions-heading');
      if (land) { if (!land.hasAttribute('tabindex')) land.setAttribute('tabindex', '-1'); land.focus(); }
    });
    actions.appendChild(go);
    actions.appendChild(cancel);
    box.appendChild(actions);
    wrap.appendChild(box);
    return wrap;
  }

  // Render status bar
  function renderStatusBar(itemId) {
    const bar = el('div', { className: 'ck-status-bar' });
    if (view && itemId && view.items[itemId]) {
      const t = view.items[itemId].own;
      const counts = el('div', { className: 'ck-counts' });
      if (t.awaiting_you) counts.appendChild(el('span', {}, [GLYPH.awaiting_you + ' ' + t.awaiting_you + ' awaiting you']));
      if (t.awaiting_agent) counts.appendChild(el('span', {}, [GLYPH.awaiting_agent + ' ' + t.awaiting_agent + ' ' + agentWords(itemId)]));
      if (t.unlocked) counts.appendChild(el('span', {}, [GLYPH.unlocked + ' ' + t.unlocked + ' unlocked']));
      if (t.stale) counts.appendChild(el('span', {}, [GLYPH.stale + ' ' + t.stale + ' stale']));
      bar.appendChild(counts);
    }
    // Sync time
    const syncText = cursor ? relTime(cursor.last_synced_at) : 'never';
    const syncEl = el('span', { title: cursor && cursor.last_synced_at ? new Date(cursor.last_synced_at).toLocaleString() : '' },
      ['Synced: ' + syncText]);
    bar.appendChild(syncEl);
    bar.appendChild(renderListening());
    // Refresh button
    const refreshBtn = el('button', { className: 'ck-refresh-btn', type: 'button' }, ['Refresh']);
    refreshBtn.addEventListener('click', async () => {
      refreshBtn.disabled = true;
      await fetchView();
      renderPanel();
      refreshBtn.disabled = false;
    });
    bar.appendChild(refreshBtn);
    // Every answer the console holds, from anywhere (§7.6). A toggle (owner,
    // 2026-09-29: "no way to get back to inbox"): on the sheet it reads as the
    // way back, because the header's Back button shows only on narrow screens.
    const onSheet = currentMode === 'sheet';
    const allBtn = el('button', {
      className: 'ck-refresh-btn ck-sheet-toggle', type: 'button', 'aria-pressed': onSheet ? 'true' : 'false'
    }, [onSheet ? (itemId ? 'Back to ' + itemId : 'Inbox') : 'All answers']);
    allBtn.addEventListener('click', () => {
      if (!onSheet) return showSheet(null, null);
      currentFork = null;
      currentMode = itemId ? 'item' : 'inbox';
      if (!itemId) fromInbox = false;
      renderPanel();
      const h = panelEl.querySelector('.ck-title');
      if (h) { h.setAttribute('tabindex', '-1'); h.focus(); }
    });
    bar.appendChild(allBtn);
    // Error
    if (cursor && cursor.last_error) {
      bar.appendChild(el('div', { className: 'ck-error-msg' }, [cursor.last_error]));
    }
    return bar;
  }

  // "Agent listening" (0.6.0): whether a session is waiting on the doorbell right
  // now, from the watch's own heartbeat (`cursor.listening`, server-judged). Words
  // and a glyph carry the state, never colour alone; an old server that sends no
  // `listening` reads as "not known", never as listening.
  function listeningWords(l) {
    if (!l || !l.state) return { state: 'unknown', glyph: '?', text: 'Agent: not known' };
    if (l.state === 'listening') return { state: 'listening', glyph: '●', text: 'Agent listening' };
    if (l.state === 'idle' && l.last_seen) {
      const d = new Date(l.last_seen);
      const sameDay = d.toDateString() === new Date().toDateString();
      const when = sameDay ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : d.toLocaleString();
      return { state: 'idle', glyph: '○', text: 'Agent idle since ' + when };
    }
    return { state: 'never', glyph: '○', text: 'No agent has listened yet' };
  }
  function renderListening() {
    const l = cursor ? cursor.listening : null;
    const w = listeningWords(l);
    const title = w.state === 'listening'
      ? 'A session is waiting on the doorbell: "Answers are in" wakes it now.'
      : w.state === 'idle'
        ? 'No session is waiting on the doorbell. Last seen ' + relTime(l.last_seen) + '; a request waits until one starts.'
        : 'No session has waited on the doorbell yet; a request waits until one starts.';
    return el('span', { className: 'ck-listening', dataState: w.state, title: title }, [
      el('span', { className: 'ck-listening-glyph', 'aria-hidden': 'true' }, [w.glyph]), ' ' + w.text
    ]);
  }

  // Render offline state
  function renderOffline() {
    return el('div', { className: 'ck-offline' }, [
      el('div', { className: 'ck-offline-icon' }, ['⚠']),
      el('div', { className: 'ck-offline-text' }, [
        cursor && cursor.last_error ? cursor.last_error :
        "Can't reach the console server — the machine may be asleep. Nothing you typed was lost."
      ])
    ]);
  }

  // Render a question card
  // CONSOLE-kit/Q37 (owner, 2026-10-02, the ★): a settled question is one line, qid · pick · state, and opens
  // on click. Open and stale questions stay whole: they are the ones that still ask something of the owner.
  const ROLLED_STATES = ['locked', 'withdrawn', 'superseded'];

  function pickWords(qData, answer) {
    if (!answer) return 'no answer';
    const labels = optionLabels(qData);
    const picks = (answer.picks || []).map(p => labels[p] || p);
    return picks.length ? picks.join(', ') : (answer.own_text ? 'your own words' : 'no pick');
  }

  function rollLine(q, open) {
    const qData = q.question;
    const head = q.answers && q.answers.length ? q.answers[q.answers.length - 1] : null;
    const line = el('button', { className: 'ck-q-line', type: 'button', dataQid: qData.qid,
      dataFocusKey: 'line-' + qData.qid, 'aria-expanded': open ? 'true' : 'false',
      'aria-label': qData.qid + ', ' + (STATE_WORDS[q.state] || q.state) + ': ' + pickWords(qData, head) +
        '. ' + truncateText(qData.text, 60) + (open ? ' Collapse.' : ' Expand.') }, [
      el('span', { className: 'ck-q-line-caret', 'aria-hidden': 'true' }, [open ? '▾' : '▸']),
      el('span', { className: 'ck-inbox-item-id' }, [qData.qid]),
      el('span', { className: 'ck-q-line-pick' }, [pickWords(qData, head)]),
      el('span', { className: 'ck-q-state', dataState: q.state }, [(GLYPH[q.state] || '') + ' ' +
        (STATE_WORDS[q.state] || q.state)])
    ]);
    line.addEventListener('click', () => {
      if (open) openForms.delete('expand-' + qData.qid); else openForms.add('expand-' + qData.qid);
      redrawKeepingPlace();
    });
    return line;
  }

  function renderQuestion(q) {
    const qData = q.question;
    const state = q.state;
    const rolled = ROLLED_STATES.includes(state);
    const open = rolled && openForms.has('expand-' + qData.qid);
    if (rolled && !open) {
      const card = el('div', { className: 'ck-question ck-question-rolled', dataQid: qData.qid }, [rollLine(q, false)]);
      if (justLocked.delete(qData.qid)) card.classList.add('ck-rolling');   // the motion runs once, on the lock
      return card;
    }
    const card = el('div', { className: 'ck-question', dataQid: qData.qid });
    if (open) card.appendChild(rollLine(q, true));

    // Header
    const hdr = el('div', { className: 'ck-q-header' }, [
      el('span', { className: 'ck-q-state', dataState: state }, [
        GLYPH[state] || '', ' ', state.replace('_', ' ')
      ])
    ]);
    const textCol = el('div', { className: 'ck-q-textcol' });
    const textEl = el('div', { className: 'ck-q-text' });
    textEl.textContent = qData.text;
    textCol.appendChild(textEl);
    if (qData.source) {
      const srcEl = el('div', { className: 'ck-q-source' });
      srcEl.textContent = qData.source;
      textCol.appendChild(srcEl);
    }
    if (qData.agent) textCol.appendChild(el('div', { className: 'ck-q-agent' }, ['asked by ' + qData.agent]));
    const chips = tagChips(questionTags(qData.qid));
    if (chips) textCol.appendChild(chips);
    const direction = renderDirectionStrip(q);
    if (direction) textCol.appendChild(direction);
    hdr.appendChild(textCol);
    card.appendChild(hdr);

    const bodyEl = el('div', { className: 'ck-q-body' });

    // Stale banner
    if (state === 'stale' && q.failing && q.failing.length > 0) {
      const banner = el('div', { className: 'ck-stale-banner' }, [
        el('strong', {}, ['Assumptions changed since you locked this'])
      ]);
      const list = el('ul', { className: 'ck-stale-list' });
      for (const c of q.failing) {
        let msg = '';
        if (c.kind === 'file_sha256') msg = 'File ' + c.path + ' changed';
        else if (c.kind === 'excerpt') msg = 'The text cited from ' + c.path + ' changed or is gone';
        else if (c.kind === 'item_status') msg = c.item + ' is no longer ' + c.status;
        list.appendChild(el('li', {}, [msg]));
      }
      banner.appendChild(list);
      // 0.5.0: say which condition failed and why, and let the owner re-lock
      // the answer as it stands once they have checked the change.
      const tools = el('div', { className: 'ck-actions' });
      const [whyBtn, whySlot] = disclosure('Why stale?', 'why-' + qData.qid, () => renderWhyStale(qData.qid));
      whyBtn.setAttribute('aria-label', 'Why is this stale: ' + truncateText(qData.text, 40));
      const [reBtn, reSlot] = disclosure('Still holds: re-lock…', 'relock-' + qData.qid, () => renderRelock(q));
      reBtn.setAttribute('aria-label', 'Re-lock this answer as it stands: ' + truncateText(qData.text, 40));
      tools.appendChild(whyBtn);
      tools.appendChild(reBtn);
      // CONSOLE-kit/Q30: the owner judges each stale ruling; neither act is a default.
      const [wdBtn, wdSlot] = disclosure('Withdraw…', 'withdraw-' + qData.qid, () => renderSettle(q, 'withdraw'));
      wdBtn.setAttribute('aria-label', 'Withdraw this ruling: ' + truncateText(qData.text, 40));
      const [kpBtn, kpSlot] = disclosure('Keep, stop checking…', 'untrack-' + qData.qid,
        () => renderSettle(q, 'untrack'));
      kpBtn.setAttribute('aria-label', 'Keep this ruling and stop checking it: ' + truncateText(qData.text, 40));
      tools.appendChild(wdBtn);
      tools.appendChild(kpBtn);
      tools.appendChild(scanControl(q));
      banner.appendChild(tools);
      banner.appendChild(whySlot);
      banner.appendChild(reSlot);
      banner.appendChild(wdSlot);
      banner.appendChild(kpSlot);
      if (q.refactor && q.refactor.advice) banner.appendChild(renderAdvice(q));
      if (q.refactor && q.refactor.proposal) banner.appendChild(renderProposal(q));
      bodyEl.appendChild(banner);
    }
    const rxNote = refactorNote(q);
    if (rxNote) bodyEl.appendChild(rxNote);

    // Show receipt for locked (or stale as locked)
    const headAnswer = q.answers && q.answers.length > 0 ? q.answers[q.answers.length - 1] : null;
    const isLocked = headAnswer && headAnswer.locked;

    if (state === 'locked' || state === 'stale') {
      // Show receipt
      bodyEl.appendChild(renderReceipt(qData, headAnswer));
      // Actions — fix #5: unique aria-label
      const actions = el('div', { className: 'ck-actions' });
      const truncText = truncateText(qData.text, 40);
      const changeBtn = el('button', {
        className: 'ck-btn',
        type: 'button',
        'aria-label': 'Change my answer: ' + truncText
      }, ['Change my answer']);
      changeBtn.addEventListener('click', () => {
        bodyEl.textContent = '';
        bodyEl.appendChild(renderAnswerForm(q, true, headAnswer.id));
      });
      actions.appendChild(changeBtn);
      // "Next step ▾" (0.8.0): follow up with seats (0.4.0; roar among them), refine or drill.
      const target = { item: qData.item, about_qid: qData.qid };
      const [nextBtn, nextPanel] = nextStepMenu('next-' + qData.qid, truncText, [
        { id: 'follow', key: 'ask-' + qData.qid, label: '⑂ Follow up…',
          aria: 'Follow up with other seats on: ' + truncText, build: () => renderAnswerFollowUp(q) },
        { id: 'refine', key: 'refine-' + qData.qid, label: 'Refine…', aria: 'Refine from: ' + truncText,
          build: () => renderStepForm(target, 'refine', 'refine-' + qData.qid) },
        { id: 'drill', key: 'drill-' + qData.qid, label: 'Drill…', aria: 'Drill from: ' + truncText,
          build: () => renderStepForm(target, 'drill', 'drill-' + qData.qid) }
      ]);
      actions.appendChild(nextBtn);
      bodyEl.appendChild(actions);
      bodyEl.appendChild(nextPanel);
      const asked = askedLine(qData.qid);
      if (asked) bodyEl.appendChild(asked);
    } else if (state === 'unlocked') {
      // One tap locks: the Undo countdown was removed; the request goes immediately.
      bodyEl.appendChild(renderReceipt(qData, headAnswer));
      if (lockErrors[qData.qid]) {
        bodyEl.appendChild(el('p', { className: 'ck-error-msg ck-lock-error' }, ['Not locked: ' + lockErrors[qData.qid]]));
      }
      const actions = el('div', { className: 'ck-actions' });
      const truncText = truncateText(qData.text, 40);
      {
        const lockBtn = el('button', {
          className: 'ck-btn ck-btn-primary ck-lock-one',
          type: 'button',
          dataFocusKey: 'lock-' + qData.qid,
          'aria-label': 'Lock this answer: ' + truncText
        }, ['Lock this answer']);
        lockBtn.addEventListener('click', () => startLock(q, headAnswer));
        actions.appendChild(lockBtn);
        const changeBtn = el('button', {
          className: 'ck-btn',
          type: 'button',
          'aria-label': 'Re-answer before locking: ' + truncText
        }, ['Re-answer before locking']);
        changeBtn.addEventListener('click', () => {
          bodyEl.textContent = '';
          bodyEl.appendChild(renderAnswerForm(q, false, null));
        });
        actions.appendChild(changeBtn);
      }
      bodyEl.appendChild(actions);
      bodyEl.appendChild(deliberateOpen(q));
    } else {
      // awaiting_you: show answer form
      bodyEl.appendChild(renderAnswerForm(q, false, null));
      bodyEl.appendChild(deliberateOpen(q));
    }

    // Answer history
    if (q.answers && q.answers.length > 1) {
      const hist = el('div', { className: 'ck-history' }, [
        el('div', { className: 'ck-history-heading' }, ['Previous answers'])
      ]);
      for (let i = q.answers.length - 2; i >= 0; i--) {
        const a = q.answers[i];
        const itm = el('div', { className: 'ck-history-item', dataSuperseded: 'true' });
        let txt = 'Picks: ' + (a.picks && a.picks.length ? a.picks.join(', ') : 'none');
        if (a.own_text) txt += ' | Own words: ' + a.own_text.substring(0, 50) + (a.own_text.length > 50 ? '...' : '');
        itm.textContent = txt;
        if (a.locked) itm.appendChild(el('span', { className: 'ck-history-locked' }, ['locked']));
        hist.appendChild(itm);
      }
      bodyEl.appendChild(hist);
    }

    // A question a roar panel wrote carries that panel's transcript, collapsed (0.8.0).
    const tr = qData.forked_from ? transcriptBlock(qData.forked_from) : null;
    if (tr) bodyEl.appendChild(tr);

    card.appendChild(bodyEl);
    return card;
  }

  // ---------------------------------------------------------------------------
  // 0.8.0: suggested next steps, the Next step menu, roar transcripts, visuals.
  // Everything from the store reaches the page through textContent (el() makes
  // text nodes) or, for an HTML mock, only through <iframe sandbox="">.
  // ---------------------------------------------------------------------------

  // The server's suggested next steps (tags.py): data with a reason, never worked out here.
  function questionTags(qid) {
    return (view && view.tags && view.tags.questions && view.tags.questions[qid]) || [];
  }
  function forkTags(fid) {
    return (view && view.tags && view.tags.forks && view.tags.forks[fid]) || [];
  }
  // A small chip per tag. Its visible word is the step; its accessible text is the step
  // AND the reason, so a screen reader hears why, and a pointer's tooltip shows the reason.
  function tagChips(tags) {
    if (!tags || !tags.length) return null;
    const row = el('div', { className: 'ck-tags' });
    for (const t of tags) {
      const chip = el('span', { className: 'ck-tag', dataStep: String(t.step), title: String(t.reason) });
      chip.appendChild(el('span', { 'aria-hidden': 'true' }, ['→ ' + t.step]));
      chip.appendChild(el('span', { className: 'ck-sr-only' }, ['Suggested next step, ' + t.step + ': ' + t.reason]));
      row.appendChild(chip);
    }
    return row;
  }

  // "Next step ▾": one button that opens the three kinds of next step, each its own form.
  // Open state is remembered by key, like every disclosure, so a live redraw keeps it.
  function nextStepMenu(key, forText, choices) {
    const btn = el('button', { className: 'ck-btn ck-next-btn', type: 'button', 'aria-expanded': 'false',
      'aria-label': 'Next step for: ' + forText }, ['Next step ▾']);
    const panel = el('div', { className: 'ck-next-panel' });
    const show = open => {
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      btn.textContent = open ? 'Next step ▴' : 'Next step ▾';
      panel.textContent = '';
      if (!open) return;
      const row = el('div', { className: 'ck-actions ck-next-menu', role: 'group', 'aria-label': 'Next step: pick one' });
      const slots = [];
      for (const c of choices) {
        const [b, s] = disclosure(c.label, c.key, c.build);
        b.setAttribute('aria-label', c.aria);
        b.setAttribute('data-step', c.id);
        row.appendChild(b);
        slots.push(s);
      }
      panel.appendChild(row);
      for (const s of slots) panel.appendChild(s);
    };
    btn.addEventListener('click', () => {
      const open = !openForms.has(key);
      if (open) openForms.add(key); else openForms.delete(key);
      show(open);
      if (open) { const first = panel.querySelector('button'); if (first) first.focus(); }
    });
    if (openForms.has(key)) show(true);
    return [btn, panel];
  }

  // A refine or drill (0.8.0): one owner fork message with `step`, on one locked answer
  // (about_qid) or one round's answers (follow_up_of). The session runs the project's own
  // skill for it; nothing is written into the project before you lock what comes back.
  function renderStepForm(target, step, key) {
    const w = STEP_WORDS[step];
    const id = key.replace(/[^A-Za-z0-9_-]/g, '-');
    const scope = target.about_qid ? 'your locked answer to ' + target.about_qid : 'this round\'s answers';
    const form = el('div', { className: 'ck-fork-form ck-step-form', dataStep: step, role: 'group',
      'aria-labelledby': id + '-h' });
    form.appendChild(el('div', { className: 'ck-confirm-heading', id: id + '-h' }, [w.title + ' from ' + scope]));
    form.appendChild(el('p', { className: 'ck-muted' }, [
      'The agent runs the project\'s ' + step + ' skill on ' + scope + ', to ' + w.what + '. It writes nothing ' +
      'before you lock: what it finds comes back here as questions, and a spec lands by pull request.']));
    const noteId = id + '-note';
    const text = el('textarea', { className: 'ck-textarea', rows: '2', id: noteId,
      placeholder: step === 'refine' ? 'e.g. The spec still says 16 columns' : 'e.g. What does a zone own?' });
    if (draftTexts[key] !== undefined) text.value = draftTexts[key];
    text.addEventListener('input', () => { draftTexts[key] = text.value; });
    form.appendChild(el('label', { for: noteId, className: 'ck-field-label' }, ['Note (optional)']));
    form.appendChild(text);
    const err = el('p', { className: 'ck-error-msg', role: 'status', 'aria-live': 'polite' });
    form.appendChild(err);
    const send = el('button', { className: 'ck-btn ck-btn-primary', type: 'button' }, ['Start the ' + step]);
    const cancel = el('button', { className: 'ck-btn', type: 'button' }, ['Cancel']);
    cancel.addEventListener('click', () => { openForms.delete(key); renderPanel(); });
    send.addEventListener('click', async () => {
      err.textContent = '';
      const body = { item: target.item, intent: 'fork', mode: w.mode, step: step,
        text: text.value.trim() || (w.title + ' from ' + scope + '.') };
      if (target.about_qid) body.about_qid = target.about_qid;
      else body.follow_up_of = target.follow_up_of;
      send.disabled = true;
      const result = await apiPost('/message', body, key);
      send.disabled = false;
      if (result.error) {
        err.textContent = 'Not sent: ' + result.error;
        announce('Error: ' + result.error);
      } else {
        delete draftTexts[key];
        openForms.delete(key);
        announce(w.title + ' requested.');
        renderPanel();
      }
    });
    form.appendChild(el('div', { className: 'ck-actions' }, [send, cancel]));
    return form;
  }

  // A roar panel's transcript, collapsed; its text only ever as text.
  function transcriptBlock(forkId) {
    const t = view && view.transcripts ? view.transcripts[forkId] : null;
    if (!t) return null;
    const kb = Math.max(1, Math.round(t.bytes / 1024));
    const det = el('details', { className: 'ck-transcript' }, [
      el('summary', {}, ['Roar transcript · three rounds · ' + kb + ' KB · ' + relTime(t.ts) +
        (t.agent ? ' · by ' + t.agent : '')])]);
    const pre = el('pre', { className: 'ck-transcript-text' });
    pre.textContent = t.text;
    det.appendChild(pre);
    return det;
  }

  // "Request a visual" (0.8.0): one owner message, intent 'visual', on the item.
  function renderVisualForm(itemId) {
    const key = 'vis-' + itemId;
    const id = key.replace(/[^A-Za-z0-9_-]/g, '-');
    const form = el('div', { className: 'ck-fork-form ck-visual-form', role: 'group', 'aria-labelledby': id + '-h' });
    form.appendChild(el('div', { className: 'ck-confirm-heading', id: id + '-h' }, ['Request a visual of ' + itemId]));
    const where = view.config && view.config.visuals_dir;
    // 0.8.1: the console stores the visual itself; visuals_dir is only where a PR lands it.
    form.appendChild(el('p', { className: 'ck-muted' }, [
      'An agent answers with a Mermaid diagram or a static HTML mock and a short doc. The console keeps it and ' +
      'shows it here, a mock only inside a sandbox that runs no script. ' + (where
        ? 'An agent can land it in the repository under ' + where + '/ by a pull request.'
        : 'This project sets no visuals_dir in .console-kit.json, so it stays in the console and is not landed ' +
          'in the repository.')]));
    const noteId = id + '-note';
    const text = el('textarea', { className: 'ck-textarea', rows: '3', id: noteId,
      placeholder: 'e.g. The grid page at phone width, with the session block open' });
    if (draftTexts[key] !== undefined) text.value = draftTexts[key];
    text.addEventListener('input', () => { draftTexts[key] = text.value; });
    form.appendChild(el('label', { for: noteId, className: 'ck-field-label' }, ['What should it show?']));
    form.appendChild(text);
    const err = el('p', { className: 'ck-error-msg', role: 'status', 'aria-live': 'polite' });
    form.appendChild(err);
    const send = el('button', { className: 'ck-btn ck-btn-primary', type: 'button' }, ['Request the visual']);
    send.addEventListener('click', async () => {
      err.textContent = '';
      if (!text.value.trim()) { err.textContent = 'Say what the visual should show.'; announce(err.textContent); return; }
      send.disabled = true;
      const result = await apiPost('/message', { item: itemId, intent: 'visual', text: text.value.trim() }, key);
      send.disabled = false;
      if (result.error) { err.textContent = 'Not sent: ' + result.error; announce('Error: ' + result.error); }
      else { delete draftTexts[key]; openForms.delete(key); announce('Visual requested.'); renderPanel(); }
    });
    form.appendChild(el('div', { className: 'ck-actions' }, [send]));
    return form;
  }

  // 0.8.19: a server-generated Mermaid flowchart of the item's questions and rounds.
  // Collapsed by default: expanding sets the iframe src, so a page that never opens it costs nothing.
  // Re-rendered under the live loop keeps the diagram fresh when a question is answered or added.
  const openCharts = new Set();      // which items currently show the flowchart; survives re-renders
  // Dashboard section chips for an item, driven by `.console-kit.json`'s `sections` map. Returns null when the
  // item has none mapped. The link uses target="_top" to break out of the console panel's docked frame.
  function renderSectionLinks(itemId) {
    const cfg = view && view.config;
    const list = (cfg && cfg.sections && cfg.sections[itemId]) || [];
    if (!list.length) return null;
    const wrap = el('div', { className: 'ck-section-links', 'aria-label': 'This item in the dashboard' });
    wrap.appendChild(el('span', { className: 'ck-section-links-label' }, ['Dashboard:']));
    for (const anchor of list) {
      const label = anchor.replace(/^#/, '');
      const a = el('a', { className: 'ck-section-link', href: anchor, target: '_top',
        rel: 'noopener', 'aria-label': 'Jump to ' + label + ' on the dashboard' }, [
        el('span', {}, [label]),
        el('span', { 'aria-hidden': 'true' }, [' →'])
      ]);
      wrap.appendChild(a);
    }
    return wrap;
  }

  // "Direction" strip above a question's text: the fork it was asked by, what it supersedes or is superseded
  // by, and the files its evidence cites. Pure CSS/JS over the view JSON; no new endpoints.
  function renderDirectionStrip(q) {
    const qd = q.question || {};
    const chips = [];
    if (qd.forked_from) {
      chips.push({ icon: '↑', label: 'from round ' + qd.forked_from.slice(0, 8), qid: null, fork: qd.forked_from });
    }
    const supersedes = qd.supersedes || qd.replaces;
    if (supersedes) {
      chips.push({ icon: '←', label: 'supersedes ' + shortQid(supersedes), qid: supersedes, fork: null });
    }
    // Reverse direction: did a newer question supersede this one?
    for (const other of Object.values(view.questions || {})) {
      const od = other.question || {};
      if ((od.supersedes || od.replaces) === qd.qid) {
        chips.push({ icon: '→', label: 'superseded by ' + shortQid(od.qid), qid: od.qid, fork: null });
      }
    }
    const ev = Array.isArray(qd.evidence) ? qd.evidence : [];
    const cited = new Set();
    for (const row of ev) {
      const p = row && row.cite ? String(row.cite).split(':')[0] : null;
      if (p && !cited.has(p)) {
        cited.add(p);
        chips.push({ icon: '↳', label: p, qid: null, fork: null });
      }
    }
    if (!chips.length) return null;
    const wrap = el('div', { className: 'ck-direction', 'aria-label': 'Related questions and sources' });
    for (const c of chips) {
      const chip = el('span', { className: 'ck-direction-chip' }, [
        el('span', { className: 'ck-direction-icon', 'aria-hidden': 'true' }, [c.icon + ' ']),
        c.label
      ]);
      if (c.qid) {
        chip.classList.add('ck-direction-link');
        chip.setAttribute('role', 'button');
        chip.setAttribute('tabindex', '0');
        const go = () => {
          const sel = '[data-qid="' + cssEscape(c.qid) + '"]';
          const node = panelEl.querySelector('.ck-question' + sel) || panelEl.querySelector(sel);
          if (node) { node.scrollIntoView({ behavior: 'smooth', block: 'center' }); pulse(node); }
        };
        chip.addEventListener('click', go);
        chip.addEventListener('keydown', e => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
        });
      }
      wrap.appendChild(chip);
    }
    return wrap;
  }

  function shortQid(qid) {
    if (typeof qid !== 'string') return String(qid || '');
    return qid.length > 24 ? qid.slice(0, 24) + '…' : qid;
  }

  // Delegate: a one-stop entry point so the owner can start a round, request a visual, or post a chat
  // without first opening an item. Reuses existing /api/message endpoints — no new server routes.
  // Posting is handled client-side; the collapsed <details> state persists across live wakes via a module var.
  let delegateOpen = false;
  function renderDelegateBar() {
    const wrap = el('details', { className: 'ck-delegate-bar' });
    if (delegateOpen) wrap.setAttribute('open', '');
    const sum = el('summary', { className: 'ck-delegate-summary' }, [
      el('span', { className: 'ck-delegate-icon', 'aria-hidden': 'true' }, ['⚑ ']),
      el('span', {}, ['Delegate']),
      el('span', { className: 'ck-muted ck-delegate-hint' }, [' — start a round, request a visual, or chat'])
    ]);
    wrap.appendChild(sum);
    wrap.addEventListener('toggle', () => { delegateOpen = wrap.open; });

    const body = el('div', { className: 'ck-delegate-body' });

    // Row 1: pick what to do.
    const kindFs = el('fieldset', { className: 'ck-roster', 'aria-label': 'What to delegate' },
      [el('legend', {}, ['What'])]);
    const kinds = [
      ['round', 'Deliberate (full round)'],
      ['visual', 'Request a visual'],
      ['chat', 'Chat with the agent'],
      ['playbook', 'Run a playbook']
    ];
    kinds.forEach(([v, label], i) => {
      const r = el('input', { type: 'radio', name: 'ck-del-kind', value: v });
      if (i === 0) r.checked = true;
      kindFs.appendChild(el('label', { className: 'ck-option' }, [r, ' ' + label]));
    });
    body.appendChild(kindFs);

    // Row 2: pick an item (except for chat, which goes to @chat).
    const itemSel = el('select', { className: 'ck-select', id: 'ck-del-item' });
    for (const id of (items ? Object.keys(items).sort() : [])) {
      const data = items[id] || {};
      itemSel.appendChild(el('option', { value: id }, [id + (data.title ? ' — ' + truncateText(data.title, 60) : '')]));
    }
    body.appendChild(el('div', { className: 'ck-field ck-del-itemrow' }, [
      el('label', { for: 'ck-del-item', className: 'ck-field-label' }, ['On item']),
      itemSel
    ]));

    // Row 3: round-only — mode + focus.
    const modeSel = el('select', { className: 'ck-select', id: 'ck-del-mode' });
    for (const m of ['explore', 'tighten']) modeSel.appendChild(el('option', { value: m }, [m]));
    const focusSel = el('select', { className: 'ck-select', id: 'ck-del-focus' });
    for (const f of FOCUSES) focusSel.appendChild(el('option', { value: f }, [f === 'whole' ? 'the whole thing' : f]));
    const roundRow = el('div', { className: 'ck-field ck-del-roundrow' }, [
      el('label', { for: 'ck-del-mode', className: 'ck-field-label' }, ['Mode']), modeSel,
      el('label', { for: 'ck-del-focus', className: 'ck-field-label' }, ['Focus']), focusSel
    ]);
    body.appendChild(roundRow);

    // Row 3b: playbook picker (shown only when kind=playbook).
    const playbookSel = el('select', { className: 'ck-select', id: 'ck-del-playbook' });
    const playbooks = (view && view.playbooks) || [];
    for (const pb of playbooks) {
      const descr = pb.description ? ' — ' + truncateText(pb.description, 50) : '';
      playbookSel.appendChild(el('option', { value: pb.name }, [pb.name + descr]));
    }
    const playbookRow = el('div', { className: 'ck-field ck-del-playbookrow' }, [
      el('label', { for: 'ck-del-playbook', className: 'ck-field-label' }, ['Playbook']),
      playbookSel
    ]);
    if (!playbooks.length) {
      playbookRow.appendChild(el('span', { className: 'ck-muted' },
        [' (no playbooks under .console-kit/playbooks/*.json)']));
    }
    body.appendChild(playbookRow);

    // Row 4: the owner's brief.
    const text = el('textarea', { className: 'ck-textarea', rows: '2',
      placeholder: 'What should the agent look at? (optional for a round; required for a visual)' });
    body.appendChild(text);

    // Row 5: send + status.
    const status = el('div', { className: 'ck-delegate-status', role: 'alert', hidden: 'hidden' });
    const send = el('button', { className: 'ck-btn ck-btn-primary', type: 'button' }, ['Delegate']);
    const updateVisibility = () => {
      const kind = body.querySelector('input[name="ck-del-kind"]:checked').value;
      roundRow.hidden = kind !== 'round';
      playbookRow.hidden = kind !== 'playbook';
      itemSel.disabled = kind === 'chat' || kind === 'playbook';
      text.hidden = kind === 'playbook';
      send.textContent = kind === 'playbook' ? 'Run playbook' : 'Delegate';
    };
    body.querySelectorAll('input[name="ck-del-kind"]').forEach(r =>
      r.addEventListener('change', updateVisibility));
    updateVisibility();

    send.addEventListener('click', async () => {
      status.hidden = true; status.textContent = '';
      const kind = body.querySelector('input[name="ck-del-kind"]:checked').value;
      const brief = text.value.trim();
      send.disabled = true;
      let result;
      if (kind === 'playbook') {
        if (!playbookSel.value) {
          send.disabled = false;
          status.textContent = 'Add a JSON file under .console-kit/playbooks/ to run one.'; status.hidden = false; return;
        }
        result = await apiPost('/playbook', { name: playbookSel.value }, 'playbook-' + playbookSel.value + '-' + Date.now());
      } else {
        let payload;
        if (kind === 'round') {
          payload = { item: itemSel.value, intent: 'fork', mode: modeSel.value, focus: focusSel.value,
            text: brief || ('Deliberate the full round: ' + itemSel.value + ' and everything under it.') };
        } else if (kind === 'visual') {
          if (!brief) { send.disabled = false; status.textContent = 'A visual needs a brief: what should it show?'; status.hidden = false; return; }
          payload = { item: itemSel.value, intent: 'visual', text: brief };
        } else {
          if (!brief) { send.disabled = false; status.textContent = 'Say what you want the agent to do.'; status.hidden = false; return; }
          payload = { item: '@chat', intent: 'chat', text: brief };
        }
        result = await apiPost('/message', payload, 'delegate-' + Date.now());
      }
      send.disabled = false;
      if (result && result.error) {
        status.textContent = 'Not delegated: ' + result.error;
        status.hidden = false;
      } else if (result && Array.isArray(result.skipped) && result.skipped.length) {
        const first = result.skipped[0];
        status.textContent = (result.records || []).length + ' step(s) ran; '
          + result.skipped.length + ' skipped (step ' + first.index + ': ' + first.why + ').';
        status.hidden = false;
      } else {
        text.value = '';
        announce(kind === 'playbook' ? 'Playbook ran.' : 'Delegated.');
      }
    });
    body.appendChild(el('div', { className: 'ck-actions' }, [send, status]));

    wrap.appendChild(body);
    return wrap;
  }

  function renderItemChart(itemId) {
    const wrap = el('details', { className: 'ck-item-chart', dataItem: itemId });
    if (openCharts.has(itemId)) wrap.setAttribute('open', '');
    const sum = el('summary', { className: 'ck-item-chart-summary' }, [
      el('span', { className: 'ck-item-chart-title' }, ['Status flowchart']),
      el('span', { className: 'ck-muted ck-item-chart-hint' }, [' — questions, rounds and how they relate'])
    ]);
    wrap.appendChild(sum);
    const slot = el('div', { className: 'ck-item-chart-slot' });
    wrap.appendChild(slot);
    const load = () => {
      if (slot.querySelector('iframe')) return;
      const frame = document.createElement('iframe');
      frame.setAttribute('sandbox', 'allow-scripts');
      frame.setAttribute('referrerpolicy', 'no-referrer');
      frame.setAttribute('title', 'Status flowchart: ' + itemId);
      frame.className = 'ck-visual-frame ck-visual-frame-mermaid ck-item-chart-frame';
      frame.src = config.api + '/item-chart?item=' + encodeURIComponent(itemId);
      slot.appendChild(frame);
    };
    wrap.addEventListener('toggle', () => {
      if (wrap.open) { openCharts.add(itemId); load(); }
      else { openCharts.delete(itemId); }
    });
    if (openCharts.has(itemId)) load();
    return wrap;
  }

  // Trigger log (0.16.0): the Inbox shows configured triggers + the last N firings so webhook / cron are
  // not opaque. The log is bounded in-process (100 entries); older firings drop off on server restart.
  let triggerLogOpen = false;
  function renderTriggerLog(triggers, log) {
    const wrap = el('details', { className: 'ck-item-chart ck-triggers' });
    if (triggerLogOpen) wrap.setAttribute('open', '');
    const kinds = (triggers || []).map(t => t.kinds && t.kinds.length
      ? t.name + ' (' + t.kinds.join('+') + ')'
      : t.name);
    const hint = (triggers && triggers.length)
      ? triggers.length + ' trigger' + (triggers.length === 1 ? '' : 's') +
        (log.length ? '; ' + log.length + ' recent firing' + (log.length === 1 ? '' : 's') : '; no firings yet')
      : (log.length ? log.length + ' recent firing' + (log.length === 1 ? '' : 's') : '');
    const sum = el('summary', { className: 'ck-item-chart-summary' }, [
      el('span', { className: 'ck-item-chart-title' }, ['Triggers']),
      el('span', { className: 'ck-muted ck-item-chart-hint' }, [' — ' + hint])
    ]);
    wrap.appendChild(sum);
    wrap.addEventListener('toggle', () => { triggerLogOpen = wrap.open; });

    const body = el('div', { className: 'ck-item-chart-slot' });

    if (triggers.length) {
      body.appendChild(el('div', { className: 'ck-section-heading' }, ['Configured']));
      const configured = el('div', { className: 'ck-trigger-configured' });
      for (const t of triggers) {
        configured.appendChild(el('div', { className: 'ck-trigger-row' }, [
          el('code', {}, [t.name]),
          el('span', { className: 'ck-muted' }, [' → playbook ']),
          el('code', {}, [t.playbook]),
          el('span', { className: 'ck-muted' }, [t.kinds && t.kinds.length ? '  (' + t.kinds.join(' + ') + ')' : '']),
        ]));
      }
      body.appendChild(configured);
    }

    if (log.length) {
      body.appendChild(el('div', { className: 'ck-section-heading' }, ['Recent firings']));
      const list = el('ol', { className: 'ck-trigger-log', 'aria-label': 'Recent trigger firings, newest first' });
      for (const row of log) {
        const li = el('li', { className: 'ck-trigger-log-row' });
        li.appendChild(el('span', { className: 'ck-trigger-log-time' }, [relTime(row.ts) || row.ts || '?']));
        li.appendChild(el('code', { className: 'ck-trigger-log-name' }, [row.name || '?']));
        li.appendChild(el('span', { className: 'ck-muted' }, [' (' + (row.kind || '?') + ') · ']));
        li.appendChild(el('span', {}, [(row.records || 0) + ' step' + ((row.records || 0) === 1 ? '' : 's')]));
        if (row.skipped) {
          li.appendChild(el('span', { className: 'ck-muted' }, ['  · ' + row.skipped + ' skipped']));
        }
        if (row.error) {
          li.appendChild(el('div', { className: 'ck-trigger-log-err' }, ['error: ' + row.error]));
        }
        list.appendChild(li);
      }
      body.appendChild(list);
    } else if (!triggers.length) {
      body.appendChild(el('p', { className: 'ck-muted' },
        ['Add .console-kit/triggers.json to arm webhook or cron triggers.']));
    }

    wrap.appendChild(body);
    return wrap;
  }

  // Impact graph (Cytoscape, 0.14.0): item + its ancestors + questions + cited files + mapped sections.
  // Click a node to highlight downstream; click an item/question node again to navigate (postMessage).
  const openImpact = new Set();
  function renderItemImpact(itemId) {
    const wrap = el('details', { className: 'ck-item-chart', dataItem: itemId });
    if (openImpact.has(itemId)) wrap.setAttribute('open', '');
    const sum = el('summary', { className: 'ck-item-chart-summary' }, [
      el('span', { className: 'ck-item-chart-title' }, ['Impact graph']),
      el('span', { className: 'ck-muted ck-item-chart-hint' }, [
        ' — questions, cited files, sections (drag to rearrange, click to highlight downstream)'
      ])
    ]);
    wrap.appendChild(sum);
    const slot = el('div', { className: 'ck-item-chart-slot' });
    wrap.appendChild(slot);
    const load = () => {
      if (slot.querySelector('iframe')) return;
      const frame = document.createElement('iframe');
      frame.setAttribute('sandbox', 'allow-scripts');
      frame.setAttribute('referrerpolicy', 'no-referrer');
      frame.setAttribute('title', 'Impact graph: ' + itemId);
      frame.className = 'ck-visual-frame ck-visual-frame-mermaid ck-item-chart-frame';
      frame.src = config.api + '/impact-graph?item=' + encodeURIComponent(itemId);
      slot.appendChild(frame);
    };
    wrap.addEventListener('toggle', () => {
      if (wrap.open) { openImpact.add(itemId); load(); } else { openImpact.delete(itemId); }
    });
    if (openImpact.has(itemId)) load();
    return wrap;
  }

  // 0.8.19: whole-project map, parent -> child, coloured by status roll-up per item. Lazy and collapsible.
  let projectMapOpen = false;
  let projectMapFilter = '';   // '' = all; else one of the state keys
  const MAP_CHIPS = [
    ['', 'All'],
    ['awaiting_you', '? Awaiting you'],
    ['stale', '! Stale'],
    ['unlocked', '~ Unlocked'],
    ['locked', 'o Locked']
  ];
  function renderProjectMap() {
    const wrap = el('details', { className: 'ck-project-map' });
    if (projectMapOpen) wrap.setAttribute('open', '');
    const sum = el('summary', { className: 'ck-item-chart-summary' }, [
      el('span', { className: 'ck-item-chart-title' }, ['Project map']),
      el('span', { className: 'ck-muted ck-item-chart-hint' }, [' — every item, parent → child, by status'])
    ]);
    wrap.appendChild(sum);
    const slot = el('div', { className: 'ck-item-chart-slot' });
    const chips = el('div', { className: 'ck-chip-row', role: 'tablist', 'aria-label': 'Filter the project map' });
    const frame = document.createElement('iframe');
    frame.setAttribute('sandbox', 'allow-scripts');
    frame.setAttribute('referrerpolicy', 'no-referrer');
    frame.setAttribute('title', 'Project map');
    frame.className = 'ck-visual-frame ck-visual-frame-mermaid ck-project-map-frame';
    const buildSrc = () => config.api + '/project-chart' +
      (projectMapFilter ? ('?state=' + encodeURIComponent(projectMapFilter)) : '');
    const loadFrame = () => {
      if (!frame.src) frame.src = buildSrc();
      else frame.src = buildSrc();
    };
    for (const [key, label] of MAP_CHIPS) {
      const on = key === projectMapFilter;
      const b = el('button', {
        className: 'ck-chip' + (on ? ' ck-chip-on' : ''),
        type: 'button', role: 'tab',
        'aria-pressed': on ? 'true' : 'false'
      }, [label]);
      b.addEventListener('click', () => {
        projectMapFilter = key;
        for (const other of chips.querySelectorAll('.ck-chip')) {
          other.classList.toggle('ck-chip-on', other === b);
          other.setAttribute('aria-pressed', other === b ? 'true' : 'false');
        }
        if (wrap.open) loadFrame();
      });
      chips.appendChild(b);
    }
    slot.appendChild(chips);
    slot.appendChild(frame);
    wrap.appendChild(slot);
    wrap.addEventListener('toggle', () => {
      projectMapOpen = wrap.open;
      if (wrap.open && !frame.src) loadFrame();
    });
    if (projectMapOpen && !frame.src) loadFrame();
    return wrap;
  }

  // This item's visual requests, newest first, each with what the agent drew.
  function renderVisuals(itemId) {
    const wrap = el('div', { className: 'ck-visuals' });
    const reqs = ((view.threads || {})[itemId] || []).filter(m => m.intent === 'visual')
      .sort((a, b) => b.seq - a.seq);
    if (!reqs.length) return wrap;
    wrap.appendChild(el('div', { className: 'ck-section-heading' }, ['Visuals']));
    const drawn = (view.visuals || {})[itemId] || [];
    for (const m of reqs) {
      const card = el('div', { className: 'ck-fork ck-visual-request', dataRequest: m.id });
      card.appendChild(el('div', { className: 'ck-fork-head' }, ['◫ Visual requested · ' + relTime(m.ts)]));
      const t = el('div', { className: 'ck-message-text' });
      t.textContent = m.text;
      card.appendChild(t);
      const mine = drawn.filter(v => v.request === m.id);
      if (!mine.length) card.appendChild(el('p', { className: 'ck-muted' }, ['Waiting for an agent to draw it.']));
      for (const v of mine) card.appendChild(renderVisual(v));
      wrap.appendChild(card);
    }
    return wrap;
  }

  function renderVisual(v) {
    const box = el('div', { className: 'ck-visual', dataFormat: v.format, dataVisual: v.id });
    const titleRow = el('div', { className: 'ck-visual-title-row' }, [
      el('div', { className: 'ck-visual-title' }, [v.title]),
      renderStarButton('visual:' + v.id, 'this visual')
    ]);
    box.appendChild(titleRow);
    box.appendChild(el('div', { className: 'ck-muted' }, [
      (v.format === 'html' ? 'HTML mock' : 'Mermaid diagram') + ' · ' + String(v.path).split('/').pop() +
      ' · ' + relTime(v.ts) + (v.agent ? ' · by ' + v.agent : '')]));
    const doc = el('div', { className: 'ck-visual-doc' });
    doc.textContent = v.text;
    box.appendChild(doc);
    const src = config.api + '/visual?id=' + encodeURIComponent(v.id);
    if (v.format === 'html') {
      // The ONLY way a mock reaches the page: an iframe whose sandbox grants nothing
      // (no scripts, no same-origin, no forms, no popups, no top navigation). The
      // server's CSP on the response says the same, even if the URL is opened alone.
      const frame = document.createElement('iframe');
      frame.setAttribute('sandbox', '');
      frame.setAttribute('referrerpolicy', 'no-referrer');
      frame.setAttribute('title', 'Visual: ' + v.title);
      frame.className = 'ck-visual-frame';
      frame.src = src;
      box.appendChild(frame);
    } else {
      // 0.8.19: Mermaid is rendered as a diagram inside a sandboxed iframe (allow-scripts, NOT
      // allow-same-origin). The vendored lib runs in an opaque origin: no parent cookies, no storage,
      // no network to this origin. A "View source" toggle still shows the raw .mmd text when wanted.
      const renderSrc = config.api + '/visual-render?id=' + encodeURIComponent(v.id);
      const frame = document.createElement('iframe');
      frame.setAttribute('sandbox', 'allow-scripts');
      frame.setAttribute('referrerpolicy', 'no-referrer');
      frame.setAttribute('title', 'Mermaid diagram: ' + v.title);
      frame.className = 'ck-visual-frame ck-visual-frame-mermaid';
      frame.src = renderSrc;
      box.appendChild(frame);
      const pre = el('pre', { className: 'ck-visual-code ck-visual-code-collapsed',
        'aria-label': 'Mermaid source: ' + v.title, hidden: 'hidden' });
      pre.textContent = 'Loading…';
      const toggle = el('button', { className: 'ck-btn ck-visual-source-toggle', type: 'button',
        'aria-expanded': 'false', 'aria-controls': 'ck-src-' + v.id }, ['View source']);
      pre.id = 'ck-src-' + v.id;
      let loaded = false;
      toggle.addEventListener('click', async () => {
        const open = toggle.getAttribute('aria-expanded') === 'true';
        if (open) {
          toggle.setAttribute('aria-expanded', 'false');
          toggle.textContent = 'View source';
          pre.hidden = true;
          return;
        }
        toggle.setAttribute('aria-expanded', 'true');
        toggle.textContent = 'Hide source';
        pre.hidden = false;
        if (loaded) return;
        try {
          const resp = await fetch(src, { credentials: 'same-origin' });
          const body = await resp.text();
          if (resp.ok) { pre.textContent = body; loaded = true; return; }
          let msg = 'HTTP ' + resp.status;
          try { msg = JSON.parse(body).error || msg; } catch (e) { /* not JSON */ }
          pre.textContent = 'Not shown: ' + msg;
        } catch (e) {
          pre.textContent = 'Not shown: ' + (e.message || 'network error');
        }
      });
      box.appendChild(toggle);
      box.appendChild(pre);
    }
    return box;
  }

  // Render answer form
  function renderAnswerForm(q, requireReason, supersedesId) {
    const qData = q.question;
    const form = el('div');
    const formId = 'form-' + qData.qid.replace('/', '-');
    const picks = [];
    let ownTextEl = null;
    let useOwnEl = null;
    let reasonEl = null;

    // Options
    if (qData.kind !== 'free' && qData.options && qData.options.length > 0) {
      const optList = el('div', { className: 'ck-options' });
      const inputType = qData.kind === 'single' ? 'radio' : 'checkbox';
      for (const opt of qData.options) {
        const label = el('label', { className: 'ck-option' });
        const input = el('input', {
          type: inputType,
          name: formId + '-opt',
          value: opt.id
        });
        // R2: ★ is NEVER pre-selected
        input.checked = false;
        input.addEventListener('change', () => {
          if (inputType === 'radio') picks.length = 0;
          const idx = picks.indexOf(opt.id);
          if (input.checked && idx === -1) picks.push(opt.id);
          else if (!input.checked && idx !== -1) picks.splice(idx, 1);
        });
        label.appendChild(input);
        const content = el('div', { className: 'ck-option-content' });
        const labelText = el('span', { className: 'ck-option-label' });
        labelText.textContent = opt.label;
        content.appendChild(labelText);
        if (qData.star === opt.id) {
          content.appendChild(el('span', { className: 'ck-option-star' }, ['★ recommended']));
        }
        if (opt.description) {
          const desc = el('div', { className: 'ck-option-desc' });
          desc.textContent = opt.description;
          content.appendChild(desc);
        }
        label.appendChild(content);
        optList.appendChild(label);
      }
      form.appendChild(optList);
    }

    // Own words (always visible per spec)
    // Fix #3: Give "None of these" the same card treatment as options
    if (qData.kind !== 'free') {
      useOwnEl = el('input', { type: 'checkbox', id: formId + '-useown' });
      const ownCard = el('label', { className: 'ck-option ck-option-own' });
      ownCard.appendChild(useOwnEl);
      const ownContent = el('div', { className: 'ck-option-content' });
      ownContent.appendChild(el('span', { className: 'ck-option-label' }, ['None of these — in my own words']));
      ownCard.appendChild(ownContent);
      form.appendChild(ownCard);
    }
    const ownWordsArea = el('div', { className: 'ck-own-words-area' });
    if (qData.kind === 'free') {
      ownWordsArea.appendChild(el('div', { className: 'ck-own-words-label' }, ['Your answer:']));
    }
    ownTextEl = el('textarea', {
      className: 'ck-textarea',
      placeholder: 'Your own words...',
      rows: '3'
    });
    // Restore draft if any
    const draftKey = qData.qid + (supersedesId || '');
    // Re-answering keeps the words already given (§7.5 F2, the page half of the #167
    // defect): a draft typed since wins, else the current answer's own words.
    const prior = q.answers && q.answers.length ? q.answers[q.answers.length - 1] : null;
    if (draftTexts[draftKey] !== undefined) ownTextEl.value = draftTexts[draftKey];
    else if (prior && prior.own_text) ownTextEl.value = prior.own_text;
    ownTextEl.addEventListener('input', () => { draftTexts[draftKey] = ownTextEl.value; });
    ownWordsArea.appendChild(ownTextEl);
    form.appendChild(ownWordsArea);

    // Reason for superseding
    if (requireReason) {
      const reasonWrap = el('div', { style: 'margin-top: 12px;' }, [
        el('label', { style: 'font-size: 13px; display: block; margin-bottom: 6px;' }, [
          'Reason for changing your locked answer (required):'
        ])
      ]);
      reasonEl = el('textarea', {
        className: 'ck-textarea',
        placeholder: 'Why are you superseding the previous answer?',
        rows: '2',
        required: 'true'
      });
      reasonWrap.appendChild(reasonEl);
      reasonWrap.appendChild(el('div', { className: 'ck-supersede-note' }, [
        'Your locked answer stays on record; the new one supersedes it.'
      ]));
      form.appendChild(reasonWrap);
    }

    // Submit — fix #5: unique aria-label per question
    const actions = el('div', { className: 'ck-actions' });
    const truncText = truncateText(qData.text, 40);
    const submitBtn = el('button', {
      className: 'ck-btn ck-btn-primary',
      type: 'button',
      'aria-label': 'Submit answer: ' + truncText
    }, ['Submit answer']);
    submitBtn.addEventListener('click', async () => {
      const finalPicks = [...picks];
      // If "own words" checked or this is free-form, clear picks or keep them
      const ownText = ownTextEl.value.trim();
      if (!finalPicks.length && !ownText) {
        announce('Please select an option or provide your own words.');
        return;
      }
      if (requireReason && (!reasonEl || !reasonEl.value.trim())) {
        announce('Please provide a reason for changing your answer.');
        return;
      }
      submitBtn.disabled = true;
      const body = { qid: qData.qid, picks: finalPicks, own_text: ownText };
      if (supersedesId) {
        body.supersedes = supersedesId;
        body.reason = reasonEl.value.trim();
      }
      const result = await apiPost('/answer', body, 'answer-' + qData.qid);
      submitBtn.disabled = false;
      if (result.error) {
        announce('Error: ' + result.error);
      } else {
        delete draftTexts[draftKey];
        renderPanel();
      }
    });
    actions.appendChild(submitBtn);
    form.appendChild(actions);

    return form;
  }

  // Render receipt showing picks and rejected
  // Fix #6: wrap rejected in <s> with visually-hidden text for screen readers
  function renderReceipt(qData, answer) {
    const receipt = el('div', { className: 'ck-receipt' }, [
      el('div', { className: 'ck-receipt-heading' }, ['Your answer'])
    ]);
    if (qData.options && qData.options.length > 0) {
      const picked = answer && answer.picks ? answer.picks : [];
      for (const opt of qData.options) {
        const isPicked = picked.includes(opt.id);
        const row = el('div', { className: isPicked ? 'ck-receipt-picked' : 'ck-receipt-rejected' });
        if (isPicked) {
          row.appendChild(document.createTextNode('✓ ' + opt.label));
        } else {
          // Wrap in <s> for semantic strikethrough
          const strikeEl = document.createElement('s');
          strikeEl.textContent = opt.label;
          row.appendChild(strikeEl);
          // Visually-hidden text for screen readers
          const srOnly = el('span', { className: 'ck-sr-only' }, [' (not chosen)']);
          row.appendChild(srOnly);
        }
        receipt.appendChild(row);
      }
    }
    if (answer && answer.own_text) {
      const own = el('div', { className: 'ck-receipt-own' });
      own.textContent = 'Your words: ' + answer.own_text;
      receipt.appendChild(own);
    }
    return receipt;
  }

  // "Why stale?" (0.5.0): the server's own check, fetched when the owner asks.
  // Every string is put in as text, never as markup.
  let checkPromise = null;
  function fetchCheck() {
    if (!checkPromise) {
      checkPromise = fetch(config.api + '/check', { credentials: 'same-origin' })
        .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .catch(e => { checkPromise = null; return { error: e.message || 'Network error' }; });
    }
    return checkPromise;
  }

  function renderWhyStale(qid) {
    const box = el('div', { className: 'ck-why', role: 'region', 'aria-label': 'Why ' + qid + ' is stale' },
      [el('p', { className: 'ck-muted' }, ['Checking…'])]);
    fetchCheck().then(data => {
      box.textContent = '';
      if (data.error) {
        box.appendChild(el('p', {}, ['Could not check just now: ' + data.error]));
        return;
      }
      const s = data.stale && data.stale[qid];
      if (!s) {
        box.appendChild(el('p', {}, ['Nothing fails any more: this answer is no longer stale. Reload to see it.']));
        return;
      }
      const list = el('ul', { className: 'ck-why-list' });
      for (const c of s.conditions) {
        if (c.holds) continue;
        const li = el('li', {}, [c.words]);
        if (c.diff) {
          li.appendChild(el('div', { className: 'ck-muted' }, ['What the question cited (−) against the file now (+):']));
          const pre = el('pre', { className: 'ck-why-diff', tabindex: '0' });
          pre.textContent = c.diff;
          li.appendChild(pre);
        }
        list.appendChild(li);
      }
      box.appendChild(list);
    });
    return box;
  }

  // Re-lock as it stands (0.5.0): one answer superseding the locked one word for
  // word, and its lock, written by the server, which re-anchors the new lock.
  function renderRelock(q) {
    const qid = q.question.qid;
    const wrap = el('div', { className: 'ck-confirm' }, [
      el('div', { className: 'ck-confirm-heading' }, ['Re-lock this answer as it stands?']),
      el('p', {}, ['Your answer stays word for word. The new lock is checked against the files as they are now, ' +
        'so it reads locked again until the text it cites changes.'])
    ]);
    const id = 'relock-reason-' + qid.replace('/', '-');
    wrap.appendChild(el('label', { for: id, style: 'font-size: 13px; display: block; margin: 8px 0 4px;' },
      ['Why it still holds (optional):']));
    const reason = el('textarea', { id: id, rows: '2', className: 'ck-textarea', maxlength: '2000' });
    wrap.appendChild(reason);
    const actions = el('div', { className: 'ck-actions', style: 'margin-top: 8px;' });
    const go = el('button', { className: 'ck-btn ck-btn-primary', type: 'button' }, ['Re-lock as it stands']);
    go.addEventListener('click', async () => {
      go.disabled = true;
      const body = { qid: qid };
      if (reason.value.trim()) body.reason = reason.value.trim();
      const result = await apiPost('/relock', body, 'relock-' + qid);
      go.disabled = false;
      if (result.error) announce('Error: ' + result.error);
      else { openForms.delete('relock-' + qid); openForms.delete('why-' + qid); renderPanel(); }
    });
    const cancel = el('button', { className: 'ck-btn', type: 'button' }, ['Cancel']);
    cancel.addEventListener('click', () => { openForms.delete('relock-' + qid); renderPanel(); });
    actions.appendChild(go);
    actions.appendChild(cancel);
    wrap.appendChild(actions);
    return wrap;
  }

  // CONSOLE-kit/Q30-Q32: what was done about a stale ruling, said on its card. Nothing when nothing was.
  function refactorNote(q) {
    const rx = q.refactor;
    if (!rx) return null;
    const lines = [];
    const o = rx.outcome;
    if (o) {
      const why = o.reason ? ': ' + o.reason : '';
      if (o.kind === 'withdrawn') lines.push('Withdrawn by you on ' + o.at + why);
      else if (o.kind === 'untracked') lines.push('Kept by you on ' + o.at + ', no longer checked against the files' + why);
      else if (o.kind === 'superseded') lines.push('Superseded on ' + o.at + ' by ' + o.replaced_by + ', which you locked');
    }
    if (rx.replaced_by && !(o && o.kind === 'superseded')) {
      lines.push('A replacement is open: ' + rx.replaced_by + '. This ruling stays in force until you lock it.');
    }
    if (rx.replaces) lines.push('Asked to replace ' + rx.replaces + '.');
    if (rx.confirmed) lines.push('Re-anchored on ' + rx.confirmed.at + ' from a proposal you confirmed.');
    if (rx.problem) lines.push(rx.problem);
    if (!lines.length) return null;
    const box = el('div', { className: 'ck-refactor-note', role: 'note' });
    for (const l of lines) box.appendChild(el('p', {}, [l]));
    return box;
  }

  // CONSOLE-kit/Q40, Q41: "Scan for a resolve" asks the steward to work out what became of stale rulings. It is
  // a request, never a ruling: the steward answers with a proposed anchor, a replacement question or a star
  // recommendation, and nothing changes until the owner presses Confirm, Withdraw or Keep. One scan at a time:
  // while one is open the buttons say so (the server refuses a second one by name in any case).
  function openScan() {
    const open = Object.values(view.scans || {}).filter(sc => !sc.done);
    open.sort((a, b) => a.seq - b.seq);
    return open[0] || null;
  }

  function staleRulings() {
    return Object.values(view.questions || {}).filter(q => q.state === 'stale' && q.lock)
      .sort((a, b) => a.question.seq - b.question.seq);
  }

  function scanWaiting(sc) {
    return sc.qids.filter(qid => !Object.keys(sc.rulings[qid] || {}).length);
  }

  async function askScan(qs, all, btn) {
    btn.disabled = true;
    const body = { action: 'scan', qids: qs.map(q => q.question.qid), locks: qs.map(q => q.lock) };
    if (all) body.all = true;
    const result = await apiPost('/refactor', body, all ? 'scan-all' : 'scan-' + qs[0].question.qid);
    btn.disabled = false;
    if (result.error) { announce('Scan not asked: ' + result.error); return; }
    announce((qs.length === 1 ? 'Scan asked for ' + qs[0].question.qid : 'Scan asked for ' + qs.length + ' stale rulings')
      + '. The steward answers each one; nothing changes until you decide.');
    redrawKeepingPlace();
  }

  function scanControl(q) {
    const qid = q.question.qid;
    const sc = openScan();
    if (!sc) {
      const btn = el('button', { className: 'ck-btn ck-scan-one', type: 'button', dataFocusKey: 'scan-' + qid,
        'aria-label': 'Scan for a resolve: ' + truncateText(q.question.text, 40) }, ['Scan for a resolve']);
      btn.addEventListener('click', () => askScan([q], false, btn));
      return btn;
    }
    let words;
    if (sc.qids.includes(qid)) {
      const r = sc.rulings[qid] || {};
      words = r.answered ? 'Scanned: the steward answered below' : 'Scan asked ' + sc.ts + ': waiting on the steward';
    } else {
      words = 'A scan is open (' + scanWaiting(sc).length + ' of ' + sc.qids.length + ' waiting); it answers first';
    }
    return el('span', { className: 'ck-scan-status', dataQid: qid }, [words]);
  }

  function renderScanAll() {
    const stale = staleRulings();
    const sc = openScan();
    if (!stale.length && !sc) return null;
    const box = el('div', { className: 'ck-scan-all', role: 'region', 'aria-label': 'Scan stale rulings for a resolve' });
    if (sc) {
      const waiting = scanWaiting(sc);
      box.appendChild(el('p', { className: 'ck-scan-status' }, ['Scan asked ' + sc.ts + ': ' + waiting.length + ' of '
        + sc.qids.length + ' rulings still wait on the steward' + (waiting.length ? ' (' + waiting.join(', ') + ')' : '')
        + '.']));
      return box;
    }
    box.appendChild(el('p', {}, [stale.length + (stale.length === 1 ? ' ruling is' : ' rulings are')
      + ' stale. The steward can work out what became of each and answer with a new anchor, a replacement question'
      + ' or a recommendation. Nothing changes until you decide.']));
    const btn = el('button', { className: 'ck-btn ck-scan-all-go', type: 'button', dataFocusKey: 'scan-all' },
      ['Scan all stale (' + stale.length + ')']);
    btn.addEventListener('click', () => askScan(stale, true, btn));
    box.appendChild(btn);
    return box;
  }

  // The steward's answer when neither a new anchor nor a replacement fits: a star recommendation, with its
  // evidence. It is advice: the owner's own Withdraw or Keep (above) is the ruling.
  function renderAdvice(q) {
    const a = q.refactor.advice;
    const what = a.star === 'withdraw' ? 'Withdraw' : 'Keep, stop checking';
    const box = el('div', { className: 'ck-advice', role: 'note', 'aria-label': 'The steward recommends for ' + q.question.qid });
    box.appendChild(el('strong', {}, ['The steward recommends: ★ ' + what]));
    box.appendChild(el('p', {}, [a.evidence]));
    box.appendChild(el('p', { className: 'ck-advice-note' }, ['Nothing changes until you press ' + what + '… above.']));
    return box;
  }

  // Withdraw (reason required) or keep without checking (reason optional): the owner's acts alone.
  function renderSettle(q, action) {
    const qid = q.question.qid;
    const withdraw = action === 'withdraw';
    const wrap = el('div', { className: 'ck-confirm' }, [
      el('div', { className: 'ck-confirm-heading' }, [withdraw ? 'Withdraw this ruling?' : 'Keep it, and stop checking?']),
      el('p', {}, [withdraw
        ? 'It no longer applies. It leaves your inbox and the record marks it withdrawn by you, with your reason.'
        : 'It stands as it is. It leaves your inbox and the record marks it as no longer checked against the files.'])
    ]);
    const id = action + '-reason-' + qid.replace('/', '-');
    wrap.appendChild(el('label', { for: id, style: 'font-size: 13px; display: block; margin: 8px 0 4px;' },
      [withdraw ? 'Why it no longer applies:' : 'Why (optional):']));
    const reason = el('textarea', { id: id, rows: '2', className: 'ck-textarea', maxlength: '2000' });
    wrap.appendChild(reason);
    const actions = el('div', { className: 'ck-actions', style: 'margin-top: 8px;' });
    const go = el('button', { className: 'ck-btn ck-btn-primary', type: 'button' },
      [withdraw ? 'Withdraw' : 'Keep, stop checking']);
    go.addEventListener('click', async () => {
      const text = reason.value.trim();
      if (withdraw && !text) { announce('Say why it no longer applies'); reason.focus(); return; }
      go.disabled = true;
      // The lock the page was shown: a lock changed since is refused by name, never acted on unseen.
      const body = { action: action, qid: qid, lock: q.lock };
      if (text) body.reason = text;
      const result = await apiPost('/refactor', body, action + '-' + qid);
      go.disabled = false;
      if (result.error) announce('Error: ' + result.error);
      else { openForms.delete(action + '-' + qid); renderPanel(); }
    });
    const cancel = el('button', { className: 'ck-btn', type: 'button' }, ['Cancel']);
    cancel.addEventListener('click', () => { openForms.delete(action + '-' + qid); renderPanel(); });
    actions.appendChild(go);
    actions.appendChild(cancel);
    wrap.appendChild(actions);
    return wrap;
  }

  // Q31: the steward's proposed anchor beside the one that failed; nothing changes until the owner confirms.
  function renderProposal(q) {
    const qid = q.question.qid;
    const p = q.refactor.proposal;
    const box = el('div', { className: 'ck-proposal', role: 'region', 'aria-label': 'Proposed anchor for ' + qid });
    box.appendChild(el('strong', {}, ['The steward proposes a new anchor']));
    box.appendChild(el('p', {}, [p.basis]));
    const cols = el('div', { className: 'ck-proposal-cols' });
    const was = el('div', { className: 'ck-proposal-old' }, [el('div', { className: 'ck-confirm-heading' }, ['Checked now'])]);
    for (const c of p.base) {
      const failing = (q.failing || []).some(f => JSON.stringify(f) === JSON.stringify(c));
      was.appendChild(el('p', {}, [(failing ? 'No longer holds: ' : 'Holds: ') + conditionWords(c)]));
    }
    const now = el('div', { className: 'ck-proposal-new' }, [el('div', { className: 'ck-confirm-heading' }, ['Proposed'])]);
    for (const c of p.anchors) {
      if (c.kind === 'excerpt') {
        now.appendChild(el('p', {}, [c.path + ' still contains:']));
        now.appendChild(el('pre', { className: 'ck-excerpt' }, [c.text]));
      } else {
        now.appendChild(el('p', {}, ['Holds: ' + conditionWords(c)]));
      }
    }
    cols.appendChild(was);
    cols.appendChild(now);
    box.appendChild(cols);
    const go = el('button', { className: 'ck-btn ck-btn-primary ck-confirm-proposal', type: 'button',
      'aria-label': 'Confirm the proposed anchor for ' + qid }, ['Confirm this anchor']);
    // Show the server's refusal (409 or other) inline below Confirm, so the owner sees why.
    // Before this, only announce() fired, which many viewers never see.
    const err = el('div', { className: 'ck-confirm-error', role: 'alert', hidden: 'hidden' });
    go.addEventListener('click', async () => {
      go.disabled = true;
      err.hidden = true;
      err.textContent = '';
      const result = await apiPost('/refactor', { action: 'confirm', qid: qid, proposal: p.id }, 'confirm-' + qid);
      go.disabled = false;
      if (result && result.error) {
        err.textContent = result.error;
        err.hidden = false;
        announce('Not confirmed: ' + result.error);
      } else {
        renderPanel();
      }
    });
    box.appendChild(el('div', { className: 'ck-actions' }, [go]));
    box.appendChild(err);
    return box;
  }

  // Locking one answer is one tap. The 5-second Undo countdown was removed; the request goes right away
  // (same POST the old confirm sent, so the owner door's checks are unchanged). lockErrors[qid] keeps the
  // server's refusal if one lands, so the question card can surface "Not locked: ..." on the next render.
  async function startLock(q, answer) {
    const qid = q.question.qid;
    if (!answer) return;
    delete lockErrors[qid];
    const ae = document.activeElement;
    const wasOnLockBtn = !!(ae && ae.getAttribute && ae.getAttribute('data-focus-key') === 'lock-' + qid);
    const result = await apiPost('/lock', { qid: qid, answer: answer.id }, 'lock-' + qid);
    if (result && result.error) {
      lockErrors[qid] = result.error;
      announce('Not locked: ' + result.error);
    } else {
      justLocked.add(qid);
      announce('Locked.');
    }
    if (!panelEl || panelEl.getAttribute('data-open') !== 'true') return;
    if (busyInPanel()) { pendingLive = true; return; }
    redrawKeepingPlace();
    // The Lock button that had focus is gone (the question rolled up); land on the question's own line.
    if (wasOnLockBtn && !lockErrors[qid]) {
      const line = panelEl.querySelector('.ck-q-line[data-qid="' + CSS.escape(qid) + '"]') ||
        panelEl.querySelector('.ck-questions-heading');
      if (line) {
        if (!line.hasAttribute('tabindex') && line.tagName !== 'BUTTON') line.setAttribute('tabindex', '-1');
        line.focus();
      }
    }
  }

  // Compatibility stubs: the countdown apparatus is gone, so nothing is ever pending.
  // Kept as no-ops because pagehide / visibilitychange listeners below still call them.
  function dropAllLocks() { /* no-op: lock is immediate; there is nothing in flight to drop */ }
  function dropLocksNotOnShow() { /* no-op: see above */ }

  // Render thread/messages
  function renderThread(itemId) {
    const wrap = el('div', { className: 'ck-thread' }, [
      el('div', { className: 'ck-thread-heading' }, ['Discussion'])
    ]);

    // Author input box
    const authorInput = el('div', { className: 'ck-author-input' }, [
      el('div', { className: 'ck-author-input-label' }, [
        'Guidance for this lane/phase/topic (the agent will read this):'
      ])
    ]);
    const inputTextarea = el('textarea', {
      className: 'ck-textarea',
      placeholder: 'Type your guidance here...',
      rows: '3'
    });
    const draftKey = 'msg-' + itemId;
    if (draftTexts[draftKey]) inputTextarea.value = draftTexts[draftKey];
    inputTextarea.addEventListener('input', () => { draftTexts[draftKey] = inputTextarea.value; });
    authorInput.appendChild(inputTextarea);
    const sendBtn = el('button', { className: 'ck-btn ck-btn-primary', type: 'button', style: 'margin-top: 8px;' },
      ['Send']);
    sendBtn.addEventListener('click', async () => {
      const text = inputTextarea.value.trim();
      if (!text) { announce('Please enter a message.'); return; }
      sendBtn.disabled = true;
      const result = await apiPost('/message', { item: itemId, text: text }, 'msg-' + itemId);
      sendBtn.disabled = false;
      if (result.error) {
        announce('Error: ' + result.error);
      } else {
        delete draftTexts[draftKey];
        inputTextarea.value = '';
        renderPanel();
      }
    });
    authorInput.appendChild(sendBtn);
    wrap.appendChild(authorInput);

    // Messages
    const thread = view && view.threads ? view.threads[itemId] : null;
    if (thread && thread.length > 0) {
      const msgsEl = el('div', { className: 'ck-messages' });
      // Sort by seq descending (newest first)
      const sorted = [...thread].sort((a, b) => b.seq - a.seq);
      for (const m of sorted) {
        const msg = el('div', { className: 'ck-message' });
        const byEl = el('span', { className: 'ck-message-by', dataBy: m.by });
        byEl.textContent = agentLabel(m, m.by);
        msg.appendChild(byEl);
        const content = el('div', { className: 'ck-message-content' });
        const textEl = el('div', { className: 'ck-message-text' });
        textEl.textContent = m.text;
        content.appendChild(textEl);
        const timeEl = el('div', { className: 'ck-message-time' });
        timeEl.textContent = m.ts ? relTime(m.ts) : '';
        if (m.ts) timeEl.title = new Date(m.ts).toLocaleString();
        content.appendChild(timeEl);
        // Reply button
        const replyBtn = el('button', { className: 'ck-reply-btn', type: 'button' }, ['Reply']);
        replyBtn.addEventListener('click', () => {
          const replyTextarea = el('textarea', {
            className: 'ck-textarea',
            placeholder: 'Your reply...',
            rows: '2',
            style: 'margin-top: 8px;'
          });
          const replySend = el('button', { className: 'ck-btn', type: 'button', style: 'margin-top: 6px;' },
            ['Send reply']);
          replySend.addEventListener('click', async () => {
            const text = replyTextarea.value.trim();
            if (!text) return;
            replySend.disabled = true;
            const result = await apiPost('/message',
              { item: itemId, text: text, reply_to: m.id },
              'reply-' + m.id);
            replySend.disabled = false;
            if (!result.error) renderPanel();
          });
          content.appendChild(replyTextarea);
          content.appendChild(replySend);
          replyBtn.remove();
        });
        content.appendChild(replyBtn);
        msg.appendChild(content);
        msgsEl.appendChild(msg);
      }
      wrap.appendChild(msgsEl);
    }

    return wrap;
  }

  // A panel header: Back, a title, Close.
  function makeHeader(titleChildren, onBack, backLabel) {
    const header = el('div', { className: 'ck-header' }, [
      el('button', { className: 'ck-back-btn', type: 'button', 'aria-label': backLabel }, ['← Back']),
      el('span', { className: 'ck-title' }, titleChildren),
      el('button', { className: 'ck-close-btn', type: 'button', 'aria-label': 'Close panel' }, ['×'])
    ]);
    header.querySelector('.ck-back-btn').addEventListener('click', onBack);
    header.querySelector('.ck-close-btn').addEventListener('click', closePanel);
    return header;
  }

  // Open the answers sheet for an item and all under it, or (itemId null) the whole console.
  function showSheet(itemId, forkId) {
    currentFork = forkId;
    if (panelEl.getAttribute('data-open') === 'true') {
      currentItem = itemId;
      currentMode = 'sheet';
      renderPanel();
      const h = panelEl.querySelector('.ck-title');
      if (h) { h.setAttribute('tabindex', '-1'); h.focus(); }
    } else {
      openPanel(itemId, 'sheet');
    }
  }

  function countLine(c) {
    return [GLYPH.awaiting_you + ' ' + c.awaiting_you + ' unanswered',
      GLYPH.unlocked + ' ' + c.unlocked + ' answered',
      GLYPH.locked + ' ' + c.locked + ' locked',
      GLYPH.stale + ' ' + c.stale + ' stale'].join('  ');
  }

  // The answers sheet (spec §7.6): every question in the scope, every answer it got.
  function renderSheet(itemId, forkId) {
    const scopeWords = itemId ? itemId + ' and all under it' : 'the whole console';
    panelEl.appendChild(makeHeader(['Answers: ' + scopeWords], () => {
      currentFork = null;
      currentMode = itemId ? 'item' : 'inbox';
      renderPanel();
    }, itemId ? 'Back to ' + itemId : 'Back to inbox'));
    panelEl.appendChild(renderStatusBar(itemId));
    const body = el('div', { className: 'ck-body' });
    panelEl.appendChild(body);
    if (!view) { body.appendChild(renderOffline()); return; }

    // Narrow to one deliberation round
    const scope = itemId ? subtree(itemId) : null;
    const forkIds = Object.keys(view.forks).filter(f => !scope || scope.has(view.forks[f].message.item));
    if (forkIds.length) {
      const sel = el('select', { className: 'ck-select', id: 'ck-sheet-fork' });
      sel.appendChild(el('option', { value: '' }, ['Every question']));
      for (const f of forkIds) {
        const m = view.forks[f].message;
        const o = el('option', { value: f }, ['⑂ ' + m.item + ' · ' + m.mode + ' · ' + relTime(m.ts) +
          ' (' + view.forks[f].questions.length + ' questions)']);
        if (f === forkId) o.selected = true;
        sel.appendChild(o);
      }
      sel.addEventListener('change', () => showSheet(itemId, sel.value || null));
      body.appendChild(el('div', { className: 'ck-field' }, [
        el('label', { for: 'ck-sheet-fork', className: 'ck-field-label' }, ['Show']), sel]));
    }

    const sheet = answersSheet(itemId, forkId);
    body.appendChild(el('p', { className: 'ck-sheet-counts' }, [
      sheet.rows.length + ' questions, ' + sheet.answers + ' answers. ' + countLine(sheet.counts)]));
    if (!sheet.rows.length) {
      body.appendChild(el('p', { className: 'ck-muted' }, ['No questions here yet.']));
    }
    for (const q of sheet.rows) body.appendChild(renderSheetRow(q));

    // Footer: the counts, Copy as Markdown, and the ready signal
    const foot = el('div', { className: 'ck-sheet-foot' }, [
      el('div', { className: 'ck-sheet-counts' }, [countLine(sheet.counts)])]);
    const copyBtn = el('button', { className: 'ck-btn', type: 'button' }, ['Copy as Markdown']);
    copyBtn.addEventListener('click', () => {
      const md = sheetMarkdown(sheet);
      const fallback = () => {
        const ta = el('textarea', { className: 'ck-textarea', rows: '8', readonly: 'true',
          'aria-label': 'The answers as Markdown; select and copy' });
        ta.value = md;
        foot.appendChild(ta);
        ta.focus();
        ta.select();
        announce('Select and copy the Markdown below.');
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(md).then(() => announce('Copied ' + sheet.rows.length + ' questions as Markdown.'), fallback);
      } else {
        fallback();
      }
    });
    foot.appendChild(el('div', { className: 'ck-actions' }, [copyBtn]));
    if (itemId) foot.appendChild(renderReadyButton(itemId, sheet.counts));
    else foot.appendChild(el('p', { className: 'ck-muted' }, [
      'The ready signal is sent from an item, so its thread records which round you mean. ' +
      'The agent processes every newly locked answer either way.']));
    body.appendChild(foot);
  }

  function renderSheetRow(q) {
    const r = q.question;
    const labels = optionLabels(r);
    const head = q.answers.length ? q.answers[q.answers.length - 1] : null;
    const row = el('details', { className: 'ck-sheet-row', dataState: q.state });
    const current = head ? (head.picks.map(p => labels[p] || p).join(', ') || 'own words') : 'no answer yet';
    row.appendChild(el('summary', {}, [
      el('span', { className: 'ck-q-state', dataState: q.state }, [GLYPH[q.state] + ' ' + STATE_WORDS[q.state]]),
      el('span', { className: 'ck-inbox-item-id' }, [r.qid]),
      el('span', { className: 'ck-sheet-q' }, [truncateText(r.text, 90)]),
      el('span', { className: 'ck-sheet-current' }, [current + (q.answers.length > 1 ? ' · ' + q.answers.length + ' answers' : '')])
    ]));
    const inner = el('div', { className: 'ck-sheet-body' });
    const text = el('div', { className: 'ck-q-text' });
    text.textContent = r.text;
    inner.appendChild(text);
    if (!items[r.item]) inner.appendChild(el('p', { className: 'ck-muted' }, ['This item is no longer in the register.']));
    if (r.star) inner.appendChild(el('div', {}, ['★ ' + (r.star_by ? r.star_by + "'s" : 'recommended') + ': ' + (labels[r.star] || r.star)]));
    if (r.forked_from) inner.appendChild(el('div', { className: 'ck-muted' }, ['From deliberation ' + r.forked_from.slice(0, 8)]));
    for (const c of q.failing) inner.appendChild(el('div', { className: 'ck-stale-banner' }, ['Stale: this no longer holds: ' + conditionWords(c)]));
    if (q.answers.length) {
      const list = el('ol', { className: 'ck-sheet-answers' });
      q.answers.forEach((a, i) => {
        const li = el('li', {});
        const tag = (i === q.answers.length - 1 ? 'current' : 'earlier') + (a.locked ? ', locked' : '');
        li.appendChild(el('div', {}, [(a.ts ? new Date(a.ts).toLocaleString() : '') + ' (' + tag + '): ' +
          (a.picks.map(p => labels[p] || p).join(', ') || 'no pick')]));
        if (a.own_text) { const w = el('div', { className: 'ck-receipt-own' }); w.textContent = a.own_text; li.appendChild(w); }
        if (a.reason) { const w = el('div', { className: 'ck-muted' }); w.textContent = 'Replaced the answer before it, because: ' + a.reason; li.appendChild(w); }
        list.appendChild(li);
      });
      inner.appendChild(list);
    }
    // Review acts in place, through the existing forms and the store's rules.
    const reviewBtn = el('button', { className: 'ck-btn', type: 'button', 'aria-label': 'Review ' + r.qid }, ['Review']);
    reviewBtn.addEventListener('click', () => { reviewBtn.replaceWith(renderQuestion(q)); });
    inner.appendChild(el('div', { className: 'ck-actions' }, [reviewBtn]));
    row.appendChild(inner);
    return row;
  }

  // The item's tools: its answers sheet, a full-round deliberation, and the ready signal.
  // A button that shows and hides a form. Which ones are open is remembered by key,
  // so a re-render (Refresh, a send) redraws them open (PR #170 review, LOW).
  function disclosure(label, key, build) {
    const btn = el('button', { className: 'ck-btn', type: 'button', 'aria-expanded': 'false' }, [label]);
    const slot = el('div');
    const show = open => {
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      slot.textContent = '';
      if (open) slot.appendChild(build());
    };
    btn.addEventListener('click', () => {
      const open = !openForms.has(key);
      if (open) openForms.add(key); else openForms.delete(key);
      show(open);
    });
    if (openForms.has(key)) show(true);
    return [btn, slot];
  }

  function renderItemTools(itemId) {
    const wrap = el('div', { className: 'ck-tools' });
    const answersBtn = el('button', { className: 'ck-btn', type: 'button' }, ['Answers']);
    answersBtn.addEventListener('click', () => showSheet(itemId, null));
    const [forkBtn, slot] = disclosure('⑂ Deliberate (full round)', 'fork-' + itemId,
      () => renderForkForm(itemId, null));
    const [visBtn, visSlot] = disclosure('◫ Request a visual…', 'vis-' + itemId, () => renderVisualForm(itemId));
    visBtn.setAttribute('aria-label', 'Request a visual of ' + itemId);
    wrap.appendChild(el('div', { className: 'ck-actions' }, [answersBtn, forkBtn, visBtn]));
    wrap.appendChild(slot);
    wrap.appendChild(visSlot);
    // Q36: while the send bar is up it is the one way to send; two controls for one signal would only differ.
    if (!sendCounts(itemId).answered) wrap.appendChild(renderReadyButton(itemId, answersSheet(itemId, null).counts));
    return wrap;
  }

  // CONSOLE-kit/Q36 (owner, 2026-10-02, the ★): once an answer is in, a bar at the top of the item says how
  // many are answered and how many are left, with "Send to agent": the same 'process' message as the button
  // it stands in for. Everything it shows comes from the view: "answered" is each question of the item whose
  // newest owner answer came after the item's newest 'process' message, so the bar goes once that is sent,
  // here or from another tab, and comes back with the next answer. It is the panel's own row, under the
  // header, so it scrolls with nothing and a host's sticky header (stacked at 100) never covers the panel
  // (1000, docked 997), the way the kit-version chip sits above it too.
  function sendCounts(itemId) {
    const out = { answered: 0, left: 0 };
    if (!view) return out;
    const sent = (view.threads[itemId] || []).filter(m => m.by === 'owner' && m.intent === 'process')
      .reduce((s, m) => Math.max(s, m.seq || 0), 0);
    for (const q of Object.values(view.questions)) {
      if (q.question.item !== itemId) continue;
      if (q.state === 'awaiting_you') { out.left += 1; continue; }
      const owners = (q.answers || []).filter(a => a.by === 'owner');
      const head = owners.length ? owners[owners.length - 1] : null;
      if (head && (head.seq || 0) > sent) out.answered += 1;
    }
    return out;
  }

  let sendingItem = null;   // the item whose 'process' message is in flight

  function renderSendBar(itemId) {
    const c = sendCounts(itemId);
    if (!c.answered) { sendBarShown.delete(itemId); return null; }
    const words = c.answered + ' answered · ' + c.left + ' left';
    const bar = el('div', { className: 'ck-send-bar', role: 'region', 'aria-label': 'Answers to send' }, [
      el('span', { className: 'ck-send-bar-count' }, [words])]);
    const btn = el('button', { className: 'ck-btn ck-btn-primary ck-send-bar-go', type: 'button',
      dataFocusKey: 'send-' + itemId,
      'aria-label': 'Send to agent: ' + words + '. An open session watching starts now; otherwise the next one does.'
    }, [sendingItem === itemId ? 'Sending…' : 'Send to agent']);
    btn.disabled = sendingItem === itemId;
    btn.addEventListener('click', async () => {
      sendingItem = itemId;
      btn.disabled = true;
      btn.textContent = 'Sending…';
      // The same message, body and nonce key as "Answers are in: process them".
      const result = await apiPost('/message',
        { item: itemId, text: 'Answers are in: process them.', intent: 'process' }, 'ready-' + itemId);
      sendingItem = null;
      if (result.error) {
        announce('Not sent: ' + result.error);
        btn.disabled = false;
        btn.textContent = 'Send to agent';
        return;
      }
      announce('Sent to the agent: ' + words + '.');
      redrawKeepingPlace();
      // The bar is gone with the button that had focus: land on the item's questions.
      if (!panelEl.contains(document.activeElement) || document.activeElement.classList.contains('ck-close-btn')) {
        const h = panelEl.querySelector('.ck-questions-heading') || panelEl.querySelector('.ck-close-btn');
        if (h) { if (!h.hasAttribute('tabindex') && h.tagName !== 'BUTTON') h.setAttribute('tabindex', '-1'); h.focus(); }
      }
    });
    bar.appendChild(btn);
    if (!sendBarShown.has(itemId)) { sendBarShown.add(itemId); bar.classList.add('ck-send-bar-in'); }
    return bar;
  }
  const sendBarShown = new Set();   // items whose bar has already appeared: the slide-in runs once per showing

  // "Answers are in: process them" (§7.3): one owner message, intent 'process'.
  function renderReadyButton(itemId, counts) {
    const wrap = el('div', { className: 'ck-ready' });
    const btn = el('button', { className: 'ck-btn ck-btn-primary', type: 'button' }, ['Answers are in: process them']);
    const open = counts.awaiting_you;
    const note = el('p', { className: 'ck-muted' }, [
      (open ? open + ' question' + (open === 1 ? ' is' : 's are') + ' still unanswered; you can send it anyway. ' : '') +
      'An open Claude Code session that is watching starts now; otherwise the next session processes these.']);
    btn.addEventListener('click', async () => {
      btn.disabled = true; // a double press in flight reuses one nonce, so it writes one signal
      const result = await apiPost('/message',
        { item: itemId, text: 'Answers are in: process them.', intent: 'process' }, 'ready-' + itemId);
      btn.disabled = false;
      if (result.error) announce('Error: ' + result.error);
      else { announce('Signal sent: the agent will process these answers.'); renderPanel(); }
    });
    wrap.appendChild(btn);
    wrap.appendChild(note);
    return wrap;
  }

  // The seat picker a follow-up uses (D13): 1 to 3 roster seats, or a typed
  // "other" seat sent as other:<role>. roles() returns the list, or null after
  // saying (visibly and to a screen reader) what is wrong with the pick.
  function seatPicker(key, errEl, withRoar, onCount) {
    const picked = new Set();
    const fs = el('fieldset', { className: 'ck-roster' }, [el('legend', {}, ['Seats (1 to 3)'])]);
    const boxes = [];
    const otherInput = el('input', { type: 'text', className: 'ck-input', maxlength: '40', id: key + '-other',
      placeholder: 'e.g. Legal, Lighting designer' });
    // Roar (0.8.0): a three-round panel of its own, so it is picked alone. At most once per
    // question: the server refuses a second, naming the first, and the refusal is shown here.
    const roarBox = withRoar ? el('input', { type: 'checkbox', value: ROAR, name: key + '-seat' }) : null;
    const saved = draftSeats[key] || (draftSeats[key] = { seats: [], other: '', roar: false });
    for (const r of saved.seats) picked.add(r);
    otherInput.value = saved.other;
    if (roarBox) roarBox.checked = saved.roar;
    const sync = () => {
      saved.seats = ROSTER.filter(r => picked.has(r));
      saved.other = otherInput.value;
      saved.roar = !!(roarBox && roarBox.checked);
      if (errEl) errEl.textContent = '';  // a changed pick clears the last complaint about it
      const roar = !!(roarBox && roarBox.checked);
      const full = picked.size + (otherInput.value.trim() ? 1 : 0) >= MAX_ROLES;
      for (const b of boxes) b.disabled = roar || (full && !b.checked);
      otherInput.disabled = roar;
      if (roarBox) roarBox.disabled = !roar && (picked.size > 0 || !!otherInput.value.trim());
      if (onCount) onCount(roar ? 1 : picked.size + (otherInput.value.trim() ? 1 : 0));
    };
    for (const r of ROSTER) {
      const b = el('input', { type: 'checkbox', value: r, name: key + '-seat' });
      b.checked = picked.has(r);
      b.addEventListener('change', () => { if (b.checked) picked.add(r); else picked.delete(r); sync(); });
      boxes.push(b);
      fs.appendChild(el('label', { className: 'ck-option' }, [b, ' ' + ROSTER_LABEL[r]]));
    }
    if (roarBox) {
      roarBox.addEventListener('change', sync);
      fs.appendChild(el('label', { className: 'ck-option ck-roar-option' }, [roarBox,
        ' Roar: a three-round panel (independent reads, deliberation, synthesis); alone, once per lock']));
    }
    otherInput.addEventListener('input', sync);
    fs.appendChild(el('label', { for: key + '-other', className: 'ck-field-label' }, ['Other seat (optional)']));
    fs.appendChild(otherInput);
    sync();
    if (errEl) errEl.textContent = '';
    const fail = msg => { if (errEl) errEl.textContent = msg; announce(msg); return null; };
    const roles = () => {
      if (roarBox && roarBox.checked) return [ROAR];
      const out = ROSTER.filter(r => picked.has(r));
      const other = otherInput.value.trim();
      if (other) {
        if (!OTHER_ROLE.test(other)) return fail('The other seat takes letters, digits, spaces and hyphens, up to 40.');
        out.push('other:' + other);
      }
      if (out.length < 1 || out.length > MAX_ROLES) return fail('Pick 1 to 3 seats.');
      return out;
    };
    return { fieldset: fs, roles };
  }

  // Follow up on ONE locked answer (owner, 2026-09-29: "a button next to each
  // locked answer"). One owner message: intent 'fork', about_qid naming the
  // question, the seats picked here. The server checks the question is real,
  // in scope and locked, and the seats; this form only shapes the request.
  function renderAnswerFollowUp(q) {
    const qData = q.question;
    const key = 'ask-' + qData.qid;
    const id = key.replace(/[^A-Za-z0-9_-]/g, '-');
    const form = el('div', { className: 'ck-fork-form ck-followup', role: 'group', 'aria-labelledby': id + '-h' });
    form.appendChild(el('div', { className: 'ck-confirm-heading', id: id + '-h' }, ['Follow up on this answer']));
    form.appendChild(el('p', { className: 'ck-muted' }, [
      'The seats you pick look at your locked answer to ' + qData.qid + ' and bring any follow-up questions back ' +
      'here, on ' + qData.item + '. Your locked answer stays as it is.']));
    const err = el('p', { className: 'ck-error-msg', role: 'status', 'aria-live': 'polite' });
    const seats = seatPicker(id, err, true);
    form.appendChild(seats.fieldset);

    const modeName = id + '-mode';
    const modeFs = el('fieldset', { className: 'ck-roster' }, [el('legend', {}, ['Mode'])]);
    for (const m of ['tighten', 'explore']) {  // tighten first and default: the question is already answered
      const r = el('input', { type: 'radio', name: modeName, value: m });
      r.checked = (draftModes[key] || 'tighten') === m;
      r.addEventListener('change', () => { if (r.checked) draftModes[key] = m; });
      modeFs.appendChild(el('label', { className: 'ck-option' }, [r, m === 'tighten'
        ? ' Tighten: test the answer and find what it leaves loose' : ' Explore: widen the options around it']));
    }
    form.appendChild(modeFs);

    const noteId = id + '-note';
    const text = el('textarea', { className: 'ck-textarea', rows: '2', id: noteId,
      placeholder: 'e.g. Does this hold for the tour rig?' });
    if (draftTexts[key] !== undefined) text.value = draftTexts[key];
    text.addEventListener('input', () => { draftTexts[key] = text.value; });
    form.appendChild(el('label', { for: noteId, className: 'ck-field-label' }, ['Note for the seats (optional)']));
    form.appendChild(text);
    form.appendChild(err);

    const send = el('button', { className: 'ck-btn ck-btn-primary', type: 'button',
      'aria-label': 'Send follow-up: ' + truncateText(qData.text, 40) }, ['Send']);
    const cancel = el('button', { className: 'ck-btn', type: 'button' }, ['Cancel']);
    cancel.addEventListener('click', () => { openForms.delete(key); renderPanel(); });
    send.addEventListener('click', async () => {
      err.textContent = '';
      const roles = seats.roles();
      if (!roles) return;
      const mode = form.querySelector('input[name="' + modeName + '"]:checked').value;
      const body = { item: qData.item, intent: 'fork', mode: mode, about_qid: qData.qid, roles: roles,
        text: text.value.trim() || ('Follow up on the locked answer to ' + qData.qid + ' with ' +
          roles.map(r => ROSTER_LABEL[r] || r.replace(/^other:/, '')).join(', ') + '.') };
      send.disabled = true;
      const result = await apiPost('/message', body, key);
      send.disabled = false;
      if (result.error) {
        err.textContent = 'Not sent: ' + result.error;
        announce('Error: ' + result.error);
      } else {
        delete draftTexts[key];
        delete draftSeats[id];
        delete draftModes[key];
        openForms.delete(key);
        announce('Follow-up requested on ' + qData.qid + '.');
        renderPanel();
      }
    });
    form.appendChild(el('div', { className: 'ck-actions' }, [send, cancel]));
    return form;
  }

  // The latest fork about one question (a follow-up, a roar, a refine or drill, or a
  // deliberation before answering), labelled by the fork's OWN kind (view.py fork_kind, fixed
  // when it was asked), and whether its final result is back: the server's `done`, the one
  // rule the skills use too (a reply counts only when its first line starts "Result:").
  function askedLine(qid) {
    const asked = Object.values(view.forks).filter(f => f.message.about_qid === qid)
      .sort((a, b) => b.message.seq - a.message.seq);
    if (!asked.length) return null;
    const f = asked[0];
    const who = (f.message.roles || []).map(r => ROSTER_LABEL[r] || r.replace(/^other:/, '')).join(', ');
    const what = STEP_WORDS[f.kind] ? STEP_WORDS[f.kind].title
      : (f.kind === 'open' ? 'Deliberation before answering with ' : 'Follow-up with ') + who;
    const n = f.questions.length;
    const qs = n + ' question' + (n === 1 ? '' : 's') + ' (' + f.questions.join(', ') + ')';
    let back;
    if (f.done && f.kind === 'open') back = 'back: ' + String(f.result.text).trim().split('\n')[0];
    else if (f.done) back = n ? qs + ' back.' : 'back: ' + String(f.result.text).trim().split('\n')[0];
    else back = n ? qs + ' so far; waiting for the result.' : 'waiting for the seats.';
    return el('p', { className: 'ck-muted ck-asked', dataFork: f.message.id,
      dataDone: f.done ? 'true' : 'false' }, ['⑂ ' + what + ' ' + relTime(f.message.ts) + ': ' + back]);
  }

  // "Deliberate before answering" (owner ruling build_reply): the button under an OPEN
  // question, and the line saying a deliberation on it is waiting or back.
  function deliberateOpen(q) {
    const qData = q.question;
    const wrap = el('div', { className: 'ck-deliberate-open' });
    const [btn, slot] = disclosure('⑂ Deliberate before answering…', 'delib-' + qData.qid,
      () => renderOpenDeliberation(q));
    btn.setAttribute('aria-label', 'Deliberate before answering: ' + truncateText(qData.text, 40));
    btn.setAttribute('data-step', 'deliberate');
    wrap.appendChild(el('div', { className: 'ck-actions' }, [btn]));
    wrap.appendChild(slot);
    const asked = askedLine(qData.qid);
    if (asked) wrap.appendChild(asked);
    return wrap;
  }

  // The cost of a deliberation, before it is sent: seats × ~100k tokens each, as measured.
  function seatCost(n) {
    if (!n) return 'Cost: about ' + SEAT_TOKENS_K + 'k tokens per seat. Pick 1 to 3 seats.';
    return 'Cost: ≈ ' + n + ' seat' + (n === 1 ? '' : 's') + ' × ~' + SEAT_TOKENS_K + 'k tokens = ~' +
      (n * SEAT_TOKENS_K) + 'k tokens.';
  }

  // Deliberate on ONE OPEN question before answering it. One owner message: intent 'fork',
  // about_qid naming the question, the seats picked here. The seats reply on the question
  // with a recommendation (and, only if they show the options are wrong, a replacement
  // question). The answer and the lock stay the owner's: this form writes neither, and the
  // server refuses a roar, refine or drill here, since those work on a locked answer.
  function renderOpenDeliberation(q) {
    const qData = q.question;
    const key = 'delib-' + qData.qid;
    const id = key.replace(/[^A-Za-z0-9_-]/g, '-');
    const form = el('div', { className: 'ck-fork-form ck-deliberate', role: 'group', 'aria-labelledby': id + '-h' });
    form.appendChild(el('div', { className: 'ck-confirm-heading', id: id + '-h' }, ['Deliberate before answering']));
    form.appendChild(el('p', { className: 'ck-muted' }, [
      'The seats you pick weigh the options of ' + qData.qid + ' and reply here with a recommendation and their ' +
      'reasons. If they find the options themselves are wrong, they ask a replacement question instead. ' +
      'Answering and locking stay yours.']));
    const err = el('p', { className: 'ck-error-msg', role: 'status', 'aria-live': 'polite' });
    const cost = el('p', { className: 'ck-cost', id: id + '-cost', 'aria-live': 'polite' }, [seatCost(0)]);
    const seats = seatPicker(id, err, false, n => { cost.textContent = seatCost(n); });
    form.appendChild(seats.fieldset);

    const modeName = id + '-mode';
    const modeFs = el('fieldset', { className: 'ck-roster' }, [el('legend', {}, ['Mode'])]);
    for (const m of ['explore', 'tighten']) {  // explore first and default: nothing is decided yet
      const r = el('input', { type: 'radio', name: modeName, value: m });
      r.checked = (draftModes[key] || 'explore') === m;
      r.addEventListener('change', () => { if (r.checked) draftModes[key] = m; });
      modeFs.appendChild(el('label', { className: 'ck-option' }, [r, m === 'explore'
        ? ' Explore: weigh every option, and say if one is missing' : ' Tighten: narrow to one recommendation']));
    }
    form.appendChild(modeFs);

    const noteId = id + '-note';
    const text = el('textarea', { className: 'ck-textarea', rows: '2', id: noteId,
      placeholder: 'e.g. Which option survives a second site?' });
    if (draftTexts[key] !== undefined) text.value = draftTexts[key];
    text.addEventListener('input', () => { draftTexts[key] = text.value; });
    form.appendChild(el('label', { for: noteId, className: 'ck-field-label' }, ['Note for the seats (optional)']));
    form.appendChild(text);
    form.appendChild(cost);
    form.appendChild(err);

    const send = el('button', { className: 'ck-btn ck-btn-primary', type: 'button',
      'aria-describedby': id + '-cost',
      'aria-label': 'Send deliberation before answering: ' + truncateText(qData.text, 40) }, ['Send']);
    const cancel = el('button', { className: 'ck-btn', type: 'button' }, ['Cancel']);
    cancel.addEventListener('click', () => { openForms.delete(key); renderPanel(); });
    send.addEventListener('click', async () => {
      err.textContent = '';
      const roles = seats.roles();
      if (!roles) return;
      const mode = form.querySelector('input[name="' + modeName + '"]:checked').value;
      const body = { item: qData.item, intent: 'fork', mode: mode, about_qid: qData.qid, roles: roles,
        text: text.value.trim() || ('Deliberate on the open question ' + qData.qid + ' before I answer it, with ' +
          roles.map(r => ROSTER_LABEL[r] || r.replace(/^other:/, '')).join(', ') + '.') };
      send.disabled = true;
      const result = await apiPost('/message', body, key);
      send.disabled = false;
      if (result.error) {
        err.textContent = 'Not sent: ' + result.error;
        announce('Error: ' + result.error);
      } else {
        delete draftTexts[key];
        delete draftSeats[id];
        delete draftModes[key];
        openForms.delete(key);
        announce('Deliberation requested on ' + qData.qid + '.');
        renderPanel();
      }
    });
    form.appendChild(el('div', { className: 'ck-actions' }, [send, cancel]));
    return form;
  }

  // A deliberation request (§6, D13, D14). followUp is the fork being followed, or null for a first round.
  function renderForkForm(itemId, followUp) {
    const key = 'fork-' + itemId + (followUp ? '-' + followUp : '');
    const form = el('div', { className: 'ck-fork-form' });
    form.appendChild(el('p', { className: 'ck-muted' }, [followUp
      ? 'Pick 1 to 3 seats for a second round on this deliberation.'
      : 'The default committee (architect, UX, security, and the project\'s own audit) deliberates on ' +
        itemId + ' and everything under it, and brings its questions back here.']));

    const seats = followUp ? seatPicker(key) : null;
    if (seats) form.appendChild(seats.fieldset);

    const focusSel = el('select', { className: 'ck-select', id: key + '-focus' });
    for (const f of FOCUSES) focusSel.appendChild(el('option', { value: f }, [f === 'whole' ? 'the whole thing' : f]));
    form.appendChild(el('div', { className: 'ck-field' }, [
      el('label', { for: key + '-focus', className: 'ck-field-label' }, ['Focus']), focusSel]));

    const modeName = key + '-mode';
    const modeFs = el('fieldset', { className: 'ck-roster' }, [el('legend', {}, ['Mode'])]);
    MODES.forEach((m, i) => {
      const r = el('input', { type: 'radio', name: modeName, value: m });
      if (i === 0) r.checked = true;
      modeFs.appendChild(el('label', { className: 'ck-option' }, [r, m === 'explore'
        ? ' Explore: widen the options' : ' Tighten: narrow to a decision']));
    });
    form.appendChild(modeFs);

    const text = el('textarea', { className: 'ck-textarea', rows: '2', 'aria-label': 'What should they look at (optional)',
      placeholder: 'What should they look at? (optional)' });
    if (draftTexts[key] !== undefined) text.value = draftTexts[key];
    text.addEventListener('input', () => { draftTexts[key] = text.value; });
    form.appendChild(text);

    const send = el('button', { className: 'ck-btn ck-btn-primary', type: 'button' }, [followUp ? 'Start the follow-up' : 'Start the deliberation']);
    send.addEventListener('click', async () => {
      const mode = form.querySelector('input[name="' + modeName + '"]:checked').value;
      const body = { item: itemId, intent: 'fork', mode: mode, focus: focusSel.value };
      if (followUp) {
        const roles = seats.roles();
        if (!roles) return;
        body.follow_up_of = followUp;
        body.roles = roles;
      }
      body.text = text.value.trim() || (followUp
        ? 'Follow-up round with ' + body.roles.map(r => r.replace(/^other:/, '')).join(', ') + '.'
        : 'Deliberate the full round: ' + itemId + ' and everything under it.');
      send.disabled = true;
      const result = await apiPost('/message', body, key);
      send.disabled = false;
      if (result.error) announce('Error: ' + result.error);
      else {
        delete draftTexts[key];
        delete draftSeats[key];
        openForms.delete(followUp ? 'fu-' + followUp : 'fork-' + itemId);  // sent, so the form closes
        announce('Deliberation requested.');
        renderPanel();
      }
    });
    form.appendChild(el('div', { className: 'ck-actions' }, [send]));
    return form;
  }

  // This item's deliberations, newest first, each with its questions and, once answered, a follow-up box.
  function renderForks(itemId) {
    const wrap = el('div');
    const mine = Object.values(view.forks).filter(f => f.message.item === itemId)
      .sort((a, b) => b.message.seq - a.message.seq);
    if (!mine.length) return wrap;
    wrap.appendChild(el('div', { className: 'ck-section-heading' }, ['Deliberations']));
    for (const f of mine) {
      const m = f.message;
      const card = el('div', { className: 'ck-fork' });
      const bits = ['⑂ ' + m.mode, m.focus || 'whole'];
      if (m.step) bits.push(STEP_WORDS[m.step] ? STEP_WORDS[m.step].title : m.step);
      else if (m.roles) bits.push(m.roles.map(r => ROSTER_LABEL[r] || r.replace(/^other:/, '')).join(', '));
      else bits.push('default committee');
      card.appendChild(el('div', { className: 'ck-fork-head' }, [bits.join(' · ') + ' · ' + relTime(m.ts)]));
      const chips = tagChips(forkTags(m.id));
      if (chips) card.appendChild(chips);
      const t = el('div', { className: 'ck-message-text' });
      t.textContent = m.text;
      card.appendChild(t);
      const tr = transcriptBlock(m.id);
      if (tr) card.appendChild(tr);
      if (m.about_qid) card.appendChild(el('div', { className: 'ck-muted' }, ['Follow-up on the locked answer to ' + m.about_qid]));
      if (m.follow_up_of) card.appendChild(el('div', { className: 'ck-muted' }, ['Follow-up of ' + m.follow_up_of.slice(0, 8)]));
      const qs = f.questions.map(qid => view.questions[qid]).filter(Boolean);
      if (!qs.length) {
        card.appendChild(el('p', { className: 'ck-muted' }, ['Waiting for the committee\'s questions.']));
      } else {
        const list = el('ul', { className: 'ck-fork-qs' });
        for (const q of qs) list.appendChild(el('li', {}, [GLYPH[q.state] + ' ' + q.question.qid + ' · ' + STATE_WORDS[q.state]]));
        card.appendChild(list);
        const answersBtn = el('button', { className: 'ck-btn', type: 'button' }, ['Answers from this round']);
        answersBtn.addEventListener('click', () => showSheet(itemId, m.id));
        const actions = el('div', { className: 'ck-actions' }, [answersBtn]);
        if (qs.some(q => q.state === 'awaiting_you' || q.state === 'unlocked')) {
          const formBtn = el('button', { className: 'ck-btn ck-btn-primary', type: 'button' }, ['Answer this round']);
          formBtn.addEventListener('click', () => openRound(m.id));
          actions.insertBefore(formBtn, answersBtn);
        }
        if (qs.every(q => q.state !== 'awaiting_you')) {
          // "Next step ▾" on the round's answer set (0.8.0): a follow-up round, refine or drill.
          const target = { item: itemId, follow_up_of: m.id };
          const [nextBtn, nextPanel] = nextStepMenu('next-' + m.id, 'this round\'s answers', [
            { id: 'follow', key: 'fu-' + m.id, label: 'Follow up with other seats…',
              aria: 'Follow up this round with other seats', build: () => renderForkForm(itemId, m.id) },
            { id: 'refine', key: 'refine-' + m.id, label: 'Refine…', aria: 'Refine from this round\'s answers',
              build: () => renderStepForm(target, 'refine', 'refine-' + m.id) },
            { id: 'drill', key: 'drill-' + m.id, label: 'Drill…', aria: 'Drill from this round\'s answers',
              build: () => renderStepForm(target, 'drill', 'drill-' + m.id) }
          ]);
          actions.appendChild(nextBtn);
          card.appendChild(actions);
          card.appendChild(nextPanel);
        } else {
          card.appendChild(actions);
        }
      }
      wrap.appendChild(card);
    }
    return wrap;
  }

  // ---------------------------------------------------------------------------
  // The review-round form (0.7.0; owner, 2026-09-29 21:00): a round's questions
  // (every question sharing one `forked_from`) open as one form, a question per
  // step. Picks and comments are DRAFTS, kept in this browser, until the review
  // page's one "Lock all & process". The server answers and locks each, then
  // sends one process request; if it would refuse any, it writes none.
  // ---------------------------------------------------------------------------

  const OPEN_STATES = ['awaiting_you', 'unlocked'];
  const roundMem = {};  // forkId -> { drafts: {qid: {picks, text}}, step, review, nonce, failure, result }

  function qNum(qid) { return parseInt(qid.split('/Q').pop(), 10); }

  // Every question of the round, in qid order, and the ones the form walks (not yet locked).
  function roundQuestions(forkId) {
    if (!view) return [];
    const f = view.forks[forkId];
    const qids = isLoose(forkId) ? looseQids(forkId) : f ? f.questions : [];   // Q35: a loose group is a round too
    return qids.map(qid => view.questions[qid]).filter(Boolean)
      .sort((a, b) => a.question.item.localeCompare(b.question.item) || qNum(a.question.qid) - qNum(b.question.qid));
  }
  function roundSteps(forkId) { return roundQuestions(forkId).filter(q => OPEN_STATES.includes(q.state)); }

  function roundFor(forkId) {
    if (!roundMem[forkId]) {
      const saved = memGet('round:' + forkId);
      roundMem[forkId] = {
        drafts: (saved && typeof saved.drafts === 'object' && saved.drafts) || {},
        step: (saved && Number.isInteger(saved.step)) ? saved.step : 0,
        review: !!(saved && saved.review),
        nonce: (saved && typeof saved.nonce === 'string') ? saved.nonce : null,
        failure: null, result: null
      };
    }
    const mem = roundMem[forkId];
    // An answered-but-unlocked question starts from the owner's current answer, so
    // "Lock all" locks what they already said unless they change it here.
    for (const q of roundSteps(forkId)) {
      const qid = q.question.qid;
      if (mem.drafts[qid] || q.state !== 'unlocked') continue;
      const head = q.answers[q.answers.length - 1];
      mem.drafts[qid] = { picks: [...head.picks], text: head.own_text || '' };
    }
    return mem;
  }
  function saveRound(forkId) {
    const mem = roundMem[forkId];
    if (!mem) return;
    memSet('round:' + forkId, { drafts: mem.drafts, step: mem.step, review: mem.review, nonce: mem.nonce });
  }
  function drafted(d) { return !!d && ((d.picks && d.picks.length > 0) || !!(d.text && d.text.trim())); }
  function draftedCount(forkId) {
    const mem = roundFor(forkId);
    return roundSteps(forkId).filter(q => drafted(mem.drafts[q.question.qid])).length;
  }

  function openRound(forkId) {
    currentFork = forkId;
    currentMode = 'round';
    const mem = roundFor(forkId);
    mem.result = null;
    if (panelEl.getAttribute('data-open') !== 'true') openPanel(null, 'round');
    else renderPanel();
    const h = panelEl.querySelector('.ck-round-title');
    if (h) h.focus();
    return mem;
  }

  function leaveRound() {
    cancelAdvance();
    currentMode = 'inbox';
    currentTab = 'inbox';
    renderPanel();
  }

  // A round's line in the Inbox: what it is, how far the drafts got, and the way in.
  function renderRoundCard(forkId) {
    const m = view.forks[forkId].message;
    const steps = roundSteps(forkId);
    const done = draftedCount(forkId);
    const isNew = steps.some(q => arrived.has(q.question.qid));
    const btn = el('button', { className: 'ck-btn ck-btn-primary', type: 'button',
      'aria-label': 'Answer this round on ' + m.item + ': ' + steps.length + ' questions, ' + done + ' drafted' },
    [done ? 'Continue this round' : 'Answer this round']);
    btn.addEventListener('click', () => openRound(forkId));
    const who = m.roles ? m.roles.map(r => ROSTER_LABEL[r] || r.replace(/^other:/, '')).join(', ') : 'committee';
    return el('div', { className: 'ck-round-card' + (isNew ? ' ck-arrived' : ''), dataFork: forkId }, [
      ring(done, steps.length),
      el('div', { className: 'ck-round-card-text' }, [
        el('div', { className: 'ck-round-card-head' }, ['⑂ ' + m.item + ' · ' + m.mode + ' · ' + who]),
        el('div', { className: 'ck-muted' }, [steps.length + ' question' + (steps.length === 1 ? '' : 's') +
          ' · ' + done + ' drafted · asked ' + relTime(m.ts)])
      ]),
      btn
    ]);
  }

  function renderRound(forkId) {
    const loose = isLoose(forkId);
    const f = view && !loose && view.forks[forkId];
    const m = f ? f.message : null;
    const first = loose ? roundQuestions(forkId)[0] : null;
    const title = el('span', { className: 'ck-round-title', tabindex: '-1' },
      [m ? 'Round: ' + m.item + ' · ' + m.mode
        : first ? 'Together: ' + first.question.item + ' · from ' + (first.question.agent || 'an agent') : 'Round']);
    panelEl.appendChild(makeHeader([title], leaveRound, 'Back to inbox'));
    panelEl.appendChild(renderStatusBar(null));
    const body = el('div', { className: 'ck-body ck-round' });
    panelEl.appendChild(body);
    if (!view) { body.appendChild(renderOffline()); return; }
    if (!f && !first) { body.appendChild(el('p', {}, ['This round is not in the console any more.'])); return; }
    const mem = roundFor(forkId);
    if (mem.result) return renderRoundResult(body, forkId, mem);
    const steps = roundSteps(forkId);
    if (mem.review || !steps.length) return renderRoundReview(body, forkId, mem);
    mem.step = Math.max(0, Math.min(mem.step, steps.length - 1));
    renderRoundStep(body, forkId, mem, steps);
  }

  function roundProgress(forkId, mem, steps) {
    const done = steps.filter(q => drafted(mem.drafts[q.question.qid])).length;
    const wrap = el('div', { className: 'ck-round-progress' });
    wrap.appendChild(ring(done, steps.length));
    wrap.appendChild(el('span', { className: 'ck-round-count' }, [
      'Question ' + (mem.step + 1) + ' of ' + steps.length + ' · ' + done + ' picked']));
    const bar = el('div', { className: 'ck-progress', role: 'progressbar', 'aria-label': 'Question ' +
      (mem.step + 1) + ' of ' + steps.length, 'aria-valuemin': '1', 'aria-valuemax': String(steps.length),
      'aria-valuenow': String(mem.step + 1) });
    bar.appendChild(el('div', { className: 'ck-progress-fill',
      style: 'width: ' + Math.round(100 * (mem.step + 1) / steps.length) + '%' }));
    wrap.appendChild(bar);
    const dots = el('div', { className: 'ck-round-dots' });
    steps.forEach((q, i) => {
      const d = mem.drafts[q.question.qid];
      const state = i === mem.step ? 'current' : drafted(d) ? 'picked' : 'open';
      const b = el('button', { className: 'ck-round-dot', type: 'button', dataState: state,
        'aria-label': 'Question ' + (i + 1) + ', ' + q.question.qid + ': ' + (drafted(d) ? 'picked' : 'not picked yet'),
        'aria-current': i === mem.step ? 'step' : 'false' }, [String(i + 1)]);
      b.addEventListener('click', () => { mem.step = i; saveRound(forkId); renderPanel(); focusRoundStep(); });
      dots.appendChild(b);
    });
    wrap.appendChild(dots);
    return wrap;
  }

  function focusRoundStep() {
    requestAnimationFrame(() => {
      const h = panelEl.querySelector('.ck-round-qtext');
      if (h) h.focus();
    });
  }

  // CONSOLE-kit/Q34 (owner, 2026-10-02, the ★): in a round, a single-choice pick moves to the next question
  // still without a pick, ADVANCE_MS later, and says so through the live region. Multi-choice, free answers
  // and a question being commented on never move by themselves; nothing moves when no question is left
  // without a pick (the review page, with its Lock button, is only ever reached by the owner's own press).
  const ADVANCE_MS = 400;
  let advanceTimer = null;
  // An ↑/↓ is held on a round option: the change it makes is a walk through the options, not a choice. Set by
  // the keydown, read and cleared by the change it causes, and cleared by the keyup, so no timing is guessed.
  let arrowWalk = false;

  function cancelAdvance() {
    if (advanceTimer) { clearTimeout(advanceTimer); advanceTimer = null; }
  }

  function scheduleAdvance(forkId, qid) {
    cancelAdvance();
    advanceTimer = setTimeout(() => {
      advanceTimer = null;
      if (currentMode !== 'round' || currentFork !== forkId || panelEl.getAttribute('data-open') !== 'true') return;
      const mem = roundFor(forkId);
      const steps = roundSteps(forkId);
      if (mem.review || mem.result || !steps[mem.step] || steps[mem.step].question.qid !== qid) return;
      if (!drafted(mem.drafts[qid])) return;   // the pick was cleared in the meantime
      const after = steps.map((q, i) => i).filter(i => i !== mem.step && !drafted(mem.drafts[steps[i].question.qid]));
      const next = after.find(i => i > mem.step) ?? after[0];
      if (next === undefined) {
        announce('Every question has a pick. Review when you are ready.');
        return;
      }
      mem.step = next;
      saveRound(forkId);
      advancedTo = steps[next].question.qid;
      renderPanel();
      focusRoundStep();
      announce('Moved on to question ' + (next + 1) + ' of ' + steps.length + ': ' +
        truncateText(steps[next].question.text, 80));
    }, ADVANCE_MS);
  }
  let advancedTo = null;        // the step just reached by an auto-advance: its card slides in, once

  function roundGo(forkId, delta) {
    cancelAdvance();
    const mem = roundFor(forkId);
    const steps = roundSteps(forkId);
    const next = mem.step + delta;
    if (next >= steps.length) { mem.review = true; }
    else if (next >= 0) { mem.step = next; }
    else return;
    saveRound(forkId);
    renderPanel();
    if (mem.review) {
      requestAnimationFrame(() => { const h = panelEl.querySelector('.ck-review-heading'); if (h) h.focus(); });
    } else {
      focusRoundStep();
    }
  }

  function renderRoundStep(body, forkId, mem, steps) {
    const q = steps[mem.step];
    const qData = q.question;
    const draft = mem.drafts[qData.qid] || (mem.drafts[qData.qid] = { picks: [], text: '' });
    const tighten = !isLoose(forkId) && view.forks[forkId].message.mode === 'tighten';
    body.appendChild(roundProgress(forkId, mem, steps));
    const card = el('div', { className: 'ck-round-step', role: 'group', 'aria-labelledby': 'ck-round-qtext' });
    if (advancedTo === qData.qid) { advancedTo = null; card.classList.add('ck-advance-in'); }
    card.appendChild(el('div', { className: 'ck-q-header' }, [
      el('span', { className: 'ck-q-state', dataState: q.state }, [GLYPH[q.state] + ' ' + STATE_WORDS[q.state]]),
      el('span', { className: 'ck-inbox-item-id' }, [qData.qid])
    ]));
    const text = el('div', { className: 'ck-q-text ck-round-qtext', id: 'ck-round-qtext', tabindex: '-1' });
    text.textContent = qData.text;
    card.appendChild(text);
    if (qData.source) card.appendChild(el('div', { className: 'ck-q-source' }, [qData.source]));
    if (qData.agent) card.appendChild(el('div', { className: 'ck-q-agent' }, ['asked by ' + qData.agent]));
    if (tighten) {
      card.appendChild(el('p', { className: 'ck-muted' }, [
        'A tighten round: each finding is Fix now, Record in findings.md, or Leave it. Nothing happens on a pick ' +
        'alone; it acts once locked and folded.']));
    }

    const inputs = [];
    if (qData.kind !== 'free' && qData.options.length) {
      const multi = qData.kind === 'multi';
      const fs = el('fieldset', { className: 'ck-options ck-round-options' }, [
        el('legend', { className: 'ck-sr-only' }, [multi ? 'Pick any' : 'Pick one'])]);
      qData.options.forEach((opt, i) => {
        const input = el('input', { type: multi ? 'checkbox' : 'radio', name: 'ck-round-opt', value: opt.id });
        input.checked = draft.picks.includes(opt.id);  // a draft, never the ★ (R2)
        input.addEventListener('change', () => {
          if (multi) {
            draft.picks = qData.options.map(o => o.id).filter(id => {
              const box = fs.querySelector('input[value="' + id + '"]');
              return box && box.checked;
            });
          } else {
            draft.picks = input.checked ? [opt.id] : [];
          }
          saveRound(forkId);
          refreshRoundProgress(forkId, mem, steps);
          // Q34: a single-choice pick moves on by itself, unless an arrow key made it (↑/↓ walk the options,
          // and every step of that walk is a change) or the owner is commenting on this question.
          const byArrow = arrowWalk;
          arrowWalk = false;
          if (!multi && qData.kind === 'single' && input.checked && !byArrow && !(draft.text && draft.text.trim()) &&
              document.activeElement !== words) {
            scheduleAdvance(forkId, qData.qid);
          } else {
            cancelAdvance();
          }
        });
        input.addEventListener('keydown', e => {
          // ←/→ never reach a radio here (onRoundKey takes them to move between questions); ↑/↓ do.
          if (e.key === 'ArrowUp' || e.key === 'ArrowDown') arrowWalk = true;
        });
        input.addEventListener('keyup', () => { arrowWalk = false; });
        inputs.push(input);
        const content = el('div', { className: 'ck-option-content' }, [
          el('span', { className: 'ck-option-label' }, [
            i < 9 ? el('kbd', { className: 'ck-kbd', 'aria-hidden': 'true' }, [String(i + 1)]) : null,
            ' ' + opt.label])]);
        if (qData.star === opt.id) {
          content.appendChild(el('span', { className: 'ck-option-star' }, [
            '★ ' + (qData.star_by ? qData.star_by.replace(/^other:/, '') + "'s pick" : 'recommended')]));
        }
        if (opt.description) {
          const desc = el('div', { className: 'ck-option-desc' });
          desc.textContent = opt.description;
          content.appendChild(desc);
        }
        fs.appendChild(el('label', { className: 'ck-option' }, [input, content]));
      });
      card.appendChild(fs);
      const clear = el('button', { className: 'ck-btn ck-btn-quiet', type: 'button' }, ['Clear my pick']);
      clear.addEventListener('click', () => {
        cancelAdvance();
        draft.picks = [];
        for (const i of inputs) i.checked = false;
        saveRound(forkId);
        refreshRoundProgress(forkId, mem, steps);
      });
      card.appendChild(el('div', { className: 'ck-actions' }, [clear]));
    }

    const wordsId = 'ck-round-words';
    card.appendChild(el('label', { for: wordsId, className: 'ck-field-label ck-round-words-label' }, [
      qData.kind === 'free' ? 'Your answer' : 'Comment (optional): it becomes your answer\'s own words']));
    const words = el('textarea', { className: 'ck-textarea', id: wordsId, rows: '3', maxlength: '20000' });
    words.value = draft.text || '';
    words.addEventListener('input', () => {
      draft.text = words.value;
      cancelAdvance();   // a question being commented on never moves by itself
      saveRound(forkId);
      refreshRoundProgress(forkId, mem, steps);
    });
    words.addEventListener('focus', cancelAdvance);
    card.appendChild(words);
    card.appendChild(renderEvidence(qData));
    body.appendChild(card);

    const prev = el('button', { className: 'ck-btn', type: 'button' }, ['← Previous']);
    prev.disabled = mem.step === 0;
    prev.addEventListener('click', () => roundGo(forkId, -1));
    const last = mem.step === steps.length - 1;
    const next = el('button', { className: 'ck-btn ck-btn-primary', type: 'button' }, [last ? 'Review →' : 'Next →']);
    next.addEventListener('click', () => roundGo(forkId, +1));
    const review = el('button', { className: 'ck-btn ck-btn-quiet', type: 'button' }, ['Review all']);
    review.addEventListener('click', () => { mem.review = true; saveRound(forkId); renderPanel();
      requestAnimationFrame(() => { const h = panelEl.querySelector('.ck-review-heading'); if (h) h.focus(); }); });
    body.appendChild(el('div', { className: 'ck-actions ck-round-nav' }, [prev, next, review]));
    body.appendChild(el('p', { className: 'ck-muted ck-round-keys' }, [
      '← → move between questions · 1–' + Math.min(9, Math.max(1, qData.options.length)) +
      ' pick an option · nothing is locked until you press Lock all on the review page']));
  }

  // Keep the step's progress (ring, count, dots) in step with a pick, without redrawing the inputs.
  function refreshRoundProgress(forkId, mem, steps) {
    const old = panelEl.querySelector('.ck-round-progress');
    if (old) old.replaceWith(roundProgress(forkId, mem, steps));
  }

  // ←/→ walk the round and 1-9 pick an option, except while typing (0.7.0).
  function onRoundKey(e) {
    if (currentMode !== 'round' || !currentFork || e.altKey || e.ctrlKey || e.metaKey) return;
    if (panelEl.getAttribute('data-open') !== 'true') return;
    const mem = roundMem[currentFork];
    if (!mem || mem.review || mem.result) return;
    const t = e.target;
    // In the panel, or nowhere in particular (a redraw can drop focus onto <body>); never the board's own controls.
    if (t !== document.body && t !== document.documentElement && !panelEl.contains(t)) return;
    const typing = t && (t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' ||
      (t.tagName === 'INPUT' && !['radio', 'checkbox'].includes(t.type)));
    if (typing) return;
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      e.preventDefault();  // on a radio this would move the pick; ↑/↓ still do that
      roundGo(currentFork, e.key === 'ArrowRight' ? +1 : -1);
    } else if (/^[1-9]$/.test(e.key)) {
      const boxes = panelEl.querySelectorAll('.ck-round-options input');
      const box = boxes[parseInt(e.key, 10) - 1];
      if (box) {
        e.preventDefault();
        box.checked = box.type === 'radio' ? true : !box.checked;
        box.dispatchEvent(new Event('change'));
        box.focus();
      }
    }
  }

  // Structured evidence (0.7.0): each row's citation, command and result, and the
  // cited lines as they are NOW, read by the server, marked unchanged or changed
  // since the question was asked. A question without evidence shows its text only.
  const evidenceCache = {};
  function fetchEvidence(qid) {
    if (!evidenceCache[qid]) {
      evidenceCache[qid] = fetch(config.api + '/evidence?qid=' + encodeURIComponent(qid), { credentials: 'same-origin' })
        .then(r => r.json().then(d => { if (!r.ok) throw new Error(d.error || 'HTTP ' + r.status); return d; }))
        .catch(e => { delete evidenceCache[qid]; return { error: e.message || 'Network error' }; });
    }
    return evidenceCache[qid];
  }
  const EVIDENCE_WORDS = { unchanged: 'unchanged', moved: 'unchanged, moved', changed: 'changed since asked',
    missing: 'file gone' };

  function renderEvidence(qData) {
    const box = el('section', { className: 'ck-evidence', 'aria-label': 'Evidence for ' + qData.qid });
    if (!qData.evidence || !qData.evidence.length) {
      box.appendChild(el('p', { className: 'ck-muted' }, [
        'No structured evidence on this question: it rests on its text above' +
        (qData.source ? ' and ' + qData.source : '') + '.']));
      return box;
    }
    box.appendChild(el('div', { className: 'ck-evidence-heading' }, ['Evidence']));
    const list = el('ul', { className: 'ck-evidence-list' });
    box.appendChild(list);
    list.appendChild(el('li', { className: 'ck-muted' }, ['Reading the cited lines…']));
    fetchEvidence(qData.qid).then(data => {
      list.textContent = '';
      const rows = data.error ? qData.evidence.map(r => ({ cite: r.cite, command: r.command, result: r.result,
        asked: r.text, state: null })) : data.evidence;
      if (data.error) box.insertBefore(el('p', { className: 'ck-error-msg' }, [
        'Could not read the lines as they are now: ' + data.error + '. Shown as asked.']), list);
      for (const r of rows) {
        const li = el('li', { className: 'ck-evidence-row', dataState: r.state || 'unknown' });
        const head = el('div', { className: 'ck-evidence-head' }, [el('code', {}, [r.cite])]);
        if (r.state) head.appendChild(el('span', { className: 'ck-evidence-state', dataState: r.state },
          [(r.state === 'unchanged' || r.state === 'moved' ? '✓ ' : '△ ') + EVIDENCE_WORDS[r.state]]));
        li.appendChild(head);
        if (r.command) {
          const c = el('pre', { className: 'ck-evidence-cmd' });
          c.textContent = '$ ' + r.command;
          li.appendChild(c);
        }
        if (r.result) {
          const c = el('pre', { className: 'ck-evidence-result', tabindex: '0' });
          c.textContent = r.result;
          li.appendChild(c);
        }
        if (r.words) li.appendChild(el('div', { className: 'ck-muted' }, [r.words]));
        const shown = r.state === 'changed' && r.diff ? r.diff : (r.now !== undefined ? r.now : r.asked);
        if (shown) {
          if (r.state === 'changed' && r.diff) {
            li.appendChild(el('div', { className: 'ck-muted' }, ['As asked (−) against the file now (+):']));
          }
          const pre = el('pre', { className: 'ck-evidence-lines', tabindex: '0' });
          pre.textContent = shown;
          li.appendChild(pre);
        }
        list.appendChild(li);
      }
    });
    return box;
  }

  // The review page: what is picked, commented and left, then one "Lock all & process".
  function renderRoundReview(body, forkId, mem) {
    const all = roundQuestions(forkId);
    const steps = roundSteps(forkId);
    body.appendChild(el('h2', { className: 'ck-review-heading', tabindex: '-1' }, ['Review this round']));
    const toLock = steps.filter(q => drafted(mem.drafts[q.question.qid]));
    const left = steps.length - toLock.length;
    body.appendChild(el('p', { className: 'ck-muted' }, [toLock.length + ' to lock, ' + left + ' left unanswered' +
      (all.length > steps.length ? ', ' + (all.length - steps.length) + ' already locked' : '') +
      '. Nothing is locked until you press the button below.']));
    if (mem.failure) body.appendChild(renderLockFailure(mem.failure));
    const refused = {};
    for (const r of (mem.failure && mem.failure.results) || []) if (r.qid) refused[r.qid] = r;
    const list = el('ol', { className: 'ck-review-list' });
    for (const q of all) {
      const qData = q.question;
      const labels = optionLabels(qData);
      const d = mem.drafts[qData.qid];
      const open = OPEN_STATES.includes(q.state);
      const status = !open ? 'locked' : drafted(d) ? 'picked' : 'left';
      const li = el('li', { className: 'ck-review-row', dataStatus: status }, [
        el('div', { className: 'ck-review-q' }, [el('span', { className: 'ck-inbox-item-id' }, [qData.qid]), ' ',
          truncateText(qData.text, 120)])]);
      if (status === 'locked') {
        li.appendChild(el('div', { className: 'ck-muted' }, ['Already ' + STATE_WORDS[q.state] + ': not changed here.']));
      } else if (status === 'picked') {
        li.appendChild(el('div', { className: 'ck-review-pick' }, ['✓ ' + (d.picks.map(p => labels[p] || p).join(', ') ||
          'your own words')]));
        if (d.text && d.text.trim()) {
          const w = el('div', { className: 'ck-receipt-own' });
          w.textContent = 'Your words: ' + d.text.trim();
          li.appendChild(w);
        }
      } else {
        li.appendChild(el('div', { className: 'ck-muted' }, ['Left: stays unanswered in the inbox.']));
      }
      if (refused[qData.qid] && refused[qData.qid].error) {
        li.appendChild(el('div', { className: 'ck-error-msg' }, [refused[qData.qid].status + ': ' + refused[qData.qid].error]));
      }
      if (open) {
        const change = el('button', { className: 'ck-btn', type: 'button', 'aria-label': 'Change ' + qData.qid }, ['Change']);
        change.addEventListener('click', () => {
          mem.review = false;
          mem.step = steps.indexOf(q);
          saveRound(forkId);
          renderPanel();
          focusRoundStep();
        });
        li.appendChild(el('div', { className: 'ck-actions' }, [change]));
      }
      list.appendChild(li);
    }
    body.appendChild(list);
    const go = el('button', { className: 'ck-btn ck-btn-primary ck-lock-all', type: 'button' }, [
      'Lock all & process (' + toLock.length + ')']);
    go.disabled = toLock.length === 0;
    go.addEventListener('click', () => (isLoose(forkId) ? lockLoose : lockAll)(forkId, go));
    const back = el('button', { className: 'ck-btn', type: 'button' }, ['Back to the questions']);
    back.disabled = steps.length === 0;
    back.addEventListener('click', () => { mem.review = false; saveRound(forkId); renderPanel(); focusRoundStep(); });
    body.appendChild(el('div', { className: 'ck-actions ck-round-nav' }, [go, back]));
    body.appendChild(el('p', { className: 'ck-muted' }, [isLoose(forkId)
      ? 'Each picked answer is sent and locked in turn, then one "Answers are in" request goes to the agent. ' +
        'If one is refused, it stops there and names it; the ones before it stay locked. A locked answer can ' +
        'later be superseded, with a reason.'
      : 'Each picked answer is locked, then one "Answers are in" request goes to the agent. If the server would ' +
        'refuse any of them, it locks none and says which. A locked answer can later be superseded, with a reason.']));
  }

  function renderLockFailure(f) {
    const words = f.status === 409 ? 'Nothing was locked. ' : f.status === 500 ? 'Stopped part way. ' : '';
    return el('div', { className: 'ck-error-msg', role: 'alert' }, [words + (f.error || 'The server refused this.')]);
  }

  async function lockAll(forkId, btn) {
    const mem = roundFor(forkId);
    const entries = roundSteps(forkId).filter(q => drafted(mem.drafts[q.question.qid])).map(q => {
      const d = mem.drafts[q.question.qid];
      return { qid: q.question.qid, picks: [...d.picks], own_text: (d.text || '').trim() };
    });
    if (!entries.length) return;
    // One nonce per attempt, kept until it succeeds: a retry after a failure part way resumes.
    if (!mem.nonce) mem.nonce = genNonce();
    saveRound(forkId);
    btn.disabled = true;
    btn.textContent = 'Locking…';
    let resp = null;
    let data = {};
    try {
      resp = await fetch(config.api + '/lock-all', { method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ fork: forkId, entries: entries, nonce: mem.nonce }) });
      data = await resp.json().catch(() => ({ error: 'HTTP ' + resp.status }));
    } catch (e) {
      data = { error: "Can't reach the console server. Nothing is known to be locked; press again when it is " +
        'back, and anything already locked is skipped.' };
    }
    if (resp && resp.ok) {
      mem.result = data;
      mem.failure = null;
      mem.drafts = {};
      mem.nonce = null;
      mem.review = false;
      mem.step = 0;
      memSet('round:' + forkId, null);
      announce('Locked ' + data.results.filter(r => r.status === 'locked').length + ' answers; the agent was asked to process them.');
    } else {
      mem.failure = { status: resp ? resp.status : 0, error: data.error, results: data.results || [] };
      saveRound(forkId);
      announce('Not locked: ' + (data.error || 'refused'));
    }
    await fetchView();
    renderPanel();
    const h = panelEl.querySelector(mem.result ? '.ck-result-heading' : '.ck-review-heading');
    if (h) h.focus();
  }

  // Q35: a loose group has no fork, so the server's one-request /lock-all (which needs a fork message) is not
  // open to it. Its review page sends what the item page would: each answer, then its lock, through the
  // owner door's own /answer and /lock, then the one 'process' message. Nothing new on the server.
  function sameList(a, b) { return (a || []).length === (b || []).length && (a || []).every((x, i) => x === b[i]); }

  async function lockLoose(forkId, btn) {
    const mem = roundFor(forkId);
    const todo = roundSteps(forkId).filter(q => drafted(mem.drafts[q.question.qid]));
    if (!todo.length) return;
    btn.disabled = true;
    btn.textContent = 'Locking…';
    const results = [];
    let failure = null;
    for (const q of todo) {
      const qid = q.question.qid;
      const d = mem.drafts[qid];
      const picks = [...d.picks];
      const own = (d.text || '').trim();
      const head = q.answers && q.answers.length ? q.answers[q.answers.length - 1] : null;
      let answerId = q.state === 'unlocked' && head && sameList(head.picks, picks) && (head.own_text || '') === own
        ? head.id : null;
      if (!answerId) {
        const a = await apiPost('/answer', { qid: qid, picks: picks, own_text: own }, 'answer-' + qid);
        if (a.error || !a.record) { failure = { qid: qid, error: a.error || 'refused' }; break; }
        answerId = a.record.id;
      }
      const l = await apiPost('/lock', { qid: qid, answer: answerId }, 'lock-' + qid);
      if (l.error) { failure = { qid: qid, error: l.error }; break; }
      results.push({ qid: qid, status: 'locked' });
    }
    let processed = false;
    if (results.length) {
      const item = todo[0].question.item;
      const m = await apiPost('/message', { item: item, text: 'Answers are in: process them.', intent: 'process' },
        'ready-' + item);
      processed = !m.error;
      if (m.error && !failure) failure = { qid: null, error: 'Locked, but the request to the agent was not sent: ' + m.error };
    }
    if (!failure) {
      mem.result = { results: results, process: processed };
      mem.failure = null;
      mem.drafts = {};
      mem.review = false;
      mem.step = 0;
      memSet('round:' + forkId, null);
      announce('Locked ' + results.length + ' answers; the agent was asked to process them.');
    } else {
      const head = 'Locked ' + results.length + ' of ' + todo.length + '. ';
      mem.failure = { status: 0, error: head + (failure.qid ? failure.qid + ' was not locked: ' : '') + failure.error,
        results: failure.qid ? [{ qid: failure.qid, status: 'refused', error: failure.error }] : [] };
      saveRound(forkId);
      announce(mem.failure.error);
    }
    await fetchView();
    renderPanel();
    const h = panelEl.querySelector(mem.result ? '.ck-result-heading' : '.ck-review-heading');
    if (h) h.focus();
  }

  function renderRoundResult(body, forkId, mem) {
    const r = mem.result;
    body.appendChild(el('h2', { className: 'ck-result-heading', tabindex: '-1' }, ['Locked, and sent to the agent']));
    const list = el('ul', { className: 'ck-review-list' });
    const words = { locked: '✓ locked now', already_locked: '✓ was already locked' };
    for (const x of r.results || []) {
      list.appendChild(el('li', { className: 'ck-review-row', dataStatus: x.status }, [
        el('span', { className: 'ck-inbox-item-id' }, [x.qid || '']), ' ' + (words[x.status] || x.status)]));
    }
    body.appendChild(list);
    body.appendChild(el('p', { className: 'ck-muted' }, [r.process
      ? 'One "Answers are in" request went to the agent: ' + listeningWords(cursor && cursor.listening).text.toLowerCase() + '.'
      : 'No process request was needed.']));
    const back = el('button', { className: 'ck-btn ck-btn-primary', type: 'button' }, ['Back to inbox']);
    back.addEventListener('click', () => { mem.result = null; leaveRound(); });
    const sheet = el('button', { className: 'ck-btn', type: 'button' }, ['Answers from this round']);
    sheet.addEventListener('click', () => {
      mem.result = null;
      if (isLoose(forkId)) { const first = roundQuestions(forkId)[0]; showSheet(first ? first.question.item : null, null); }
      else showSheet(view.forks[forkId].message.item, forkId);
    });
    body.appendChild(el('div', { className: 'ck-actions' }, [back, sheet]));
  }

  // ---------------------------------------------------------------------------
  // The Feed (0.7.0): every store record as an event, newest first, filterable
  // by kind and by item. Folds and pull requests are not store records, so they
  // are not here (a PR has no seq to page by); the footer says where they are.
  // ---------------------------------------------------------------------------

  const FEED_LABEL = { question: 'Question asked', answer: 'Answered', lock: 'Locked', reanchor: 'Re-anchored',
    fork: 'Deliberation requested', process: 'Answers are in', chat: 'Chat', reply: 'Agent replied', note: 'Your note',
    transcript: 'Roar transcript', visual: 'Visual' };
  const feedState = { kind: '', item: '', events: null, next: null, error: null, seq: -1, key: '' };

  function feedWords(ev) {
    if (ev.kind === 'answer' && ev.supersedes) return 'Answer changed (supersedes a lock)';
    if (ev.kind === 'lock' && ev.relock) return 'Re-locked';
    if (ev.kind === 'visual') return ev.by === 'owner' ? 'Visual requested' : 'Visual drawn';
    if (ev.kind === 'fork') {
      if (ev.step && STEP_WORDS[ev.step]) {
        return STEP_WORDS[ev.step].title + (ev.about_qid ? ' on ' + ev.about_qid : ' on a round');
      }
      if (ev.roles && ev.roles.indexOf(ROAR) >= 0 && ev.about_qid) return 'Roar on ' + ev.about_qid;
      if (ev.about_qid) return 'Follow-up on ' + ev.about_qid;
      if (ev.follow_up_of) return 'Follow-up round';
    }
    if (ev.kind === 'chat') return ev.by === 'owner' ? 'You, in the chat' : 'Agent, in the chat';
    return FEED_LABEL[ev.kind] || ev.kind;
  }

  async function loadFeed(listEl, append) {
    const key = feedState.kind + '|' + feedState.item;
    let url = config.api + '/feed?limit=50';
    if (feedState.kind) url += '&kind=' + encodeURIComponent(feedState.kind);
    if (feedState.item) url += '&item=' + encodeURIComponent(feedState.item);
    if (append && feedState.next) url += '&before=' + feedState.next;
    try {
      const resp = await fetch(url, { credentials: 'same-origin' });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || 'HTTP ' + resp.status);
      if (key !== feedState.kind + '|' + feedState.item) return; // the filter changed while this loaded
      feedState.events = append && feedState.events ? feedState.events.concat(data.events) : data.events;
      feedState.next = data.next_before;
      feedState.seq = append ? feedState.seq : data.seq;
      feedState.key = key;
      feedState.error = null;
    } catch (e) {
      feedState.error = e.message || 'Network error';
    }
    if (document.contains(listEl)) fillFeedList(listEl);
  }

  function renderFeed(body) {
    const kindSel = el('select', { className: 'ck-select', id: 'ck-feed-kind' });
    kindSel.appendChild(el('option', { value: '' }, ['Every kind']));
    for (const k of Object.keys(FEED_LABEL)) {
      const o = el('option', { value: k }, [FEED_LABEL[k]]);
      if (feedState.kind === k) o.selected = true;
      kindSel.appendChild(o);
    }
    const itemSel = el('select', { className: 'ck-select', id: 'ck-feed-item' });
    itemSel.appendChild(el('option', { value: '' }, ['Every item']));
    const chatOpt = el('option', { value: CHAT_ITEM }, ['The chat']);
    if (feedState.item === CHAT_ITEM) chatOpt.selected = true;
    itemSel.appendChild(chatOpt);
    for (const id of treeOrder()) {
      const o = el('option', { value: id }, [id + (items[id].title ? ' · ' + truncateText(items[id].title, 40) : '')]);
      if (feedState.item === id) o.selected = true;
      itemSel.appendChild(o);
    }
    const list = el('ol', { className: 'ck-feed', 'aria-label': 'What happened, newest first' });
    const refilter = () => {
      feedState.kind = kindSel.value;
      feedState.item = itemSel.value;
      feedState.events = null;
      list.textContent = '';
      list.appendChild(el('li', { className: 'ck-muted' }, ['Loading…']));
      loadFeed(list, false);
    };
    kindSel.addEventListener('change', refilter);
    itemSel.addEventListener('change', refilter);
    body.appendChild(el('div', { className: 'ck-feed-filters' }, [
      el('label', { for: 'ck-feed-kind', className: 'ck-field-label' }, ['Show']), kindSel,
      el('label', { for: 'ck-feed-item', className: 'ck-field-label' }, ['on']), itemSel]));
    body.appendChild(list);
    const key = feedState.kind + '|' + feedState.item;
    if (feedState.events && feedState.key === key) fillFeedList(list);
    else list.appendChild(el('li', { className: 'ck-muted' }, ['Loading…']));
    if (!feedState.events || feedState.key !== key || feedState.seq !== view.seq) loadFeed(list, false);
    body.appendChild(el('p', { className: 'ck-muted ck-feed-foot' }, [
      'Folds happen in the repository, not in the console\'s store, so they are not listed here. Pull ' +
      'requests, open and recently merged or closed, are in the PRs tab.']));
  }

  function fillFeedList(list) {
    list.textContent = '';
    if (feedState.error) {
      list.appendChild(el('li', { className: 'ck-error-msg' }, ['Could not load the feed: ' + feedState.error]));
      return;
    }
    const evs = feedState.events || [];
    if (!evs.length) list.appendChild(el('li', { className: 'ck-muted' }, ['Nothing here yet.']));
    for (const ev of evs) {
      const isNew = ev.seq > seenAtOpen && ev.by === 'agent';
      const where = ev.item === CHAT_ITEM ? 'chat' : (ev.qid || ev.item || '');
      const row = el('li', { className: 'ck-feed-row' + (isNew ? ' ck-feed-new' : ''), dataKind: ev.kind, dataSeq: String(ev.seq) });
      const head = el('div', { className: 'ck-feed-head' }, [
        el('span', { className: 'ck-feed-kind', dataKind: ev.kind }, [feedWords(ev)]),
        el('span', { className: 'ck-inbox-item-id' }, [where]),
        el('span', { className: 'ck-feed-time', title: new Date(ev.ts).toLocaleString() }, [relTime(ev.ts)])
      ]);
      if (ev.agent) head.appendChild(el('span', { className: 'ck-feed-agent' }, ['by ' + ev.agent]));
      if (isNew) head.appendChild(el('span', { className: 'ck-feed-newtag' }, ['new']));
      row.appendChild(head);
      if (ev.text) {
        const t = el('div', { className: 'ck-feed-text' });
        t.textContent = ev.text;
        row.appendChild(t);
      }
      const target = ev.item === CHAT_ITEM ? 'chat' : (items[ev.item] ? ev.item : null);
      if (target) {
        const open = el('button', { className: 'ck-btn ck-btn-quiet', type: 'button',
          'aria-label': 'Open ' + (target === 'chat' ? 'the chat' : target) }, ['Open']);
        open.addEventListener('click', () => {
          if (target === 'chat') return selectTab('chat');
          openFromInbox(target);
        });
        row.appendChild(open);
      }
      list.appendChild(row);
    }
    if (feedState.next) {
      const more = el('button', { className: 'ck-btn', type: 'button' }, ['Older']);
      more.addEventListener('click', () => { more.disabled = true; loadFeed(list, true); });
      list.appendChild(el('li', { className: 'ck-feed-more' }, [more]));
    }
  }

  // ---------------------------------------------------------------------------
  // Pull requests (owner ask: "have PR requests and history show up"). The
  // server never talks to GitHub: the steward runs `agent.py prs-push`, which
  // runs gh in its own process and sends the list as data. Titles and branch
  // names are whatever someone typed on GitHub, so every one goes in as text
  // (el() and textContent), never HTML, and a link goes only to an
  // https://github.com/ URL.
  // ---------------------------------------------------------------------------

  const prsState = { data: null, error: null, ver: undefined, loading: false };
  const PR_CHECKS = { success: 'checks pass', failure: 'checks fail', pending: 'checks running', none: 'no checks' };
  const PR_ID_TOKEN = /[A-Za-z0-9][A-Za-z0-9_.\-]*(?:\/Q[1-9][0-9]*)?/g;
  const PR_MAX_LINKS = 5;

  async function loadPRs(listEl) {
    if (prsState.loading) return;
    prsState.loading = true;
    const ver = liveVer;
    try {
      const resp = await fetch(config.api + '/prs', { credentials: 'same-origin' });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || 'HTTP ' + resp.status);
      prsState.data = data;
      prsState.error = null;
    } catch (e) {
      prsState.error = e.message || 'Network error';
    }
    prsState.ver = ver;
    prsState.loading = false;
    if (document.contains(listEl)) fillPRs(listEl);
  }

  function renderPRs(body) {
    const box = el('div', { className: 'ck-prs' });
    body.appendChild(box);
    if (prsState.data || prsState.error) fillPRs(box);
    else box.appendChild(el('p', { className: 'ck-muted' }, ['Loading…']));
    // A live wake (a push bumps the version) or the first open: read the steward's push again.
    if (prsState.ver !== liveVer || (!prsState.data && !prsState.error)) loadPRs(box);
  }

  // Portfolio tab — a card per peer (and self), with state tallies and desktop notifications
  // for new "awaiting_you" or "stale" counts. All data is fetched through the owner's own server
  // (/api/portfolio); the browser never sees peer secrets, so a browser window on one console does
  // not need direct Access to the others.
  const portfolioState = {
    data: null,         // last payload from /api/portfolio: { self, peers }
    error: null,
    ver: undefined,     // the liveVer when `data` was fetched; drives revalidation
    loading: false,
    lastSeen: {}        // name → { awaiting_you, stale, ts } — in-session dedupe for notifications
  };
  const PORTFOLIO_POLL_MS = 30000;       // soft background poll while the tab is open
  const PORTFOLIO_NOTIFY_DEDUPE_MS = 60000;
  let portfolioTimer = null;

  function portfolioNotifyEnabled() {
    try { return localStorage.getItem('ck-portfolio-notify') === '1'; } catch (e) { return false; }
  }
  function setPortfolioNotify(on) {
    try { localStorage.setItem('ck-portfolio-notify', on ? '1' : '0'); } catch (e) {}
  }
  function portfolioDndUntil() {
    try {
      const v = Number(localStorage.getItem('ck-portfolio-dnd-until') || '0');
      return Number.isFinite(v) ? v : 0;
    } catch (e) { return 0; }
  }
  function setPortfolioDndUntil(ms) {
    try { localStorage.setItem('ck-portfolio-dnd-until', String(ms || 0)); } catch (e) {}
  }

  async function loadPortfolio(container) {
    if (portfolioState.loading) return;
    portfolioState.loading = true;
    const ver = liveVer;
    try {
      const resp = await fetch(config.api + '/portfolio', { credentials: 'same-origin' });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || 'HTTP ' + resp.status);
      portfolioState.data = data;
      portfolioState.error = null;
      maybeNotifyPortfolio(data);
    } catch (e) {
      portfolioState.error = e.message || 'Network error';
    }
    portfolioState.ver = ver;
    portfolioState.loading = false;
    if (container && document.contains(container)) fillPortfolio(container);
  }

  // The browser-side notifier. Compares each peer's current `awaiting_you` and `stale` against the
  // last-seen counts in this session; fires one OS notification per peer when either climbs. The
  // dedupe window drops repeats, and the DND lid silences everything until a chosen moment.
  function maybeNotifyPortfolio(data) {
    if (!portfolioNotifyEnabled()) return;
    if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
    const now = Date.now();
    if (now < portfolioDndUntil()) return;
    const rows = (data && data.peers) || [];
    for (const row of rows) {
      if (!row || !row.ok || !row.name) continue;
      const you = Number(row.awaiting_you || 0);
      const stale = Number(row.stale || 0);
      const prev = portfolioState.lastSeen[row.name] || { awaiting_you: 0, stale: 0, ts: 0 };
      const grew = (you > prev.awaiting_you) || (stale > prev.stale);
      portfolioState.lastSeen[row.name] = { awaiting_you: you, stale: stale, ts: prev.ts };
      if (!grew) continue;
      if (now - prev.ts < PORTFOLIO_NOTIFY_DEDUPE_MS) continue;
      portfolioState.lastSeen[row.name].ts = now;
      const bits = [];
      if (you > prev.awaiting_you) bits.push((you - prev.awaiting_you) + ' new awaiting you');
      if (stale > prev.stale) bits.push((stale - prev.stale) + ' newly stale');
      try {
        const n = new Notification((row.project || row.name) + ' · console-kit', {
          body: bits.join(' · '),
          tag: 'ck-portfolio-' + row.name,
          silent: false
        });
        n.onclick = () => { try { window.open(row.url, '_blank', 'noopener'); n.close(); } catch (e) {} };
      } catch (e) {}
    }
  }

  // The roll-up for the Portfolio tab's note: total `awaiting_you` across every peer plus self.
  function portfolioAwaitingTotal() {
    const d = portfolioState.data;
    if (!d) return 0;
    let n = Number((d.self && d.self.awaiting_you) || 0);
    for (const p of (d.peers || [])) if (p && p.ok) n += Number(p.awaiting_you || 0);
    return n;
  }

  function renderPortfolio(body) {
    const box = el('div', { className: 'ck-portfolio' });
    body.appendChild(box);

    // Notification controls strip.
    const ctrl = el('div', { className: 'ck-portfolio-controls' });
    const supportsNotify = typeof Notification !== 'undefined';
    const notifOn = supportsNotify && portfolioNotifyEnabled() && Notification.permission === 'granted';
    const btn = el('button', { className: 'ck-btn ck-portfolio-notify', type: 'button' },
      [notifOn ? '🔔 Notifications on' : '🔕 Notifications off']);
    btn.addEventListener('click', async () => {
      if (!supportsNotify) { announce('This browser does not support desktop notifications.'); return; }
      if (!portfolioNotifyEnabled() || Notification.permission !== 'granted') {
        let perm = Notification.permission;
        if (perm !== 'granted') {
          try { perm = await Notification.requestPermission(); } catch (e) { perm = 'denied'; }
        }
        if (perm !== 'granted') { announce('Notifications blocked by the browser.'); return; }
        setPortfolioNotify(true);
        announce('Portfolio notifications on.');
      } else {
        setPortfolioNotify(false);
        announce('Portfolio notifications off.');
      }
      renderPanel();
    });
    ctrl.appendChild(btn);

    const dndUntil = portfolioDndUntil();
    const dndActive = Date.now() < dndUntil;
    const dnd = el('button', { className: 'ck-btn ck-portfolio-dnd', type: 'button' },
      [dndActive ? ('🌙 Snoozed · ' + Math.max(1, Math.round((dndUntil - Date.now()) / 60000)) + ' min')
                 : '🌙 Snooze 1h']);
    dnd.addEventListener('click', () => {
      if (dndActive) setPortfolioDndUntil(0);
      else setPortfolioDndUntil(Date.now() + 60 * 60 * 1000);
      renderPanel();
    });
    ctrl.appendChild(dnd);
    box.appendChild(ctrl);

    const grid = el('div', { className: 'ck-portfolio-grid' });
    box.appendChild(grid);
    if (portfolioState.data || portfolioState.error) fillPortfolio(box);
    else grid.appendChild(el('p', { className: 'ck-muted' }, ['Loading…']));

    if (portfolioState.ver !== liveVer || (!portfolioState.data && !portfolioState.error)) loadPortfolio(box);

    // One soft background poll while the tab is open; cleared on redraw.
    if (portfolioTimer) { clearTimeout(portfolioTimer); portfolioTimer = null; }
    portfolioTimer = setTimeout(() => {
      portfolioTimer = null;
      if (currentTab === 'portfolio') loadPortfolio(box);
    }, PORTFOLIO_POLL_MS);
  }

  function fillPortfolio(container) {
    const grid = container.querySelector('.ck-portfolio-grid');
    if (!grid) return;
    grid.innerHTML = '';
    if (portfolioState.error) {
      grid.appendChild(el('p', { className: 'ck-portfolio-err', role: 'alert' },
        ['Could not load the portfolio: ', portfolioState.error]));
      return;
    }
    const data = portfolioState.data || {};
    const self = data.self || null;
    const peers = Array.isArray(data.peers) ? data.peers.slice() : [];
    // Self first, then peers alphabetically by name.
    peers.sort((a, b) => String(a && a.name || '').localeCompare(String(b && b.name || '')));
    if (self) grid.appendChild(renderPortfolioCard(self, true));
    for (const p of peers) grid.appendChild(renderPortfolioCard(p, false));
    if (!self && !peers.length) {
      grid.appendChild(el('p', { className: 'ck-muted' },
        ['No peers configured. Add some in .console-kit/portfolio.json and ',
         el('code', {}, ['agent.py portfolio-token']),
         ' for setup.']));
    }
  }

  function renderPortfolioCard(row, isSelf) {
    const ok = !row || row.ok !== false;
    const health = portfolioHealth(row);
    const card = el('div', { className: 'ck-portfolio-card' + (isSelf ? ' ck-portfolio-self' : '') +
      (ok ? '' : ' ck-portfolio-err-card'), tabindex: '0',
      dataHealth: health,
      'aria-label': 'Project ' + (row.project || row.name || '?') + (isSelf ? ' (this console)' : '') });
    const head = el('div', { className: 'ck-portfolio-head' }, [
      el('span', { className: 'ck-portfolio-dot', dataHealth: health,
        title: portfolioHealthTitle(health, row), 'aria-label': 'Status: ' + health }, []),
      el('span', { className: 'ck-portfolio-name' }, [String(row.project || row.name || '?')]),
      isSelf ? el('span', { className: 'ck-portfolio-tag' }, ['this']) : null
    ].filter(Boolean));
    card.appendChild(head);
    if (!ok) {
      card.appendChild(el('div', { className: 'ck-portfolio-cardbody' }, [
        el('span', { className: 'ck-portfolio-errline' }, [String(row.error || 'unreachable')])
      ]));
      return card;
    }
    const you = Number(row.awaiting_you || 0);
    const unl = Number(row.unlocked || 0);
    const lock = Number(row.locked || 0);
    const stale = Number(row.stale || 0);
    const vis = Number(row.visuals_waiting || 0);
    const tallies = el('div', { className: 'ck-portfolio-tallies' }, [
      renderTally('?you', you, 'awaiting-you'),
      renderTally('~unl', unl, 'unlocked'),
      renderTally('!stale', stale, 'stale'),
      renderTally('○lock', lock, 'locked'),
      vis ? renderTally('◫vis', vis, 'visual') : null
    ].filter(Boolean));
    card.appendChild(tallies);
    const peek = Array.isArray(row.peek) ? row.peek : [];
    if (peek.length) {
      const list = el('ul', { className: 'ck-portfolio-peek', 'aria-label': 'Top awaiting questions' });
      for (const p of peek) {
        if (!p || !p.qid) continue;
        const li = el('li', { className: 'ck-portfolio-peek-row' }, [
          el('span', { className: 'ck-portfolio-peek-qid' }, [String(p.qid)]),
          el('span', { className: 'ck-portfolio-peek-text' },
            [p.text ? ' · ' + p.text : '']),
          el('span', { className: 'ck-portfolio-peek-when ck-muted' },
            [p.ts ? ' · ' + relTime(p.ts) : ''])
        ]);
        list.appendChild(li);
      }
      card.appendChild(list);
    }
    const foot = el('div', { className: 'ck-portfolio-foot' }, [
      el('span', { className: 'ck-portfolio-when' },
        [row.last_activity_at ? ('active ' + relTime(row.last_activity_at)) : 'no activity yet']),
      row.items != null ? el('span', { className: 'ck-portfolio-count ck-muted' },
        [' · ' + row.items + ' item' + (row.items === 1 ? '' : 's')]) : null
    ].filter(Boolean));
    card.appendChild(foot);
    if (!isSelf && row.url) {
      const open = () => { try { window.open(row.url, '_blank', 'noopener'); } catch (e) {} };
      card.addEventListener('click', open);
      card.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
      });
      card.style.cursor = 'pointer';
    }
    return card;
  }

  // The traffic-light dot per peer. green: no awaiting; yellow: oldest within 1h; orange: within 24h;
  // red: older than 24h; grey: the peer errored. Based on the server's oldest_awaiting_at timestamp.
  function portfolioHealth(row) {
    if (!row || row.ok === false) return 'err';
    if (!Number(row.awaiting_you || 0)) return 'ok';
    const old = row.oldest_awaiting_at && Date.parse(row.oldest_awaiting_at);
    if (!old || Number.isNaN(old)) return 'warn';
    const hrs = (Date.now() - old) / 3_600_000;
    if (hrs < 1) return 'warn';
    if (hrs < 24) return 'late';
    return 'overdue';
  }
  function portfolioHealthTitle(health, row) {
    if (health === 'err') return row && row.error ? row.error : 'peer unreachable';
    if (health === 'ok') return 'nothing awaiting';
    const ts = row && row.oldest_awaiting_at ? ' (oldest ' + relTime(row.oldest_awaiting_at) + ')' : '';
    if (health === 'warn') return 'awaiting under 1h' + ts;
    if (health === 'late') return 'awaiting over 1h' + ts;
    return 'awaiting over 24h' + ts;
  }

  function renderTally(label, n, kind) {
    return el('span', { className: 'ck-portfolio-tally', dataKind: kind, dataZero: n ? 'false' : 'true' },
      [label + ' ', el('b', {}, [String(n)])]);
  }

  // 0.8.19: Favorite tab — starred visuals grouped under their items, plus any starred items with no stars yet.
  function renderFavorites(body) {
    const favs = (view && view.favorites) || [];
    const favSet = new Set(favs);
    const starredItems = favs.filter(k => k.startsWith('item:')).map(k => k.slice(5));
    const starredVisuals = favs.filter(k => k.startsWith('visual:')).map(k => k.slice(7));
    if (!favs.length) {
      body.appendChild(el('p', { className: 'ck-muted', style: 'padding: 20px; text-align: center;' }, [
        'Nothing starred yet. Tap ☆ on a visual or an item to keep it here.'
      ]));
      return;
    }
    // Walk every item that is starred or has starred visuals; show its row + its starred visuals.
    const byItem = {};
    for (const id of starredItems) if (!byItem[id]) byItem[id] = { item: id, visuals: [] };
    for (const vid of starredVisuals) {
      const v = findVisual(vid);
      if (!v) continue;
      const key = v.item || '_';
      if (!byItem[key]) byItem[key] = { item: key, visuals: [] };
      byItem[key].visuals.push(v);
    }
    const list = el('div', { className: 'ck-inbox-list ck-favorites' });
    for (const id of Object.keys(byItem).sort()) {
      const slot = byItem[id];
      const itemData = items && items[id];
      const header = el('div', { className: 'ck-inbox-item ck-favorite-item', tabindex: '0' }, [
        el('span', { className: 'ck-star ck-star-on', 'aria-label': 'Starred item' }, ['★']),
        el('span', { className: 'ck-inbox-item-id' }, [id]),
        el('span', { className: 'ck-inbox-item-title' }, [itemData ? itemData.title : ''])
      ]);
      if (itemData) header.addEventListener('click', () => openFromInbox(id));
      list.appendChild(header);
      for (const v of slot.visuals) {
        const row = el('div', { className: 'ck-favorite-visual' }, [
          el('span', { className: 'ck-star ck-star-on', 'aria-label': 'Starred visual' }, ['★']),
          el('span', { className: 'ck-visual-title' }, [v.title]),
          el('span', { className: 'ck-muted' }, [
            ' · ' + (v.format === 'html' ? 'HTML mock' : 'Mermaid') + ' · ' + relTime(v.ts)
          ])
        ]);
        row.addEventListener('click', () => openFromInbox(v.item));
        list.appendChild(row);
      }
    }
    body.appendChild(list);
  }

  function findVisual(vid) {
    const byItem = (view && view.visuals) || {};
    for (const id of Object.keys(byItem)) {
      for (const v of byItem[id]) if (v.id === vid) return { ...v, item: id };
    }
    return null;
  }

  async function toggleFavorite(key) {
    try {
      const r = await apiPost('/favorite', { id: key }, 'fav-' + key);
      if (r && r.error) announce('Could not change favorite: ' + r.error);
    } catch (e) {
      announce('Could not change favorite: ' + (e.message || 'error'));
    }
  }

  // 0.8.19: a star toggle. `key` is "visual:<rid>" or "item:<id>"; `what` names it for the label.
  function renderStarButton(key, what) {
    const on = !!(view && view.favorites && view.favorites.indexOf(key) >= 0);
    const btn = el('button', {
      className: 'ck-star' + (on ? ' ck-star-on' : ''),
      type: 'button',
      'aria-pressed': on ? 'true' : 'false',
      'aria-label': (on ? 'Unstar ' : 'Star ') + what,
      title: on ? 'Starred — click to remove' : 'Star to keep in Favorites'
    }, [on ? '★' : '☆']);
    btn.addEventListener('click', e => {
      e.stopPropagation();
      e.preventDefault();
      toggleFavorite(key);
    });
    return btn;
  }

  // Only GitHub, only https: anything else is shown as text with no link.
  function prHref(url) {
    return (typeof url === 'string' && url.startsWith('https://github.com/')) ? url : null;
  }

  // The items and questions a PR's title or branch names, as the console knows them (exact, case-sensitive).
  function prLinks(pr) {
    const found = [];
    const text = String(pr.title || '') + ' ' + String(pr.head || '');
    for (const m of text.matchAll(PR_ID_TOKEN)) {
      const tok = m[0].replace(/[._\-]+$/, '');
      let item = null;
      // Own keys only: a title word like "constructor" must not match Object.prototype.
      if (view && view.questions && Object.hasOwn(view.questions, tok)) item = (view.questions[tok].question || {}).item || tok.split('/Q')[0];
      else if (!tok.includes('/') && items && Object.hasOwn(items, tok)) item = tok;
      if (item && items && Object.hasOwn(items, item) && !found.some(f => f.id === tok)) found.push({ id: tok, item: item });
      if (found.length >= PR_MAX_LINKS) break;
    }
    return found;
  }

  function prWhen(pr) {
    if (pr.state === 'merged') return ['merged ', pr.merged_at];
    if (pr.state === 'closed') return ['closed ', pr.closed_at];
    return ['opened ', pr.created_at];
  }

  function renderPRRow(pr) {
    const row = el('li', { className: 'ck-feed-row ck-pr-row', dataState: pr.state, dataNumber: String(pr.number) });
    const href = prHref(pr.url);
    const titleKids = [el('span', { className: 'ck-pr-num' }, ['#' + pr.number]), ' ',
      el('span', { className: 'ck-pr-title' }, [String(pr.title)])];
    const title = href
      ? el('a', { className: 'ck-pr-link', href: href, target: '_blank', rel: 'noopener noreferrer' }, titleKids)
      : el('span', { className: 'ck-pr-link' }, titleKids);
    const head = el('div', { className: 'ck-feed-head' }, [title]);
    if (pr.draft) head.appendChild(el('span', { className: 'ck-pr-badge', dataKind: 'draft' }, ['draft']));
    if (pr.state !== 'open') head.appendChild(el('span', { className: 'ck-pr-badge', dataKind: pr.state }, [pr.state]));
    row.appendChild(head);
    const [verb, ts] = prWhen(pr);
    const meta = el('div', { className: 'ck-pr-meta' }, [
      el('span', { className: 'ck-pr-branch' }, [String(pr.head) + ' → ' + String(pr.base)]),
      el('span', { className: 'ck-feed-agent' }, ['by ' + (pr.author || 'unknown')]),
      el('span', { className: 'ck-pr-checks', dataChecks: pr.checks }, [PR_CHECKS[pr.checks] || 'checks unknown']),
      el('span', { className: 'ck-feed-time', title: ts ? new Date(ts).toLocaleString() : '' }, [verb + relTime(ts)])
    ]);
    row.appendChild(meta);
    const links = prLinks(pr);
    if (links.length) {
      const bar = el('div', { className: 'ck-pr-items' });
      for (const l of links) {
        const b = el('button', { className: 'ck-btn ck-btn-quiet', type: 'button', 'aria-label': 'Open ' + l.id }, ['Open ' + l.id]);
        b.addEventListener('click', () => openFromInbox(l.item));
        bar.appendChild(b);
      }
      row.appendChild(bar);
    }
    return row;
  }

  function fillPRs(box) {
    box.textContent = '';
    if (prsState.error) {
      box.appendChild(el('p', { className: 'ck-error-msg' }, ['Could not load the pull requests: ' + prsState.error]));
      return;
    }
    const d = prsState.data;
    if (!d || !d.pushed) {   // never a blank list, never invented PRs (F63)
      box.appendChild(el('p', { className: 'ck-prs-note', role: 'status' },
        [(d && d.note) || 'The steward has not pushed pull requests yet (agent.py prs-push).']));
      return;
    }
    const by = d.by ? 'the steward (' + d.by + ')' : 'the steward';
    box.appendChild(el('p', { className: 'ck-muted ck-prs-pushed', title: new Date(d.pushed_at).toLocaleString() },
      [String(d.repo) + ' · pushed ' + relTime(d.pushed_at) + ' by ' + by]));
    const prs = Array.isArray(d.prs) ? d.prs : [];
    const open = prs.filter(p => p.state === 'open');
    const done = prs.filter(p => p.state !== 'open');
    const groups = [['Open', open, 'No open pull requests.'],
      ['Merged or closed, last ' + d.window_days + ' days', done, 'None merged or closed in that window.']];
    for (const [label, list, empty] of groups) {
      box.appendChild(el('h3', { className: 'ck-prs-heading' }, [label + ' (' + list.length + ')']));
      const ol = el('ol', { className: 'ck-feed ck-prs-list', 'aria-label': label });
      if (!list.length) ol.appendChild(el('li', { className: 'ck-muted' }, [empty]));
      for (const pr of list) ol.appendChild(renderPRRow(pr));
      box.appendChild(ol);
    }
  }

  // ---------------------------------------------------------------------------
  // The chat (0.7.0; owner: "answered by whichever session is watching"). A
  // general message, not tied to a question: an owner message on the chat thread
  // with intent 'chat', which rings the doorbell so a watching session wakes and
  // replies in the same thread with `agent.py reply @chat`.
  // ---------------------------------------------------------------------------

  function renderChat(body) {
    body.appendChild(renderChatLog());
    body.appendChild(renderChatStatus());
    body.appendChild(renderChatCompose());
  }

  // A reply that arrives while the owner types is drawn into the log in place: the
  // box they are typing in, its caret and its draft are never touched.
  function refreshChatInPlace() {
    const log = panelEl.querySelector('.ck-chat-log');
    const status = panelEl.querySelector('.ck-chat-status');
    if (!log || !status) return false;
    const fresh = renderChatLog();
    log.replaceWith(fresh);
    fresh.scrollTop = fresh.scrollHeight;
    status.replaceWith(renderChatStatus());
    const tabs = panelEl.querySelector('.ck-tabs');
    if (tabs && !tabs.contains(document.activeElement)) tabs.replaceWith(renderTabs());
    return true;
  }

  function renderChatLog() {
    const msgs = ((view.threads && view.threads[CHAT_ITEM]) || []).slice().sort((a, b) => a.seq - b.seq);
    const log = el('div', { className: 'ck-chat-log', role: 'log', 'aria-label': 'Chat with the agent', tabindex: '0' });
    if (!msgs.length) {
      log.appendChild(el('p', { className: 'ck-muted' }, [
        'Ask anything that is not tied to one question: a status, a "why", a request. A session that is ' +
        'watching answers here.']));
    }
    for (const m of msgs) {
      const b = el('div', { className: 'ck-chat-msg' + (arrived.has(m.id) ? ' ck-arrived' : ''), dataBy: m.by });
      b.appendChild(el('div', { className: 'ck-chat-who' }, [
        m.by === 'owner' ? 'You' : agentLabel(m, 'Agent'), ' · ',
        el('span', { title: new Date(m.ts).toLocaleString() }, [relTime(m.ts)])]));
      const t = el('div', { className: 'ck-chat-text' });
      t.textContent = m.text;
      b.appendChild(t);
      log.appendChild(b);
    }
    return log;
  }

  function renderChatStatus() {
    const w = listeningWords(cursor ? cursor.listening : null);
    const waiting = !!(view.chat && view.chat.awaiting_agent);
    return el('p', { className: 'ck-chat-status', dataState: w.state }, [
      waiting ? (w.state === 'listening' ? '● An agent is listening and will answer here.'
        : '○ Waiting: no session is watching right now, so your message waits until one starts.')
        : (w.state === 'listening' ? '● An agent is listening.' : '○ ' + w.text + '.')]);
  }

  function renderChatCompose() {
    const id = 'ck-chat-input';
    const box = el('textarea', { className: 'ck-textarea', id: id, rows: '3', maxlength: String(MAX_CHAT),
      placeholder: 'Ask the agent… (Enter sends, Shift+Enter for a new line)' });
    if (draftTexts.chat === undefined) draftTexts.chat = memGet('chat-draft') || '';
    box.value = draftTexts.chat;
    const count = el('span', { className: 'ck-chat-count', 'aria-live': 'off' }, [box.value.length + ' / ' + MAX_CHAT]);
    const err = el('p', { className: 'ck-error-msg', role: 'status', 'aria-live': 'polite' });
    box.addEventListener('input', () => {
      draftTexts.chat = box.value;
      memSet('chat-draft', box.value || null);
      count.textContent = box.value.length + ' / ' + MAX_CHAT;
    });
    const send = el('button', { className: 'ck-btn ck-btn-primary', type: 'button' }, ['Send']);
    const doSend = async () => {
      const text = box.value.trim();
      if (!text) { err.textContent = 'Type a message first.'; return; }
      send.disabled = true;
      err.textContent = '';
      const result = await apiPost('/message', { item: CHAT_ITEM, text: text, intent: 'chat' }, 'chat');
      send.disabled = false;
      if (result.error) {
        err.textContent = 'Not sent: ' + result.error;
        announce('Not sent: ' + result.error);
        return;
      }
      draftTexts.chat = '';
      memSet('chat-draft', null);
      renderPanel();
      const again = panelEl.querySelector('#' + id);
      if (again) again.focus();
    };
    send.addEventListener('click', doSend);
    box.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); doSend(); }
    });
    return el('div', { className: 'ck-chat-compose' }, [
      el('label', { for: id, className: 'ck-field-label' }, ['Message']), box,
      el('div', { className: 'ck-chat-foot' }, [count, send]), err]);
  }

  // ---------------------------------------------------------------------------
  // The live loop (0.7.0): a long poll on the store's sequence number. The page
  // asks /api/wait "anything since seq S?"; the server answers the moment
  // something changes, or after 25 s with "no". Only a change fetches the view.
  // Paused while the tab is hidden; on errors it backs off 2 s, 4 s … 60 s.
  // A server with no /api/wait (before 0.7.0) stops the loop, and Refresh still works.
  // ---------------------------------------------------------------------------

  const LIVE_WAIT_S = 25;
  const LIVE_BACKOFF_MIN = 2000;
  const LIVE_BACKOFF_MAX = 60000;
  const LIVE_MIN_GAP = 1000;   // never two polls closer than this, whatever the server says
  let liveVer = null;
  let liveBackoff = 0;
  let liveAbort = null;
  let liveStopped = false;
  let liveRunning = false;
  let liveWake = null;          // resolves a pause early (tab shown again)
  let pendingLive = false;      // a live change arrived while the owner was typing in the panel

  function sleep(ms) {
    return new Promise(resolve => {
      const t = setTimeout(() => { liveWake = null; resolve(); }, ms);
      liveWake = () => { clearTimeout(t); liveWake = null; resolve(); };
    });
  }

  function whenVisible() {
    return new Promise(resolve => {
      const on = () => {
        if (document.visibilityState !== 'hidden') { document.removeEventListener('visibilitychange', on); resolve(); }
      };
      document.addEventListener('visibilitychange', on);
    });
  }

  async function liveLoop() {
    if (liveRunning) return;
    liveRunning = true;
    try {
      while (!liveStopped) {
        if (document.visibilityState === 'hidden') { await whenVisible(); continue; }
        const since = view && typeof view.seq === 'number' ? view.seq : null;
        if (since === null) {  // no view yet, or an older server's view without a seq
          const got = await fetchView();
          if (!got) { await backoff(); continue; }
          if (typeof view.seq !== 'number') { liveStopped = true; break; }
          onLive(false);
          continue;
        }
        const started = Date.now();
        const ctl = new AbortController();
        liveAbort = ctl;
        const guard = setTimeout(() => ctl.abort(), (LIVE_WAIT_S + 15) * 1000);
        let resp;
        try {
          let url = config.api + '/wait?since=' + since + '&timeout=' + LIVE_WAIT_S;
          if (liveVer) url += '&ver=' + encodeURIComponent(liveVer);
          resp = await fetch(url, { credentials: 'same-origin', signal: ctl.signal });
        } catch (e) {
          clearTimeout(guard);
          liveAbort = null;
          if (document.visibilityState === 'hidden') continue;  // aborted because the tab was hidden
          await backoff();
          continue;
        }
        clearTimeout(guard);
        liveAbort = null;
        if (resp.status === 404) { liveStopped = true; break; }  // a server from before 0.7.0
        if (!resp.ok) { await backoff(); continue; }
        let data;
        try { data = await resp.json(); } catch (e) { await backoff(); continue; }
        liveBackoff = 0;
        const verChanged = liveVer !== null && data.ver !== liveVer;
        liveVer = data.ver;
        if (data.seq !== view.seq) {  // not already fetched by one of this page's own writes
          if (await fetchView()) onLive(true);
          else await backoff();
        } else if (data.seq !== since) {
          cursor = data.cursor || cursor;
        } else if (verChanged && view.items_note) {
          // Q24: the first items-push moves no store record, only the version; while the page still says
          // "no items yet", that is the change it is waiting for, so the view is fetched again.
          cursor = data.cursor || cursor;
          if (await fetchView()) onLive(false);
          else await backoff();
        } else if (verChanged && data.cursor) {
          cursor = data.cursor;
          onLive(false);
        } else if (data.cursor) {
          cursor = data.cursor;  // the listening heartbeat, refreshed at least every poll
          refreshStatusBits();
        }
        const gap = Date.now() - started;
        if (gap < LIVE_MIN_GAP) await sleep(LIVE_MIN_GAP - gap);
      }
    } finally {
      liveRunning = false;
    }
  }

  async function backoff() {
    liveBackoff = liveBackoff ? Math.min(liveBackoff * 2, LIVE_BACKOFF_MAX) : LIVE_BACKOFF_MIN;
    await sleep(liveBackoff + Math.floor(Math.random() * 500));
  }

  // Is the owner in the middle of something in the panel? Then a redraw would take their focus and caret.
  function busyInPanel() {
    const a = document.activeElement;
    if (!panelEl || !a || a === document.body || !panelEl.contains(a)) return false;
    if (currentMode === 'round') return true;  // walking a round: the next step redraws it anyway
    return a.tagName === 'TEXTAREA' || a.tagName === 'SELECT' || (a.tagName === 'INPUT' && a.type !== 'radio' && a.type !== 'checkbox');
  }

  // A live change arrived. The buttons outside the panel are already updated (fetchView);
  // the open panel is redrawn, unless the owner is typing in it: then a "Show" note waits.
  function onLive(storeChanged) {
    updateInboxButton();
    updateItemButtons();
    if (storeChanged && arrived.size) {
      const qs = [...arrived].filter(i => view.questions[i]).length;
      const msgs = arrived.size - qs;
      const parts = [];
      if (qs) parts.push(qs + ' new question' + (qs === 1 ? '' : 's'));
      if (msgs) parts.push(msgs + ' new message' + (msgs === 1 ? '' : 's'));
      announce(parts.join(', ') + '.');
    }
    if (!panelEl || panelEl.getAttribute('data-open') !== 'true') return;
    if (busyInPanel()) {
      if (currentMode === 'inbox' && currentTab === 'chat' && refreshChatInPlace()) return;
      pendingLive = true;  // redrawn when the owner leaves the text box
      if (storeChanged && arrived.size) showLiveNote();
      return;
    }
    redrawKeepingPlace();
  }

  function redrawKeepingPlace() {
    const bodyEl = panelEl.querySelector('.ck-body');
    const top = bodyEl ? bodyEl.scrollTop : 0;
    const hadFocus = panelEl.contains(document.activeElement) && document.activeElement !== document.body;
    // A control that names itself (data-focus-key) gets focus back on its redrawn twin: an Undo mid-countdown
    // must keep focus through a live redraw, or Enter would land somewhere else.
    const focusKey = hadFocus && document.activeElement.getAttribute('data-focus-key');
    renderPanel();
    const again = panelEl.querySelector('.ck-body');
    if (again && currentTab !== 'chat') again.scrollTop = top;
    if (focusKey) {
      const twin = panelEl.querySelector('[data-focus-key="' + CSS.escape(focusKey) + '"]');
      if (twin) twin.focus();
    }
    // Focus was on a control the redraw replaced: put it somewhere stable in the panel, never on the board.
    if (hadFocus && !panelEl.contains(document.activeElement)) {
      const t = panelEl.querySelector('[role="tab"][aria-selected="true"]') || panelEl.querySelector('.ck-close-btn');
      if (t) t.focus();
    }
  }

  function showLiveNote() {
    pendingLive = true;
    if (panelEl.querySelector('.ck-live-note')) return;
    const show = el('button', { className: 'ck-btn ck-btn-quiet', type: 'button' }, ['Show']);
    show.addEventListener('click', () => { redrawKeepingPlace(); });
    const note = el('div', { className: 'ck-live-note', role: 'status' }, [
      el('span', {}, ['New activity. ']), show]);
    const bar = panelEl.querySelector('.ck-status-bar');
    if (bar) bar.appendChild(note);
  }

  // Cheap updates that need no redraw: the "agent listening" words.
  function refreshStatusBits() {
    const old = panelEl && panelEl.querySelector('.ck-status-bar .ck-listening');
    if (old) old.replaceWith(renderListening());
  }

  // Pause while hidden: stop the open poll (it holds a server thread), and resume at once when shown.
  function onVisibility() {
    if (document.visibilityState === 'hidden') {
      if (liveAbort) liveAbort.abort();
    } else {
      if (liveWake) liveWake();
      if (!liveStopped && !liveRunning) liveLoop();
    }
  }

  // When the owner leaves a text box, a redraw that waited for them can happen.
  function onPanelFocusOut() {
    if (!pendingLive) return;
    setTimeout(() => { if (pendingLive && !busyInPanel()) redrawKeepingPlace(); }, 0);
  }

  // Live values (AB-2/Q4, owner): the committed page stays the page, and an
  // open tab catches up from /api/board. Only a page that marks values with
  // data-live* polls at all. An element is touched only when its value
  // differs, so a load straight after a write changes nothing and moves nothing.
  // A board whose shape differs from the page's (an item added, moved or
  // retitled) is never patched: a half-patched tree would be quietly wrong, so
  // the page says it changed and what fixes it instead (showBoardStale).
  const BOARD_POLL_MS = 60000;
  let boardTimer = null;
  let boardBusy = false;
  let boardDone = false; // stale, or no board() on this project: stop polling

  function stopBoard() {
    boardDone = true;
    if (boardTimer !== null) { clearInterval(boardTimer); boardTimer = null; }
  }

  function boardShapeEl() {
    return document.querySelector('[data-live-shape]');
  }

  function applyBoard(data) {
    const wrap = boardShapeEl();
    if (!wrap || !data || typeof data.values !== 'object' || data.values === null) return 0;
    if (data.shape !== wrap.getAttribute('data-live-shape')) {
      showBoardStale();
      return 0;
    }
    const values = data.values;
    const get = (e, attr) => {
      const k = e.getAttribute(attr);
      const v = Object.prototype.hasOwnProperty.call(values, k) ? values[k] : null;
      return typeof v === 'string' ? v : null;
    };
    let changed = 0;
    document.querySelectorAll('[data-live]').forEach(e => {
      const v = get(e, 'data-live');
      if (v !== null && e.textContent !== v) { e.textContent = v; changed++; }
    });
    document.querySelectorAll('[data-live-title]').forEach(e => {
      const v = get(e, 'data-live-title');
      if (v !== null && e.title !== v) { e.title = v; changed++; }
    });
    document.querySelectorAll('[data-live-width]').forEach(e => {
      const v = get(e, 'data-live-width');
      // A percentage, 0-100, digits only: nothing else reaches a style.
      if (v === null || !/^[0-9]{1,3}$/.test(v) || Number(v) > 100) return;
      if (e.style.width !== v + '%') { e.style.width = v + '%'; changed++; }
    });
    document.querySelectorAll('[data-live-status]').forEach(e => {
      const v = get(e, 'data-live-status');
      if (v === null || !/^[a-z][a-z-]*$/.test(v) || e.classList.contains('s-' + v)) return;
      Array.from(e.classList).filter(c => c.startsWith('s-')).forEach(c => e.classList.remove(c));
      e.classList.add('s-' + v);
      changed++;
    });
    if (changed) {
      announce('The lane board has updated.');
      document.dispatchEvent(new CustomEvent('ck:board-updated', { detail: { changed } }));
    }
    return changed;
  }

  // The served page is a PUBLISHED SNAPSHOT (CONSOLE-kit/Q28, Q29), so a reload serves the same page with the
  // same shape and this bar comes straight back (owner, 2026-10-01: "the 'board has changed since page loaded'
  // message is not going away"). It therefore offers no reload: it names the only things that fix it. A page
  // staged and waiting is known from the proposal the server rendered into this page; its button opens the
  // same review dialog as the "New dashboard page waiting" button (bindPageProposal), where "Use this page"
  // publishes. Without one, the steward must stage a new page. The bar stays and
  // cannot be dismissed: polling has stopped, so it is the only sign the numbers on the page are frozen.
  // It is role=status, fixed at the bottom edge and traps nothing, so it never blocks the page.
  const BOARD_STALE_LEAD = 'The dashboard has changed since this page was published, so its live numbers are paused. ';

  function showBoardStale() {
    stopBoard();
    if (document.querySelector('.ck-board-stale')) return;
    const kids = [];
    if (openPageDialog) {
      const show = el('button', { type: 'button', className: 'ck-board-show-proposal',
                                  'aria-haspopup': 'dialog' }, ['Review the new page']);
      show.addEventListener('click', () => openPageDialog(show));
      kids.push(el('span', {}, [BOARD_STALE_LEAD +
        'A new page is waiting: review it and press "Use this page" to publish it. ']), show);
    } else {
      kids.push(el('span', {}, [BOARD_STALE_LEAD +
        'The steward must stage a new page (agent.py page-snapshot) before it can be published here.']));
    }
    const bar = el('div', { className: 'ck-board-stale', role: 'status' }, kids);
    document.body.insertBefore(bar, document.body.firstChild);
  }

  async function fetchBoard() {
    if (boardDone || boardBusy || !config || !config.api || !boardShapeEl()) return;
    if (document.visibilityState === 'hidden') return;
    boardBusy = true;
    try {
      const resp = await fetch(config.api + '/board', { credentials: 'same-origin' });
      if (resp.status === 404) { stopBoard(); return; } // this project offers no live values
      if (!resp.ok) return; // a transient failure: the page stands, the next poll retries
      applyBoard(await resp.json());
    } catch (e) {
      // Offline or asleep: the page as loaded is still true to its own moment.
    } finally {
      boardBusy = false;
    }
  }

  function startBoard() {
    if (!boardShapeEl()) return;
    fetchBoard();
    boardTimer = setInterval(fetchBoard, BOARD_POLL_MS);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') fetchBoard();
    });
  }

  // --- 0.8.6: the usage footer (owner, 2026-09-30: "a probe on a small footer bar for claude
  // usage and account (probed once every 30s-1m)"). The server reads both files and forwards
  // only checked fields; the page formats them as text, never as markup. There is no footer
  // unless the server was started with --usage-file or --account-file, and none against an
  // older server, which answers /api/usage with 404.
  const USAGE_POLL_MS = 45000;
  const USAGE_STALE_MS = 10 * 60 * 1000;  // the status line writes only while a session runs
  let footerEl = null;

  function fmtReset(iso) {
    if (!iso) return '';
    const t = new Date(iso);
    if (isNaN(t.getTime())) return '';
    const time = t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    if (t.toDateString() === new Date().toDateString()) return time;
    return t.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + time;
  }

  function fmtAge(ms) {
    const m = Math.round(ms / 60000);
    if (m < 1) return 'just now';
    if (m < 60) return m + ' min ago';
    const h = Math.round(m / 60);
    return h < 48 ? h + ' h ago' : Math.round(h / 24) + ' days ago';
  }

  function usageWindow(label, w) {
    if (!w || w.used_percentage === null || w.used_percentage === undefined) return label + ' —';
    const r = fmtReset(w.resets_at);
    return label + ' ' + Math.round(w.used_percentage) + '%' + (r ? ' (resets ' + r + ')' : '');
  }

  // Owner, 2026-10-01: "version number should be in footer". The running kit's version, as the server that
  // served this page knows it (`console_kit.__version__`, the value /health reports), never typed in here.
  // It shares the usage footer's bar, which already makes room for itself, and keeps that bar on when usage
  // is off or has nothing to say. A page served by no kit server (no version in its config) gets none.
  function kitVersion() {
    const v = config && config.version;
    return typeof v === 'string' && /^[0-9A-Za-z.+-]{1,40}$/.test(v) ? v : null;
  }

  function renderFooter(u) {
    const root = document.documentElement;
    const version = kitVersion();
    const usageOn = !!(u && u.enabled);
    if (!usageOn && !version) {
      if (footerEl) { footerEl.remove(); footerEl = null; }
      root.classList.remove('ck-footer-on');
      return;
    }
    if (!footerEl) {
      footerEl = el('div', { className: 'ck-footer', role: 'contentinfo',
                             'aria-label': 'Console kit version, Claude usage and account' }, []);
      document.body.appendChild(footerEl);
      root.classList.add('ck-footer-on');
    }
    const parts = [];
    let stale = false;
    const usage = usageOn ? u : {};   // usage off: the bar carries the version alone
    if (usage.usage) {
      const age = Date.now() - new Date(usage.usage.updated_at).getTime();
      stale = age > USAGE_STALE_MS;
      parts.push('Claude usage: ' + usageWindow('5-hour', usage.usage.five_hour) + ', ' +
                 usageWindow('7-day', usage.usage.seven_day));
      parts.push('as of ' + fmtAge(age));
    } else if (usage.usage_problem) {
      parts.push('Claude usage: ' + usage.usage_problem);
    }
    if (usage.account) parts.push(usage.account);
    if (version) parts.push('console-kit ' + version);
    footerEl.textContent = parts.join(' · ');
    footerEl.setAttribute('data-stale', stale ? 'true' : 'false');
    // The host page gets room for the footer at whatever height it wrapped to.
    root.style.setProperty('--ck-footer-h', footerEl.offsetHeight + 'px');
  }

  async function fetchUsage() {
    if (!config || !config.api || document.visibilityState === 'hidden') return;
    try {
      const resp = await fetch(config.api + '/usage', { credentials: 'same-origin' });
      if (resp.status === 404) return renderFooter(null);  // an older server: no footer
      if (!resp.ok) return;  // a blip keeps what is shown; the next poll tries again
      renderFooter(await resp.json());
    } catch (e) {
      // offline: keep what is shown
    }
  }

  function startUsage() {
    renderFooter(null);   // the version shows at once, before (or without) any usage answer
    if (!config || !config.api) return;
    fetchUsage();
    setInterval(fetchUsage, USAGE_POLL_MS);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') fetchUsage();
    });
  }

  // Q29: the owner's "Use this page". The server rendered the proposal; this only sends the commit it
  // showed, so a page staged after the owner looked is refused by name rather than published unseen.
  // Owner, 2026-10-02: "can you create a modal rather than place on bottom (button hides under header)".
  // The server renders the proposal as a <dialog> without `open`, laid out inline until this upgrades it
  // (data-ck-ready), so a page whose console script fails still shows it. Upgraded, it is reviewed in a modal
  // (showModal: focus moves in and is held there, Esc closes, a backdrop) that opens ONLY from a button the
  // owner presses: the fixed "New dashboard page waiting" chip, or the stale-board bar's. Never on load.
  // Opening, closing and Esc publish nothing; the "Use this page" click below is the page's one publish.
  let openPageDialog = null;   // set once the dialog is bound: (opener) => void

  function bindPageProposal() {
    const wrap = document.querySelector('.ck-page-proposed');
    const dlg = wrap && wrap.querySelector('dialog.ck-page-dialog');
    const btn = dlg && dlg.querySelector('.ck-page-use');
    if (!btn || !config || !config.api || typeof dlg.showModal !== 'function') return;
    const errEl = dlg.querySelector('.ck-page-use-error');
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      btn.textContent = 'Publishing…';
      let resp = null;
      let data = {};
      try {
        resp = await fetch(config.api + '/page-publish', { method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ commit: btn.getAttribute('data-commit') }) });
        data = await resp.json().catch(() => ({ error: 'HTTP ' + resp.status }));
      } catch (e) {
        data = { error: "Can't reach the console server; nothing was published. Press again when it is back." };
      }
      if (resp && resp.ok) {
        location.reload();
        return;
      }
      btn.disabled = false;
      btn.textContent = 'Use this page';
      if (errEl) {
        errEl.textContent = 'Not published: ' + (data.error || 'refused');
        errEl.hidden = false;
      }
    });
    const chip = wrap.querySelector('.ck-page-waiting');
    const notNow = dlg.querySelector('.ck-page-not-now');
    let opener = null;
    openPageDialog = from => {
      if (dlg.open) return;
      opener = from || null;
      dlg.showModal();
      dlg.focus();   // the dialog itself, so its name is read and no button is pre-chosen
    };
    dlg.addEventListener('close', () => {
      const o = opener;
      opener = null;
      if (o && o.isConnected) o.focus();
    });
    // Esc is the dialog's own (it closes it); the inbox's document-level Esc must not also close the panel.
    dlg.addEventListener('keydown', e => { if (e.key === 'Escape') e.stopPropagation(); });
    if (notNow) notNow.addEventListener('click', () => dlg.close());
    wrap.setAttribute('data-ck-ready', '');
    if (chip) {
      chip.addEventListener('click', () => openPageDialog(chip));
      chip.hidden = false;
      const root = document.documentElement;
      root.classList.add('ck-page-waiting-on');
      root.style.setProperty('--ck-chip-h', chip.offsetHeight + 'px');
    }
  }

  // Initialize
  function init() {
    // Read config from injected script
    const configEl = document.getElementById('console-kit-config');
    if (configEl) {
      try {
        config = JSON.parse(configEl.textContent);
      } catch (e) {
        config = null;
      }
    }
    createPanel();
    document.addEventListener('keydown', onRoundKey);
    panelEl.addEventListener('focusout', onPanelFocusOut);
    document.addEventListener('visibilitychange', onVisibility);
    // Q33: a page leaving drops every countdown. Timers would die with it anyway, but a page kept in the
    // back-forward cache resumes its timers when shown again, and its lock must not land then.
    // Q33: a countdown never ends where the owner cannot see it. Leaving the page (pagehide, which also covers
    // a back/forward-cache stop: its timers would otherwise resume on return) and hiding the tab both cancel.
    window.addEventListener('pagehide', () => dropAllLocks());
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') dropAllLocks(); });
    injectItemButtons();
    // Initial fetch, then the live loop (0.7.0) keeps the page current.
    bindPageProposal();   // before the board: a stale bar offers the dialog only once it is bound
    fetchView().then(() => {
      if (config && config.api) liveLoop();
      // Inbox default open: on a docked (wide) screen, open the Inbox after the first fetch so a landing
      // owner sees waiting rows without clicking. Narrow screens stay collapsed (the panel is an overlay
      // there and would hide the dashboard). Reading list.sessionStorage.ck-inbox-closed lets the owner
      // close it once and have that stick for the session.
      try {
        const closedThisSession = sessionStorage.getItem('ck-inbox-closed') === '1';
        if (isDocked() && !closedThisSession && panelEl.getAttribute('data-open') !== 'true') {
          openPanel(null, 'inbox');
        }
      } catch (e) { /* storage blocked — fall through */ }
    });
    startBoard();
    startUsage();
  }

  // Public API
  window.ConsoleKit = {
    open: function(itemId) {
      openPanel(itemId, 'item');
    },
    refreshBoard: function() {
      return fetchBoard();
    },
    // 0.8.6: poll the usage footer now, rather than at its next 45 s tick.
    refreshUsage: function() {
      return fetchUsage();
    },
    // 0.7.0: open the inbox on a tab ('inbox', 'feed', 'chat'), or a round's form.
    openTab: function(tab) {
      currentTab = TABS.some(t => t[0] === tab) ? tab : 'inbox';
      openPanel(null, 'inbox');
    },
    openRound: function(forkId) {
      openRound(forkId);
    },
    refresh: function() {
      return fetchView().then(() => {
        if (panelEl && panelEl.getAttribute('data-open') === 'true') {
          renderPanel();
        }
      });
    }
  };

  // Run on DOMContentLoaded or immediately if already loaded
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
