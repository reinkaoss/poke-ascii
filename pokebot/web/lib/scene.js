// OpenASCII-backed scenes: draw anything on a hidden source canvas, OpenASCII turns it into
// animated ASCII on the visible canvas.
import { createOpenAsciiRuntime } from './openascii.js';

const { AsciiEngine } = createOpenAsciiRuntime();

export const TYPE_COLORS = {
  normal: '#a8a77a', fire: '#ee8130', water: '#6390f0', electric: '#f7d02c', grass: '#7ac74c',
  ice: '#96d9d6', fighting: '#c22e28', poison: '#a33ea1', ground: '#e2bf65', flying: '#a98ff3',
  psychic: '#f95587', bug: '#a6b91a', rock: '#b6a136', ghost: '#735797', dragon: '#6f35fc',
  dark: '#705746', steel: '#b7b7ce', fairy: '#d685ad',
};

const cssVar = (name, fallback) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;

const BASE = {
  font: 'Menlo', characterSet: 'detailed', ditherAlgorithm: 'floyd-steinberg', ditherStrength: 0.3,
  brightness: 50, contrast: 1.45, gamma: 0.95, localContrast: 0.7, edgeEnhance: 0.8, saliencyDetail: 0.7,
  fontSize: 9, characterSpacing: 1, densityScale: 1.05, quality: 260, vignette: 0.12, borderGlow: 0,
  backgroundStyle: 'solid', fxPreset: 'noise-field', fxStrength: 0.12, glowStrength: 0.12,
  mouseMode: 'attract', hoverStrength: 10, areaSize: 140, temporalPersistence: 0, noiseOpacity: 0.02,
};

export const THEMES = {
  pokedex: () => ({
    ...BASE, artStyle: 'classic-ascii', colorMode: 'full-color', colorMix: 0.95, colorSaturation: 1.3,
    brightness: 57, contrast: 1.3, gamma: 0.82, highlightBoost: 0.5,
    foreground: '#ece9ff', background: cssVar('--bg', '#0b0a12'), accent: cssVar('--color-purple-medium', '#7062ab'),
  }),
  phosphor: () => ({
    ...BASE, artStyle: 'classic-ascii', colorMode: 'custom', colorMix: 0.6,
    foreground: '#c9c1f5', background: cssVar('--bg', '#0b0a12'), accent: cssVar('--color-purple-medium', '#7062ab'),
    fxPreset: 'crt', fxStrength: 0.22, glowStrength: 0.22,
  }),
  braille: () => ({
    ...BASE, artStyle: 'braille', colorMode: 'full-color', colorMix: 0.9, fontSize: 8, characterSpacing: 0.9,
    densityScale: 1.18, ditherAlgorithm: 'bayer', ditherStrength: 0.45, contrast: 1.6,
    foreground: '#ece9ff', background: cssVar('--bg', '#0b0a12'), accent: '#e6fbff', mouseMode: 'push',
  }),
  pixel: () => ({
    ...BASE, artStyle: 'retro-art', characterSet: 'blocks', colorMode: 'full-color', colorMix: 0.92,
    fontSize: 8, characterSpacing: 0.86, densityScale: 1.12, primitiveShape: 'square', contrast: 1.6,
    gamma: 0.84, ditherAlgorithm: 'atkinson', ditherStrength: 0.34, fxPreset: 'none',
    foreground: '#f7f0dc', background: cssVar('--bg', '#0b0a12'), accent: '#77e8ff', mouseMode: 'push',
  }),
};

export let currentTheme = localStorage.getItem('pokebot-theme') || 'pokedex';
export function setCurrentTheme(name) {
  currentTheme = THEMES[name] ? name : 'pokedex';
  localStorage.setItem('pokebot-theme', currentTheme);
}

const spriteCache = new Map();
/**
 * Sprite artwork cropped to its visible pixels, so every Pokémon is measured the same way.
 * `view` is 'front' (official artwork) or 'back' (pixel-art back sprite; resolves null if there is none).
 */
export function loadSprite(pokemonId, view = 'front') {
  const key = `${view}:${pokemonId}`;
  if (!spriteCache.has(key)) {
    spriteCache.set(key, new Promise((resolve) => {
      const img = new Image();
      img.onload = () => resolve(trimTransparent(img));
      img.onerror = () => resolve(null);
      img.src = view === 'back' ? `/api/sprite/back/${pokemonId}.webp` : `/api/sprite/${pokemonId}.webp`;
    }));
  }
  return spriteCache.get(key);
}

function trimTransparent(img) {
  const full = document.createElement('canvas');
  full.width = img.naturalWidth;
  full.height = img.naturalHeight;
  const ctx = full.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0);
  const { data, width, height } = ctx.getImageData(0, 0, full.width, full.height);
  let minX = width, minY = height, maxX = -1, maxY = -1;
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      if (data[(y * width + x) * 4 + 3] > 24) {
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
    }
  }
  if (maxX < 0) return full;
  const out = document.createElement('canvas');
  out.width = maxX - minX + 1;
  out.height = maxY - minY + 1;
  out.getContext('2d').drawImage(full, minX, minY, out.width, out.height, 0, 0, out.width, out.height);
  return out;
}

const silhouettes = new WeakMap();
/** The sprite filled with one flat colour (hit flashes, evolution glow). */
export function silhouette(img, color) {
  if (!silhouettes.has(img)) silhouettes.set(img, new Map());
  const byColor = silhouettes.get(img);
  if (!byColor.has(color)) {
    const c = document.createElement('canvas');
    c.width = img.width;
    c.height = img.height;
    const x = c.getContext('2d');
    x.drawImage(img, 0, 0);
    x.globalCompositeOperation = 'source-in';
    x.fillStyle = color;
    x.fillRect(0, 0, c.width, c.height);
    byColor.set(color, c);
  }
  return byColor.get(color);
}

/**
 * Relative on-screen size for a Pokémon of `height` metres next to one of `tallest` metres.
 * Square-root compressed so a 0.3 m Pokémon is still visible beside a 14 m one.
 */
export const heightScale = (height, tallest) => Math.max(0.4, Math.sqrt((height || 1) / (tallest || 1)));

/** Size an image to fit a box, anchored at its bottom centre (a Pokémon standing on the ground). */
export function standingRect(img, cx, groundY, maxW, maxH) {
  const scale = Math.min(maxW / img.width, maxH / img.height);
  const w = img.width * scale;
  const h = img.height * scale;
  return { x: cx - w / 2, y: groundY - h, w, h, cx, cy: groundY - h / 2 };
}

export function drawRect(ctx, img, rect, { flip = false } = {}) {
  if (!img) return;
  ctx.save();
  ctx.translate(rect.cx, rect.y + rect.h / 2);
  if (flip) ctx.scale(-1, 1);
  ctx.drawImage(img, -rect.w / 2, -rect.h / 2, rect.w, rect.h);
  ctx.restore();
}

/** Map an element's box into a scene's source-canvas coordinates. */
export function sourceRect(el, canvas, w) {
  const c = canvas.getBoundingClientRect();
  const r = el.getBoundingClientRect();
  if (!c.width || !r.width) return null;
  const k = w / c.width;
  return { x: (r.left - c.left) * k, y: (r.top - c.top) * k, w: r.width * k, h: r.height * k,
    right: (r.right - c.left) * k, bottom: (r.bottom - c.top) * k };
}

export class AsciiScene {
  /** @param {HTMLCanvasElement} canvas  @param {(ctx, w, h, now) => void} painter */
  constructor(canvas, painter, { sourceWidth = 560, fps = 15 } = {}) {
    this.canvas = canvas;
    this.painter = painter;
    this.sourceWidth = sourceWidth;
    this.frameInterval = 1000 / fps;
    this.source = document.createElement('canvas');
    this.source.width = sourceWidth;
    this.source.height = Math.round(sourceWidth * 0.6);
    this.ctx = this.source.getContext('2d', { willReadFrequently: true });
    this.engine = new AsciiEngine(canvas, THEMES[currentTheme]());
    this.animatingUntil = 0;
    this.lastBuild = 0;
    this.paint();
    this.engine.setSource(this.source);
    this.observer = new ResizeObserver(() => this.fit());
    this.observer.observe(canvas);
    this.running = false;
    this.resume();
  }

  fit() {
    const box = this.canvas.getBoundingClientRect();
    if (box.width < 2 || box.height < 2) return;
    const height = Math.round(this.sourceWidth * (box.height / box.width));
    if (height !== this.source.height) {
      this.source.height = height;
      this.paint();
    }
    this.engine.resize();
    this.rebuild();
  }

  paint(now = performance.now()) {
    const { ctx, source } = this;
    ctx.save();
    ctx.clearRect(0, 0, source.width, source.height);
    this.painter(ctx, source.width, source.height, now);
    ctx.restore();
  }

  rebuild() {
    if (this.engine.source && this.canvas.width > 1) this.engine.buildFrame();
    this.lastBuild = performance.now();
  }

  /** Repaint and re-sample now (for state changes outside an animation). */
  refresh() {
    this.paint();
    this.rebuild();
  }

  /** Keep repainting for `ms` (throttled to the scene fps) - resolves when done. */
  animate(ms) {
    this.animatingUntil = Math.max(this.animatingUntil, performance.now() + ms);
    this.tick();
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  tick() {
    if (this.ticking) return;
    this.ticking = true;
    const loop = (now) => {
      if (!this.running) { this.ticking = false; return; }
      if (now - this.lastBuild >= this.frameInterval) {
        this.paint(now);
        this.rebuild();
      }
      if (now < this.animatingUntil) requestAnimationFrame(loop);
      else { this.paint(now); this.rebuild(); this.ticking = false; }
    };
    requestAnimationFrame(loop);
  }

  setTheme(name) {
    this.engine.setConfig(THEMES[name]());
    this.rebuild();
  }

  pause() {
    this.running = false;
    this.engine.stop();
  }

  resume() {
    if (this.running) return;
    this.running = true;
    this.engine.start();
    requestAnimationFrame(() => this.fit());
  }

  destroy() {
    this.pause();
    this.observer.disconnect();
    this.engine.destroy();
  }
}

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
export const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2);

/** HP bar as text: [██████░░░░] */
export function textBar(value, max, width) {
  const filled = Math.max(0, Math.min(width, Math.round((width * value) / max)));
  return '█'.repeat(filled) + '░'.repeat(width - filled);
}
