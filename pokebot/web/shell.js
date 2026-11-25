// Shell: composes the micro-frontends and owns layout-level state only.
import { getJSON } from './lib/api.js';
import { bus } from './lib/bus.js';
import { currentTheme, setCurrentTheme } from './lib/scene.js';

const stage = document.querySelector('.stage');
const views = () => [...stage.querySelectorAll('[data-view]')];
const viewEl = (name) => stage.querySelector(`[data-view="${name}"]`);
let current = 'scope';
let focused = null;

function show(name) {
  current = name;
  for (const el of views()) el.hidden = el.dataset.view !== name;
  for (const tab of stage.querySelectorAll('[data-tab]')) tab.setAttribute('aria-pressed', String(tab.dataset.tab === name));
}

/** A view mid-animation keeps the stage until it finishes. */
const busy = () => (current === 'arena' || current === 'evolve') && viewEl(current).playing;

stage.querySelector('.tabs').addEventListener('click', async (event) => {
  const name = event.target.closest('[data-tab]')?.dataset.tab;
  if (!name) return;
  show(name);
  if (name === 'evolve' && !viewEl('evolve').data && focused) {
    bus.emit('evolve:play', { evolution: await getJSON(`/api/evolution/${focused.id}`) });
  }
});

bus.on('battle:play', () => {
  stage.querySelector('[data-tab="arena"]').disabled = false;
  show('arena');
});
bus.on('evolve:play', () => show('evolve'));
bus.on('arena:close', () => show('scope'));
bus.on('evolve:close', () => show('scope'));
bus.on('pokemon:focus', ({ card }) => {
  focused = card;
  if (!busy()) show('scope');
});

const select = document.getElementById('theme-select');
select.value = currentTheme;
select.addEventListener('change', () => {
  setCurrentTheme(select.value);
  bus.emit('theme:change', { theme: select.value });
});

getJSON('/api/status')
  .then(({ llm, counts }) => {
    document.getElementById('status-line').textContent =
      `${counts.species} species · ${counts.moves} moves · ${llm ? `llm: ${llm}` : 'template answers (no llm)'} · offline`;
  })
  .catch(() => { document.getElementById('status-line').textContent = 'server unreachable'; });
