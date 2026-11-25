// <poke-evolve>: an Egg hatches, then the Pokémon evolves stage by stage, as an OpenASCII scene.
import { bus } from '../../lib/bus.js';
import {
  AsciiScene, TYPE_COLORS, drawRect, ease, heightScale, loadSprite, silhouette, sleep, sourceRect, standingRect,
} from '../../lib/scene.js';

const template = document.createElement('template');
template.innerHTML = `
<style>
  :host { display: grid; grid-template-rows: minmax(0, 1fr) auto auto; overflow: hidden; }
  [hidden] { display: none !important; }
  .scene { position: relative; min-height: 0; }
  canvas { position: absolute; inset: 0; width: 100%; height: 100%; display: block; touch-action: none; }
  .trail {
    position: absolute; top: 16px; left: 12px; right: 12px; margin: 0; font: inherit; font-size: 12px;
    white-space: pre-wrap; color: var(--muted); pointer-events: none;
  }
  .trail b { color: var(--color-purple-medium); font-weight: normal; }
  .trail i { color: var(--text); font-style: normal; }
  .caption {
    min-height: calc(2 * 1.45em + 18px); padding: 8px 14px; border-top: 1px dashed var(--line);
    background: color-mix(in srgb, var(--bg) 88%, transparent);
  }
  .caption .sub { color: var(--muted); }
  /* Reserve one row of buttons: resizing the canvas makes OpenASCII rebuild its particles from scratch. */
  .choices { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; min-height: 22px; }
  .tools { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; padding: 6px 10px; border-top: 1px dashed var(--line); }
  button {
    background: var(--bg); color: var(--text); border: 1px solid var(--line); font: inherit;
    font-size: 12px; padding: 2px 8px; cursor: pointer;
  }
  button:hover { border-color: var(--color-purple-medium); color: var(--color-purple-medium); }
  .choices button span { color: var(--muted); }
  :host(.narrow) .trail { font-size: 10.5px; }
  :host(.narrow) .caption { font-size: 12px; padding: 6px 10px; }
  :host(.narrow) button { font-size: 11px; padding: 2px 6px; }
  :host(.narrow) .choices button span { display: none; }
</style>
<div class="scene">
  <canvas aria-label="Evolution scene"></canvas>
  <pre class="trail" aria-label="Evolution path"></pre>
</div>
<div class="caption" role="status" aria-live="polite"><div class="text"></div><div class="sub"></div><div class="choices"></div></div>
<div class="tools">
  <button data-action="skip">[ skip ]</button>
  <button data-action="replay">[ replay ]</button>
  <button data-action="close">[ back ]</button>
</div>
`;

const clamp01 = (x) => Math.max(0, Math.min(1, x));
const WOBBLES = [[0.08, 0.3, 0.08], [0.42, 0.64, 0.14], [0.74, 0.96, 0.2]]; // [start, end, amplitude in radians]

class PokeEvolve extends HTMLElement {
  static observedAttributes = ['hidden'];

  constructor() {
    super();
    this.attachShadow({ mode: 'open' }).appendChild(template.content.cloneNode(true));
    this.$ = (sel) => this.shadowRoot.querySelector(sel);
    this.token = 0;
    this.speed = 1;
    this.phase = null;
    this.sprites = new Map();
    this.trail = [];
    this.playing = false;
    this.wakers = new Set();
  }

  connectedCallback() {
    this.scene = new AsciiScene(this.$('canvas'), (ctx, w, h, now) => this.paint(ctx, w, h, now), { sourceWidth: 560, fps: 18 });
    if (this.hasAttribute('hidden')) this.scene.pause();
    this.offs = [
      bus.on('evolve:play', ({ evolution }) => this.play(evolution)),
      bus.on('theme:change', ({ theme }) => this.scene.setTheme(theme)),
    ];
    this.shadowRoot.addEventListener('click', (e) => this.onClick(e));
    this.resizer = new ResizeObserver(() => {
      this.classList.toggle('narrow', this.getBoundingClientRect().width < 560);
      this.scene.refresh();
    });
    this.resizer.observe(this);
  }

  disconnectedCallback() {
    this.offs?.forEach((off) => off());
    this.resizer?.disconnect();
    this.scene?.destroy();
  }

  attributeChangedCallback(name) {
    if (name !== 'hidden' || !this.scene) return;
    if (this.hasAttribute('hidden')) {
      this.cancel();
      this.scene.pause();
    } else {
      this.scene.resume();
    }
  }

  onClick(event) {
    const action = event.target.closest('button')?.dataset.action;
    if (action === 'skip') {
      this.speed = 10;
      this.wakeAll();
    } else if (action === 'replay' && this.data) {
      this.play(this.data);
    } else if (action === 'close') {
      bus.emit('evolve:close');
    }
  }

  // ---------------------------------------------------------------- playback
  cancel() {
    this.token++;
    this.playing = false;
    this.wakeAll();
    this.cancelChoice?.();
    this.cancelChoice = null;
    this.$('.choices').replaceChildren();
  }

  async play(data) {
    this.cancel();
    const token = this.token;
    this.data = data;
    this.speed = 1;
    this.playing = true;
    const byId = new Map(data.nodes.map((n) => [n.id, n]));
    const children = (node) => data.nodes.filter((n) => n.parent === node.id);
    const line = [];
    for (let n = byId.get(data.target); n; n = n.parent != null ? byId.get(n.parent) : null) line.unshift(n);
    this.tallest = Math.max(...data.nodes.map((n) => n.height || 1));
    await Promise.all(data.nodes.map(async (n) => this.sprites.set(n.id, await loadSprite(n.id))));
    if (token !== this.token) return;

    this.trail = [];
    this.childrenOf = children;
    // startAt: jump to a shown Pokémon and wait for [evolve], like after an Egg hatches.
    // Otherwise play the family from the root (Egg / legendary appear).
    // Forms outside the evo tree (e.g. Gigantamax) fall back to the species node.
    const start = data.startAt != null ? (byId.get(data.startAt) || byId.get(data.target)) : null;
    let current;
    let ahead = [];
    if (start) {
      current = start;
      this.trail = line.includes(start) ? line.slice(0, line.indexOf(start) + 1) : [start];
      this.renderTrail();
      this.say(`Here's ${start.name}!`);
      await this.run({ kind: 'appear', node: start }, 1400);
      if (token !== this.token) return;
      await this.run({ kind: 'idle', node: start }, 600);
    } else {
      current = line[0];
      if (data.hatches) await this.hatch(current, token);
      else await this.appear(current, token);
      ahead = line.slice(1);
    }
    while (token === this.token) {
      const options = children(current);
      if (!options.length) break;
      const next = await this.prompt(current, ahead.length ? [ahead.shift()] : options, token);
      if (!next || token !== this.token) return;
      await this.evolve(current, next, token);
      current = next;
    }
    if (token !== this.token) return;
    const evolved = this.trail.filter((x) => x !== 'egg').length > 1;
    const ending = current.mega ? this.$('.caption .text').textContent : `${current.name} is fully evolved.`;
    this.say(evolved ? ending : `${current.name} does not evolve.`,
      evolved ? 'Press replay to watch again, or ask about another Pokémon.' : '');
    this.playing = false;
  }

  async hatch(node, token) {
    this.trail = ['egg'];
    this.renderTrail();
    this.say('Oh? The Egg is moving!');
    await this.run({ kind: 'egg', node }, 2600);
    if (token !== this.token) return;
    this.say('Something is about to hatch…');
    await this.run({ kind: 'crack', node }, 1500);
    if (token !== this.token) return;
    await this.run({ kind: 'hatch', node }, 1500);
    if (token !== this.token) return;
    this.say(`${node.name} hatched from the Egg!`, node.baby ? 'A baby Pokémon!' : '');
    this.trail.push(node);
    this.renderTrail();
    await this.run({ kind: 'idle', node }, 1300);
  }

  async appear(node, token) {
    this.trail = [node];
    this.renderTrail();
    this.say(`A wild ${node.name} appeared!`, 'Legendary and mythical Pokémon don\'t hatch from Eggs.');
    await this.run({ kind: 'appear', node }, 1800);
    if (token !== this.token) return;
    await this.run({ kind: 'idle', node }, 1000);
  }

  async evolve(from, to, token) {
    const primal = to.name.startsWith('Primal');
    if (to.mega) this.say(primal ? `${from.name} is glowing with ancient power!` : `${from.name}'s ${to.condition.replace(/^holding (the )?/, '')} is reacting!`);
    else this.say(`What? ${from.name} is evolving!`);
    await this.run({ kind: 'evolve', from, to }, to.mega ? 3600 : 4800);
    if (token !== this.token) return;
    this.trail.push(to);
    this.renderTrail();
    if (to.mega) this.say(primal ? `${from.name}'s Primal Reversion! It reverted to its primal form!` : `${from.name} has Mega Evolved into ${to.name}!`, `how: ${to.condition}`);
    else this.say(`Congratulations! Your ${from.name} evolved into ${to.name}!`, to.condition ? `how: ${to.condition}` : '');
    bus.emit('dex:seen', { species: to.species_id });
    await this.run({ kind: 'idle', node: to }, 2000);
  }

  /** Wait for the player to press evolve (or pick a branch) instead of evolving on its own. */
  prompt(node, options, token) {
    const evolutions = options.filter((o) => !o.mega);
    const megas = options.filter((o) => o.mega);
    if (evolutions.length > 1) {
      this.say(`${node.name} can evolve in ${evolutions.length} different ways. Pick one:`, megas.length ? 'It can also Mega Evolve.' : '');
    } else if (evolutions.length) {
      const how = evolutions[0].condition;
      this.say(this.$('.caption .text').textContent, `${node.name} is ready to evolve${how ? ` (${how})` : ''}.`);
    } else {
      const verb = megas[0].name.startsWith('Primal') ? 'undergo Primal Reversion' : 'Mega Evolve';
      this.say(this.$('.caption .text').textContent,
        `${node.name} can ${verb}${megas.length === 1 ? ` (${megas[0].condition})` : '. Pick a form:'}`);
    }
    this.renderTrail(evolutions.length > 0);
    this.phase = { kind: 'idle', node, start: performance.now(), dur: 0 };
    this.scene.refresh();
    this.playing = false;
    const label = (option, group) => {
      if (group.length === 1) return option.mega ? (option.name.startsWith('Primal') ? 'primal reversion' : 'mega evolve') : 'evolve';
      return option.name;
    };
    return new Promise((resolve) => {
      const box = this.$('.choices');
      box.replaceChildren(...[...evolutions.map((o) => [o, evolutions]), ...megas.map((o) => [o, megas])].map(([option, group]) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.title = option.condition || '';
        button.append(`[ ${label(option, group)}`);
        if (group.length > 1 && option.condition) {
          button.append(Object.assign(document.createElement('span'), { textContent: ` · ${option.condition}` }));
        }
        button.append(' ]');
        button.addEventListener('click', () => {
          box.replaceChildren();
          this.speed = 1;
          this.playing = true;
          this.cancelChoice = null;
          resolve(token === this.token ? option : null);
        });
        return button;
      }));
      this.cancelChoice = () => resolve(null);
    });
  }

  run(phase, ms) {
    const dur = ms / this.speed;
    this.phase = { ...phase, start: performance.now(), dur };
    this.scene.animate(dur);
    return new Promise((resolve) => {
      const done = () => { this.wakers.delete(done); resolve(); };
      this.wakers.add(done);
      sleep(dur).then(done);
    });
  }

  wakeAll() {
    for (const wake of [...this.wakers]) wake();
    this.wakers.clear();
  }

  // ---------------------------------------------------------------- overlays
  say(text, sub = '') {
    this.$('.caption .text').textContent = text;
    this.$('.caption .sub').textContent = sub;
  }

  renderTrail(branching = false) {
    const parts = this.trail.map((step, i) => {
      const name = step === 'egg' ? 'egg' : step.name.toLowerCase();
      return i === this.trail.length - 1 ? `<b>${name}</b>` : `<i>${name}</i>`;
    });
    const last = this.trail[this.trail.length - 1];
    if (last && last !== 'egg' && (branching || this.childrenOf(last).some((n) => !n.mega))) parts.push('?');
    this.$('.trail').innerHTML = parts.join(' ─> ');
  }

  // ---------------------------------------------------------------- painting
  paint(ctx, w, h, now) {
    const ground = h * 0.9;
    const sky = ctx.createLinearGradient(0, 0, 0, h);
    sky.addColorStop(0, '#0b0a12');
    sky.addColorStop(1, '#17132a');
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, w, h);
    ctx.fillStyle = '#2b2642';
    ctx.fillRect(0, ground, w, 2);
    const ph = this.phase;
    if (!ph) return;
    const t = ph.dur ? clamp01((now - ph.start) / ph.dur) : 1;
    const trail = sourceRect(this.$('.trail'), this.$('canvas'), w);
    const top = (trail ? trail.bottom : h * 0.1) + h * 0.05;
    const maxH = Math.max(h * 0.3, ground - top);
    const rectFor = (node, k = 1) => standingRect(this.sprites.get(node.id), w / 2, ground, w * 0.7,
      maxH * heightScale(node.height, this.tallest) * k);
    const node = ph.node || ph.to;
    const color = TYPE_COLORS[node?.types?.[0]] || '#7062ab';
    const eggH = Math.min(maxH * 0.55, (w * 0.32) / 0.78);

    if (ph.kind === 'egg' || ph.kind === 'crack') {
      let angle = 0;
      if (ph.kind === 'egg') {
        for (const [a, b, amp] of WOBBLES) {
          if (t >= a && t <= b) {
            const u = (t - a) / (b - a);
            angle = Math.sin(u * Math.PI * 6) * amp * Math.sin(u * Math.PI);
          }
        }
      } else {
        angle = Math.sin(t * 50) * 0.05 * t;
      }
      this.shadow(ctx, w / 2, ground, eggH * 0.45);
      if (ph.kind === 'crack' && t > 0.55) this.glow(ctx, w / 2, ground - eggH * 0.5, eggH * (0.6 + t), '#ffffff', (t - 0.55) * 1.2);
      this.egg(ctx, w / 2, ground, eggH, color, angle, ph.kind === 'crack' ? ease(t) : 0, 0);
    } else if (ph.kind === 'hatch') {
      const split = ease(clamp01((t - 0.12) / 0.5));
      const grow = 0.35 + 0.65 * ease(clamp01((t - 0.15) / 0.55));
      const white = 1 - clamp01((t - 0.62) / 0.38);
      const rect = rectFor(ph.node, grow);
      this.glow(ctx, w / 2, rect.cy, Math.max(rect.w, rect.h) * 0.8, color, 0.6 * (1 - t));
      this.shadow(ctx, w / 2, ground, Math.max(rect.w * 0.5, eggH * 0.45 * (1 - split)));
      this.sprite(ctx, ph.node, rect, white);
      if (split < 1) this.egg(ctx, w / 2, ground, eggH, color, 0, 1, split);
      this.fragments(ctx, w / 2, ground - eggH * 0.5, eggH, t);
      this.flash(ctx, w, h, t < 0.18 ? t / 0.18 : 1 - clamp01((t - 0.18) / 0.35));
    } else if (ph.kind === 'appear') {
      const rect = rectFor(ph.node);
      this.sparkles(ctx, rect, now, (1 - t) * 1.4, '#ffffff', 18);
      ctx.globalAlpha = ease(t);
      this.shadow(ctx, w / 2, ground, rect.w * 0.5);
      this.sprite(ctx, ph.node, rect, 1 - clamp01((t - 0.5) / 0.5));
      ctx.globalAlpha = 1;
    } else if (ph.kind === 'idle') {
      const rect = rectFor(ph.node);
      const bob = Math.sin(now / 380) * h * 0.006;
      this.glow(ctx, w / 2, rect.cy, Math.max(rect.w, rect.h) * 0.7, color, 0.25);
      this.shadow(ctx, w / 2, ground, rect.w * 0.5);
      this.sprite(ctx, ph.node, { ...rect, y: rect.y + bob }, 0);
    } else if (ph.kind === 'evolve') {
      this.paintEvolution(ctx, w, h, ground, t, now, rectFor(ph.from), rectFor(ph.to), ph);
    }
  }

  paintEvolution(ctx, w, h, ground, t, now, from, to, ph) {
    const color = TYPE_COLORS[ph.to.types?.[0]] || '#7062ab';
    ctx.fillStyle = `rgba(0, 0, 0, ${0.35 * Math.sin(Math.PI * t)})`;
    ctx.fillRect(0, 0, w, h);
    if (t < 0.15) {
      const u = t / 0.15;
      this.glow(ctx, w / 2, from.cy, Math.max(from.w, from.h) * (0.6 + u * 0.4), '#ffffff', u * 0.6);
      this.shadow(ctx, w / 2, ground, from.w * 0.5);
      this.sprite(ctx, ph.from, from, u);
    } else if (t < 0.8) {
      const k = (t - 0.15) / 0.65;
      const showNew = Math.floor(k * k * 18) % 2 === 1;
      const rect = showNew ? to : from;
      const pulse = 1 + 0.05 * Math.sin(now / 55);
      const scaled = { ...rect, w: rect.w * pulse, h: rect.h * pulse, y: ground - rect.h * pulse };
      this.glow(ctx, w / 2, rect.cy, Math.max(rect.w, rect.h) * 1.05, color, 0.55);
      this.shadow(ctx, w / 2, ground, rect.w * 0.5);
      this.sparkles(ctx, rect, now, 1, '#ffffff', 14);
      this.sprite(ctx, showNew ? ph.to : ph.from, scaled, 1);
    } else if (t < 0.9) {
      this.shadow(ctx, w / 2, ground, to.w * 0.5);
      this.sprite(ctx, ph.to, to, 1);
      this.flash(ctx, w, h, 1 - Math.abs((t - 0.85) / 0.05));
    } else {
      const u = (t - 0.9) / 0.1;
      this.glow(ctx, w / 2, to.cy, Math.max(to.w, to.h) * 0.9, color, 0.5 * (1 - u));
      this.shadow(ctx, w / 2, ground, to.w * 0.5);
      this.sprite(ctx, ph.to, to, 1 - u);
    }
  }

  /** The sprite, washed towards white by `white` (0 = full colour, 1 = white silhouette). */
  sprite(ctx, node, rect, white) {
    const img = this.sprites.get(node.id);
    if (!img) return;
    const alpha = ctx.globalAlpha;
    if (white < 1) drawRect(ctx, img, rect);
    if (white > 0) {
      ctx.globalAlpha = alpha * white;
      drawRect(ctx, silhouette(img, '#ffffff'), rect);
      ctx.globalAlpha = alpha;
    }
  }

  /** An Egg standing at (cx, ground), `crack` 0..1 drawn across it, `split` 0..1 opening it. */
  egg(ctx, cx, ground, eh, color, angle, crack, split) {
    const ew = eh * 0.78;
    const path = () => {
      ctx.beginPath();
      ctx.ellipse(0, -eh * 0.42, ew / 2, eh * 0.58, 0, Math.PI, Math.PI * 2);
      ctx.ellipse(0, -eh * 0.42, ew / 2, eh * 0.42, 0, 0, Math.PI);
      ctx.closePath();
    };
    const body = () => {
      path();
      const shade = ctx.createRadialGradient(-ew * 0.18, -eh * 0.68, eh * 0.05, 0, -eh * 0.45, eh * 0.7);
      shade.addColorStop(0, '#fffaf0');
      shade.addColorStop(0.6, '#efe3c4');
      shade.addColorStop(1, '#b9a982');
      ctx.fillStyle = shade;
      ctx.fill();
      ctx.save();
      path();
      ctx.clip();
      ctx.fillStyle = color;
      for (const [x, y, r] of [[-0.22, -0.72, 0.11], [0.2, -0.45, 0.14], [-0.15, -0.22, 0.09], [0.12, -0.88, 0.07]]) {
        ctx.beginPath();
        ctx.ellipse(x * ew * 1.4, y * eh, r * eh, r * eh * 0.8, 0, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();
    };
    const crackY = -eh * 0.52;
    ctx.save();
    ctx.translate(cx, ground);
    ctx.rotate(angle);
    if (split > 0) {
      ctx.globalAlpha = 1 - split;
      ctx.save();
      ctx.beginPath();
      ctx.rect(-ew, crackY, ew * 2, eh);
      ctx.clip();
      body();
      ctx.restore();
      ctx.save();
      ctx.translate(-split * ew * 0.3, -split * eh * 0.9);
      ctx.rotate(-split * 0.8);
      ctx.beginPath();
      ctx.rect(-ew, -eh * 1.2, ew * 2, eh * 1.2 + crackY);
      ctx.clip();
      body();
      ctx.restore();
    } else {
      body();
    }
    if (crack > 0 && split === 0) {
      const steps = 8;
      ctx.strokeStyle = '#2b2642';
      ctx.lineWidth = Math.max(2, eh * 0.03);
      ctx.beginPath();
      for (let i = 0; i <= steps * crack; i++) {
        const x = -ew / 2 + (ew * i) / steps;
        const y = crackY + (i % 2 ? -1 : 1) * eh * 0.05;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
    ctx.restore();
  }

  fragments(ctx, cx, cy, eh, t) {
    if (t < 0.12 || t > 0.85) return;
    const u = (t - 0.12) / 0.73;
    ctx.fillStyle = '#efe3c4';
    ctx.globalAlpha = 1 - u;
    for (let i = 0; i < 12; i++) {
      const angle = (i / 12) * Math.PI * 2 + 0.4;
      const speed = eh * (0.6 + (i % 3) * 0.25);
      const x = cx + Math.cos(angle) * speed * u;
      const y = cy + Math.sin(angle) * speed * u * 0.7 + eh * 1.2 * u * u;
      const s = eh * 0.05;
      ctx.fillRect(x - s / 2, y - s / 2, s, s);
    }
    ctx.globalAlpha = 1;
  }

  sparkles(ctx, rect, now, spread, color, count) {
    ctx.fillStyle = color;
    for (let i = 0; i < count; i++) {
      const phase = ((now / 1400 + i / count) % 1);
      const x = rect.cx + Math.cos(i * 2.4) * rect.w * 0.55 * spread;
      const y = rect.y + rect.h * (1 - phase) - rect.h * 0.1;
      const s = Math.max(3, rect.h * 0.025) * Math.sin(Math.PI * phase);
      ctx.globalAlpha = Math.sin(Math.PI * phase);
      ctx.fillRect(x - s / 2, y - s * 1.5, s, s * 3);
      ctx.fillRect(x - s * 1.5, y - s / 2, s * 3, s);
    }
    ctx.globalAlpha = 1;
  }

  glow(ctx, x, y, radius, color, strength) {
    if (strength <= 0) return;
    const g = ctx.createRadialGradient(x, y, 0, x, y, radius);
    const alpha = Math.round(clamp01(strength) * 160).toString(16).padStart(2, '0');
    g.addColorStop(0, `${color}${alpha}`);
    g.addColorStop(1, '#00000000');
    ctx.fillStyle = g;
    ctx.fillRect(x - radius, y - radius, radius * 2, radius * 2);
  }

  shadow(ctx, cx, ground, rx) {
    ctx.fillStyle = '#3a3360';
    ctx.beginPath();
    ctx.ellipse(cx, ground, Math.max(rx, 14), Math.max(rx * 0.12, 4), 0, 0, Math.PI * 2);
    ctx.fill();
  }

  flash(ctx, w, h, strength) {
    if (strength <= 0) return;
    ctx.fillStyle = `rgba(255, 255, 255, ${clamp01(strength)})`;
    ctx.fillRect(0, 0, w, h);
  }
}

customElements.define('poke-evolve', PokeEvolve);
