// <poke-chat>: streaming Q&A with the offline Pokédex assistant.
import { askStream, getJSON } from '../../lib/api.js';
import { bus } from '../../lib/bus.js';

const EXAMPLES = [
  'what is gengar weak to?',
  'pikachu vs charizard',
  'show me eevee',
  'how does eevee evolve?',
  'hatch an egg',
  'fastest dragon pokemon',
  'can snorlax learn surf?',
  'what does levitate do?',
];

const template = document.createElement('template');
template.innerHTML = `
<style>
  :host { display: flex; flex-direction: column; min-height: 0; height: 100%; }
  .messages {
    flex: 1; min-height: 0; overflow-y: auto; -webkit-overflow-scrolling: touch;
    padding: 14px 14px 8px; display: flex; flex-direction: column; gap: 12px;
  }
  .msg { white-space: pre-wrap; word-break: break-word; }
  .prompt { color: var(--muted); user-select: none; }
  .user .prompt { color: var(--color-purple-medium); }
  .bot .text b { color: #fff; }
  .meta { color: var(--muted); font-size: 11px; margin-top: 2px; }
  .cursor { color: var(--color-purple-medium); animation: blink 1s steps(1) infinite; }
  @keyframes blink { 50% { opacity: 0; } }
  .art { margin: 6px 0 0; color: var(--color-purple-medium); font-size: 7px; line-height: 1.05; white-space: pre; overflow-x: auto; }
  .notice { color: var(--color-purple-medium); }
  .error { color: var(--bad); }
  .actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
  .actions button {
    background: var(--bg); color: var(--text); border: 1px solid var(--line);
    font: inherit; font-size: 12px; padding: 4px 8px; cursor: pointer;
  }
  .actions button:hover { border-color: var(--color-purple-medium); color: var(--color-purple-medium); }
  .chips {
    flex-shrink: 0; display: flex; flex-wrap: nowrap; gap: 6px; padding: 6px 14px;
    border-top: 1px dashed var(--line); overflow-x: auto; scrollbar-width: none;
  }
  .chips::-webkit-scrollbar { display: none; }
  .chips button {
    flex: 0 0 auto; background: none; color: var(--muted); border: 1px solid var(--line);
    font: inherit; font-size: 11px; padding: 4px 8px; cursor: pointer; white-space: nowrap;
  }
  .chips button:hover { color: var(--color-purple-medium); border-color: var(--color-purple-medium); }
  form {
    flex-shrink: 0; display: flex; align-items: center; gap: 8px;
    min-height: 44px; padding: 8px 12px 10px;
    border-top: 1px solid var(--line); background: var(--panel);
  }
  form span { color: var(--color-purple-medium); }
  input {
    flex: 1; min-width: 0; background: transparent; border: 0; outline: none; color: var(--text);
    font: inherit; font-size: 16px; caret-color: var(--color-purple-medium);
  }
  input::placeholder { color: var(--muted); }
  @media (min-width: 901px) {
    input { font-size: inherit; }
    .chips { flex-wrap: wrap; overflow-x: visible; }
    .chips button { padding: 1px 6px; white-space: normal; }
    form { min-height: 0; padding: 10px 14px 12px; }
    .messages { padding: 18px 14px 8px; }
  }
</style>
<div class="messages" role="log" aria-live="polite"></div>
<div class="chips"></div>
<form autocomplete="off">
  <span>›</span>
  <input name="q" placeholder="ask about a pokémon… try “pikachu vs mewtwo”" aria-label="Question">
</form>
`;

const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const formatAnswer = (s) => escapeHtml(s).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
const shrinkArt = (art) => art.split('\n').filter((_, i) => i % 2 === 0).map((line) => [...line].filter((_, i) => i % 2 === 0).join('')).join('\n');

class PokeChat extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' }).appendChild(template.content.cloneNode(true));
    this.$ = (sel) => this.shadowRoot.querySelector(sel);
    this.history = [];
    this.context = [];
    this.controller = null;
  }

  connectedCallback() {
    const chips = this.$('.chips');
    for (const example of EXAMPLES) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = example;
      button.addEventListener('click', () => this.ask(example));
      chips.append(button);
    }
    this.$('form').addEventListener('submit', (event) => {
      event.preventDefault();
      const input = this.$('input');
      const question = input.value.trim();
      if (question) this.ask(question);
      input.value = '';
    });
    this.off = bus.on('chat:ask', ({ question }) => this.ask(question));
    this.addBot('Hi! I\'m an offline Pokédex. Ask me about any Pokémon, move, ability, item or type — or make two Pokémon fight with "X vs Y".');
    this.$('input').focus();
  }

  disconnectedCallback() {
    this.off?.();
  }

  addMessage(cls, html) {
    const el = document.createElement('div');
    el.className = `msg ${cls}`;
    el.innerHTML = html;
    const list = this.$('.messages');
    list.append(el);
    list.scrollTop = list.scrollHeight;
    return el;
  }

  addBot(text) {
    return this.addMessage('bot', `<span class="prompt">dex ›</span> <span class="text">${formatAnswer(text)}</span>`);
  }

  offerEvolve(bot, card) {
    if (!card?.id || bot.querySelector('[data-evolve]')) return;
    const row = document.createElement('div');
    row.className = 'actions';
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.evolve = String(card.id);
    button.textContent = '[ evolve ]';
    button.title = `Evolve ${card.name}`;
    button.addEventListener('click', async () => {
      const evolution = await getJSON(`/api/evolution/${card.id}`);
      evolution.startAt = card.id;
      bus.emit('evolve:play', { evolution });
    });
    row.append(button);
    bot.append(row);
  }

  scrollDown() {
    const list = this.$('.messages');
    list.scrollTop = list.scrollHeight;
  }

  async ask(question) {
    this.controller?.abort();
    const controller = (this.controller = new AbortController());
    this.addMessage('user', `<span class="prompt">you ›</span> ${escapeHtml(question)}`);
    const bot = this.addMessage('bot', '<span class="prompt">dex ›</span> <span class="text"></span><span class="cursor">▌</span>');
    const textEl = bot.querySelector('.text');
    let answer = '';
    let intent = null;
    let source = null;
    try {
      for await (const event of askStream({ question, context: this.context, history: this.history.slice(-4) }, controller.signal)) {
        if (event.type === 'plan') {
          intent = event.intent;
        } else if (event.type === 'card') {
          bus.emit('pokemon:focus', { card: event.card });
          if (intent === 'show' && event.card.ascii) {
            const pre = document.createElement('pre');
            pre.className = 'art';
            pre.textContent = shrinkArt(event.card.ascii);
            bot.append(pre);
          }
          // Same idea as after an Egg hatches: offer [evolve] for the Pokémon just shown.
          if (intent !== 'evolve') this.offerEvolve(bot, event.card);
        } else if (event.type === 'evolve') {
          bot.insertAdjacentHTML('beforeend', '<div class="notice">» playing in the evolution view</div>');
          bus.emit('evolve:play', { evolution: event.evolution });
        } else if (event.type === 'battle') {
          const { a, b } = event.result;
          bot.insertAdjacentHTML('beforeend', `<div class="notice">» ${escapeHtml(a.name)} vs ${escapeHtml(b.name)} — playing in the arena</div>`);
          bus.emit('battle:play', { result: event.result });
        } else if (event.type === 'token') {
          answer += event.text;
          textEl.innerHTML = formatAnswer(answer);
          this.scrollDown();
        } else if (event.type === 'done') {
          source = event.source;
          if (event.context?.length) this.context = event.context;
        } else if (event.type === 'error') {
          textEl.insertAdjacentHTML('beforeend', `<span class="error">Something went wrong: ${escapeHtml(event.message)}</span>`);
        }
      }
    } catch (err) {
      if (err.name === 'AbortError') return;
      textEl.innerHTML = `<span class="error">Can't reach the local server (${escapeHtml(err.message)}). Is \`python -m pokebot serve\` running?</span>`;
    } finally {
      bot.querySelector('.cursor')?.remove();
    }
    if (source) bot.insertAdjacentHTML('beforeend', `<div class="meta">[${source}]</div>`);
    if (intent !== 'unknown') this.history.push({ role: 'user', content: question }, { role: 'assistant', content: answer });
    this.scrollDown();
  }
}

customElements.define('poke-chat', PokeChat);
