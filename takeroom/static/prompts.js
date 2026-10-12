'use strict';

/* Takeroom — prompt preset picker.
 *
 * Self-attaching, like midi.js: app.js is untouched. It inserts a category /
 * preset pair of selects above the Brief field and fills the form on choose.
 * Adjust FIELD_IDS if your input ids have changed.
 */

const PRESET_FIELD_IDS = {
  brief: 'brief',
  style: 'style',
  lyrics: 'lyrics',
  duration: 'duration',
  durationValue: 'duration-value',
  instrumental: 'instrumental',
  anchor: 'brief',        // the picker is inserted above this field's .field
};

let PRESET_LIBRARY = null;

function presetEl(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text) el.textContent = text;
  return el;
}

function fieldWrapper(id) {
  const input = document.getElementById(id);
  return input ? input.closest('.field') || input.parentElement : null;
}

function applyPreset(preset) {
  const set = (id, value) => {
    const el = document.getElementById(id);
    if (el && value !== undefined && value !== null && value !== '') el.value = value;
  };

  set(PRESET_FIELD_IDS.brief, preset.name);
  set(PRESET_FIELD_IDS.style, preset.prompt);

  // Every preset is instrumental, so clear lyrics and tick the box.
  const lyrics = document.getElementById(PRESET_FIELD_IDS.lyrics);
  if (lyrics) lyrics.value = '';
  const instrumental = document.getElementById(PRESET_FIELD_IDS.instrumental);
  if (instrumental) instrumental.checked = true;

  if (preset.duration) {
    const slider = document.getElementById(PRESET_FIELD_IDS.duration);
    if (slider) {
      slider.value = preset.duration;
      slider.dispatchEvent(new Event('input', { bubbles: true }));
      const label = document.getElementById(PRESET_FIELD_IDS.durationValue);
      if (label) {
        const m = Math.floor(preset.duration / 60);
        const s = String(preset.duration % 60).padStart(2, '0');
        label.textContent = `${m}:${s}`;
      }
    }
  }

  showWorkflow(preset);
}

function showWorkflow(preset) {
  let note = document.getElementById('preset-workflow');
  if (!note) return;
  if (!preset || !preset.workflow) {
    note.textContent = '';
    note.style.display = 'none';
    return;
  }
  const bits = [];
  if (preset.bpm) bits.push(`${preset.bpm} bpm`);
  if (preset.key) bits.push(preset.key);
  bits.push(preset.workflow);
  note.textContent = bits.join(' · ');
  note.style.display = 'block';
}

function buildPicker(library) {
  const anchor = fieldWrapper(PRESET_FIELD_IDS.anchor);
  if (!anchor || document.getElementById('preset-category')) return;

  const wrap = presetEl('div', 'field');
  const label = presetEl('label', '', 'Preset');
  label.setAttribute('for', 'preset-category');
  const hint = presetEl('span', 'hint', `${library.count} for hip-hop`);
  label.appendChild(hint);
  wrap.appendChild(label);

  const row = presetEl('div', 'row');

  const categorySelect = presetEl('select');
  categorySelect.id = 'preset-category';
  categorySelect.appendChild(new Option('—', ''));
  library.categories.forEach((c) => categorySelect.appendChild(new Option(c.name, c.id)));

  const presetSelect = presetEl('select');
  presetSelect.id = 'preset-name';
  presetSelect.appendChild(new Option('—', ''));
  presetSelect.disabled = true;

  row.appendChild(categorySelect);
  row.appendChild(presetSelect);
  wrap.appendChild(row);

  const note = presetEl('div', '');
  note.id = 'preset-workflow';
  note.style.cssText = 'margin-top:.45rem;font-size:12.5px;color:var(--ink-dim);' +
                       'font-family:var(--mono);line-height:1.45;display:none';
  wrap.appendChild(note);

  anchor.parentElement.insertBefore(wrap, anchor);

  categorySelect.addEventListener('change', () => {
    const category = library.categories.find((c) => c.id === categorySelect.value);
    presetSelect.innerHTML = '';
    presetSelect.appendChild(new Option('—', ''));
    if (!category) {
      presetSelect.disabled = true;
      showWorkflow(null);
      return;
    }
    category.presets.forEach((p) => presetSelect.appendChild(new Option(p.name, p.id)));
    presetSelect.disabled = false;
    if (category.note) {
      const note2 = document.getElementById('preset-workflow');
      note2.textContent = category.note;
      note2.style.display = 'block';
    }
  });

  presetSelect.addEventListener('change', () => {
    const category = library.categories.find((c) => c.id === categorySelect.value);
    if (!category) return;
    const preset = category.presets.find((p) => p.id === presetSelect.value);
    if (preset) applyPreset(preset);
  });
}

(async function initPresets() {
  try {
    const res = await fetch('/api/prompts');
    if (!res.ok) return;
    PRESET_LIBRARY = await res.json();
    if (PRESET_LIBRARY.count) buildPicker(PRESET_LIBRARY);
  } catch (err) {
    /* the picker is a convenience; if it can't load, the form still works */
  }
})();
