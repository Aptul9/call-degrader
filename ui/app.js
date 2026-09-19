// UI for call-degrader. No framework, no build step: this file is served as it
// is written.
//
// The split that matters: anything touched during a call stays on screen, and
// anything set once lives in a fold. With all forty controls on screen at once,
// the three that actually get used were lost among them.

// Always visible. Section, field, label.
const QUICK_CHECKS = [
  ['link',  'enabled',      'bad line on'],
  ['video', 'keep_colours', 'keep colours'],
  ['video', 'mirror',       'mirror camera'],
  ['pedal', 'ghost',        'ghost me under the loop'],
  ['pedal', 'mute_on_loop', 'mute mic while looping'],
  ['pedal', 'enabled',      'hotkeys on'],
];

// Folded away. field, label, min, max, step.
const FIELDS = {
  link: [
    ['quality',    'quality',                0, 100, 1],
    ['ceiling',    'best it ever gets',      0, 100, 1],
    ['floor',      'worst it ever gets',     0, 100, 1],
    ['drift',      'wander',                 0, 30,  0.5],
    ['stall_rate', 'stalls per minute',      0, 30,  0.5],
    ['stall_min',  'shortest stall (s)',     0, 5,   0.1],
    ['stall_max',  'longest stall (s)',      0, 10,  0.1],
    ['latency',    'extra delay (s)',        0, 3,   0.05],
    ['desync',     'audio behind video (s)', -1, 1,  0.02],
  ],
  video: [
    ['drop_weight',       'dropped frames',  0, 2, 0.05],
    ['blockiness_weight', 'compression',     0, 2, 0.05],
    ['resolution_weight', 'resolution loss', 0, 2, 0.05],
    ['banding_weight',    'colour banding',  0, 2, 0.05],
    ['smear_weight',      'freeze smear',    0, 2, 0.05],
    ['tearing_weight',    'tearing',         0, 2, 0.05],
  ],
  audio: [
    ['dropout_weight',  'dropouts',       0, 2, 0.05],
    ['stutter_weight',  'packet stutter', 0, 2, 0.05],
    ['bitcrush_weight', 'bitrate crush',  0, 2, 0.05],
    ['warble_weight',   'pitch warble',   0, 2, 0.05],
    ['metallic_weight', 'metallic ring',  0, 2, 0.05],
  ],
  pedal: [
    ['max_seconds', 'max recording (s)', 1, 120, 1],
    ['min_seconds', 'min recording (s)', 0.2, 5, 0.1],
    ['crossfade',   'crossfade (s)',     0, 2, 0.05],
    ['overlay',     'ghost opacity',     0, 1, 0.05],
  ],
};

const FOLD_TOGGLES = {
  devices: [['audio', 'monitor', 'monitor the processed sound on the speakers']],
};

const LOOP_STYLES = [
  ['bounce', 'bounce - plays back and forth, no join'],
  ['crossfade', 'crossfade - wraps round, dissolves the join'],
];

const $ = (id) => document.getElementById(id);
let settings = null;
let pending = null;
let activePreset = null;

// -- talking to the server ---------------------------------------------

async function send(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? '{}' : JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

// Slider moves arrive far faster than they are worth sending. Coalesce them
// into one request per animation frame, keeping the last value per path.
function patch(section, field, value) {
  settings[section][field] = value;
  pending = pending || {};
  pending[section] = Object.assign(pending[section] || {}, { [field]: value });
  if (patch.queued) return;
  patch.queued = true;
  requestAnimationFrame(async () => {
    const payload = pending;
    pending = null;
    patch.queued = false;
    try {
      settings = (await send('/api/settings', payload)).settings;
      if (payload.link) markPreset(null);  // a hand edit is no longer that preset
    } catch (err) {
      showError(err.message);
    }
  });
}

// -- controls ------------------------------------------------------------

function checkbox(section, field, label) {
  const row = document.createElement('label');
  row.className = 'toggle';
  const box = document.createElement('input');
  box.type = 'checkbox';
  box.checked = Boolean(settings[section][field]);
  box.addEventListener('change', () => patch(section, field, box.checked));
  const text = document.createElement('span');
  text.textContent = label;
  row.append(box, text);
  return row;
}

function slider(section, field, label, min, max, step) {
  const row = document.createElement('label');
  row.className = 'field';

  const name = document.createElement('span');
  name.textContent = label;

  const input = document.createElement('input');
  input.type = 'range';
  Object.assign(input, { min, max, step, value: settings[section][field] });

  const out = document.createElement('output');
  out.textContent = fmt(input.value);

  input.addEventListener('input', () => {
    out.textContent = fmt(input.value);
    patch(section, field, parseFloat(input.value));
  });

  row.append(name, input, out);
  return row;
}

function fmt(value) {
  const n = parseFloat(value);
  return Number.isInteger(n) ? String(n) : n.toFixed(2);
}

function buildQuick() {
  const host = $('quick-checks');
  host.innerHTML = '';
  for (const [section, field, label] of QUICK_CHECKS) {
    host.appendChild(checkbox(section, field, label));
  }

  const loop = $('loop-pick');
  loop.innerHTML = '';
  for (const [value, text] of LOOP_STYLES) loop.appendChild(new Option(text, value));
  loop.value = settings.pedal.loop_mode;
  loop.onchange = () => patch('pedal', 'loop_mode', loop.value);
}

function buildGroup(section) {
  const host = $(`group-${section}`);
  if (!host) return;
  host.innerHTML = '';
  for (const [field, label, min, max, step] of FIELDS[section] || []) {
    host.appendChild(slider(section, field, label, min, max, step));
  }
  for (const [sec, field, label] of FOLD_TOGGLES[section] || []) {
    host.appendChild(checkbox(sec, field, label));
  }
}

// The picker lists DirectShow devices, so the chosen index is only meaningful
// with that backend. Both are sent together; sending the index alone would pick
// a different camera, because MSMF enumerates in its own order.
async function buildCameras() {
  const pick = $('camera-pick');
  pick.innerHTML = '';
  let cameras = [];
  try {
    cameras = (await (await fetch('/api/cameras')).json()).cameras || [];
  } catch (err) {
    showError(`could not list cameras: ${err.message}`);
  }

  if (!cameras.length) {
    pick.appendChild(new Option('no camera found', ''));
    pick.disabled = true;
    return;
  }

  pick.disabled = false;
  for (const cam of cameras) pick.appendChild(new Option(cam.name, String(cam.index)));
  const current = String(settings.video.camera);
  if (cameras.some((c) => String(c.index) === current)) pick.value = current;

  pick.onchange = async () => {
    const index = parseInt(pick.value, 10);
    if (Number.isNaN(index)) return;
    try {
      // Not through patch(): the video chain restarts on this, and the
      // coalescing there would let a second change race the restart.
      settings = (await send('/api/settings',
        { video: { camera: index, backend: 'dshow' } })).settings;
    } catch (err) {
      showError(err.message);
    }
  };
}

function buildPresets(names) {
  const host = $('presets');
  host.innerHTML = '';
  for (const name of names) {
    const btn = document.createElement('button');
    btn.textContent = name.replace(/-/g, ' ');
    btn.dataset.preset = name;
    btn.addEventListener('click', async () => {
      try {
        settings = (await send(`/api/preset/${name}`)).settings;
        markPreset(name);
        render();
      } catch (err) {
        showError(err.message);
      }
    });
    host.appendChild(btn);
  }
}

function markPreset(name) {
  activePreset = name;
  for (const btn of document.querySelectorAll('#presets button')) {
    btn.classList.toggle('on', btn.dataset.preset === name);
  }
}

function render() {
  buildQuick();
  for (const section of Object.keys(FIELDS)) buildGroup(section);
  buildGroup('devices');
  buildCameras();
  markPreset(activePreset);
}

// -- folds remember whether they were open -------------------------------

function wireFolds() {
  for (const fold of document.querySelectorAll('.fold')) {
    const key = `fold:${fold.dataset.fold}`;
    try {
      if (localStorage.getItem(key) === 'open') fold.open = true;
    } catch (err) {
      // Private windows and blocked site data throw on access. The folds then
      // start closed, which is the sensible default anyway.
    }
    fold.addEventListener('toggle', () => {
      try {
        localStorage.setItem(key, fold.open ? 'open' : 'shut');
      } catch (err) { /* nothing to do, and nothing worth saying */ }
    });
  }
}

// -- pedal ---------------------------------------------------------------

function wirePedal() {
  const rec = $('btn-record');
  let held = false;

  const down = (event) => {
    event.preventDefault();
    if (held) return;
    held = true;
    rec.classList.add('held');
    send('/api/pedal/record-start').catch((e) => showError(e.message));
  };
  const up = () => {
    if (!held) return;
    held = false;
    rec.classList.remove('held');
    send('/api/pedal/record-stop').catch((e) => showError(e.message));
  };

  rec.addEventListener('pointerdown', down);
  // Release anywhere counts. Letting go outside the button would otherwise
  // leave it recording with the UI showing it as held.
  window.addEventListener('pointerup', up);
  window.addEventListener('pointercancel', up);

  $('btn-rescan').addEventListener('click', buildCameras);
  $('btn-live').addEventListener('click', () => send('/api/pedal/live'));
  $('btn-clear').addEventListener('click', () => send('/api/pedal/clear'));

  $('btn-route').addEventListener('click', async () => {
    const note = $('route-result');
    note.textContent = 'switching...';
    note.classList.remove('bad');
    try {
      const out = await send('/api/route-microphone', { match: 'CABLE Output' });
      note.textContent = out.ok ? `default mic is now ${out.device}` : out.error;
      note.classList.toggle('bad', !out.ok);
    } catch (err) {
      note.textContent = String(err.message);
      note.classList.add('bad');
    }
  });
}

// -- live status ----------------------------------------------------------

function wireStatus() {
  const socket = new WebSocket(`ws://${location.host}/ws`);
  socket.addEventListener('message', (event) => paint(JSON.parse(event.data)));
  socket.addEventListener('close', () => setTimeout(wireStatus, 1500));
}

function paint(status) {
  const state = status.video?.pedal?.state || 'live';
  setPill('pill-state', state, { live: 'ok', rec: 'bad', loop: 'warn' }[state]);

  const link = status.link || {};
  if (!link.enabled) setPill('pill-link', 'link off', '');
  else if (link.stalled) setPill('pill-link', 'STALL', 'bad');
  else setPill('pill-link', `link ${Math.round(link.quality)}`,
               link.quality > 70 ? 'ok' : link.quality > 35 ? 'warn' : 'bad');
  $('stall-flash').classList.toggle('on', Boolean(link.stalled));

  setPill('pill-fps', `${status.video?.fps ?? 0} fps`, status.video?.running ? 'ok' : '');

  const vcam = status.video?.virtual_camera;
  setPill('pill-vcam', vcam ? short(vcam) : 'no virtual camera', vcam ? 'ok' : 'warn');

  const audio = status.audio || {};
  setPill('pill-audio', audio.running ? `audio ${audio.samplerate} Hz` : 'audio off',
          audio.running ? 'ok' : 'warn');
  $('meter-in').style.width = `${Math.min(100, (audio.level_in || 0) * 320)}%`;
  $('meter-out').style.width = `${Math.min(100, (audio.level_out || 0) * 320)}%`;

  const problems = [status.video?.error, status.audio?.error, status.hotkeys?.error].filter(Boolean);
  const box = $('errors');
  box.textContent = problems.join(' | ');
  box.classList.toggle('bad', problems.length > 0);
}

function setPill(id, text, cls) {
  const el = $(id);
  el.textContent = text;
  el.className = `pill ${cls || ''}`.trim();
}

function short(text) {
  return String(text).replace(/\s*\(.*\)\s*/, '').slice(0, 26);
}

function showError(message) {
  const box = $('errors');
  box.textContent = message;
  box.classList.add('bad');
}

// -- boot -----------------------------------------------------------------

(async function boot() {
  const state = await (await fetch('/api/state')).json();
  settings = state.settings;
  buildPresets(state.presets);
  render();
  wireFolds();
  wirePedal();
  wireStatus();
  $('hotkey-hint').textContent =
    `Hold ${settings.pedal.record_key} to record, ${settings.pedal.live_key} to go live. `
    + 'The hotkeys work while another window has focus.';
})();
