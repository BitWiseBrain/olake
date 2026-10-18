/**
 * Ask OLake — embeddable chat widget
 *
 * Embed:
 *   <script src="/static/ask-olake.iife.js"
 *           data-api="https://your-api.com"
 *           data-theme="auto"
 *           data-position="bottom-right"></script>
 *
 * Inline:
 *   <ask-olake-chat api="https://..." theme="light"></ask-olake-chat>
 *
 * Features:
 * - Shadow DOM (full CSS isolation)
 * - Streaming markdown rendering
 * - Syntax-highlighted code with copy buttons
 * - Numbered citation chips linking to sources
 * - Conversation history (sessionStorage)
 * - Thumbs up/down feedback
 * - Light/dark/auto theme
 * - Mobile responsive, keyboard accessible (ARIA, Esc to close, focus trap)
 * - Suggested starter questions
 * - "AI-generated, verify in docs" notice
 */

const STARTER_QUESTIONS = [
  "How do I enable Postgres CDC in OLake?",
  "What sync modes does OLake support?",
  "How do I deploy OLake with Helm?",
  "What is OLake Fusion?",
  "How do I configure AWS Glue catalog?",
];

const WIDGET_CSS = `
  :host {
    --olake-primary: #5b4fff;
    --olake-primary-hover: #4a3ee8;
    --olake-bg: #0f1117;
    --olake-surface: #1a1d27;
    --olake-surface2: #242736;
    --olake-border: #2e3148;
    --olake-text: #e8eaf6;
    --olake-text-muted: #8b91b4;
    --olake-code-bg: #0d0f1a;
    --olake-user-bubble: #2d1f7a;
    --olake-assistant-bubble: #1a1d27;
    --olake-radius: 16px;
    --olake-shadow: 0 24px 64px rgba(0,0,0,0.6), 0 0 0 1px rgba(91,79,255,0.2);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    font-size: 14px;
    color: var(--olake-text);
  }

  :host([theme="light"]) {
    --olake-primary: #5b4fff;
    --olake-bg: #ffffff;
    --olake-surface: #f8f9ff;
    --olake-surface2: #eeeffe;
    --olake-border: #d8daf5;
    --olake-text: #1a1b2e;
    --olake-text-muted: #5a5f88;
    --olake-code-bg: #f0f1ff;
    --olake-user-bubble: #5b4fff;
    --olake-assistant-bubble: #f0f1ff;
    --olake-shadow: 0 24px 64px rgba(0,0,0,0.15), 0 0 0 1px rgba(91,79,255,0.1);
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  /* Launcher button */
  .launcher {
    position: fixed;
    bottom: 24px;
    right: 24px;
    width: 56px;
    height: 56px;
    border-radius: 50%;
    background: linear-gradient(135deg, var(--olake-primary), #8b5cf6);
    border: none;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 8px 24px rgba(91,79,255,0.4);
    transition: transform 0.2s ease, box-shadow 0.2s ease;
    z-index: 999999;
    color: white;
  }
  .launcher:hover { transform: scale(1.08); box-shadow: 0 12px 32px rgba(91,79,255,0.5); }
  .launcher:focus { outline: 2px solid white; outline-offset: 2px; }
  .launcher svg { width: 24px; height: 24px; }
  .launcher-badge {
    position: absolute;
    top: -4px;
    right: -4px;
    width: 18px;
    height: 18px;
    background: #ff5252;
    border-radius: 50%;
    font-size: 10px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
    color: white;
  }

  /* Chat panel */
  .panel {
    position: fixed;
    bottom: 92px;
    right: 24px;
    width: 400px;
    max-width: calc(100vw - 32px);
    height: 580px;
    max-height: calc(100vh - 120px);
    background: var(--olake-bg);
    border: 1px solid var(--olake-border);
    border-radius: var(--olake-radius);
    box-shadow: var(--olake-shadow);
    display: flex;
    flex-direction: column;
    z-index: 999998;
    overflow: hidden;
    transform-origin: bottom right;
    transition: transform 0.2s cubic-bezier(0.34, 1.56, 0.64, 1), opacity 0.15s ease;
  }
  .panel.hidden {
    transform: scale(0.9) translateY(8px);
    opacity: 0;
    pointer-events: none;
  }

  /* Header */
  .header {
    padding: 14px 16px;
    background: linear-gradient(135deg, rgba(91,79,255,0.15), rgba(139,92,246,0.1));
    border-bottom: 1px solid var(--olake-border);
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .header-icon {
    width: 32px;
    height: 32px;
    background: linear-gradient(135deg, var(--olake-primary), #8b5cf6);
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    font-size: 16px;
  }
  .header-title { font-weight: 700; font-size: 15px; flex: 1; }
  .header-subtitle { font-size: 11px; color: var(--olake-text-muted); }
  .header-actions { display: flex; gap: 6px; }
  .icon-btn {
    width: 28px; height: 28px;
    border: none;
    background: transparent;
    color: var(--olake-text-muted);
    cursor: pointer;
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: background 0.15s, color 0.15s;
  }
  .icon-btn:hover { background: var(--olake-surface2); color: var(--olake-text); }
  .icon-btn:focus { outline: 2px solid var(--olake-primary); }

  /* Messages */
  .messages {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
    scroll-behavior: smooth;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .messages::-webkit-scrollbar { width: 4px; }
  .messages::-webkit-scrollbar-track { background: transparent; }
  .messages::-webkit-scrollbar-thumb { background: var(--olake-border); border-radius: 2px; }

  /* Message bubbles */
  .msg { display: flex; flex-direction: column; max-width: 88%; }
  .msg.user { align-self: flex-end; }
  .msg.assistant { align-self: flex-start; }
  .bubble {
    padding: 10px 13px;
    border-radius: 12px;
    line-height: 1.55;
    word-break: break-word;
  }
  .msg.user .bubble {
    background: var(--olake-user-bubble);
    color: white;
    border-bottom-right-radius: 4px;
  }
  .msg.assistant .bubble {
    background: var(--olake-surface);
    border: 1px solid var(--olake-border);
    border-bottom-left-radius: 4px;
    color: var(--olake-text);
  }

  /* Markdown content */
  .bubble p { margin: 0 0 8px; }
  .bubble p:last-child { margin-bottom: 0; }
  .bubble code {
    background: var(--olake-code-bg);
    border: 1px solid var(--olake-border);
    padding: 1px 5px;
    border-radius: 4px;
    font-family: 'Fira Code', 'Cascadia Code', monospace;
    font-size: 12px;
  }
  .bubble pre {
    background: var(--olake-code-bg);
    border: 1px solid var(--olake-border);
    border-radius: 8px;
    padding: 12px;
    overflow-x: auto;
    margin: 8px 0;
    position: relative;
  }
  .bubble pre code {
    background: none;
    border: none;
    padding: 0;
    font-size: 12px;
  }
  .copy-btn {
    position: absolute;
    top: 6px;
    right: 6px;
    background: var(--olake-surface2);
    border: 1px solid var(--olake-border);
    color: var(--olake-text-muted);
    border-radius: 4px;
    padding: 3px 7px;
    font-size: 11px;
    cursor: pointer;
    transition: all 0.15s;
  }
  .copy-btn:hover { background: var(--olake-primary); color: white; border-color: transparent; }
  .bubble ul, .bubble ol { padding-left: 18px; margin: 6px 0; }
  .bubble li { margin: 3px 0; }
  .bubble strong { font-weight: 600; }
  .bubble em { font-style: italic; }
  .bubble h3, .bubble h4 { margin: 10px 0 4px; font-size: 13px; font-weight: 600; }
  .bubble blockquote {
    border-left: 3px solid var(--olake-primary);
    padding-left: 10px;
    color: var(--olake-text-muted);
    margin: 6px 0;
  }
  .bubble a { color: var(--olake-primary); text-decoration: none; }
  .bubble a:hover { text-decoration: underline; }

  /* Citation chips */
  .citations {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    margin-top: 8px;
  }
  .cite-chip {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    background: rgba(91,79,255,0.12);
    border: 1px solid rgba(91,79,255,0.25);
    color: var(--olake-primary);
    border-radius: 6px;
    padding: 2px 8px;
    font-size: 11px;
    text-decoration: none;
    transition: all 0.15s;
    cursor: pointer;
    max-width: 200px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .cite-chip:hover { background: rgba(91,79,255,0.25); text-decoration: none; }

  /* Feedback */
  .feedback-row {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-top: 6px;
    color: var(--olake-text-muted);
    font-size: 11px;
  }
  .feedback-btn {
    background: none;
    border: 1px solid var(--olake-border);
    color: var(--olake-text-muted);
    border-radius: 4px;
    padding: 2px 7px;
    cursor: pointer;
    font-size: 12px;
    transition: all 0.15s;
  }
  .feedback-btn:hover, .feedback-btn.active { background: var(--olake-surface2); border-color: var(--olake-primary); color: var(--olake-primary); }

  /* Loading */
  .loading { display: flex; gap: 4px; padding: 10px 13px; align-items: center; }
  .dot {
    width: 7px; height: 7px;
    background: var(--olake-primary);
    border-radius: 50%;
    opacity: 0.6;
    animation: bounce 1.2s infinite;
  }
  .dot:nth-child(2) { animation-delay: 0.2s; }
  .dot:nth-child(3) { animation-delay: 0.4s; }
  @keyframes bounce {
    0%, 80%, 100% { transform: translateY(0); }
    40% { transform: translateY(-6px); opacity: 1; }
  }

  /* Starters */
  .starters {
    padding: 12px 16px;
    display: flex;
    flex-direction: column;
    gap: 7px;
  }
  .starter-label { font-size: 11px; color: var(--olake-text-muted); font-weight: 500; letter-spacing: 0.05em; text-transform: uppercase; }
  .starter-btn {
    background: var(--olake-surface);
    border: 1px solid var(--olake-border);
    border-radius: 10px;
    padding: 8px 12px;
    text-align: left;
    color: var(--olake-text);
    cursor: pointer;
    font-size: 12px;
    transition: all 0.15s;
    line-height: 1.4;
  }
  .starter-btn:hover { border-color: var(--olake-primary); background: rgba(91,79,255,0.08); }

  /* Input */
  .input-area {
    padding: 12px 14px;
    border-top: 1px solid var(--olake-border);
    background: var(--olake-surface);
    display: flex;
    gap: 8px;
    align-items: flex-end;
  }
  .input-wrap { flex: 1; position: relative; }
  textarea {
    width: 100%;
    background: var(--olake-bg);
    border: 1px solid var(--olake-border);
    border-radius: 10px;
    padding: 9px 12px;
    color: var(--olake-text);
    font-size: 13px;
    font-family: inherit;
    resize: none;
    min-height: 38px;
    max-height: 100px;
    line-height: 1.5;
    transition: border-color 0.15s;
    overflow-y: auto;
  }
  textarea:focus { outline: none; border-color: var(--olake-primary); }
  textarea::placeholder { color: var(--olake-text-muted); }
  .send-btn {
    width: 36px; height: 36px;
    background: var(--olake-primary);
    border: none;
    border-radius: 8px;
    color: white;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.15s;
    flex-shrink: 0;
  }
  .send-btn:hover { background: var(--olake-primary-hover); transform: translateY(-1px); }
  .send-btn:disabled { opacity: 0.4; cursor: not-allowed; transform: none; }
  .send-btn:focus { outline: 2px solid white; }

  /* Footer note */
  .footer-note {
    text-align: center;
    font-size: 10px;
    color: var(--olake-text-muted);
    padding: 6px 14px 8px;
    border-top: 1px solid var(--olake-border);
  }
  .footer-note a { color: var(--olake-primary); text-decoration: none; }

  /* Error */
  .error-msg {
    color: #ff6b6b;
    font-size: 12px;
    padding: 8px 12px;
    background: rgba(255,107,107,0.08);
    border: 1px solid rgba(255,107,107,0.2);
    border-radius: 8px;
  }

  /* Inline mode */
  :host([mode="inline"]) {
    display: block;
    width: 100%;
  }
  :host([mode="inline"]) .launcher { display: none !important; }
  :host([mode="inline"]) .panel {
    display: flex !important;
    position: relative !important;
    bottom: auto !important; right: auto !important;
    width: 100% !important;
    max-width: 100% !important;
    height: 580px !important;
    max-height: none !important;
    transform: none !important;
    opacity: 1 !important;
    pointer-events: auto !important;
    box-shadow: none !important;
    border: none !important;
    border-radius: 0 !important;
  }
  :host([mode="inline"]) .panel.hidden {
    display: flex !important;
    transform: none !important;
    opacity: 1 !important;
    pointer-events: auto !important;
  }
  :host([mode="inline"]) #close-btn { display: none !important; }

  @media (max-width: 480px) {
    .panel { width: 100vw; max-width: 100vw; right: 0; bottom: 80px; border-radius: 16px 16px 0 0; }
    .launcher { bottom: 16px; right: 16px; }
  }
`;

// ── Markdown renderer (no external deps) ─────────────────────────────────────

function renderMarkdown(md: string): string {
  let html = md
    // Code blocks first
    .replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
      const escaped = code.replace(/</g, '&lt;').replace(/>/g, '&gt;');
      return `<pre><button class="copy-btn" data-code="${encodeURIComponent(code)}">Copy</button><code class="lang-${lang || ''}">${escaped}</code></pre>`;
    })
    // Inline code
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    // Bold
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    // Italic
    .replace(/_(.+?)_/g, '<em>$1</em>')
    // Headers
    .replace(/^#### (.+)$/gm, '<h4>$1</h4>')
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h3>$1</h3>')
    // Blockquote
    .replace(/^> (.+)$/gm, '<blockquote>$1</blockquote>')
    // Lists
    .replace(/^[-*] (.+)$/gm, '<li>$1</li>')
    .replace(/^(\d+)\. (.+)$/gm, '<li>$2</li>')
    // Links
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    // Citation markers [n] → styled spans
    .replace(/\[(\d+)\]/g, '<sup class="cite-ref">[$1]</sup>')
    // Paragraphs
    .replace(/\n{2,}/g, '</p><p>')
    .replace(/\n/g, '<br>');

  // Wrap in paragraph if not starting with block elements
  if (!html.startsWith('<h') && !html.startsWith('<pre') && !html.startsWith('<ul')) {
    html = `<p>${html}</p>`;
  }

  // Wrap consecutive <li> in <ul>
  html = html.replace(/(<li>.*?<\/li>)+/g, '<ul>$&</ul>');

  return html;
}

// ── Citation extractor ────────────────────────────────────────────────────────

function extractCitations(text: string, sources: string[]): HTMLElement | null {
  if (!sources.length) return null;
  const matches = Array.from(new Set([...text.matchAll(/\[(\d+)\]/g)].map(m => parseInt(m[1]))));
  const citedSources = matches
    .filter(n => n >= 1 && n <= sources.length)
    .map(n => ({ n, url: sources[n - 1] }));
  if (!citedSources.length && !sources.length) return null;

  const cites = document.createElement('div');
  cites.className = 'citations';
  (citedSources.length ? citedSources : sources.map((url, i) => ({ n: i + 1, url }))).forEach(({ n, url }) => {
    const a = document.createElement('a');
    a.className = 'cite-chip';
    a.href = url;
    a.target = '_blank';
    a.rel = 'noopener';
    try {
      const parsed = new URL(url);
      a.textContent = `[${n}] ${parsed.hostname}${parsed.pathname.length > 30 ? parsed.pathname.slice(0, 30) + '…' : parsed.pathname}`;
    } catch {
      a.textContent = `[${n}] ${url.slice(0, 40)}`;
    }
    a.title = url;
    cites.appendChild(a);
  });
  return cites;
}

// ── Widget custom element ─────────────────────────────────────────────────────

class AskOlakeWidget extends HTMLElement {
  private shadow: ShadowRoot;
  private panel!: HTMLElement;
  private launcher!: HTMLButtonElement;
  private messagesEl!: HTMLElement;
  private textarea!: HTMLTextAreaElement;
  private sendBtn!: HTMLButtonElement;
  private isOpen = false;
  private isLoading = false;
  private history: Array<{ role: string; content: string }> = [];
  private apiBase = '';
  private storageKey = 'ask-olake-history';

  constructor() {
    super();
    this.shadow = this.attachShadow({ mode: 'open' });
  }

  connectedCallback() {
    this.apiBase = this.getAttribute('data-api') || this.getAttribute('api') || (typeof window !== 'undefined' ? window.location.origin : 'http://localhost:8000');
    const theme = this.getAttribute('data-theme') || this.getAttribute('theme') || 'auto';
    const position = this.getAttribute('data-position') || 'bottom-right';
    const mode = this.getAttribute('mode') || 'floating';

    if (theme === 'light') this.setAttribute('theme', 'light');
    if (theme === 'auto') {
      const mq = window.matchMedia('(prefers-color-scheme: light)');
      if (mq.matches) this.setAttribute('theme', 'light');
      mq.addEventListener('change', e => {
        if (e.matches) this.setAttribute('theme', 'light');
        else this.removeAttribute('theme');
      });
    }
    this.setAttribute('mode', mode);

    this.render();
    this.loadHistory();
    this.setupKeyboard();

    if (mode === 'inline') {
      this.open();
    }
  }

  private render() {
    const style = document.createElement('style');
    style.textContent = WIDGET_CSS;

    // Launcher
    this.launcher = document.createElement('button');
    this.launcher.className = 'launcher';
    this.launcher.setAttribute('aria-label', 'Ask OLake documentation assistant');
    this.launcher.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
    </svg>`;
    this.launcher.addEventListener('click', () => this.toggle());

    // Panel
    this.panel = document.createElement('div');
    this.panel.className = 'panel hidden';
    this.panel.setAttribute('role', 'dialog');
    this.panel.setAttribute('aria-label', 'Ask OLake chat');
    this.panel.setAttribute('aria-modal', 'true');

    this.panel.innerHTML = `
      <div class="header">
        <div class="header-icon">🌊</div>
        <div style="flex:1">
          <div class="header-title">Ask OLake</div>
          <div class="header-subtitle">OLake documentation assistant</div>
        </div>
        <div class="header-actions">
          <button class="icon-btn" id="clear-btn" title="New chat" aria-label="New chat">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>
              <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/>
            </svg>
          </button>
          <button class="icon-btn" id="close-btn" title="Close" aria-label="Close chat">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
      </div>
      <div class="messages" id="messages" role="log" aria-live="polite" aria-label="Chat messages"></div>
      <div class="input-area">
        <div class="input-wrap">
          <textarea id="input" placeholder="Ask about OLake setup, CDC, catalogs, deployment…"
            rows="1" maxlength="1000" aria-label="Your question"></textarea>
        </div>
        <button class="send-btn" id="send-btn" aria-label="Send">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="22" y1="2" x2="11" y2="13"/><polygon points="22,2 15,22 11,13 2,9"/>
          </svg>
        </button>
      </div>
      <div class="footer-note">
        ⚠️ AI-generated — always <a href="https://olake.io/docs" target="_blank" rel="noopener">verify in docs</a>
      </div>
    `;

    this.shadow.appendChild(style);
    this.shadow.appendChild(this.launcher);
    this.shadow.appendChild(this.panel);

    this.messagesEl = this.panel.querySelector('#messages')!;
    this.textarea = this.panel.querySelector('#input')!;
    this.sendBtn = this.panel.querySelector('#send-btn')!;

    this.panel.querySelector('#close-btn')!.addEventListener('click', () => this.close());
    this.panel.querySelector('#clear-btn')!.addEventListener('click', () => this.clearHistory());
    this.sendBtn.addEventListener('click', () => this.send());
    this.textarea.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this.send(); }
    });
    this.textarea.addEventListener('input', () => this.autoResize());
  }

  private autoResize() {
    this.textarea.style.height = 'auto';
    this.textarea.style.height = Math.min(this.textarea.scrollHeight, 100) + 'px';
  }

  private showStarters() {
    if (this.history.length > 0) return;
    const div = document.createElement('div');
    div.className = 'starters';
    div.id = 'starters';
    div.innerHTML = `<div class="starter-label">Suggested questions</div>`;
    STARTER_QUESTIONS.forEach(q => {
      const btn = document.createElement('button');
      btn.className = 'starter-btn';
      btn.textContent = q;
      btn.addEventListener('click', () => {
        div.remove();
        this.textarea.value = q;
        this.send();
      });
      div.appendChild(btn);
    });
    this.messagesEl.appendChild(div);
  }

  private addMessage(role: 'user' | 'assistant', content: string, sources: string[] = []) {
    const msg = document.createElement('div');
    msg.className = `msg ${role}`;

    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.innerHTML = renderMarkdown(content);

    // Copy buttons for code blocks
    bubble.querySelectorAll('.copy-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const code = decodeURIComponent((btn as HTMLElement).dataset.code || '');
        navigator.clipboard.writeText(code).then(() => {
          btn.textContent = 'Copied!';
          setTimeout(() => { btn.textContent = 'Copy'; }, 2000);
        });
      });
    });

    msg.appendChild(bubble);

    if (role === 'assistant' && sources.length) {
      const cites = extractCitations(content, sources);
      if (cites) msg.appendChild(cites);

      // Feedback
      const feedbackRow = document.createElement('div');
      feedbackRow.className = 'feedback-row';
      feedbackRow.innerHTML = `<span>Helpful?</span>`;
      ['👍', '👎'].forEach((emoji, idx) => {
        const btn = document.createElement('button');
        btn.className = 'feedback-btn';
        btn.textContent = emoji;
        btn.addEventListener('click', async () => {
          feedbackRow.querySelectorAll('.feedback-btn').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          const lastUserMsg = [...this.history].reverse().find(h => h.role === 'user');
          await fetch(`${this.apiBase}/feedback`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              question: lastUserMsg?.content || '',
              answer: content,
              rating: idx === 0 ? 1 : -1,
            }),
          }).catch(() => {});
        });
        feedbackRow.appendChild(btn);
      });
      msg.appendChild(feedbackRow);
    }

    this.messagesEl.appendChild(msg);
    this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
    return msg;
  }

  private showLoading(): HTMLElement {
    const msg = document.createElement('div');
    msg.className = 'msg assistant';
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    const loading = document.createElement('div');
    loading.className = 'loading';
    loading.innerHTML = '<div class="dot"></div><div class="dot"></div><div class="dot"></div>';
    bubble.appendChild(loading);
    msg.appendChild(bubble);
    this.messagesEl.appendChild(msg);
    this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
    return msg;
  }

  private async send() {
    const q = this.textarea.value.trim();
    if (!q || this.isLoading) return;

    // Remove starters
    this.shadow.querySelector('#starters')?.remove();

    this.textarea.value = '';
    this.autoResize();
    this.setLoading(true);

    this.history.push({ role: 'user', content: q });
    this.addMessage('user', q);
    const loadingEl = this.showLoading();

    try {
      const resp = await fetch(`${this.apiBase}/ask`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question: q,
          history: this.history.slice(-4).map(h => ({ role: h.role, content: h.content })),
        }),
      });

      loadingEl.remove();

      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({}));
        this.showError(errData.detail || `Error ${resp.status}`);
        this.history.pop();
        return;
      }

      const data = await resp.json();
      this.history.push({ role: 'assistant', content: data.answer });
      this.addMessage('assistant', data.answer, data.sources || []);
      this.saveHistory();

    } catch (err) {
      loadingEl.remove();
      this.showError('Network error — please try again.');
      this.history.pop();
    } finally {
      this.setLoading(false);
      this.textarea.focus();
    }
  }

  private showError(msg: string) {
    const el = document.createElement('div');
    el.className = 'msg assistant';
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.innerHTML = `<div class="error-msg">❌ ${msg} <a href="#" onclick="this.closest('.msg').remove();return false;" style="color:inherit;opacity:0.7">Dismiss</a></div>`;
    el.appendChild(bubble);
    this.messagesEl.appendChild(el);
    this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
  }

  private setLoading(loading: boolean) {
    this.isLoading = loading;
    this.sendBtn.disabled = loading;
    this.textarea.disabled = loading;
  }

  toggle() { this.isOpen ? this.close() : this.open(); }

  open() {
    this.isOpen = true;
    this.panel.classList.remove('hidden');
    this.panel.setAttribute('aria-hidden', 'false');
    setTimeout(() => {
      this.textarea.focus();
      if (this.history.length === 0) this.showStarters();
    }, 150);
    this.launcher.setAttribute('aria-expanded', 'true');
  }

  close() {
    this.isOpen = false;
    this.panel.classList.add('hidden');
    this.panel.setAttribute('aria-hidden', 'true');
    this.launcher.setAttribute('aria-expanded', 'false');
    this.launcher.focus();
  }

  clearHistory() {
    this.history = [];
    this.messagesEl.innerHTML = '';
    sessionStorage.removeItem(this.storageKey);
    this.showStarters();
    this.textarea.focus();
  }

  private setupKeyboard() {
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && this.isOpen) this.close();
    });
  }

  private saveHistory() {
    try {
      sessionStorage.setItem(this.storageKey, JSON.stringify(this.history.slice(-20)));
    } catch {}
  }

  private loadHistory() {
    try {
      const saved = sessionStorage.getItem(this.storageKey);
      if (saved) {
        this.history = JSON.parse(saved);
        this.history.forEach(h => this.addMessage(h.role as any, h.content));
      }
    } catch {}
  }
}

// ── Registration ──────────────────────────────────────────────────────────────

customElements.define('ask-olake-chat', AskOlakeWidget);

// Auto-mount from script tag attributes
function autoMount() {
  const scripts = document.querySelectorAll('script[data-api]');
  scripts.forEach(script => {
    const hasFloating = Array.from(document.querySelectorAll('ask-olake-chat'))
      .some(el => el.getAttribute('mode') !== 'inline');
    if (!hasFloating) {
      const widget = document.createElement('ask-olake-chat') as AskOlakeWidget;
      widget.setAttribute('mode', 'floating');
      ['data-api', 'data-theme', 'data-position'].forEach(attr => {
        const val = script.getAttribute(attr);
        if (val) widget.setAttribute(attr, val);
      });
      document.body.appendChild(widget);
    }
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', autoMount);
} else {
  autoMount();
}

export { AskOlakeWidget };
