'use strict';

const $ = (id) => document.getElementById(id);
const audio = new Audio();
audio.crossOrigin = 'anonymous';

let playingId = null;
let takes = [];
let expanded = new Set();
let pollTimer = null;
let activeFilter = 'all'; // 'all' | 'kept'
let searchQuery = '';

// Web Audio API Visualizer setup
let audioCtx = null;
let analyser = null;
let audioSource = null;
let visualizerAnimFrame = null;

function initAudioContext() {
  if (audioCtx) return;
  try {
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return;
    audioCtx = new AudioContextClass();
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 64;
    audioSource = audioCtx.createMediaElementSource(audio);
    audioSource.connect(analyser);
    analyser.connect(audioCtx.destination);
  } catch (err) {
    console.warn('Web Audio API initialized in fallback mode:', err);
  }
}

// ------------------------------------------------------------------ API utils

async function api(path, options) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

const clock = (s) => {
  if (!isFinite(s) || s < 0) s = 0;
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
};

const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

function notice(message) {
  $('notice').innerHTML = message ? `<div class="notice">${esc(message)}</div>` : '';
}

// ----------------------------------------------------------------- Status

async function refreshStatus() {
  try {
    const s = await api('/api/status');
    const gen = s.generator || {};
    const ok = gen.ok && gen.endpoint_present !== false;
    $('lamp-gen').className = 'lamp ' + (s.current ? 'work' : ok ? 'on' : 'bad');
    $('gen-name').textContent = s.backend;
    $('lamp-writer').className = 'lamp ' + (s.writer.available ? 'on' : '');
    $('lamp-stems').className = 'lamp ' + (s.stems.available ? 'on' : '');
    $('lamp-midi').className = 'lamp ' + (s.midi && s.midi.available ? 'on' : '');
    $('queue').textContent = `queue ${s.queue_depth}`;
    $('loudness').textContent = `${s.target_lufs.toFixed(1)} LUFS`;

    if (!gen.ok) {
      notice(`Generator unreachable: ${gen.detail || 'no response'}. ` +
             `Start ACE-Step (./start_api_server_macos.sh), or set TAKEROOM_BACKEND=mock to test the pipeline.`);
    } else if (gen.endpoint_present === false) {
      notice(`ACE-Step is up but has no ${gen.endpoint} endpoint. Run: python -m takeroom.discover`);
    } else {
      notice('');
    }
  } catch (err) {
    $('lamp-gen').className = 'lamp bad';
    notice(`Takeroom server not responding: ${err.message}`);
  }
}

// ----------------------------------------------------------------- Takes Engine

async function refreshTakes() {
  try {
    const data = await api('/api/takes');
    takes = data.takes;
    render();
    const busy = takes.some((t) => t.status === 'running' || t.status === 'queued');
    schedule(busy ? 1500 : 8000);
  } catch (err) {
    schedule(5000);
  }
}

function schedule(ms) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(refreshTakes, ms);
}

function stateLabel(t) {
  if (t.status === 'ready') return ['on', 'ready'];
  if (t.status === 'failed') return ['bad', 'failed'];
  if (t.status === 'running') return ['work', t.stage || 'working'];
  return ['work', 'queued'];
}

function getFilteredTakes() {
  let list = takes;
  if (activeFilter === 'kept') {
    list = list.filter((t) => t.starred);
  }
  if (searchQuery.trim()) {
    const q = searchQuery.toLowerCase().trim();
    list = list.filter((t) =>
      (t.title && t.title.toLowerCase().includes(q)) ||
      (t.brief && t.brief.toLowerCase().includes(q)) ||
      (t.style_prompt && t.style_prompt.toLowerCase().includes(q)) ||
      (t.seed && String(t.seed).includes(q))
    );
  }
  return list;
}

function render() {
  const host = $('takes');
  const filtered = getFilteredTakes();
  $('take-count-badge').textContent = `${takes.length} take${takes.length === 1 ? '' : 's'}`;

  if (!filtered.length) {
    if (takes.length > 0) {
      host.innerHTML = `
        <div class="empty">
          <div class="empty-icon">🔍</div>
          <h3>No matching takes</h3>
          <p>No takes matched your filter query "${esc(searchQuery)}". Clear your search to view all session takes.</p>
        </div>`;
    } else {
      host.innerHTML = `
        <div class="empty">
          <div class="empty-icon">🎵</div>
          <h3>No session takes yet</h3>
          <p>Write a brief and hit <strong>Generate Takes</strong>. Leave style prompt and lyrics blank and the local model will draft them first.</p>
        </div>`;
    }
    return;
  }

  const total = takes.length;
  host.innerHTML = filtered.map((t) => {
    const originalIndex = total - takes.findIndex((item) => item.id === t.id);
    const [lamp, label] = stateLabel(t);
    const open = expanded.has(t.id);
    const playing = playingId === t.id;

    const meta = [
      `<span class="state"><i class="lamp ${lamp}"></i>${esc(label)}</span>`,
      t.duration ? clock(t.duration) : '',
      t.seed != null ? `seed ${t.seed}` : '',
      t.output_lufs != null ? `${t.output_lufs.toFixed(1)} LUFS` : '',
      t.peak_dbfs != null ? `${t.peak_dbfs.toFixed(1)} dBFS` : '',
      t.render_seconds ? `${t.render_seconds}s` : '',
    ].filter(Boolean).join('');

    return `
    <article class="take ${playing ? 'playing' : ''} ${t.status === 'failed' ? 'failed' : ''}" data-id="${t.id}">
      <div class="index">#${originalIndex}</div>
      <div>
        <h2 class="title">${esc(t.title || 'Untitled Take')}</h2>
        <div class="meta">${meta}</div>

        ${t.has_master ? `
        <div class="transport">
          <button class="play" data-act="play" aria-label="${playing ? 'Pause' : 'Play'}">${playing ? '❚❚' : '▶'}</button>
          <div class="scrub" data-act="seek" tabindex="0" role="slider" aria-label="Position">
            <div class="fill" ${playing ? '' : 'style="width:0"'}></div>
            <div class="scrub-tooltip">0:00</div>
          </div>
          <span class="clock">${playing ? '' : clock(t.duration || 0)}</span>
        </div>` : ''}

        ${t.error ? `<div class="error">${esc(t.error)}</div>` : ''}

        <div class="actions">
          <button class="ghost" data-act="detail">${open ? 'Hide parameters' : 'Details'}</button>
          ${t.has_master ? `<button class="ghost" data-act="reroll">⚡ Re-roll</button>` : ''}
          ${t.has_master ? `<a class="ghost" style="text-decoration:none" href="/api/takes/${t.id}/audio?kind=master" download>WAV</a>` : ''}
          ${t.has_mp3 ? `<a class="ghost" style="text-decoration:none" href="/api/takes/${t.id}/audio?kind=mp3" download>MP3</a>` : ''}
          ${t.has_master ? `<button class="ghost" data-act="stems">${t.has_stems ? 'Stems ✓' : 'Stems'}</button>` : ''}
          ${t.has_stems ? `<button class="ghost" data-act="midi">${t.has_midi ? 'MIDI ✓' : '🎹 MIDI'}</button>` : ''}
          <button class="ghost ${t.starred ? 'starred' : ''}" data-act="star">${t.starred ? '⭐ Kept' : 'Keep'}</button>
          <button class="ghost danger" data-act="delete">Delete</button>
        </div>

        <div class="stems-container" data-stems="${t.id}"></div>
        <div class="midi-container" data-midi="${t.id}"></div>

        ${open ? `
        <dl class="detail">
          <dt>Style Prompt</dt><dd class="mono">${esc(t.style_prompt) || '—'}</dd>
          <dt>Lyrics</dt><dd class="mono">${esc(t.lyrics) || '—'}</dd>
          <dt>Parameters</dt><dd class="mono">${esc(JSON.stringify(t.params, null, 2))}</dd>
        </dl>` : ''}
      </div>
    </article>`;
  }).join('');
}

// -------------------------------------------------------------- Playback & Visualizer

function playTake(id) {
  initAudioContext();
  if (audioCtx && audioCtx.state === 'suspended') {
    audioCtx.resume();
  }

  if (playingId === id && !audio.paused) {
    audio.pause();
    return;
  }
  if (playingId !== id) {
    playingId = id;
    audio.src = `/api/takes/${id}/audio?kind=master`;
  }
  audio.play()
    .then(() => startVisualizerAnimation())
    .catch((err) => notice(`Playback failed: ${err.message}`));
  render();
}

audio.addEventListener('timeupdate', () => {
  const el = document.querySelector(`.take[data-id="${playingId}"]`);
  if (!el) return;
  const pct = audio.duration ? (audio.currentTime / audio.duration) * 100 : 0;
  const fill = el.querySelector('.fill');
  if (fill) fill.style.width = `${pct}%`;
  const clockEl = el.querySelector('.clock');
  if (clockEl) {
    clockEl.textContent = `${clock(audio.currentTime)} / ${clock(audio.duration)}`;
  }
});

audio.addEventListener('ended', () => {
  playingId = null;
  render();
});
audio.addEventListener('pause', () => { render(); });
audio.addEventListener('play', () => { render(); });

// Real-Time Canvas Spectrum Visualizer Render Loop
function drawSpectrum() {
  const canvas = $('visualizer-canvas');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const width = canvas.width;
  const height = canvas.height;

  ctx.clearRect(0, 0, width, height);

  if (!analyser || audio.paused || !playingId) {
    // Ambient Idle VU Bar
    ctx.fillStyle = '#323C35';
    const numBars = 16;
    const barWidth = 8;
    const gap = 4;
    for (let i = 0; i < numBars; i++) {
      ctx.fillRect(i * (barWidth + gap) + 10, height - 4, barWidth, 3);
    }
    return;
  }

  const bufferLength = analyser.frequencyBinCount;
  const dataArray = new Uint8Array(bufferLength);
  analyser.getByteFrequencyData(dataArray);

  const numBars = 18;
  const barWidth = 7;
  const gap = 4;

  for (let i = 0; i < numBars; i++) {
    const val = dataArray[i * 1] || 0;
    const barHeight = Math.max(3, (val / 255) * height);
    const x = i * (barWidth + gap) + 8;
    const y = height - barHeight;

    // Glowing Spectrum Gradient (Teal to Amber peak)
    const grad = ctx.createLinearGradient(0, height, 0, 0);
    grad.addColorStop(0, '#38A382');
    grad.addColorStop(0.7, '#4ECCA3');
    grad.addColorStop(1, '#F5A623');

    ctx.fillStyle = grad;
    ctx.fillRect(x, y, barWidth, barHeight);
  }

  visualizerAnimFrame = requestAnimationFrame(drawSpectrum);
}

function startVisualizerAnimation() {
  if (visualizerAnimFrame) cancelAnimationFrame(visualizerAnimFrame);
  drawSpectrum();
}

// ---------------------------------------------------------------- Take Actions

$('takes').addEventListener('mousemove', (e) => {
  const scrub = e.target.closest('.scrub');
  if (!scrub) return;
  const tooltip = scrub.querySelector('.scrub-tooltip');
  if (!tooltip) return;

  const rect = scrub.getBoundingClientRect();
  const relX = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
  const pct = relX / rect.width;

  const article = scrub.closest('.take');
  const takeId = article ? article.dataset.id : null;
  const takeObj = takes.find((t) => t.id === takeId);
  const dur = (playingId === takeId && audio.duration) ? audio.duration : (takeObj ? takeObj.duration || 120 : 120);
  
  tooltip.textContent = clock(pct * dur);
  tooltip.style.left = `${relX}px`;
});

$('takes').addEventListener('click', async (event) => {
  const article = event.target.closest('.take');
  const button = event.target.closest('[data-act]');
  if (!article || !button) return;
  const id = article.dataset.id;

  switch (button.dataset.act) {
    case 'play':
      playTake(id);
      break;

    case 'seek': {
      if (playingId !== id || !audio.duration) break;
      const box = button.getBoundingClientRect();
      audio.currentTime = ((event.clientX - box.left) / box.width) * audio.duration;
      break;
    }

    case 'detail':
      expanded.has(id) ? expanded.delete(id) : expanded.add(id);
      render();
      break;

    case 'reroll':
      button.disabled = true;
      try {
        await api(`/api/takes/${id}/reroll`, { method: 'POST', body: '{}' });
        refreshTakes();
      } catch (err) {
        notice(err.message);
        button.disabled = false;
      }
      break;

    case 'star': {
      const take = takes.find((t) => t.id === id);
      await api(`/api/takes/${id}/star`, {
        method: 'POST',
        body: JSON.stringify({ starred: !take.starred }),
      });
      refreshTakes();
      break;
    }

    case 'stems':
      button.disabled = true;
      button.textContent = 'Separating…';
      try {
        const res = await api(`/api/takes/${id}/stems`, {
          method: 'POST',
          body: JSON.stringify({ device: 'mps' }),
        });
        showStems(id, res.stems);
        button.textContent = 'Stems ✓';
      } catch (err) {
        notice(err.message);
        button.textContent = 'Stems';
      } finally {
        button.disabled = false;
      }
      break;

    case 'delete':
      if (!confirm('Delete this take and its audio?')) break;
      if (playingId === id) {
        audio.pause();
        playingId = null;
      }
      await api(`/api/takes/${id}`, { method: 'DELETE' });
      refreshTakes();
      break;

    case 'midi':
      button.disabled = true;
      button.textContent = 'Transcribing…';
      try {
        const res = await api(`/api/takes/${id}/midi`, {
          method: 'POST',
          body: '{}',
        });
        showMidi(id, res.midi);
        button.textContent = 'MIDI ✓';
      } catch (err) {
        notice(err.message);
        button.textContent = '🎹 MIDI';
      } finally {
        button.disabled = false;
      }
      break;
  }
});

function showStems(id, names) {
  const host = document.querySelector(`[data-stems="${id}"]`);
  if (!host) return;

  if (!names || !names.length) {
    host.innerHTML = '';
    return;
  }

  host.innerHTML = `
    <div class="stem-rack">
      <div class="stem-rack-header">Separated Stems</div>
      ${names.map((n) => {
        const stemName = n.replace('.wav', '').replace(`${id}-`, '');
        const stemUrl = `/api/takes/${id}/stems/${encodeURIComponent(n)}`;
        return `
        <div class="stem-track">
          <span class="stem-name">${esc(stemName)}</span>
          <audio controls controlsList="nodownload" class="stem-audio-player" src="${stemUrl}"></audio>
          <a class="ghost" style="text-decoration:none; padding: .2rem .5rem;" href="${stemUrl}" download>WAV</a>
        </div>`;
      }).join('')}
    </div>`;
}

function showMidi(id, names) {
  let host = document.querySelector(`[data-midi="${id}"]`);
  if (!host) {
    // Append a midi container after the stems container
    const stemsHost = document.querySelector(`[data-stems="${id}"]`);
    if (stemsHost) {
      const div = document.createElement('div');
      div.setAttribute('data-midi', id);
      stemsHost.after(div);
      host = div;
    } else {
      return;
    }
  }

  if (!names || !names.length) {
    host.innerHTML = '';
    return;
  }

  host.innerHTML = `
    <div class="stem-rack midi-rack">
      <div class="stem-rack-header">🎹 MIDI Transcriptions</div>
      ${names.map((n) => {
        const midiName = n.replace('.mid', '');
        const midiUrl = `/api/takes/${id}/midi/${encodeURIComponent(n)}`;
        return `
        <div class="stem-track midi-track">
          <span class="stem-name">${esc(midiName)}</span>
          <a class="ghost midi-download" href="${midiUrl}" download>⬇ .mid</a>
        </div>`;
      }).join('')}
    </div>`;
}

// ---------------------------------------------------------------- Tag Chips Builder

document.querySelectorAll('.tag-chip').forEach((chip) => {
  chip.addEventListener('click', () => {
    const tag = chip.dataset.tag;
    const styleInput = $('style');
    let current = styleInput.value.trim();
    if (!current) {
      styleInput.value = tag;
    } else if (!current.toLowerCase().includes(tag.toLowerCase())) {
      styleInput.value = `${current}, ${tag}`;
    }
  });
});

// ---------------------------------------------------------------- Filter & Search

$('search-input').addEventListener('input', (e) => {
  searchQuery = e.target.value;
  render();
});

$('filter-all').addEventListener('click', () => {
  activeFilter = 'all';
  $('filter-all').classList.add('active');
  $('filter-kept').classList.remove('active');
  render();
});

$('filter-kept').addEventListener('click', () => {
  activeFilter = 'kept';
  $('filter-kept').classList.add('active');
  $('filter-all').classList.remove('active');
  render();
});

// ---------------------------------------------------------------- Generator Inputs

$('duration').addEventListener('input', (e) => {
  $('duration-value').textContent = clock(Number(e.target.value));
});

$('generate').addEventListener('click', async () => {
  const button = $('generate');
  const seedRaw = $('seed').value.trim();
  const body = {
    brief: $('brief').value,
    style_prompt: $('style').value,
    lyrics: $('lyrics').value,
    instrumental: $('instrumental').checked,
    duration: Number($('duration').value),
    steps: Number($('steps').value),
    guidance: Number($('guidance').value),
    count: Number($('takes-count').value),
    seed: seedRaw === '' ? null : Number(seedRaw),
  };
  if (!body.brief.trim() && !body.style_prompt.trim()) {
    notice('Write a brief or a style prompt first.');
    return;
  }

  button.disabled = true;
  button.textContent = 'Queueing…';
  try {
    await api('/api/takes', { method: 'POST', body: JSON.stringify(body) });
    notice('');
    refreshTakes();
  } catch (err) {
    notice(err.message);
  } finally {
    button.disabled = false;
    button.innerHTML = '<span class="btn-icon">⚡</span> Generate Takes';
  }
});

$('draft').addEventListener('click', async () => {
  const button = $('draft');
  button.disabled = true;
  button.textContent = 'Writing…';
  try {
    const res = await api('/api/write', {
      method: 'POST',
      body: JSON.stringify({
        brief: $('brief').value,
        instrumental: $('instrumental').checked,
      }),
    });
    $('style').value = res.style_prompt;
    if (res.lyrics) $('lyrics').value = res.lyrics;
    notice('');
  } catch (err) {
    notice(err.message);
  } finally {
    button.disabled = false;
    button.innerHTML = '<span class="btn-icon">✍️</span> Draft Words Only';
  }
});

// ---------------------------------------------------------------- DAW Keyboard Shortcuts

document.addEventListener('keydown', (e) => {
  const isInput = ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName);

  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
    e.preventDefault();
    $('generate').click();
    return;
  }

  if (e.key === 'Escape') {
    if (playingId && !audio.paused) {
      audio.pause();
    }
    notice('');
    return;
  }

  if (!isInput && e.code === 'Space') {
    e.preventDefault();
    if (playingId) {
      playTake(playingId);
    } else if (takes.length > 0 && takes[0].has_master) {
      playTake(takes[0].id);
    }
    return;
  }

  if (!isInput && (e.key === 's' || e.key === 'S')) {
    if (playingId) {
      const take = takes.find((t) => t.id === playingId);
      if (take) {
        api(`/api/takes/${playingId}/star`, {
          method: 'POST',
          body: JSON.stringify({ starred: !take.starred }),
        }).then(refreshTakes);
      }
    }
  }
});

// ---------------------------------------------------------------- Boot

refreshStatus();
refreshTakes();
setInterval(refreshStatus, 6000);
drawSpectrum();
