'use strict';

/* Takeroom — MIDI panel.
 *
 * Self-attaching, so app.js is untouched. It watches the takes list and adds a
 * MIDI control to each take that has stems. If your markup has drifted, the two
 * selectors below are the only things to adjust.
 */

const MIDI_SELECTORS = {
  take: '.take[data-id]',       // one take row, carrying data-id
  actions: '.actions',          // the button row inside a take
};

const MIDI_STATE = new Map();   // take id -> { parts, bpm, busy }

function midiEl(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text) el.textContent = text;
  return el;
}

async function midiApi(path, options) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

function midiRack(takeId) {
  let rack = document.querySelector(`[data-midi-rack="${takeId}"]`);
  if (!rack) {
    const take = document.querySelector(`${MIDI_SELECTORS.take}[data-id="${takeId}"]`);
    if (!take) return null;
    rack = midiEl('div', 'stems');
    rack.dataset.midiRack = takeId;
    rack.style.marginTop = '0.5rem';
    (take.querySelector(MIDI_SELECTORS.actions) || take).after(rack);
  }
  return rack;
}

function renderMidi(takeId) {
  const rack = midiRack(takeId);
  const state = MIDI_STATE.get(takeId);
  if (!rack || !state) return;

  rack.innerHTML = '';

  if (state.bpm) {
    const label = midiEl('span', '', `${state.bpm.toFixed(1)} bpm`);
    label.style.cssText = 'font-family:var(--mono);font-size:12.5px;color:var(--ink-dim);align-self:center;margin-right:.3rem';
    rack.appendChild(label);
  }

  (state.parts || []).forEach((part) => {
    const link = midiEl('a', 'ghost',
      `${part.midi.replace('.mid', '')} · ${part.notes}`);
    link.href = `/api/takes/${takeId}/midi/${encodeURIComponent(part.midi)}`;
    link.download = '';
    link.style.textDecoration = 'none';
    link.title = `${part.kind} — ${part.method}` +
                 (part.lowest != null ? `, range ${part.lowest}-${part.highest}` : '') +
                 (part.quantized ? ', quantised' : '');
    rack.appendChild(link);
  });

  (state.errors || []).forEach((err) => {
    const note = midiEl('span', '', `${err.stem}: ${err.error}`);
    note.style.cssText = 'color:var(--fault);font-size:12.5px;align-self:center';
    rack.appendChild(note);
  });
}

async function convertToMidi(takeId, button, quantize) {
  if (MIDI_STATE.get(takeId)?.busy) return;
  MIDI_STATE.set(takeId, { ...(MIDI_STATE.get(takeId) || {}), busy: true });

  const original = button.textContent;
  button.disabled = true;
  button.textContent = 'Transcribing…';

  try {
    const result = await midiApi(`/api/takes/${takeId}/midi`, {
      method: 'POST',
      body: JSON.stringify({ quantize: quantize || 0, polyphonic: true }),
    });
    MIDI_STATE.set(takeId, {
      parts: result.parts, errors: result.errors, bpm: result.bpm, busy: false,
    });
    renderMidi(takeId);
    button.textContent = 'MIDI ✓';
  } catch (err) {
    MIDI_STATE.set(takeId, { busy: false });
    const rack = midiRack(takeId);
    if (rack) {
      rack.innerHTML = '';
      const note = midiEl('span', '', err.message);
      note.style.cssText = 'color:var(--fault);font-size:12.5px';
      rack.appendChild(note);
    }
    button.textContent = original;
  } finally {
    button.disabled = false;
  }
}

function attachMidiControls() {
  document.querySelectorAll(MIDI_SELECTORS.take).forEach((take) => {
    const actions = take.querySelector(MIDI_SELECTORS.actions);
    if (!actions || actions.querySelector('[data-act="midi"]')) return;

    // Only offer it where stems exist — transcription needs them.
    const hasStems = Array.from(actions.querySelectorAll('button'))
      .some((b) => /stems/i.test(b.textContent) && /✓/.test(b.textContent));
    if (!hasStems) return;

    const takeId = take.dataset.id;
    const button = midiEl('button', 'ghost', 'MIDI');
    button.dataset.act = 'midi';
    button.title = 'Transcribe stems to MIDI. Shift-click to quantise to 1/16.';
    button.addEventListener('click', (event) => {
      event.stopPropagation();
      convertToMidi(takeId, button, event.shiftKey ? 4 : 0);
    });
    actions.appendChild(button);

    if (MIDI_STATE.has(takeId)) renderMidi(takeId);
  });
}

// app.js re-renders the whole list on every poll, so watch rather than bind once.
const midiObserver = new MutationObserver(() => attachMidiControls());
const midiHost = document.getElementById('takes') || document.body;
midiObserver.observe(midiHost, { childList: true, subtree: true });
attachMidiControls();
