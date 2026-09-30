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
  let pendingNonces = {};
  let draftTexts = {};
  let currentFork = null; // the answers sheet's fork filter (spec §7.6)
  const openForms = new Set(); // disclosure keys the owner left open

  // Mirrors schema.py: the fork fields and the D13 roster. The server refuses
  // anything else by name, so these only shape the form.
  const FOCUSES = ['whole', 'code', 'design', 'ui', 'backend'];
  const MODES = ['explore', 'tighten'];
  const ROSTER = ['devops', 'ux', 'adversarial', 'security', 'architect', 'analyst'];
  const ROSTER_LABEL = { devops: 'DevOps', ux: 'UX', adversarial: 'Adversarial (red team)',
    security: 'Security', architect: 'Architect', analyst: 'Analyst' };
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

  function conditionWords(c) {
    return c.kind === 'item_status'
      ? 'item ' + c.item + ' has status ' + c.status
      : c.path + ' is unchanged since the question was asked';
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
      cursor = data.cursor;
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
  function agentWords(itemId) { return agentActive(itemId) ? 'agent active' : 'awaiting agent'; }

  // Update inbox button badge — use view.inbox.length to avoid double-counting rolled-up totals
  function updateInboxButton() {
    if (!inboxBtn || !view) return;
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
    for (const b of [inboxBtn, dockStrip]) {
      if (!b) continue;
      const countEl = b.querySelector('.ck-inbox-count');
      if (countEl) countEl.textContent = total || '';
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

  // Update item indicator buttons in the dashboard
  function updateItemButtons() {
    if (!view) return;
    document.querySelectorAll('details[id^="item-"]').forEach(det => {
      const id = det.id.replace('item-', '');
      const btn = det.querySelector('.ck-item-btn');
      if (!btn) return;
      const data = view.items[id];
      if (!data) return;
      const t = data.total;
      const parts = [];
      if (t.awaiting_you > 0) parts.push(GLYPH.awaiting_you + ' ' + t.awaiting_you + ' you');
      if (t.awaiting_agent > 0) parts.push(GLYPH.awaiting_agent + ' ' + t.awaiting_agent + (agentActive(id) ? ' agent active' : ' agent'));
      if (t.unlocked > 0) parts.push(GLYPH.unlocked + ' ' + t.unlocked + ' unlocked');
      if (t.stale > 0) parts.push(GLYPH.stale + ' ' + t.stale + ' stale');
      const hasItems = parts.length > 0;
      btn.setAttribute('data-has-items', hasItems ? 'true' : 'false');
      // Clear and rebuild
      btn.textContent = '';
      if (hasItems) {
        btn.appendChild(document.createTextNode(parts.join('  ')));
      } else {
        btn.appendChild(document.createTextNode('discuss'));
      }
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
      el('span', { className: 'ck-agent-count', 'aria-hidden': 'true' }, [''])
    ]);
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
      el('span', { className: 'ck-agent-count', 'aria-hidden': 'true' }, [''])
    ]);
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
    if (currentMode === 'inbox') {
      renderInbox();
    } else if (currentMode === 'sheet') {
      renderSheet(currentItem, currentFork);
    } else if (currentItem) {
      renderItem(currentItem);
    }
  }

  // Render inbox mode
  function renderInbox() {
    const header = el('div', { className: 'ck-header' }, [
      el('button', {
        className: 'ck-back-btn',
        type: 'button',
        'aria-label': 'Back to board'
      }, ['← Back']),
      el('span', { className: 'ck-title' }, ['Inbox']),
      el('button', {
        className: 'ck-close-btn',
        type: 'button',
        'aria-label': 'Close panel'
      }, ['×'])
    ]);
    header.querySelector('.ck-back-btn').addEventListener('click', closePanel);
    header.querySelector('.ck-close-btn').addEventListener('click', closePanel);
    panelEl.appendChild(header);

    // Status bar
    const statusBar = renderStatusBar(null);
    panelEl.appendChild(statusBar);

    // Body
    const body = el('div', { className: 'ck-body' });
    if (!view) {
      body.appendChild(renderOffline());
    } else {
      // Questions awaiting owner
      if (view.inbox && view.inbox.length > 0) {
        body.appendChild(el('div', { className: 'ck-section-heading' }, ['Questions for you']));
        const list = el('div', { className: 'ck-inbox-list' });
        for (const qid of view.inbox) {
          const q = view.questions[qid];
          if (!q) continue;
          const itemData = items[q.question.item];
          const item = el('div', { className: 'ck-inbox-item', tabindex: '0' }, [
            el('span', { className: 'ck-q-state', dataState: q.state }, [
              GLYPH[q.state] || '', ' ', q.state.replace('_', ' ')
            ]),
            el('span', { className: 'ck-inbox-item-id' }, [q.question.item]),
            el('span', { className: 'ck-inbox-item-title' }, [itemData ? itemData.title : ''])
          ]);
          item.addEventListener('click', () => {
            currentItem = q.question.item;
            currentMode = 'item';
            renderPanel();
          });
          item.addEventListener('keydown', e => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              currentItem = q.question.item;
              currentMode = 'item';
              renderPanel();
            }
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
          item.addEventListener('click', () => {
            currentItem = itemId;
            currentMode = 'item';
            renderPanel();
          });
          list.appendChild(item);
        }
        body.appendChild(list);
      }

      if ((!view.inbox || view.inbox.length === 0) && (!view.awaiting_agent || view.awaiting_agent.length === 0)) {
        body.appendChild(el('p', { style: 'color: var(--c-fg-muted); text-align: center; padding: 20px;' },
          ['No pending items.']));
      }
    }
    panelEl.appendChild(body);
  }

  // Render item view
  function renderItem(itemId) {
    const itemData = items ? items[itemId] : null;

    const header = el('div', { className: 'ck-header' }, [
      el('button', {
        className: 'ck-back-btn',
        type: 'button',
        'aria-label': 'Back to board'
      }, ['← Back']),
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
    header.querySelector('.ck-back-btn').addEventListener('click', closePanel);
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
        for (const q of qs) {
          body.appendChild(renderQuestion(q));
        }
      }

      body.appendChild(renderForks(itemId));

      // Thread
      body.appendChild(renderThread(itemId));
    }
    panelEl.appendChild(body);
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
        else if (c.kind === 'item_status') msg = c.item + ' is no longer ' + c.status;
        list.appendChild(el('li', {}, [msg]));
      }
      banner.appendChild(list);
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
      // Follow up on this locked answer with other seats (0.4.0).
      const [fuBtn, fuSlot] = disclosure('⑂ Follow up…', 'ask-' + qData.qid, () => renderAnswerFollowUp(q));
      fuBtn.setAttribute('aria-label', 'Follow up with other seats on: ' + truncText);
      actions.appendChild(fuBtn);
      bodyEl.appendChild(actions);
      bodyEl.appendChild(fuSlot);
      const asked = Object.values(view.forks).filter(f => f.message.about_qid === qData.qid)
        .sort((a, b) => b.message.seq - a.message.seq);
      if (asked.length) {
        const f = asked[0];
        const who = (f.message.roles || []).map(r => ROSTER_LABEL[r] || r.replace(/^other:/, '')).join(', ');
        bodyEl.appendChild(el('p', { className: 'ck-muted' }, [
          '⑂ Follow-up with ' + who + ' ' + relTime(f.message.ts) + ': ' + (f.questions.length
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

    card.appendChild(bodyEl);
    return card;
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
        byEl.textContent = m.by;
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
    wrap.appendChild(el('div', { className: 'ck-actions' }, [answersBtn, forkBtn]));
    wrap.appendChild(slot);
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
  function seatPicker(key, errEl) {
    const picked = new Set();
    const fs = el('fieldset', { className: 'ck-roster' }, [el('legend', {}, ['Seats (1 to 3)'])]);
    const boxes = [];
    const otherInput = el('input', { type: 'text', className: 'ck-input', maxlength: '40', id: key + '-other',
      placeholder: 'e.g. Legal, Lighting designer' });
    const sync = () => {
      if (errEl) errEl.textContent = '';  // a changed pick clears the last complaint about it
      const full = picked.size + (otherInput.value.trim() ? 1 : 0) >= MAX_ROLES;
      for (const b of boxes) b.disabled = full && !b.checked;
    };
    for (const r of ROSTER) {
      const b = el('input', { type: 'checkbox', value: r, name: key + '-seat' });
      b.addEventListener('change', () => { if (b.checked) picked.add(r); else picked.delete(r); sync(); });
      boxes.push(b);
      fs.appendChild(el('label', { className: 'ck-option' }, [b, ' ' + ROSTER_LABEL[r]]));
    }
    otherInput.addEventListener('input', sync);
    fs.appendChild(el('label', { for: key + '-other', className: 'ck-field-label' }, ['Other seat (optional)']));
    fs.appendChild(otherInput);
    const fail = msg => { if (errEl) errEl.textContent = msg; announce(msg); return null; };
    const roles = () => {
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
    const seats = seatPicker(id, err);
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
      if (m.roles) bits.push(m.roles.map(r => ROSTER_LABEL[r] || r.replace(/^other:/, '')).join(', '));
      else bits.push('default committee');
      card.appendChild(el('div', { className: 'ck-fork-head' }, [bits.join(' · ') + ' · ' + relTime(m.ts)]));
      const t = el('div', { className: 'ck-message-text' });
      t.textContent = m.text;
      card.appendChild(t);
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
        if (qs.every(q => q.state !== 'awaiting_you')) {
          const [fuBtn, slot] = disclosure('Follow up with other seats…', 'fu-' + m.id,
            () => renderForkForm(itemId, m.id));
          actions.appendChild(fuBtn);
          card.appendChild(actions);
          card.appendChild(slot);
        } else {
          card.appendChild(actions);
        }
      }
      wrap.appendChild(card);
    }
    return wrap;
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
    injectItemButtons();
    // Initial fetch
    fetchView();
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
