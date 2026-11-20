/* Pinguide chat UI: sidebar history, streaming answers, Markdown, edit/retry. */
'use strict';

const $ = (id) => document.getElementById(id);
const els = {
  app: $('app'), sidebar: $('sidebar'), scrim: $('scrim'), menuBtn: $('menuBtn'),
  sidebarClose: $('sidebarClose'), newChat: $('newChat'), chatSearch: $('chatSearch'),
  chatList: $('chatList'), account: $('account'), chatTitle: $('chatTitle'),
  signin: $('signinDialog'), signinError: $('signinError'), googleBtn: $('googleBtn'),
  signinOr: $('signinOr'), emailForm: $('emailForm'), emailInput: $('emailInput'),
  emailSent: $('emailSent'), help: $('helpDialog'),
  messages: $('messages'), thread: $('thread'), composer: $('composer'),
  input: $('composerInput'), sendBtn: $('sendBtn'), srStatus: $('srStatus'),
};

const SUGGESTIONS = [
  'How does multiball work in Metallica?',
  'How do I get better at post passes?',
  "What's the newest Stern machine?",
  'What should I check before buying a used pinball machine?',
];

// ---------------------------------------------------------------------------
// Chat storage. In-memory for visitors who aren't signed in (gone when the tab
// closes); the same interface is backed by the server for signed-in users.
// ---------------------------------------------------------------------------

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Server error ${res.status}`);
  return data;
}

const memoryStore = {
  chats: [],
  async list(query) {
    const q = (query || '').trim().toLowerCase();
    return this.chats
      .filter((c) => !q || c.title.toLowerCase().includes(q)
        || c.messages.some((m) => m.content.toLowerCase().includes(q)))
      .sort((a, b) => b.updated - a.updated);
  },
  async load(id) { return this.chats.find((c) => c.id === id) || null; },
  async create() {
    const chat = { id: crypto.randomUUID(), title: 'New chat', updated: Date.now(), messages: [] };
    this.chats.push(chat);
    return chat;
  },
  async rename(id, title) { const c = await this.load(id); if (c) c.title = title; },
  async remove(id) { this.chats = this.chats.filter((c) => c.id !== id); },
};

const serverStore = {
  async list(query) {
    const d = await api(`/api/conversations?q=${encodeURIComponent((query || '').trim())}`);
    return d.conversations.map((c) => ({ id: c.id, title: c.title, updated: Date.parse(c.updated) }));
  },
  async load(id) {
    const c = await api(`/api/conversations/${encodeURIComponent(id)}`);
    return { ...c, updated: Date.parse(c.updated) };
  },
  // The browser picks the id; the server creates the chat with the first message.
  async create() { return { id: crypto.randomUUID(), title: 'New chat', updated: Date.now(), messages: [] }; },
  async rename(id, title) {
    await api(`/api/conversations/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify({ title }) });
  },
  async remove(id) { await api(`/api/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' }); },
};

let store = memoryStore;
let user = null;         // {email, name} when signed in
let current = null;      // the open chat: {id, title, messages: [...]}
let streaming = null;    // {controller} while an answer streams

// ---------------------------------------------------------------------------
// Markdown (sanitized: answers can quote web pages)
// ---------------------------------------------------------------------------

let mdReady = false;
function setupMarkdown() {
  if (mdReady || !window.marked || !window.DOMPurify) return mdReady;
  marked.setOptions({ gfm: true, breaks: false });
  DOMPurify.addHook('afterSanitizeAttributes', (node) => {
    if (node.tagName === 'A') {
      node.setAttribute('target', '_blank');
      node.setAttribute('rel', 'noopener noreferrer');
    }
  });
  mdReady = true;
  return true;
}

function renderMarkdown(el, text) {
  if (setupMarkdown()) {
    el.innerHTML = DOMPurify.sanitize(marked.parse(text), { USE_PROFILES: { html: true } });
  } else {
    el.textContent = text; // CDN unavailable: plain text is still readable
  }
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function announce(text) {
  els.srStatus.textContent = '';
  setTimeout(() => { els.srStatus.textContent = text; }, 50);
}

function nearBottom() {
  const m = els.messages;
  return m.scrollHeight - m.scrollTop - m.clientHeight < 120;
}
function scrollToBottom() { els.messages.scrollTop = els.messages.scrollHeight; }

function renderWelcome() {
  els.thread.innerHTML = '';
  const box = document.createElement('div');
  box.className = 'welcome';
  box.innerHTML = '<img class="welcome-logo" src="/static/logo.png" alt="Pinguide" width="240" height="161">'
    + '<h2>Your pinball companion</h2>'
    + '<p>Rules and strategy for thousands of machines, playing tips, buying and repair advice, '
    + 'and the latest pinball news.</p>'
    + '<p class="welcome-help">New here? <button type="button" class="link-btn" aria-haspopup="dialog">'
    + 'See how to use Pinguide</button></p>';
  box.querySelector('.welcome-help button').addEventListener('click', openHelp);
  const grid = document.createElement('div');
  grid.className = 'suggestions';
  for (const s of SUGGESTIONS) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'suggestion';
    b.textContent = s;
    b.addEventListener('click', () => send(s));
    grid.appendChild(b);
  }
  box.appendChild(grid);
  els.thread.appendChild(box);
}

function renderThread() {
  els.chatTitle.textContent = current ? current.title : 'New chat';
  document.title = current ? `${current.title} | Pinguide` : 'Pinguide: your AI pinball companion';
  if (!current || current.messages.length === 0) { renderWelcome(); return; }
  els.thread.innerHTML = '';
  current.messages.forEach((m, i) => els.thread.appendChild(renderMessage(m, i)));
  scrollToBottom();
}

function actionButton(label, onClick) {
  const b = document.createElement('button');
  b.type = 'button';
  b.textContent = label;
  b.addEventListener('click', onClick);
  return b;
}

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
    button.textContent = 'Copied';
    announce('Copied to clipboard');
    setTimeout(() => { button.textContent = 'Copy'; }, 1500);
  } catch {
    announce('Copy failed');
  }
}

function renderMessage(m, index) {
  const art = document.createElement('article');
  art.className = `msg ${m.role}`;
  if (m.role === 'user') {
    art.setAttribute('aria-label', 'You said');
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = m.content;
    art.appendChild(bubble);
    const actions = document.createElement('div');
    actions.className = 'msg-actions';
    actions.append(
      actionButton('Edit', () => startEdit(art, index)),
      actionButton('Copy', (e) => copyText(m.content, e.currentTarget)),
    );
    art.appendChild(actions);
    return art;
  }

  art.setAttribute('aria-label', 'Pinguide said');
  const parts = {};

  parts.thinking = document.createElement('details');
  parts.thinking.className = 'thinking-panel';
  const summary = document.createElement('summary');
  summary.textContent = 'Thought process';
  parts.thinking.appendChild(summary);
  parts.thinkingText = document.createElement('div');
  parts.thinkingText.className = 'thinking-text';
  parts.thinking.appendChild(parts.thinkingText);
  parts.thinking.hidden = !m.thinking;
  parts.thinkingText.textContent = m.thinking || '';
  art.appendChild(parts.thinking);

  parts.searches = document.createElement('div');
  art.appendChild(parts.searches);
  for (const q of m.searches || []) parts.searches.appendChild(searchLine(q));

  parts.status = document.createElement('div');
  parts.status.className = 'status-line';
  parts.status.hidden = true;
  art.appendChild(parts.status);

  parts.body = document.createElement('div');
  parts.body.className = 'md';
  if (m.error) {
    parts.body.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'error-text';
    p.textContent = m.error;
    parts.body.appendChild(p);
  } else {
    renderMarkdown(parts.body, m.content || '');
  }
  art.appendChild(parts.body);

  parts.sources = document.createElement('div');
  art.appendChild(parts.sources);
  if (m.sources && m.sources.length) parts.sources.replaceWith(parts.sources = buildSources(m.sources));

  parts.actions = document.createElement('div');
  parts.actions.className = 'msg-actions';
  art.appendChild(parts.actions);
  fillAssistantActions(parts.actions, m, index);

  art._parts = parts;
  return art;
}

function fillAssistantActions(container, m, index) {
  container.innerHTML = '';
  if (m.content) container.appendChild(actionButton('Copy', (e) => copyText(m.content, e.currentTarget)));
  if (current && index === current.messages.length - 1) {
    container.appendChild(actionButton('Retry', () => retry(index)));
  }
}

function searchLine(query) {
  const div = document.createElement('div');
  div.className = 'search-line';
  div.append('Searched the web for ');
  const q = document.createElement('span');
  q.className = 'q';
  q.textContent = `“${query}”`;
  div.appendChild(q);
  return div;
}

function setStatus(parts, text) {
  if (!text) { parts.status.hidden = true; parts.status.textContent = ''; return; }
  parts.status.hidden = false;
  parts.status.innerHTML = '<span class="dots" aria-hidden="true"><span></span><span></span><span></span></span>';
  parts.status.append(text);
}

function sourceLabel(url) {
  if (!url) return '';
  const known = [
    ['pinballrulesheets.com', 'Pinball Rulesheets'], ['tiltforums.com', 'Tiltforums'],
    ['pinball.org', 'PAPA'], ['silverballmania.com', "Bob's Guide"], ['sternpinball.com', 'Stern'],
    ['chicago-gaming.com', 'Chicago Gaming'], ['kineticist.com', 'Kineticist'],
  ];
  for (const [host, label] of known) if (url.includes(host)) return label;
  try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return ''; }
}

function buildSources(sources) {
  const wrap = document.createElement('div');
  wrap.className = 'sources';
  const h = document.createElement('p');
  h.className = 'sources-heading';
  h.textContent = 'Sources';
  wrap.appendChild(h);
  const list = document.createElement('div');
  list.className = 'sources-list';
  const seen = new Set();
  for (const s of sources) {
    const key = s.web ? s.url : `${s.game}|${s.section_name}`;
    if (seen.has(key)) continue;
    seen.add(key);
    if (seen.size > 10) break;
    const safeUrl = /^https?:\/\//i.test(s.url || '') ? s.url : '';
    const chip = document.createElement(safeUrl ? 'a' : 'span');
    chip.className = 'source-chip';
    if (safeUrl) { chip.href = safeUrl; chip.target = '_blank'; chip.rel = 'noopener noreferrer'; }
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = s.web ? (s.title || sourceLabel(safeUrl)) : `${s.game}: ${s.section_name}`;
    chip.appendChild(name);
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = `${sourceLabel(safeUrl)}${safeUrl ? ' ↗' : ''}`;
    chip.appendChild(label);
    if (safeUrl) chip.setAttribute('aria-label', `${name.textContent}, ${sourceLabel(safeUrl)} (opens in a new tab)`);
    list.appendChild(chip);
  }
  wrap.appendChild(list);
  return wrap;
}

// ---------------------------------------------------------------------------
// Sending and streaming
// ---------------------------------------------------------------------------

function setStreaming(on) {
  els.sendBtn.classList.toggle('stop', on);
  els.sendBtn.setAttribute('aria-label', on ? 'Stop answer' : 'Send message');
}

async function send(text) {
  text = (text || '').trim();
  if (!text || streaming) return;
  if (!current) {
    current = await store.create();
    renderSidebar();
  }
  // Signed in: the server holds the history; "keep" says how many earlier
  // messages stay (Edit and Retry cut the chat back before sending).
  const payload = store === serverStore
    ? { message: text, conversation_id: current.id, keep: current.messages.length }
    : {
      message: text,
      history: current.messages.filter((m) => m.content && !m.error)
        .map((m) => ({ role: m.role, content: m.content })),
    };
  current.messages.push({ role: 'user', content: text });
  const reply = { role: 'assistant', content: '', thinking: '', searches: [], sources: [] };
  current.messages.push(reply);
  current.updated = Date.now();
  renderThread();
  await streamAnswer(current, payload, reply);
}

async function streamAnswer(chat, payload, reply) {
  const art = els.thread.lastElementChild;
  const parts = art._parts;
  const controller = new AbortController();
  streaming = { controller };
  setStreaming(true);
  announce('Pinguide is answering');
  setStatus(parts, 'Thinking');

  let frame = 0;
  const paint = () => {
    frame = 0;
    const stick = nearBottom();
    renderMarkdown(parts.body, reply.content);
    if (stick) scrollToBottom();
  };

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: controller.signal,
    });
    if (!res.ok || !res.body) {
      const j = await res.json().catch(() => ({}));
      throw new Error(j.error || `Server error ${res.status}`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, cut);
        buf = buf.slice(cut + 2);
        for (const line of block.split('\n')) {
          if (!line.startsWith('data: ')) continue;
          const ev = JSON.parse(line.slice(6));
          if (ev.type === 'status') setStatus(parts, ev.text);
          else if (ev.type === 'thinking') {
            reply.thinking += ev.text;
            parts.thinking.hidden = false;
            parts.thinkingText.textContent = reply.thinking;
            if (!reply.content) setStatus(parts, 'Thinking');
          } else if (ev.type === 'search') {
            reply.searches.push(ev.query);
            parts.searches.appendChild(searchLine(ev.query));
            setStatus(parts, 'Searching the web');
          } else if (ev.type === 'text') {
            reply.content += ev.text;
            setStatus(parts, '');
            if (!frame) frame = requestAnimationFrame(paint);
          } else if (ev.type === 'sources') {
            reply.sources = ev.sources;
          } else if (ev.type === 'done') {
            if (ev.title && chat.title === 'New chat') chat.title = ev.title;
          } else if (ev.type === 'error') {
            reply.error = ev.text;
          }
        }
      }
    }
  } catch (err) {
    if (err.name !== 'AbortError') reply.error = err.message || 'Something went wrong. Please try again.';
  } finally {
    if (frame) cancelAnimationFrame(frame);
    streaming = null;
    setStreaming(false);
    chat.updated = Date.now();
    if (chat === current) renderThread();
    renderSidebar();
    announce(reply.error ? 'The answer failed' : 'Answer ready');
  }
}

function stop() {
  if (streaming) streaming.controller.abort();
}

function retry(index) {
  if (streaming || !current) return;
  const userMsg = current.messages[index - 1];
  if (!userMsg || userMsg.role !== 'user') return;
  current.messages = current.messages.slice(0, index - 1);
  send(userMsg.content);
}

function startEdit(art, index) {
  if (streaming) return;
  const original = current.messages[index].content;
  art.innerHTML = '';
  const box = document.createElement('div');
  box.className = 'edit-box';
  const label = document.createElement('label');
  label.className = 'sr-only';
  label.textContent = 'Edit your message';
  const ta = document.createElement('textarea');
  ta.id = `edit-${index}`;
  label.htmlFor = ta.id;
  ta.value = original;
  const row = document.createElement('div');
  row.className = 'row';
  const cancel = document.createElement('button');
  cancel.type = 'button'; cancel.className = 'btn'; cancel.textContent = 'Cancel';
  const save = document.createElement('button');
  save.type = 'button'; save.className = 'btn primary'; save.textContent = 'Send';
  row.append(cancel, save);
  box.append(label, ta, row);
  art.appendChild(box);
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);

  cancel.addEventListener('click', renderThread);
  const submit = () => {
    const text = ta.value.trim();
    if (!text) return;
    current.messages = current.messages.slice(0, index);
    send(text);
  };
  save.addEventListener('click', submit);
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
    if (e.key === 'Escape') { e.preventDefault(); renderThread(); }
  });
}

// ---------------------------------------------------------------------------
// Sidebar
// ---------------------------------------------------------------------------

function groupLabel(ts) {
  const day = 24 * 3600 * 1000;
  const startOfToday = new Date().setHours(0, 0, 0, 0);
  if (ts >= startOfToday) return 'Today';
  if (ts >= startOfToday - 7 * day) return 'Previous 7 days';
  return 'Older';
}

let openMenu = null;
function closeMenu() {
  if (!openMenu) return;
  openMenu.menu.remove();
  openMenu.button.setAttribute('aria-expanded', 'false');
  openMenu = null;
}

async function renderSidebar() {
  let chats;
  try {
    // Memory chats appear once they have a message; server chats always have one.
    chats = (await store.list(els.chatSearch.value)).filter((c) => !c.messages || c.messages.length);
  } catch {
    els.chatList.innerHTML = '<p class="chat-list-empty">Couldn\u2019t load your chats.</p>';
    return;
  }
  els.chatList.innerHTML = '';
  if (!chats.length) {
    const p = document.createElement('p');
    p.className = 'chat-list-empty';
    p.textContent = els.chatSearch.value ? 'No chats match.' : 'Your chats will appear here.';
    els.chatList.appendChild(p);
    return;
  }
  let group = null;
  let ul = null;
  for (const chat of chats) {
    const label = groupLabel(chat.updated);
    if (label !== group) {
      group = label;
      const h = document.createElement('h2');
      h.className = 'chat-group-label';
      h.textContent = label;
      ul = document.createElement('ul');
      els.chatList.append(h, ul);
    }
    ul.appendChild(chatItem(chat));
  }
}

function chatItem(chat) {
  const li = document.createElement('li');
  li.className = 'chat-item' + (current && chat.id === current.id ? ' active' : '');
  const link = document.createElement('button');
  link.type = 'button';
  link.className = 'chat-link';
  link.textContent = chat.title;
  if (current && chat.id === current.id) link.setAttribute('aria-current', 'page');
  link.addEventListener('click', () => openChat(chat.id));

  const menuBtn = document.createElement('button');
  menuBtn.type = 'button';
  menuBtn.className = 'icon-btn chat-menu-btn';
  menuBtn.setAttribute('aria-label', `Options for ${chat.title}`);
  menuBtn.setAttribute('aria-haspopup', 'true');
  menuBtn.setAttribute('aria-expanded', 'false');
  menuBtn.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24" width="16" height="16"><circle cx="5" cy="12" r="1.8" fill="currentColor"/><circle cx="12" cy="12" r="1.8" fill="currentColor"/><circle cx="19" cy="12" r="1.8" fill="currentColor"/></svg>';
  menuBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    const wasOpen = openMenu && openMenu.button === menuBtn;
    closeMenu();
    if (!wasOpen) showChatMenu(li, menuBtn, chat);
  });
  li.append(link, menuBtn);
  return li;
}

function showChatMenu(li, button, chat) {
  const menu = document.createElement('div');
  menu.className = 'chat-menu';
  const rename = document.createElement('button');
  rename.type = 'button';
  rename.textContent = 'Rename';
  const del = document.createElement('button');
  del.type = 'button';
  del.className = 'danger';
  del.textContent = 'Delete';
  menu.append(rename, del);
  li.appendChild(menu);
  button.setAttribute('aria-expanded', 'true');
  openMenu = { menu, button };
  rename.focus();

  menu.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closeMenu(); button.focus(); }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      (document.activeElement === rename ? del : rename).focus();
    }
  });
  rename.addEventListener('click', () => { closeMenu(); startRename(li, chat); });
  del.addEventListener('click', async () => {
    // Two-step delete instead of a browser confirm() dialog.
    if (del.dataset.confirm !== '1') {
      del.dataset.confirm = '1';
      del.textContent = 'Confirm delete';
      return;
    }
    closeMenu();
    try { await store.remove(chat.id); } catch { announce('Delete failed'); return; }
    if (current && current.id === chat.id) { current = null; renderThread(); }
    renderSidebar();
    announce('Chat deleted');
    els.input.focus();
  });
}

function startRename(li, chat) {
  li.innerHTML = '';
  const label = document.createElement('label');
  label.className = 'sr-only';
  label.textContent = 'Chat name';
  const input = document.createElement('input');
  input.className = 'rename-input';
  input.id = `rename-${chat.id}`;
  label.htmlFor = input.id;
  input.value = chat.title;
  input.maxLength = 120;
  li.append(label, input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const title = input.value.trim();
    if (save && title && title !== chat.title) {
      try { await store.rename(chat.id, title); } catch { announce('Rename failed'); }
      if (current && current.id === chat.id) { current.title = title; els.chatTitle.textContent = title; }
    }
    await renderSidebar();
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
}

async function openChat(id) {
  if (streaming) stop();
  let chat = null;
  try { chat = await store.load(id); } catch { announce('That chat couldn\u2019t be opened'); }
  if (!chat) return;
  current = chat;
  renderThread();
  renderSidebar();
  closeDrawer();
  els.input.focus();
}

function newChat() {
  if (streaming) stop();
  current = null;
  renderThread();
  renderSidebar();
  closeDrawer();
  els.input.focus();
}

// Drawer (phones) / collapse (desktop)
const isMobile = () => window.matchMedia('(max-width: 800px)').matches;

const mainEl = document.querySelector('.main');

function openDrawer() {
  els.app.classList.add('drawer-open');
  mainEl.inert = true; // keyboard focus stays in the drawer
  els.menuBtn.setAttribute('aria-expanded', 'true');
  els.newChat.focus();
}
function closeDrawer() {
  if (!els.app.classList.contains('drawer-open')) return;
  els.app.classList.remove('drawer-open');
  mainEl.inert = false;
  els.menuBtn.setAttribute('aria-expanded', 'false');
  els.menuBtn.focus();
}
function toggleSidebar() {
  if (isMobile()) {
    els.app.classList.contains('drawer-open') ? closeDrawer() : openDrawer();
  } else {
    const collapsed = els.app.classList.toggle('sidebar-collapsed');
    els.menuBtn.setAttribute('aria-expanded', String(!collapsed));
    els.menuBtn.setAttribute('aria-label', collapsed ? 'Show sidebar' : 'Hide sidebar');
  }
}
function syncMenuButton() {
  const open = isMobile() ? els.app.classList.contains('drawer-open') : !els.app.classList.contains('sidebar-collapsed');
  els.menuBtn.setAttribute('aria-expanded', String(open));
  els.menuBtn.setAttribute('aria-label', open ? 'Hide sidebar' : 'Show sidebar');
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------

function autoGrow() {
  els.input.style.height = 'auto';
  els.input.style.height = `${Math.min(els.input.scrollHeight, 220)}px`;
}

els.composer.addEventListener('submit', (e) => {
  e.preventDefault();
  if (streaming) { stop(); return; }
  const text = els.input.value;
  if (!text.trim()) return;
  els.input.value = '';
  autoGrow();
  send(text);
});
els.input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    if (!streaming) els.composer.requestSubmit();
  }
});
els.input.addEventListener('input', autoGrow);

els.newChat.addEventListener('click', newChat);
els.menuBtn.addEventListener('click', toggleSidebar);
els.sidebarClose.addEventListener('click', closeDrawer);
els.scrim.addEventListener('click', closeDrawer);
let searchTimer = 0;
els.chatSearch.addEventListener('input', () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(renderSidebar, 200);
});
document.addEventListener('click', (e) => {
  if (openMenu && !openMenu.menu.contains(e.target)) closeMenu();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && els.app.classList.contains('drawer-open')) closeDrawer();
});
window.matchMedia('(max-width: 800px)').addEventListener('change', syncMenuButton);

// ---------------------------------------------------------------------------
// Account
// ---------------------------------------------------------------------------

function renderAccount() {
  els.account.innerHTML = '';
  const actions = document.createElement('div');
  actions.className = 'account-actions';
  if (!user) {
    const p = document.createElement('p');
    p.textContent = 'Sign in to save your chats.';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn primary';
    btn.textContent = 'Sign in';
    btn.addEventListener('click', () => openSignin());
    actions.appendChild(btn);
    els.account.append(p, actions);
    return;
  }
  const who = document.createElement('p');
  who.className = 'who';
  who.textContent = user.email;
  const out = document.createElement('button');
  out.type = 'button';
  out.className = 'btn';
  out.textContent = 'Sign out';
  out.addEventListener('click', async () => {
    try { await api('/api/logout', { method: 'POST', body: '{}' }); } catch { /* reload either way */ }
    window.location.assign('/');
  });
  const del = document.createElement('button');
  del.type = 'button';
  del.className = 'btn danger';
  del.textContent = 'Delete account';
  del.addEventListener('click', async () => {
    // Two steps instead of a browser confirm() dialog.
    if (del.dataset.confirm !== '1') {
      del.dataset.confirm = '1';
      del.textContent = 'Delete account and all chats';
      announce('Press again to delete your account and all saved chats. This cannot be undone.');
      return;
    }
    try {
      await api('/api/me', { method: 'DELETE' });
      window.location.assign('/');
    } catch (err) {
      announce(err.message);
    }
  });
  actions.append(out, del);
  els.account.append(who, actions);
}

let linkMinutes = 0;  // from /api/config at startup

function openHelp() { els.help.showModal(); }
$('helpSidebar').addEventListener('click', () => { closeDrawer(); openHelp(); });
$('helpFooter').addEventListener('click', openHelp);
$('helpClose').addEventListener('click', () => els.help.close());

function fillHelp(config) {
  if (config.machines) $('helpMachines').textContent = ` (${config.machines.toLocaleString()} machines)`;
  const limits = config.searches_per_day;
  if (limits) {
    const item = $('helpLimits');
    item.textContent = `Web searches are limited to ${limits.anon} a day without an account and ${limits.user} `
      + 'a day when signed in. Past the limit Pinguide still answers, but it can\u2019t check the web.';
    item.hidden = false;
  }
  if (config.link_minutes) linkMinutes = config.link_minutes;
}

function openSignin(error) {
  els.signinError.hidden = !error;
  els.signinError.textContent = error || '';
  els.emailSent.textContent = '';
  els.signin.showModal();
}

els.emailForm.addEventListener('submit', async (e) => {
  e.preventDefault();
  const button = els.emailForm.querySelector('button');
  button.disabled = true;
  els.signinError.hidden = true;
  try {
    await api('/auth/email', { method: 'POST', body: JSON.stringify({ email: els.emailInput.value }) });
    els.emailSent.textContent = 'Check your email for a sign-in link.'
      + (linkMinutes ? ` It expires in ${linkMinutes} minutes.` : '');
  } catch (err) {
    els.signinError.textContent = err.message;
    els.signinError.hidden = false;
  } finally {
    button.disabled = false;
  }
});
$('signinClose').addEventListener('click', () => els.signin.close());

function showSupport(url) {
  if (!/^https:\/\//i.test(url || '')) return;
  const side = $('supportSidebar');
  side.href = url;
  side.hidden = false;
  $('supportFooterLink').href = url;
  $('supportFooter').hidden = false;
}

async function start() {
  $('year').textContent = new Date().getFullYear();
  syncMenuButton();
  renderThread();
  els.input.focus();

  const [config, me] = await Promise.all([
    api('/api/config').catch(() => ({})),
    api('/api/me').catch(() => ({})),
  ]);
  if (config.machines) $('machineCount').textContent = `\u00b7 ${config.machines.toLocaleString()} machines`;
  showSupport(config.support_url);
  fillHelp(config);
  els.googleBtn.hidden = !config.google;
  els.signinOr.hidden = !config.google;

  user = me.user || null;
  store = user ? serverStore : memoryStore;
  renderAccount();
  renderSidebar();

  // Messages from the sign-in redirects.
  const notices = {
    '#signin-error': 'Sign-in didn\u2019t work. Please try again.',
    '#signin-expired': 'That sign-in link has expired or was already used. Request a new one below.',
  };
  if (notices[location.hash]) {
    history.replaceState(null, '', '/');
    openSignin(notices[location.hash]);
  }
}

start();
