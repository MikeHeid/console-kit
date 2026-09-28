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
  let lastFocused = null;
  let currentItem = null;
  let currentMode = null; // 'item' or 'inbox'
  let pendingNonces = {};
  let draftTexts = {};

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

  // Update inbox button badge — use view.inbox.length to avoid double-counting rolled-up totals
  function updateInboxButton() {
    if (!inboxBtn || !view) return;
    // Fix #1: inbox.length is the true count, not summed totals which double-count children
    const total = (view.inbox ? view.inbox.length : 0);
    const countEl = inboxBtn.querySelector('.ck-inbox-count');
    if (countEl) countEl.textContent = total || '';
    inboxBtn.setAttribute('data-empty', total === 0 ? 'true' : 'false');
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
      if (t.awaiting_agent > 0) parts.push(GLYPH.awaiting_agent + ' ' + t.awaiting_agent + ' agent');
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

    // Inbox button
    inboxBtn = el('button', {
      className: 'ck-inbox-btn',
      type: 'button',
      'aria-label': 'Open inbox'
    }, [
      el('span', {}, ['Inbox']),
      el('span', { className: 'ck-inbox-count' }, ['0'])
    ]);
    inboxBtn.addEventListener('click', () => openPanel(null, 'inbox'));
    document.body.appendChild(inboxBtn);

    // Escape key handler
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && panelEl.getAttribute('data-open') === 'true') {
        closePanel();
      }
    });
  }

  // Focus trapping inside panel
  function trapFocus(e) {
    if (panelEl.getAttribute('data-open') !== 'true') return;
    const focusable = panelEl.querySelectorAll(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
    );
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
    backdropEl.setAttribute('data-open', 'true');
    panelEl.setAttribute('data-open', 'true');
    panelEl.setAttribute('aria-label', mode === 'inbox' ? 'Inbox' : 'Console: ' + (itemId || ''));
    renderPanel();
    // Fix #2: Focus the close button reliably after render
    requestAnimationFrame(() => {
      const closeBtn = panelEl.querySelector('.ck-close-btn');
      if (closeBtn) closeBtn.focus();
    });
    panelEl.addEventListener('keydown', trapFocus);
    // Fetch fresh view
    fetchView().then(() => {
      renderPanel();
      // Re-focus after re-render if panel still open
      requestAnimationFrame(() => {
        if (panelEl.getAttribute('data-open') === 'true' && !panelEl.contains(document.activeElement)) {
          const closeBtn = panelEl.querySelector('.ck-close-btn');
          if (closeBtn) closeBtn.focus();
        }
      });
    });
  }

  // Close panel
  function closePanel() {
    panelEl.setAttribute('data-open', 'false');
    backdropEl.setAttribute('data-open', 'false');
    panelEl.removeEventListener('keydown', trapFocus);
    if (lastFocused && lastFocused.focus) lastFocused.focus();
  }

  // Render panel content
  function renderPanel() {
    panelEl.textContent = '';
    if (currentMode === 'inbox') {
      renderInbox();
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
        body.appendChild(el('div', { className: 'ck-section-heading' }, ['Awaiting agent']));
        const list = el('div', { className: 'ck-inbox-list' });
        for (const itemId of view.awaiting_agent) {
          const itemData = items[itemId];
          const item = el('div', { className: 'ck-inbox-item', tabindex: '0' }, [
            el('span', { className: 'ck-q-state', dataState: 'awaiting_agent' }, [
              GLYPH.awaiting_agent, ' awaiting agent'
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

      if (qs.length > 0) {
        body.appendChild(el('div', { className: 'ck-section-heading' }, ['Questions']));
        for (const q of qs) {
          body.appendChild(renderQuestion(q));
        }
      }

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
      if (t.awaiting_agent) counts.appendChild(el('span', {}, [GLYPH.awaiting_agent + ' ' + t.awaiting_agent + ' awaiting agent']));
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
    const textCol = el('div', { style: 'flex: 1;' });
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
      bodyEl.appendChild(actions);
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
    if (draftTexts[draftKey]) ownTextEl.value = draftTexts[draftKey];
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
  }

  // Public API
  window.ConsoleKit = {
    open: function(itemId) {
      openPanel(itemId, 'item');
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
