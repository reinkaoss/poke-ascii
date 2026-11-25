// <poke-dex>: browse every species, with search, type/generation filters and a "seen" tracker.
import { getJSON } from '../../lib/api.js';
import { bus } from '../../lib/bus.js';
import { TYPE_COLORS, loadSprite } from '../../lib/scene.js';

const SEEN_KEY = 'pokebot-seen';
const THUMB_COLS = 24;
const THUMB_ROWS = 11;
const RAMP = '.:-=+*#%@';
const CELL_ASPECT = 0.602; // Menlo glyph width / line height

const thumbs = new Map();
/** A tiny coloured ASCII rendering of the sprite, as HTML. */
function thumbnail(species) {
  if (!thumbs.has(species)) {
    thumbs.set(species, loadSprite(species).then((img) => {
      if (!img) return '';
      const scale = Math.min((THUMB_COLS * CELL_ASPECT) / img.width, THUMB_ROWS / img.height);
      const cols = (img.width * scale) / CELL_ASPECT;
      const rows = img.height * scale;
      const canvas = document.createElement('canvas');
      canvas.width = THUMB_COLS;
      canvas.height = THUMB_ROWS;
      const ctx = canvas.getContext('2d', { willReadFrequently: true });
      ctx.imageSmoothingQuality = 'high';
      ctx.drawImage(img, (THUMB_COLS - cols) / 2, (THUMB_ROWS - rows) / 2, cols, rows);
      const { data } = ctx.getImageData(0, 0, THUMB_COLS, THUMB_ROWS);
      const lines = [];
      for (let y = 0; y < THUMB_ROWS; y++) {
        let line = '';
        for (let x = 0; x < THUMB_COLS; x++) {
          const i = (y * THUMB_COLS + x) * 4;
          const alpha = data[i + 3] / 255;
          if (alpha < 0.2) { line += ' '; continue; }
          // Lift dark colours so they stay visible on the dark background.
          let [r, g, b] = [data[i], data[i + 1], data[i + 2]];
          const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
          const lift = Math.max(1, 110 / Math.max(r, g, b, 1));
          [r, g, b] = [r, g, b].map((v) => Math.min(255, Math.round(v * lift)));
          const char = RAMP[Math.min(RAMP.length - 1, Math.floor((0.25 + 0.75 * lum) * alpha * RAMP.length))];
          line += `<span style="color:rgb(${r},${g},${b})">${char}</span>`;
        }
        lines.push(line);
      }
      return lines.join('\n');
    }));
  }
  return thumbs.get(species);
}

const template = document.createElement('template');
template.innerHTML = `
<style>
  :host { display: grid; grid-template-rows: auto minmax(0, 1fr); overflow: hidden; }
  [hidden] { display: none !important; }
  .bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 18px 12px 8px; border-bottom: 1px dashed var(--line); }
  .bar span { color: var(--color-purple-medium); }
  input, select {
    background: var(--bg); color: var(--text); border: 1px solid var(--line); font: inherit; font-size: 12px; padding: 3px 6px;
  }
  input { flex: 1 1 160px; min-width: 0; outline: none; caret-color: var(--color-purple-medium); }
  input:focus, select:focus { border-color: var(--color-purple-medium); }
  .count { color: var(--muted); font-size: 12px; margin-left: auto; white-space: nowrap; }
  .list { overflow-y: auto; padding: 4px 0 12px; }
  .row {
    display: grid; grid-template-columns: 1.5ch var(--thumb-width) 6ch minmax(0, 1fr) 19ch 9ch; gap: 1ch; align-items: center;
    --thumb-font: 5px; --thumb-width: calc(${THUMB_COLS} * 0.602 * var(--thumb-font));
    width: 100%; padding: 2px 12px; background: none; border: 0; color: var(--text); font: inherit; font-size: 12.5px;
    text-align: left; cursor: pointer;
  }
  .row:hover, .row:focus-visible { background: color-mix(in srgb, var(--color-purple-medium) 18%, transparent); outline: none; }
  .row .mark { color: var(--color-purple-medium); }
  .row.unseen .mark, .row .num, .row .tag { color: var(--muted); }
  .row .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .thumb {
    margin: 0; height: calc(${THUMB_ROWS} * var(--thumb-font)); font: bold var(--thumb-font)/1 Menlo, monospace;
    letter-spacing: 0; white-space: pre; overflow: hidden;
  }
  .empty { padding: 16px 12px; color: var(--muted); }
  :host(.narrow) .row { grid-template-columns: 1.5ch var(--thumb-width) 5ch minmax(0, 1fr) 16ch; font-size: 11.5px; padding: 3px 10px; }
  :host(.narrow) .row .tag { display: none; }
  :host(.narrow) .bar { padding: 16px 10px 6px; gap: 6px; }
</style>
<div class="bar">
  <span>›</span>
  <input type="search" placeholder="search name or #" aria-label="Search the Pokédex">
  <select data-filter="type" aria-label="Type"><option value="">all types</option></select>
  <select data-filter="gen" aria-label="Generation"><option value="">all gens</option></select>
  <select data-filter="seen" aria-label="Seen">
    <option value="">all</option><option value="seen">seen</option><option value="unseen">not seen</option>
  </select>
  <div class="count"></div>
</div>
<div class="list" role="listbox" aria-label="Pokémon"></div>
`;

class PokeDex extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' }).appendChild(template.content.cloneNode(true));
    this.$ = (sel) => this.shadowRoot.querySelector(sel);
    this.entries = [];
    this.seen = new Set(JSON.parse(localStorage.getItem(SEEN_KEY) || '[]'));
  }

  connectedCallback() {
    const type = this.$('[data-filter="type"]');
    for (const t of Object.keys(TYPE_COLORS)) type.append(new Option(t, t));
    const gen = this.$('[data-filter="gen"]');
    for (let g = 1; g <= 9; g++) gen.append(new Option(`gen ${g}`, String(g)));
    this.$('.bar').addEventListener('input', () => this.render());
    this.$('input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') this.$('.row')?.click();
    });
    this.$('.list').addEventListener('click', (e) => {
      const row = e.target.closest('.row');
      if (row) this.open(Number(row.dataset.pokemon));
    });
    this.offs = [
      bus.on('pokemon:focus', ({ card }) => this.markSeen(card.species_id)),
      bus.on('battle:play', ({ result }) => { this.markSeen(result.a.species_id); this.markSeen(result.b.species_id); }),
      bus.on('dex:seen', ({ species }) => this.markSeen(species)),
    ];
    new ResizeObserver(() => this.classList.toggle('narrow', this.getBoundingClientRect().width < 560)).observe(this);
    getJSON('/api/dex')
      .then((rows) => {
        this.entries = rows.map(([species, name, generation, types, pokemon, tag]) =>
          ({ species, name, generation, types, pokemon, tag, key: name.toLowerCase() }));
        this.render();
      })
      .catch(() => { this.$('.list').innerHTML = '<div class="empty">server unreachable</div>'; });
  }

  disconnectedCallback() {
    this.offs?.forEach((off) => off());
    this.thumbObserver?.disconnect();
  }

  markSeen(species) {
    if (!species || this.seen.has(species)) return;
    this.seen.add(species);
    localStorage.setItem(SEEN_KEY, JSON.stringify([...this.seen]));
    const row = this.shadowRoot.querySelector(`.row[data-species="${species}"]`);
    if (row) {
      row.classList.remove('unseen');
      row.querySelector('.mark').textContent = '●';
    }
    this.renderCount();
  }

  async open(pokemonId) {
    bus.emit('pokemon:focus', { card: await getJSON(`/api/pokemon/${pokemonId}`) });
  }

  filtered() {
    const query = this.$('input').value.trim().toLowerCase().replace(/^#/, '');
    const type = this.$('[data-filter="type"]').value;
    const gen = Number(this.$('[data-filter="gen"]').value);
    const seen = this.$('[data-filter="seen"]').value;
    const number = /^\d+$/.test(query) ? Number(query) : null;
    return this.entries.filter((e) =>
      (!query || (number !== null ? e.species === number || String(e.species).startsWith(query) : e.key.includes(query))) &&
      (!type || e.types.includes(type)) &&
      (!gen || e.generation === gen) &&
      (!seen || (seen === 'seen') === this.seen.has(e.species)));
  }

  render() {
    const list = this.$('.list');
    const rows = this.filtered();
    const fragment = document.createDocumentFragment();
    for (const e of rows) {
      const row = document.createElement('button');
      const seen = this.seen.has(e.species);
      row.type = 'button';
      row.className = `row${seen ? '' : ' unseen'}`;
      row.dataset.species = e.species;
      row.dataset.pokemon = e.pokemon;
      row.setAttribute('role', 'option');
      const types = e.types.map((t) => `<span style="color:${TYPE_COLORS[t]}">${t}</span>`).join(' ');
      row.innerHTML =
        `<span class="mark">${seen ? '●' : '○'}</span><pre class="thumb" aria-hidden="true"></pre>` +
        `<span class="num">#${String(e.species).padStart(4, '0')}</span>` +
        `<span class="name"></span><span>${types}</span><span class="tag">${e.tag}</span>`;
      row.querySelector('.name').textContent = e.name;
      fragment.append(row);
    }
    this.thumbObserver?.disconnect();
    list.replaceChildren(fragment);
    // Thumbnails are drawn only as rows scroll into view: 1025 sprites up front would be ~8 MB.
    this.thumbObserver = new IntersectionObserver((seen) => {
      for (const { isIntersecting, target } of seen) {
        if (!isIntersecting) continue;
        this.thumbObserver.unobserve(target);
        thumbnail(Number(target.dataset.species)).then((html) => { target.querySelector('.thumb').innerHTML = html; });
      }
    }, { root: list, rootMargin: '300px 0px' });
    for (const row of list.querySelectorAll('.row')) this.thumbObserver.observe(row);
    if (!rows.length) list.innerHTML = '<div class="empty">No Pokémon match those filters.</div>';
    this.renderCount(rows.length);
  }

  renderCount(shown = this.$('.list').querySelectorAll('.row').length) {
    const total = this.entries.length;
    this.$('.count').textContent = `${shown === total ? '' : `${shown} shown · `}seen ${this.seen.size}/${total}`;
  }
}

customElements.define('poke-dex', PokeDex);
