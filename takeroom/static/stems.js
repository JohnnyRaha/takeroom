'use strict';

/* Takeroom — persistent stem rack.
 *
 * The takes list is rebuilt on every poll, so anything written into a take row
 * by hand disappears a second later. This renders the rack from server state
 * instead, which means it survives re-renders, page reloads and restarts: if
 * the stems exist on disk, they show.
 *
 * Self-attaching — app.js is untouched.
 */

const STEM_SELECTORS = {
  take: '.take[data-id]',
  actions: '.actions',
  host: 'takes',
};

// Demucs order, loudest-first rather than alphabetical.
const STEM_ORDER = ['drums', 'bass', 'vocals', 'other', 'guitar', 'piano'];

const STEM_SEEN = new Map();   // take id -> filenames, for spotting real changes
let stemAudio = null;
let stemPlaying = null;

function stemEl(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text) el.textContent = text;
  return el;
}

function stemRank(name) {
  const stem = name.replace(/\.(wav|mid)$/i, '').toLowerCase();
  const i = STEM_ORDER.findIndex((s) => stem.includes(s));
  return i === -1 ? STEM_ORDER.length : i;
}

function soloStem(takeId, name, button) {
  const url = `/api/takes/${takeId}/stems/${encodeURIComponent(name)}`;
  if (!stemAudio) {
    stemAudio = new Audio();
    stemAudio.addEventListener('ended', () => { stemPlaying = null; paintStemButtons(); });
    stemAudio.addEventListener('pause', paintStemButtons);
    stemAudio.addEventListener('play', paintStemButtons);
  }
  const id = `${takeId}/${name}`;
  if (stemPlaying === id && !stemAudio.paused) {
    stemAudio.pause();
    return;
  }
  if (stemPlaying !== id) {
    stemPlaying = id;
    stemAudio.src = url;
  }
  stemAudio.play().catch(() => {});
  paintStemButtons();
}

function paintStemButtons() {
  document.querySelectorAll('[data-stem-play]').forEach((b) => {
    const active = b.dataset.stemPlay === stemPlaying && stemAudio && !stemAudio.paused;
    b.classList.toggle('starred', !!active);
    b.textContent = active ? '❚❚' : '▶';
  });
}

function renderRack(take) {
  const row = document.querySelector(`${STEM_SELECTORS.take}[data-id="${take.id}"]`);
  if (!row) return;

  const stems = (take.stems || []).slice().sort((a, b) => stemRank(a) - stemRank(b));
  const midi = new Set(take.midi || []);

  let rack = row.querySelector('[data-stem-rack]');

  if (!stems.length) {
    if (rack) rack.remove();
    STEM_SEEN.delete(take.id);
    return;
  }

  // Only rebuild when the contents actually changed, so playback isn't
  // interrupted by a poll that brought nothing new.
  const signature = stems.join(',') + '|' + [...midi].sort().join(',');
  if (rack && STEM_SEEN.get(take.id) === signature) return;
  STEM_SEEN.set(take.id, signature);

  if (!rack) {
    rack = stemEl('div');
    rack.dataset.stemRack = take.id;
    rack.style.cssText = 'margin-top:.6rem;border-top:1px solid var(--rule);padding-top:.6rem';
    (row.querySelector(STEM_SELECTORS.actions) || row).after(rack);
  }
  rack.innerHTML = '';

  stems.forEach((name) => {
    const label = name.replace(/\.wav$/i, '');
    const line = stemEl('div');
    line.style.cssText = 'display:flex;align-items:center;gap:.4rem;margin-bottom:.3rem';

    const play = stemEl('button', 'ghost', '▶');
    play.dataset.stemPlay = `${take.id}/${name}`;
    play.style.cssText = 'width:2rem;flex:none';
    play.title = `Solo ${label}`;
    play.addEventListener('click', (e) => {
      e.stopPropagation();
      soloStem(take.id, name, play);
    });
    line.appendChild(play);

    const title = stemEl('span', '', label);
    title.style.cssText = 'flex:1;font-family:var(--mono);font-size:12.5px;color:var(--ink)';
    line.appendChild(title);

    const wav = stemEl('a', 'ghost', 'wav');
    wav.href = `/api/takes/${take.id}/stems/${encodeURIComponent(name)}`;
    wav.download = '';
    wav.style.textDecoration = 'none';
    line.appendChild(wav);

    const midiName = name.replace(/\.wav$/i, '.mid');
    if (midi.has(midiName)) {
      const mid = stemEl('a', 'ghost', 'mid');
      mid.href = `/api/takes/${take.id}/midi/${encodeURIComponent(midiName)}`;
      mid.download = '';
      mid.style.textDecoration = 'none';
      line.appendChild(mid);
    }

    rack.appendChild(line);
  });

  paintStemButtons();
}

async function refreshRacks() {
  try {
    const res = await fetch('/api/takes');
    if (!res.ok) return;
    const data = await res.json();
    data.takes.forEach(renderRack);
  } catch (err) {
    /* the rack is additive; if this fails the rest of the UI is unaffected */
  }
}

// Re-attach after app.js rebuilds the list, and poll independently so the rack
// appears after a separation finishes without needing a click.
const stemObserver = new MutationObserver(() => refreshRacks());
stemObserver.observe(document.getElementById(STEM_SELECTORS.host) || document.body,
                     { childList: true, subtree: true });
refreshRacks();
setInterval(refreshRacks, 5000);
