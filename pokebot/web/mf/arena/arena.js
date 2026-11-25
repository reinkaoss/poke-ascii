// <poke-arena>: plays a simulated battle as an animated OpenASCII scene with ASCII HUDs.
import { getJSON } from '../../lib/api.js';
import { bus } from '../../lib/bus.js';
import {
  AsciiScene, TYPE_COLORS, drawRect, ease, heightScale, loadSprite, silhouette, sleep, sourceRect, standingRect, textBar,
} from '../../lib/scene.js';

const STATUS_COLORS = { PAR: '#f7d02c', BRN: '#ee8130', PSN: '#a33ea1', TOX: '#a33ea1', SLP: '#8a84a8', FRZ: '#96d9d6', CNF: '#f95587' };

const template = document.createElement('template');
template.innerHTML = `
<style>
  :host { display: grid; grid-template-rows: minmax(0, 1fr) auto auto; overflow: hidden; }
  [hidden] { display: none !important; }
  .scene { position: relative; min-height: 0; }
  canvas { position: absolute; inset: 0; width: 100%; height: 100%; display: block; touch-action: none; }
  pre { margin: 0; font: inherit; white-space: pre; }
  .hud {
    position: absolute; padding: 0; font-size: var(--hud-font, 12px); line-height: 1.25;
    background: color-mix(in srgb, var(--bg) 78%, transparent); pointer-events: none;
  }
  /* Game Boy layout: the foe's box top-left, yours bottom-right. */
  .hud-b { top: 16px; left: 10px; }
  .hud-a { bottom: 10px; right: 10px; }
  .tools { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; padding: 6px 10px; border-top: 1px dashed var(--line); }
  .log {
    height: var(--log-height, 118px); padding: 6px 14px 8px; border-top: 1px dashed var(--line);
    background: color-mix(in srgb, var(--bg) 88%, transparent);
    overflow: hidden; display: flex; flex-direction: column; justify-content: flex-end;
  }
  .log div { white-space: pre-wrap; }
  button {
    background: var(--bg); color: var(--text); border: 1px solid var(--line); font: inherit;
    font-size: 12px; padding: 2px 8px; cursor: pointer;
  }
  button:hover { border-color: var(--color-purple-medium); color: var(--color-purple-medium); }
  .muted { color: var(--muted); }
  .accent { color: var(--color-purple-medium); }
  .good { color: var(--good); } .warn { color: var(--warn); } .bad { color: var(--bad); }
  .banner {
    position: absolute; left: 50%; top: 55%; transform: translate(-50%, -50%);
    font-size: var(--banner-font, 18px); line-height: 1.2; font-weight: bold; color: #fff; background: var(--bg);
    text-shadow: 0 0 12px var(--color-purple-medium); pointer-events: none;
    animation: pop 0.35s ease-out;
  }
  .banner span { color: var(--color-purple-medium); text-shadow: none; }
  @keyframes pop { from { opacity: 0; transform: translate(-50%, -50%) scale(0.8); } }
  :host(.narrow) { --hud-font: 10px; --log-height: 92px; --banner-font: 13px; }
  :host(.narrow) .log { font-size: 11px; padding: 4px 10px 6px; }
  :host(.narrow) button { font-size: 11px; padding: 2px 6px; }
</style>
<div class="scene">
  <canvas aria-label="Battle scene"></canvas>
  <pre class="hud hud-a"></pre>
  <pre class="hud hud-b"></pre>
  <pre class="banner" hidden></pre>
</div>
<div class="tools">
  <button data-action="speed">[ 1× ]</button>
  <button data-action="skip">[ skip ]</button>
  <button data-action="replay">[ replay ]</button>
  <button data-action="rematch">[ rematch ]</button>
  <button data-action="close">[ back ]</button>
</div>
<div class="log" role="log" aria-live="polite"></div>
`;

class PokeArena extends HTMLElement {
  static observedAttributes = ['hidden'];

  constructor() {
    super();
    this.attachShadow({ mode: 'open' }).appendChild(template.content.cloneNode(true));
    this.$ = (sel) => this.shadowRoot.querySelector(sel);
    this.fighters = {};
    this.effects = [];
    this.speed = 1;
    this.skipping = false;
    this.playing = false;
    this.token = 0;
  }

  connectedCallback() {
    this.scene = new AsciiScene(this.$('canvas'), (ctx, w, h, now) => this.paint(ctx, w, h, now), { sourceWidth: 640, fps: 14 });
    if (this.hasAttribute('hidden')) this.scene.pause();
    this.offs = [
      bus.on('battle:play', ({ result }) => this.play(result)),
      bus.on('theme:change', ({ theme }) => this.scene.setTheme(theme)),
    ];
    this.shadowRoot.addEventListener('click', (e) => this.onClick(e));
    this.hudWidth = 34;
    this.resizer = new ResizeObserver(() => this.fitHud());
    this.resizer.observe(this);
  }

  fitHud() {
    const width = this.getBoundingClientRect().width;
    if (!width) return;
    this.classList.toggle('narrow', width < 560);
    const charWidth = (width < 560 ? 10 : 12) * 0.602;
    this.hudWidth = Math.max(18, Math.min(34, Math.floor((width / 2 - 18) / charWidth)));
    this.renderHud();
    this.scene?.refresh();
  }

  disconnectedCallback() {
    this.offs?.forEach((off) => off());
    this.resizer?.disconnect();
    this.scene?.destroy();
  }

  attributeChangedCallback(name) {
    if (name !== 'hidden' || !this.scene) return;
    if (this.hasAttribute('hidden')) {
      this.token++;
      this.playing = false;
      this.scene.pause();
    } else {
      this.scene.resume();
    }
  }

  async onClick(event) {
    const button = event.target.closest('button');
    const action = button?.dataset.action;
    if (action === 'speed') {
      this.speed = this.speed >= 4 ? 1 : this.speed * 2;
      button.textContent = `[ ${this.speed}× ]`;
    } else if (action === 'skip') {
      this.skipping = true;
    } else if (action === 'replay' && this.result) {
      this.play(this.result);
    } else if (action === 'rematch' && this.result) {
      const { a, b } = this.result;
      this.log('Setting up a rematch…', 'muted');
      this.play(await getJSON(`/api/battle?a=${a.pokemon_id}&b=${b.pokemon_id}&level=${this.result.level}`));
    } else if (action === 'close') {
      bus.emit('arena:close');
    }
  }

  // ---------------------------------------------------------------- playback
  async play(result) {
    const token = ++this.token;
    this.result = result;
    this.playing = true;
    this.skipping = false;
    this.effects = [];
    this.showBanner(null);
    this.$('.log').replaceChildren();
    for (const side of ['a', 'b']) {
      const mon = result[side];
      this.fighters[side] = {
        ...mon, hp: mon.max_hp, shownHp: mon.max_hp, status: null, fainted: false,
        height: result.cards?.[side]?.height ?? 1,
        ...(await this.spritesFor(side, mon.pokemon_id)),
      };
    }
    if (token !== this.token) return;
    this.renderHud();
    this.addEffect('enter', 'a', 900);
    this.addEffect('enter', 'b', 900);
    await this.wait(950);
    for (const event of result.events) {
      if (token !== this.token) return;
      await this.step(event);
    }
    if (token !== this.token) return;
    const odds = result.percent;
    this.log(`odds over 200 re-runs: ${result.a.name} ${odds.a}% · ${result.b.name} ${odds.b}%` +
      (odds.draw ? ` · draw ${odds.draw}%` : ''), 'muted');
    this.playing = false;
    this.skipping = false;
    this.scene.refresh();
  }

  async step(ev) {
    const actor = this.fighters[ev.side];
    const foeSide = ev.side === 'a' ? 'b' : 'a';
    switch (ev.t) {
      case 'start':
        this.log(ev.text, 'accent');
        break;
      case 'turn':
        this.log(`── ${ev.text.toLowerCase()} ──`, 'muted');
        await this.wait(250);
        break;
      case 'move': {
        this.log(ev.text);
        const color = TYPE_COLORS[ev.type] || '#ffffff';
        if (ev.category === 'status') {
          this.addEffect('glow', ev.side, 550, { color });
        } else {
          this.addEffect('lunge', ev.side, 450);
          if (ev.category === 'special') this.addEffect('projectile', ev.side, 450, { color, to: foeSide });
        }
        this.lastColor = color;
        await this.wait(470);
        break;
      }
      case 'charge':
        this.log(ev.text);
        this.addEffect('glow', ev.side, 650, { color: TYPE_COLORS[ev.type] || '#ffffff' });
        await this.wait(650);
        break;
      case 'damage':
        this.addEffect('hit', ev.side, 420);
        this.addEffect('burst', ev.side, 520, { color: this.lastColor || '#ffffff', seed: Math.random() });
        this.log(ev.text, ev.crit ? 'warn' : '');
        await this.tweenHp(ev.side, ev.hp, 450);
        this.lastColor = null;
        break;
      case 'heal':
        this.addEffect('sparkle', ev.side, 650, { color: '#7ac74c', seed: Math.random() });
        this.log(ev.text, 'good');
        await this.tweenHp(ev.side, ev.hp, 450);
        break;
      case 'miss':
        this.addEffect('dodge', ev.side, 380);
        this.log(ev.text, 'muted');
        await this.wait(420);
        break;
      case 'immune':
        this.log(ev.text, 'muted');
        await this.wait(550);
        break;
      case 'status':
        actor.status = ev.status;
        if (ev.status) this.addEffect('tint', ev.side, 600, { color: STATUS_COLORS[ev.status] || '#ffffff' });
        this.log(ev.text, ev.status ? 'warn' : 'good');
        this.renderHud();
        await this.wait(600);
        break;
      case 'stat':
        this.addEffect('tint', ev.side, 450, { color: ev.change > 0 ? '#ff7b7b' : '#7bb6ff' });
        this.log(ev.text);
        await this.wait(500);
        break;
      case 'transform':
        Object.assign(actor, await this.spritesFor(ev.side, ev.pokemon_id ?? ev.species_id));
        actor.types = ev.types;
        actor.height = this.fighters[foeSide].height;
        this.addEffect('tint', ev.side, 700, { color: '#c9a6ff' });
        this.log(ev.text, 'accent');
        await this.wait(700);
        break;
      case 'faint':
        actor.fainted = true;
        this.addEffect('faint', ev.side, 850);
        this.log(ev.text, 'bad');
        await this.wait(850);
        break;
      case 'end':
        this.showBanner(ev.winner ? `${this.fighters[ev.winner].name} wins!` : 'Draw!');
        this.log(ev.text, 'accent');
        this.scene.animate(400);
        await this.wait(400);
        break;
      default:
        this.log(ev.text, ev.effectiveness > 1 ? 'warn' : '');
        await this.wait(ev.effectiveness ? 450 : 550);
    }
  }

  /** Your side (a) is seen from behind; without a back sprite, the front artwork is mirrored instead. */
  async spritesFor(side, pokemonId) {
    const front = await loadSprite(pokemonId);
    const back = side === 'a' ? await loadSprite(pokemonId, 'back') : null;
    return { sprite: back || front, flip: side === 'a' && !back, pixelArt: Boolean(back) };
  }

  wait(ms) {
    if (this.skipping) return Promise.resolve();
    const scaled = ms / this.speed;
    this.scene.animate(scaled);
    return sleep(scaled);
  }

  addEffect(kind, side, ms, extra = {}) {
    if (this.skipping && kind !== 'faint') return;
    this.effects.push({ kind, side, start: performance.now(), dur: this.skipping ? 1 : ms / this.speed, ...extra });
  }

  tweenHp(side, target, ms) {
    const f = this.fighters[side];
    const from = f.shownHp;
    f.hp = target;
    if (this.skipping) {
      f.shownHp = target;
      this.renderHud();
      return Promise.resolve();
    }
    const dur = ms / this.speed;
    const start = performance.now();
    this.scene.animate(dur);
    return new Promise((resolve) => {
      const frame = (now) => {
        const t = Math.min(1, (now - start) / dur);
        f.shownHp = Math.round(from + (target - from) * ease(t));
        this.renderHud();
        if (t < 1) requestAnimationFrame(frame);
        else resolve();
      };
      requestAnimationFrame(frame);
    });
  }

  // ---------------------------------------------------------------- overlays
  log(text, cls = '') {
    const line = document.createElement('div');
    line.textContent = text;
    if (cls) line.className = cls;
    const log = this.$('.log');
    log.append(line);
    while (log.children.length > 7) log.firstElementChild.remove();
  }

  renderHud() {
    for (const side of ['a', 'b']) {
      const f = this.fighters[side];
      if (!f) continue;
      const width = this.hudWidth;
      const inner = width - 2;
      const ratio = f.shownHp / f.max_hp;
      const barClass = ratio > 0.5 ? 'good' : ratio > 0.2 ? 'warn' : 'bad';
      const status = f.status || '';
      const level = ` Lv${f.level}`;
      const title = f.name.toUpperCase().slice(0, inner - status.length - level.length - 3) + level;
      const types = f.types.join('/');
      const hpText = `${f.shownHp}/${f.max_hp}`;
      const barWidth = inner - 4 - hpText.length - 2;
      const row1 = ` ${title}`.padEnd(inner - status.length - 1) + status + ' ';
      const row2 = ` <span class="muted">HP</span> <span class="${barClass}">${textBar(f.shownHp, f.max_hp, barWidth)}</span> ${hpText} `;
      const row3 = ` ${types} · ${f.ability}`.slice(0, inner).padEnd(inner);
      const statusHtml = status ? row1.replace(status, `<span style="color:${STATUS_COLORS[status]}">${status}</span>`) : row1;
      this.$(`.hud-${side}`).innerHTML =
        `┌${'─'.repeat(inner)}┐\n│${statusHtml}│\n│${row2}│\n│<span class="muted">${row3}</span>│\n└${'─'.repeat(inner)}┘`;
    }
  }

  // ---------------------------------------------------------------- painting
  /**
   * Game Boy layout: the foe (b) far away on a platform top-right, your Pokémon (a) close up bottom-left,
   * seen from behind. Both are sized by their real heights; the near one gets more room.
   */
  layout(side, w, h) {
    const f = this.fighters[side];
    const near = side === 'a';
    const hud = sourceRect(this.$(near ? '.hud-a' : '.hud-b'), this.$('canvas'), w);
    const cx = near ? w * 0.27 : w * 0.72;
    const ground = near ? h * 0.99 : h * 0.5;
    const top = near ? h * 0.38 : h * 0.06;
    // Keep each sprite clear of the box on its own row (the boxes sit in the opposite corner).
    const maxW = Math.min(w * (near ? 0.46 : 0.4), hud ? 2 * (near ? hud.x - cx : cx - hud.right) - w * 0.04 : w);
    const maxH = ground - top;
    const tallest = Math.max(...['a', 'b'].map((s) => this.fighters[s]?.height || 1));
    const rect = f?.sprite
      ? standingRect(f.sprite, cx, ground, Math.max(w * 0.2, maxW), maxH * heightScale(f.height, tallest))
      : { x: cx, y: ground - maxH / 2, w: maxH / 2, h: maxH / 2, cx, cy: ground - maxH / 4 };
    return { ...rect, ground, size: rect.h, flip: Boolean(f?.flip), from: near ? w * 0.9 : -w * 0.9,
      platform: near ? w * 0.22 : w * 0.18 };
  }

  paint(ctx, w, h, now) {
    const sky = ctx.createLinearGradient(0, 0, 0, h);
    sky.addColorStop(0, '#0b0a12');
    sky.addColorStop(1, '#17132a');
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, w, h);
    for (const side of ['b', 'a']) {
      if (!this.fighters[side]) continue;
      const p = this.layout(side, w, h);
      const rx = Math.max(p.platform, p.w * 0.6);
      ctx.fillStyle = '#4a4378';
      ctx.beginPath();
      ctx.ellipse(p.cx, p.ground, rx, rx * 0.16, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = '#2f2a4c';
      ctx.beginPath();
      ctx.ellipse(p.cx, p.ground - rx * 0.02, rx * 0.86, rx * 0.11, 0, 0, Math.PI * 2);
      ctx.fill();
    }
    this.effects = this.effects.filter((e) => now - e.start < e.dur || e.kind === 'faint');
    for (const side of ['b', 'a']) this.paintFighter(ctx, side, w, h, now);
    for (const e of this.effects) this.paintParticles(ctx, e, w, h, now);
  }

  showBanner(text) {
    const el = this.$('.banner');
    el.hidden = !text;
    if (!text) return;
    const inner = `   ${text.toUpperCase().split('').join(' ')}   `;
    const rule = '═'.repeat(inner.length);
    const frame = (s) => Object.assign(document.createElement('span'), { textContent: s });
    el.replaceChildren(frame(`╔${rule}╗`), '\n', frame('║'), inner, frame('║'), '\n', frame(`╚${rule}╝`));
  }

  progress(e, now) {
    return Math.min(1, Math.max(0, (now - e.start) / e.dur));
  }

  paintFighter(ctx, side, w, h, now) {
    const f = this.fighters[side];
    if (!f?.sprite) return;
    const p = this.layout(side, w, h);
    let dx = 0, dy = 0, alpha = 1, flash = 0, tint = null, tintAlpha = 0, glow = null;
    const toward = side === 'a' ? 1 : -1;
    for (const e of this.effects) {
      if (e.side !== side) continue;
      const t = this.progress(e, now);
      if (e.kind === 'enter') dx += p.from * (1 - ease(t));
      if (e.kind === 'lunge') { dx += toward * Math.sin(Math.PI * t) * w * 0.08; dy -= toward * Math.sin(Math.PI * t) * h * 0.04; }
      if (e.kind === 'hit') { dx += Math.sin(t * 38) * 10 * (1 - t); flash = Math.max(flash, (1 - t) * (Math.sin(t * 25) > 0 ? 1 : 0.3)); }
      if (e.kind === 'dodge') dx -= toward * Math.sin(Math.PI * t) * w * 0.06;
      if (e.kind === 'faint') { dy += ease(t) * h * 0.35; alpha = 1 - t; }
      if (e.kind === 'tint') { tint = e.color; tintAlpha = Math.sin(Math.PI * t) * 0.65; }
      if (e.kind === 'glow') { glow = e.color; tintAlpha = Math.max(tintAlpha, Math.sin(Math.PI * t) * 0.4); tint = e.color; }
    }
    if (f.fainted && !this.effects.some((e) => e.kind === 'faint' && e.side === side)) return;
    ctx.globalAlpha = alpha;
    ctx.imageSmoothingEnabled = !f.pixelArt;
    const rect = { ...p, cx: p.cx + dx, y: p.y + dy, cy: p.cy + dy };
    if (glow) {
      const g = ctx.createRadialGradient(rect.cx, rect.cy, 0, rect.cx, rect.cy, Math.max(p.w, p.h) * 0.75);
      g.addColorStop(0, `${glow}99`);
      g.addColorStop(1, '#00000000');
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, w, h);
    }
    drawRect(ctx, f.sprite, rect, { flip: p.flip });
    if (tint && tintAlpha > 0) {
      ctx.globalAlpha = alpha * tintAlpha;
      drawRect(ctx, silhouette(f.sprite, tint), rect, { flip: p.flip });
    }
    if (flash > 0) {
      ctx.globalAlpha = alpha * flash;
      drawRect(ctx, silhouette(f.sprite, '#ffffff'), rect, { flip: p.flip });
    }
    ctx.globalAlpha = 1;
    ctx.imageSmoothingEnabled = true;
  }

  paintParticles(ctx, e, w, h, now) {
    const t = this.progress(e, now);
    if (e.kind === 'projectile') {
      const from = this.layout(e.side, w, h);
      const to = this.layout(e.to, w, h);
      for (let i = 0; i < 6; i++) {
        const k = Math.max(0, ease(t) - i * 0.05);
        const x = from.cx + (to.cx - from.cx) * k;
        const y = from.cy + (to.cy - from.cy) * k - Math.sin(Math.PI * k) * h * 0.08;
        ctx.globalAlpha = 1 - i / 6;
        ctx.fillStyle = e.color;
        ctx.beginPath();
        ctx.arc(x, y, h * (0.05 - i * 0.006), 0, Math.PI * 2);
        ctx.fill();
      }
    } else if (e.kind === 'burst' || e.kind === 'sparkle') {
      const p = this.layout(e.side, w, h);
      const count = e.kind === 'burst' ? 14 : 10;
      ctx.fillStyle = e.color;
      for (let i = 0; i < count; i++) {
        const angle = (i / count) * Math.PI * 2 + e.seed * 6;
        const span = Math.max(p.w, p.h, h * 0.15);
        const r = (e.kind === 'burst' ? span * 0.6 : span * 0.35) * ease(t);
        const x = p.cx + Math.cos(angle) * r;
        const y = e.kind === 'burst' ? p.cy + Math.sin(angle) * r * 0.7 : p.ground - t * span + Math.sin(angle) * 10;
        ctx.globalAlpha = 1 - t;
        ctx.fillRect(x - 5, y - 5, 10, 10);
      }
    }
    ctx.globalAlpha = 1;
  }
}

customElements.define('poke-arena', PokeArena);
