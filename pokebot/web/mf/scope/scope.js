// <poke-scope>: animated OpenASCII portrait + ASCII stat card for the Pokémon in focus.
import { getJSON } from '../../lib/api.js';
import { bus } from '../../lib/bus.js';
import { AsciiScene, TYPE_COLORS, drawRect, ease, loadSprite, sourceRect, textBar } from '../../lib/scene.js';

const STAT_ROWS = [['hp', 'HP '], ['attack', 'ATK'], ['defense', 'DEF'], ['sp_attack', 'SPA'], ['sp_defense', 'SPD'], ['speed', 'SPE']];

const template = document.createElement('template');
template.innerHTML = `
<style>
  :host { display: block; overflow: hidden; }
  [hidden] { display: none !important; }
  canvas, .art { position: absolute; inset: 0; width: 100%; height: 100%; }
  canvas { display: block; touch-action: none; }
  .art {
    margin: 0; display: grid; place-items: center; color: var(--color-purple-medium);
    white-space: pre; line-height: 1.05; overflow: hidden;
  }
  .card {
    position: absolute; left: 14px; bottom: 12px; margin: 0; padding: 8px 10px;
    background: color-mix(in srgb, var(--bg) 82%, transparent); border: 1px dashed var(--line);
    font-size: 12px; line-height: 1.35; white-space: pre; pointer-events: none;
  }
  .muted { color: var(--muted); }
  .accent { color: var(--color-purple-medium); }
  .tools { position: absolute; top: 16px; right: 12px; left: 12px; display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; pointer-events: none; }
  .tools button { pointer-events: auto; }
  button, select {
    background: var(--bg); color: var(--text); border: 1px solid var(--line); font: inherit;
    font-size: 12px; padding: 2px 8px; cursor: pointer;
  }
  select { pointer-events: auto; max-width: 22ch; }
  button:hover, select:hover { border-color: var(--color-purple-medium); color: var(--color-purple-medium); }
  .empty { position: absolute; inset: 0; display: grid; place-items: center; color: var(--muted); }
  :host(.narrow) .card { left: 8px; right: 8px; bottom: 8px; font-size: 10.5px; padding: 6px 8px; }
  :host(.narrow) .tools { top: 10px; right: 8px; }
  :host(.narrow) button, :host(.narrow) select { font-size: 11px; padding: 2px 6px; }
  :host(.narrow) select { max-width: 16ch; }
</style>
<canvas aria-label="ASCII portrait"></canvas>
<pre class="art" hidden></pre>
<pre class="card" hidden></pre>
<div class="tools">
  <select class="forms" aria-label="Form" title="Alternate forms: Mega Evolutions, regional variants, Gigantamax…" hidden></select>
  <button data-action="art" title="Toggle between the OpenASCII scene and the original vsoch ASCII art">[ text art ]</button>
  <button data-action="random">[ random ]</button>
  <button data-action="evolve" title="Hatch it from an Egg and watch it evolve">[ evolve ]</button>
  <button data-action="fight" title="Battle this Pokémon against a random opponent">[ fight ]</button>
</div>
<div class="empty">loading…</div>
`;

class PokeScope extends HTMLElement {
  static observedAttributes = ['hidden'];

  constructor() {
    super();
    this.attachShadow({ mode: 'open' }).appendChild(template.content.cloneNode(true));
    this.$ = (sel) => this.shadowRoot.querySelector(sel);
    this.card = null;
    this.sprite = null;
    this.introStart = 0;
    this.showText = false;
  }

  connectedCallback() {
    this.scene = new AsciiScene(this.$('canvas'), (ctx, w, h, now) => this.paint(ctx, w, h, now), { sourceWidth: 480 });
    this.offs = [
      bus.on('pokemon:focus', ({ card }) => this.show(card)),
      bus.on('theme:change', ({ theme }) => this.scene.setTheme(theme)),
    ];
    this.shadowRoot.addEventListener('click', (e) => this.onClick(e));
    this.$('.forms').addEventListener('change', (e) => {
      getJSON(`/api/pokemon/${e.target.value}`).then((card) => bus.emit('pokemon:focus', { card }));
    });
    new ResizeObserver(() => this.onResize()).observe(this);
    getJSON('/api/random').then((card) => bus.emit('pokemon:focus', { card })).catch(() => { this.$('.empty').textContent = 'server unreachable'; });
  }

  disconnectedCallback() {
    this.offs?.forEach((off) => off());
    this.scene?.destroy();
  }

  attributeChangedCallback(name) {
    if (name === 'hidden' && this.scene) this.hasAttribute('hidden') ? this.scene.pause() : this.scene.resume();
  }

  onClick(event) {
    const action = event.target.closest('button')?.dataset.action;
    if (action === 'art') {
      this.showText = !this.showText;
      this.$('.art').hidden = !this.showText;
      this.$('canvas').hidden = this.showText;
      event.target.textContent = this.showText ? '[ scene ]' : '[ text art ]';
      this.fitArt();
    } else if (action === 'random') {
      getJSON('/api/random').then((card) => bus.emit('pokemon:focus', { card }));
    } else if (action === 'fight' && this.card) {
      bus.emit('chat:ask', { question: `${this.card.name} vs a random pokemon` });
    } else if (action === 'evolve' && this.card) {
      getJSON(`/api/evolution/${this.card.id}`).then((evolution) => bus.emit('evolve:play', { evolution }));
    }
  }

  async show(card) {
    if (!card || card.id === this.card?.id) return;
    this.card = card;
    const forms = this.$('.forms');
    forms.hidden = !(card.forms?.length > 1);
    forms.replaceChildren(...(card.forms || []).map((f) => new Option(`form: ${f.name}`, f.id, false, f.id === card.id)));
    const sprite = await loadSprite(card.id);
    if (this.card !== card) return;
    this.sprite = sprite;
    this.$('.empty').hidden = true;
    this.$('.art').textContent = card.ascii || '(no ASCII art for this Pokémon)';
    this.renderCard();
    this.fitArt();
    this.introStart = performance.now();
    this.scene.animate(700);
  }

  onResize() {
    const narrow = this.getBoundingClientRect().width < 560;
    if (narrow !== this.classList.contains('narrow')) {
      this.classList.toggle('narrow', narrow);
      if (this.card) this.renderCard();
    }
    this.fitArt();
    this.scene?.refresh();
  }

  /** The largest free area for the portrait: beside the stat card, or above it. */
  freeBox(w, h) {
    const pad = w * 0.03;
    const tools = sourceRect(this.$('.tools'), this.$('canvas'), w);
    const card = this.$('.card').hidden ? null : sourceRect(this.$('.card'), this.$('canvas'), w);
    const top = (tools ? tools.bottom : 0) + pad;
    if (!card) return { x: pad, y: top, w: w - 2 * pad, h: h - top - pad };
    const beside = { x: card.right + pad, y: top, w: w - card.right - 2 * pad, h: h - top - pad };
    const above = { x: pad, y: top, w: w - 2 * pad, h: card.y - top - pad };
    const fit = (b) => (this.sprite && b.w > 0 && b.h > 0 ? Math.min(b.w / this.sprite.width, b.h / this.sprite.height) : 0);
    return fit(beside) >= fit(above) ? beside : above;
  }

  paint(ctx, w, h, now) {
    const t = this.introStart ? ease(Math.min(1, (now - this.introStart) / 700)) : 1;
    const color = TYPE_COLORS[this.card?.types?.[0]] || '#7062ab';
    const box = this.freeBox(w, h);
    const cx = box.x + box.w / 2;
    const cy = box.y + box.h / 2;
    const glow = ctx.createRadialGradient(cx, cy, 0, cx, cy, Math.max(box.w, box.h) * 0.6);
    glow.addColorStop(0, `${color}55`);
    glow.addColorStop(1, '#00000000');
    ctx.fillStyle = '#0b0a12';
    ctx.fillRect(0, 0, w, h);
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, w, h);
    if (!this.sprite || box.w <= 0 || box.h <= 0) return;
    ctx.globalAlpha = t;
    const scale = Math.min(box.w / this.sprite.width, box.h / this.sprite.height) * (0.82 + 0.18 * t);
    const sw = this.sprite.width * scale;
    const sh = this.sprite.height * scale;
    drawRect(ctx, this.sprite, { cx, y: cy - sh / 2 + (1 - t) * 30, w: sw, h: sh });
  }

  renderCard() {
    const c = this.card;
    const narrow = this.classList.contains('narrow');
    const types = c.types.map((t) => `<span style="color:${TYPE_COLORS[t]}">[${t}]</span>`).join(' ');
    const tag = c.legendary ? ' <span class="accent">legendary</span>' : c.mythical ? ' <span class="accent">mythical</span>' : '';
    const statCell = ([key, label]) =>
      `<span class="muted">${label}</span> ${textBar(Math.min(c.stats[key], 180), 180, narrow ? 8 : 18)} ${String(c.stats[key]).padStart(3)}`;
    const stats = narrow
      ? [0, 1, 2].map((i) => `${statCell(STAT_ROWS[i])}   ${statCell(STAT_ROWS[i + 3])}`).join('\n')
      : STAT_ROWS.map(statCell).join('\n');
    const weak = Object.entries(c.weaknesses)
      .filter(([mult]) => Number(mult) >= 2)
      .flatMap(([mult, list]) => list.map((t) => `<span style="color:${TYPE_COLORS[t]}">${t}</span>${Number(mult) > 2 ? '×4' : ''}`))
      .join(' ');
    this.$('.card').innerHTML =
      `<span class="accent">#${String(c.species_id).padStart(4, '0')}</span>  <b>${c.name.toUpperCase()}</b>  ${types}${tag}\n` +
      `<span class="muted">${c.genus} · gen ${c.generation} · ${c.height} m · ${c.weight} kg</span>\n${narrow ? '' : '\n'}` +
      (narrow
        ? `${stats}\n<span class="muted">total</span> ${c.total}  <span class="muted">weak to</span> ${weak || 'nothing'}`
        : `${stats}\n<span class="muted">                    total</span> ${String(c.total).padStart(4)}\n` +
          `<span class="muted">weak to</span> ${weak || 'nothing'}`);
    this.$('.card').hidden = false;
  }

  fitArt() {
    const art = this.$('.art');
    if (!this.showText || !this.card?.ascii) return;
    const lines = this.card.ascii.split('\n');
    const cols = Math.max(...lines.map((l) => l.length));
    const box = this.getBoundingClientRect();
    const size = Math.min(box.width / (cols * 0.62), (box.height * 0.78) / (lines.length * 1.05));
    art.style.fontSize = `${Math.max(5, Math.min(18, size))}px`;
  }
}

customElements.define('poke-scope', PokeScope);
