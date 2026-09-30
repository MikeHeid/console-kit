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
    stale: '◌'            // ◌ dotted circle
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
  let currentFork = null; // the answers sheet's fork filter (spec §7.6), or the round the form walks
  const openForms = new Set(); // disclosure keys the owner left open
  let lockingAll = null;        // 0.8.5: the item whose answers are being locked in turn, or null
  const lockAllError = {};      // 0.8.5: item -> why its last 'Lock all' stopped
  // 0.7.0: the inbox's tabs, the round form, and the live loop.
  let currentTab = 'inbox';     // 'inbox' | 'feed' | 'chat', inside inbox mode
  let arrived = new Set();      // qids and message ids that arrived with the latest live update
  let knownIds = null;          // every qid and message id the page has seen; null before the first view
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
  const STEP_WORDS = {
    refine: { title: 'Refine', mode: 'tighten',
      what: 'revise the spec or document this answer rests on, to match what you decided' },
    drill: { title: 'Drill', mode: 'explore',
      what: 'drill into what this leaves unspecified and draft the questions a spec for it needs' }
  };
  const OTHER_ROLE = /^[A-Za-z0-9][A-Za-z0-9 \-]{0,39}$/;
  const MAX_ROLES = 3;
  const STATE_WORDS = { awaiting_you: 'unanswered', unlocked: 'answered, not locked',
    locked: 'locked', stale: 'stale' };

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
      checkPromise = null; // a new view: "why stale" is checked afresh
      cursor = data.cursor;
      noteArrivals();
      updateInboxButton();
      updateItemButtons();
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
    if (knownIds === null) { knownIds = ids; arrived = new Set(); return; }
    arrived = new Set([...ids].filter(i => !knownIds.has(i)));
    knownIds = ids;
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
    return n;
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
    applyDock();
    // Back to what opened it; when that was the strip, it is the strip again.
    if (lastFocused && lastFocused.focus && document.contains(lastFocused)) lastFocused.focus();
  }

  // Render panel content
  function renderPanel() {
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

    // One tab panel; the tab bar above says which view it holds (0.7.0).
    const body = el('div', { className: 'ck-body', role: 'tabpanel', id: 'ck-tabpanel',
      'aria-labelledby': 'ck-tab-' + currentTab });
    if (!view) {
      body.appendChild(renderOffline());
    } else if (currentTab === 'feed') {
      renderFeed(body);
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

  // The inbox's three views (0.7.0): what waits on you, what happened, and the chat.
  const TABS = [['inbox', 'Inbox'], ['feed', 'Feed'], ['chat', 'Chat']];

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
    const rounds = new Map();
    const loose = [];
    for (const qid of view.inbox || []) {
      const q = view.questions[qid];
      if (!q) continue;
      const f = q.question.forked_from;
      if (f && view.forks[f]) {
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
    if (loose.length) {
      body.appendChild(el('div', { className: 'ck-section-heading' }, ['Questions for you']));
      const list = el('div', { className: 'ck-inbox-list' });
      for (const q of loose) {
        const itemData = items[q.question.item];
        const item = el('div', { className: 'ck-inbox-item' + (arrived.has(q.question.qid) ? ' ck-arrived' : ''),
          tabindex: '0', dataQid: q.question.qid }, [
          el('span', { className: 'ck-q-state', dataState: q.state }, [
            GLYPH[q.state] || '', ' ', q.state.replace('_', ' ')
          ]),
          el('span', { className: 'ck-inbox-item-id' }, [q.question.item]),
          el('span', { className: 'ck-inbox-item-title' }, [itemData ? itemData.title : ''])
        ]);
        const go = () => openFromInbox(q.question.item);
        item.addEventListener('click', go);
        item.addEventListener('keydown', e => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
        });
        list.appendChild(item);
      }
      body.appendChild(list);
    }

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

  // 0.8.5: the inbox opens an item in its own panel, so the item must offer the way back.
  function openFromInbox(itemId) {
    currentItem = itemId;
    currentMode = 'item';
    fromInbox = true;
    renderPanel();
    const back = panelEl.querySelector('.ck-back-btn');
    if (back) back.focus();
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
      el('button', {
        className: 'ck-close-btn',
        type: 'button',
        'aria-label': 'Close panel'
      }, ['×'])
    ]);
    header.querySelector('.ck-back-btn').addEventListener('click', fromInbox ? backToInbox : closePanel);
    header.querySelector('.ck-close-btn').addEventListener('click', closePanel);
    panelEl.appendChild(header);

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
      const order = ['awaiting_you', 'unlocked', 'stale', 'locked'];
      qs.sort((a, b) => order.indexOf(a.state) - order.indexOf(b.state));

      body.appendChild(renderItemTools(itemId));

      if (qs.length > 0) {
        body.appendChild(el('div', { className: 'ck-section-heading' }, ['Questions']));
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
      wrap.appendChild(el('div', { className: 'ck-error-msg', role: 'alert' }, [lockAllError[itemId]]));
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
    cancel.addEventListener('click', () => { openForms.delete(key); renderPanel(); });
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
  function renderQuestion(q) {
    const qData = q.question;
    const state = q.state;
    const card = el('div', { className: 'ck-question' });

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
      banner.appendChild(tools);
      banner.appendChild(whySlot);
      banner.appendChild(reSlot);
      bodyEl.appendChild(banner);
    }

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
      const asked = Object.values(view.forks).filter(f => f.message.about_qid === qData.qid)
        .sort((a, b) => b.message.seq - a.message.seq);
      if (asked.length) {
        const f = asked[0];
        const who = f.message.step ? 'a ' + f.message.step
          : (f.message.roles || []).map(r => ROSTER_LABEL[r] || r.replace(/^other:/, '')).join(', ');
        bodyEl.appendChild(el('p', { className: 'ck-muted' }, [
          '⑂ ' + (f.message.step ? STEP_WORDS[f.message.step].title : 'Follow-up with ' + who) + ' ' +
          relTime(f.message.ts) + ': ' + (f.questions.length
            ? f.questions.length + ' question' + (f.questions.length === 1 ? '' : 's') + ' back (' + f.questions.join(', ') + ').'
            : 'waiting for the seats\' questions.')]));
      }
    } else if (state === 'unlocked') {
      // Fix #4: Two-step locking — first show receipt and "Lock this answer..." button
      bodyEl.appendChild(renderReceipt(qData, headAnswer));
      const actions = el('div', { className: 'ck-actions' });
      const truncText = truncateText(qData.text, 40);
      const lockTriggerBtn = el('button', {
        className: 'ck-btn ck-btn-primary',
        type: 'button',
        'aria-label': 'Lock this answer: ' + truncText
      }, ['Lock this answer…']);
      lockTriggerBtn.addEventListener('click', () => {
        // Replace actions with confirmation step
        actions.textContent = '';
        actions.appendChild(renderLockConfirm(q, headAnswer));
      });
      actions.appendChild(lockTriggerBtn);
      // Also allow re-answering
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
      bodyEl.appendChild(actions);
    } else {
      // awaiting_you: show answer form
      bodyEl.appendChild(renderAnswerForm(q, false, null));
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
    box.appendChild(el('div', { className: 'ck-visual-title' }, [v.title]));
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
      // Mermaid is shown as its source, as text: no diagram library runs on this page.
      const pre = el('pre', { className: 'ck-visual-code', 'aria-label': 'Mermaid source: ' + v.title });
      pre.textContent = 'Loading…';
      box.appendChild(pre);
      fetch(src, { credentials: 'same-origin' }).then(async resp => {
        const body = await resp.text();
        if (resp.ok) { pre.textContent = body; return; }
        let msg = 'HTTP ' + resp.status;
        try { msg = JSON.parse(body).error || msg; } catch (e) { /* not JSON: keep the status */ }
        pre.textContent = 'Not shown: ' + msg;
      }).catch(e => { pre.textContent = 'Not shown: ' + (e.message || 'network error'); });
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

  // Render lock confirmation step (fix #4: two-step with Cancel)
  function renderLockConfirm(q, answer) {
    const truncText = truncateText(q.question.text, 40);
    const wrap = el('div', { className: 'ck-confirm' }, [
      el('div', { className: 'ck-confirm-heading' }, ['Lock this answer?'])
    ]);
    wrap.appendChild(renderReceipt(q.question, answer));
    const actions = el('div', { className: 'ck-actions', style: 'margin-top: 12px;' });
    const lockBtn = el('button', {
      className: 'ck-btn ck-btn-primary',
      type: 'button',
      'aria-label': 'Lock answer: ' + truncText
    }, ['Lock answer']);
    lockBtn.addEventListener('click', async () => {
      lockBtn.disabled = true;
      const result = await apiPost('/lock', { qid: q.question.qid, answer: answer.id }, 'lock-' + q.question.qid);
      lockBtn.disabled = false;
      if (result.error) {
        announce('Error: ' + result.error);
      } else {
        renderPanel();
      }
    });
    actions.appendChild(lockBtn);
    // Cancel button to dismiss confirmation
    const cancelBtn = el('button', { className: 'ck-btn', type: 'button' }, ['Cancel']);
    cancelBtn.addEventListener('click', () => renderPanel());
    actions.appendChild(cancelBtn);
    wrap.appendChild(actions);
    return wrap;
  }

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
    const sheet = answersSheet(itemId, null);
    wrap.appendChild(renderReadyButton(itemId, sheet.counts));
    return wrap;
  }

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
  function seatPicker(key, errEl, withRoar) {
    const picked = new Set();
    const fs = el('fieldset', { className: 'ck-roster' }, [el('legend', {}, ['Seats (1 to 3)'])]);
    const boxes = [];
    const otherInput = el('input', { type: 'text', className: 'ck-input', maxlength: '40', id: key + '-other',
      placeholder: 'e.g. Legal, Lighting designer' });
    // Roar (0.8.0): a three-round panel of its own, so it is picked alone. At most once per
    // question: the server refuses a second, naming the first, and the refusal is shown here.
    const roarBox = withRoar ? el('input', { type: 'checkbox', value: ROAR, name: key + '-seat' }) : null;
    const sync = () => {
      if (errEl) errEl.textContent = '';  // a changed pick clears the last complaint about it
      const roar = !!(roarBox && roarBox.checked);
      const full = picked.size + (otherInput.value.trim() ? 1 : 0) >= MAX_ROLES;
      for (const b of boxes) b.disabled = roar || (full && !b.checked);
      otherInput.disabled = roar;
      if (roarBox) roarBox.disabled = !roar && (picked.size > 0 || !!otherInput.value.trim());
    };
    for (const r of ROSTER) {
      const b = el('input', { type: 'checkbox', value: r, name: key + '-seat' });
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
      if (m === 'tighten') r.checked = true;
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
        openForms.delete(key);
        announce('Follow-up requested on ' + qData.qid + '.');
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
    const f = view && view.forks[forkId];
    if (!f) return [];
    return f.questions.map(qid => view.questions[qid]).filter(Boolean)
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
    const f = view && view.forks[forkId];
    const m = f ? f.message : null;
    const title = el('span', { className: 'ck-round-title', tabindex: '-1' },
      [m ? 'Round: ' + m.item + ' · ' + m.mode : 'Round']);
    panelEl.appendChild(makeHeader([title], leaveRound, 'Back to inbox'));
    panelEl.appendChild(renderStatusBar(null));
    const body = el('div', { className: 'ck-body ck-round' });
    panelEl.appendChild(body);
    if (!view) { body.appendChild(renderOffline()); return; }
    if (!f) { body.appendChild(el('p', {}, ['This round is not in the console any more.'])); return; }
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

  function roundGo(forkId, delta) {
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
    const tighten = view.forks[forkId].message.mode === 'tighten';
    body.appendChild(roundProgress(forkId, mem, steps));
    const card = el('div', { className: 'ck-round-step', role: 'group', 'aria-labelledby': 'ck-round-qtext' });
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
        });
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
      saveRound(forkId);
      refreshRoundProgress(forkId, mem, steps);
    });
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
    go.addEventListener('click', () => lockAll(forkId, go));
    const back = el('button', { className: 'ck-btn', type: 'button' }, ['Back to the questions']);
    back.disabled = steps.length === 0;
    back.addEventListener('click', () => { mem.review = false; saveRound(forkId); renderPanel(); focusRoundStep(); });
    body.appendChild(el('div', { className: 'ck-actions ck-round-nav' }, [go, back]));
    body.appendChild(el('p', { className: 'ck-muted' }, [
      'Each picked answer is locked, then one "Answers are in" request goes to the agent. If the server would ' +
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
    sheet.addEventListener('click', () => { mem.result = null; showSheet(view.forks[forkId].message.item, forkId); });
    body.appendChild(el('div', { className: 'ck-actions' }, [back, sheet]));
  }

  // ---------------------------------------------------------------------------
  // The Feed (0.7.0): every store record as an event, newest first, filterable
  // by kind and by item. Folds and PR merges are not store records, so they are
  // not here; the footer says so.
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
      'Folds and pull-request merges happen in the repository, not in the console\'s store, so they are not ' +
      'listed here: see the project\'s pull requests.']));
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
    renderPanel();
    const again = panelEl.querySelector('.ck-body');
    if (again && currentTab !== 'chat') again.scrollTop = top;
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
  // the page says it changed and offers a reload instead.
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

  function showBoardStale() {
    stopBoard();
    if (document.querySelector('.ck-board-stale')) return;
    const reload = el('button', { type: 'button', className: 'ck-board-reload' }, ['Reload']);
    reload.addEventListener('click', () => location.reload());
    const bar = el('div', { className: 'ck-board-stale', role: 'status' }, [
      el('span', {}, ['The board has changed since this page loaded. ']), reload
    ]);
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
    injectItemButtons();
    // Initial fetch, then the live loop (0.7.0) keeps the page current
    fetchView().then(() => { if (config && config.api) liveLoop(); });
    startBoard();
  }

  // Public API
  window.ConsoleKit = {
    open: function(itemId) {
      openPanel(itemId, 'item');
    },
    refreshBoard: function() {
      return fetchBoard();
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
