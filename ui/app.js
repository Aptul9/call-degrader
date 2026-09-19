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
let activeAudioPreset = null;

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
      // The reply carries which presets the values now amount to, so the
      // highlight is read off the settings rather than guessed from the last
      // thing clicked. A slider dragged back onto a preset lights it again.
      const reply = await send('/api/settings', payload);
      settings = reply.settings;
      markPreset(reply.preset);
      markAudioPreset(reply.audio_preset);
      if (payload.link) markInert();
      // The line drives the audio chain as well, so either section changes
      // what the far end hears.
      if (payload.link || payload.audio) abRefreshSoon();
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

// The line fold is shown under both tabs, so its rows go to every matching
// host rather than to one id. Each host gets its own control objects; sharing
// one node between two panels would move it instead of copying it.
function buildGroup(section) {
  const hosts = [
    ...document.querySelectorAll(`#group-${section}, [data-group="${section}"]`),
  ];
  for (const host of hosts) {
    host.innerHTML = '';
    for (const [field, label, min, max, step] of FIELDS[section] || []) {
      host.appendChild(slider(section, field, label, min, max, step));
    }
    for (const [sec, field, label] of FOLD_TOGGLES[section] || []) {
      host.appendChild(checkbox(sec, field, label));
    }
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
        const reply = await send(`/api/preset/${name}`);
        settings = reply.settings;
        markPreset(reply.preset);
        markAudioPreset(reply.audio_preset);
        render();
        // A line preset is an audio change too, so the call side follows it.
        abRefreshSoon();
      } catch (err) {
        showError(err.message);
      }
    });
    host.appendChild(btn);
  }
}

function buildAudioPresets(names) {
  const host = $('audio-presets');
  host.innerHTML = '';
  for (const name of names) {
    const btn = document.createElement('button');
    btn.textContent = name;
    btn.dataset.audioPreset = name;
    btn.addEventListener('click', async () => {
      try {
        const reply = await send(`/api/audio-preset/${encodeURIComponent(name)}`);
        settings = reply.settings;
        markPreset(reply.preset);
        markAudioPreset(reply.audio_preset);
        render();
        abRefreshSoon();
      } catch (err) {
        showError(err.message);
      }
    });
    host.appendChild(btn);
  }
}

function markAudioPreset(name) {
  activeAudioPreset = name;
  for (const btn of document.querySelectorAll('#audio-presets button')) {
    btn.classList.toggle('on', btn.dataset.audioPreset === name);
  }
}

// Every effect weight scales a reaction to a falling line. With the line off
// they all multiply zero, so the sliders move and nothing happens. That is
// exactly the trap this banner exists to close.
function markInert() {
  const off = !settings.link.enabled;
  for (const note of document.querySelectorAll('[data-needs-link]')) note.hidden = !off;
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
  markAudioPreset(activeAudioPreset);
  markInert();
}

// -- folds remember whether they were open -------------------------------

// Tabs. Which one is showing is remembered; the folds are not, because a fold
// left open came back open on the next load and looked like it had never
// collapsed in the first place.
function wireTabs() {
  const show = (name) => {
    for (const tab of document.querySelectorAll('.tab')) {
      tab.classList.toggle('on', tab.dataset.tab === name);
    }
    for (const panel of document.querySelectorAll('.panel')) {
      panel.hidden = panel.dataset.panel !== name;
    }
    try {
      localStorage.setItem('tab', name);
    } catch (err) { /* private window, not worth reporting */ }
  };

  for (const tab of document.querySelectorAll('.tab')) {
    tab.addEventListener('click', () => show(tab.dataset.tab));
  }

  let start = 'video';
  try {
    const saved = localStorage.getItem('tab');
    if (saved && document.querySelector(`.panel[data-panel="${saved}"]`)) start = saved;
  } catch (err) { /* private window */ }
  show(start);
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

// -- audio A/B ------------------------------------------------------------

// Both sides are captured inside the audio callback, so "before" is the raw
// microphone and "after" is the exact block written to the cable. They will not
// be sample-aligned when the line has latency or desync set: that delay is part
// of what the far end gets, so it is left in rather than corrected for.
// Hold to record, the same gesture as the video pedal. A fixed countdown meant
// guessing when to talk; holding puts the start and the end where the speaking
// is.
function wireAudioTest() {
  const btn = $('btn-audio-test');
  const note = $('audio-test-note');
  const players = $('audio-test-players');
  let held = false;
  let ticker = null;

  const down = async (event) => {
    event.preventDefault();
    if (held) return;
    held = true;
    btn.classList.add('held');
    players.hidden = true;
    note.classList.remove('bad');

    try {
      const started = await send('/api/audio-test/start');
      if (started.ok === false) throw new Error(started.error);
      const warn = started.link_on ? '' : '  (the line is off, both sides will match)';
      ticker = setInterval(async () => {
        const state = await (await fetch('/api/audio-test/status')).json();
        note.textContent = `recording ${state.seconds.toFixed(1)}s, `
          + `mic peak ${state.peak_in.toFixed(3)}${warn}`;
      }, 200);
    } catch (err) {
      held = false;
      btn.classList.remove('held');
      note.textContent = err.message;
      note.classList.add('bad');
    }
  };

  const up = async () => {
    if (!held) return;
    held = false;
    btn.classList.remove('held');
    clearInterval(ticker);

    try {
      const done = await send('/api/audio-test/stop');
      if (!done.captured) {
        note.textContent = 'nothing captured, hold the button a little longer';
        note.classList.add('bad');
        return;
      }

      players.hidden = false;
      await abLoad({ both: true });

      // Two recordings of near-silence sound identical however hard the chain
      // worked on one of them, so a quiet microphone gets said out loud rather
      // than left to look like a broken effect.
      const quiet = done.peak_in < 0.02;
      note.textContent = `${done.seconds.toFixed(1)}s captured, mic peak `
        + `${done.peak_in.toFixed(3)}, out peak ${done.peak_out.toFixed(3)}`
        + (quiet ? ' - the microphone barely heard anything, so both will sound empty' : '');
      note.classList.toggle('bad', quiet);
    } catch (err) {
      note.textContent = err.message;
      note.classList.add('bad');
    }
  };

  btn.addEventListener('pointerdown', down);
  window.addEventListener('pointerup', up);
  window.addEventListener('pointercancel', up);
}

// -- the A/B player -------------------------------------------------------
//
// Two takes, one transport. Both buffers play at once through their own gain,
// and the switch crossfades between them in 8 ms rather than restarting, so
// flipping sides lands on the same syllable instead of the top of the take.
// Hearing the same word twice, a second apart, is not the same test.
//
// The processed side is rendered on the server from the stored raw take, so
// every settings change only needs it fetched again. Nothing is re-recorded.

const AB = {
  ctx: null,
  master: null,
  buffers: { before: null, after: null },
  gains: { before: null, after: null },
  sources: null,
  side: 'after',
  playing: false,
  startedAt: 0,   // ctx.currentTime that position 0 of this run corresponds to
  offset: 0,      // where the playhead sits while paused
  loop: false,
  raf: null,
  pending: null,  // debounce handle for a re-render
};

function abDuration() {
  const { before, after } = AB.buffers;
  if (!before && !after) return 0;
  if (!before || !after) return (before || after).duration;
  return Math.min(before.duration, after.duration);
}

function abPosition() {
  if (!AB.playing) return AB.offset;
  const span = abDuration();
  const at = AB.ctx.currentTime - AB.startedAt;
  if (!span) return 0;
  return AB.loop ? at % span : Math.min(at, span);
}

async function abFetch(side) {
  // The query string is what makes the browser refetch rather than hand back
  // the previous render out of its cache.
  const res = await fetch(`/api/audio-test/${side}.wav?t=${Date.now()}`);
  if (!res.ok) throw new Error(`${side}.wav: ${res.status}`);
  return AB.ctx.decodeAudioData(await res.arrayBuffer());
}

async function abLoad({ both = false } = {}) {
  if (!AB.ctx) {
    AB.ctx = new (window.AudioContext || window.webkitAudioContext)();
    AB.master = AB.ctx.createGain();
    AB.master.connect(AB.ctx.destination);
  }

  const wasPlaying = AB.playing;
  const at = abPosition();

  // Duck before swapping a buffer out from under a running source. A hard cut
  // on the side you are listening to clicks, and a click reads as broken
  // software rather than as the setting you just moved.
  if (wasPlaying) {
    AB.master.gain.cancelScheduledValues(AB.ctx.currentTime);
    AB.master.gain.setValueAtTime(AB.master.gain.value, AB.ctx.currentTime);
    AB.master.gain.linearRampToValueAtTime(0.0001, AB.ctx.currentTime + 0.012);
  }

  const sides = both ? ['before', 'after'] : ['after'];
  const loaded = await Promise.all(sides.map(abFetch));
  sides.forEach((side, i) => { AB.buffers[side] = loaded[i]; });

  abDrawWave();
  if (wasPlaying) abStart(at);
  else abPaint();
}

function abStart(at) {
  abStopSources();
  const span = abDuration();
  if (!span) return;

  // A context built outside a click starts suspended, and everything below
  // then runs correctly against a clock that is not moving.
  if (AB.ctx.state === 'suspended') AB.ctx.resume();

  const start = AB.loop ? at % span : Math.min(at, span);
  AB.sources = {};
  for (const side of ['before', 'after']) {
    const buffer = AB.buffers[side];
    if (!buffer) continue;
    const src = AB.ctx.createBufferSource();
    src.buffer = buffer;
    // Both sides loop over the shared span, so a length difference between
    // the raw take and the render can never let the two drift apart.
    src.loop = AB.loop;
    src.loopStart = 0;
    src.loopEnd = span;

    const gain = AB.ctx.createGain();
    gain.gain.value = side === AB.side ? 1 : 0;
    src.connect(gain).connect(AB.master);
    src.start(0, start);

    AB.sources[side] = src;
    AB.gains[side] = gain;
  }

  if (!AB.loop) {
    const ender = AB.sources.after || AB.sources.before;
    if (ender) ender.onended = () => { if (AB.playing) abPause(span); };
  }

  AB.startedAt = AB.ctx.currentTime - start;
  AB.playing = true;
  AB.master.gain.cancelScheduledValues(AB.ctx.currentTime);
  AB.master.gain.setValueAtTime(Math.max(0.0001, AB.master.gain.value), AB.ctx.currentTime);
  AB.master.gain.linearRampToValueAtTime(1, AB.ctx.currentTime + 0.012);
  abPaint();
  if (!AB.raf) AB.raf = requestAnimationFrame(abTick);
}

function abStopSources() {
  if (!AB.sources) return;
  for (const src of Object.values(AB.sources)) {
    src.onended = null;
    try { src.stop(); } catch { /* already finished */ }
  }
  AB.sources = null;
}

function abPause(at) {
  AB.offset = at === undefined ? abPosition() : at;
  abStopSources();
  AB.playing = false;
  if (AB.raf) { cancelAnimationFrame(AB.raf); AB.raf = null; }
  abPaint();
}

function abToggle() {
  if (AB.playing) abPause();
  else abStart(AB.offset);
}

function abSetSide(side) {
  AB.side = side;
  if (AB.playing && AB.sources) {
    const now = AB.ctx.currentTime;
    for (const key of ['before', 'after']) {
      const gain = AB.gains[key];
      if (!gain) continue;
      gain.gain.cancelScheduledValues(now);
      gain.gain.setValueAtTime(gain.gain.value, now);
      gain.gain.linearRampToValueAtTime(key === side ? 1 : 0, now + 0.008);
    }
  }
  for (const btn of document.querySelectorAll('#ab-switch button')) {
    btn.classList.toggle('on', btn.dataset.side === side);
  }
  abDrawWave();
}

function abTick() {
  AB.raf = AB.playing ? requestAnimationFrame(abTick) : null;
  abPaint();
}

// -- drawing --------------------------------------------------------------
//
// Peak envelope, one min/max pair per pixel column. The microphone sits behind
// as a dim silhouette and the call side is drawn over it, so a dropout reads
// as the accent colour simply not being there.

function abEnvelope(buffer, columns) {
  const data = buffer.getChannelData(0);
  const per = data.length / columns;
  const out = new Float32Array(columns);
  for (let x = 0; x < columns; x++) {
    const from = Math.floor(x * per);
    const to = Math.min(data.length, Math.floor((x + 1) * per));
    let peak = 0;
    for (let i = from; i < to; i++) {
      const v = Math.abs(data[i]);
      if (v > peak) peak = v;
    }
    out[x] = peak;
  }
  return out;
}

function abDrawWave() {
  const canvas = $('ab-wave');
  const box = canvas.getBoundingClientRect();
  if (!box.width) return;

  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(box.width);
  const h = Math.round(box.height);
  canvas.width = w * dpr;
  canvas.height = h * dpr;

  const g = canvas.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);

  const mid = h / 2;
  const style = getComputedStyle(document.documentElement);
  const back = style.getPropertyValue('--muted').trim() || '#8d95a8';
  const front = style.getPropertyValue('--accent').trim() || '#6ea8ff';

  g.strokeStyle = style.getPropertyValue('--line').trim() || '#313747';
  g.beginPath();
  g.moveTo(0, mid);
  g.lineTo(w, mid);
  g.stroke();

  const order = AB.side === 'before'
    ? [['after', back, 0.25], ['before', front, 1]]
    : [['before', back, 0.35], ['after', front, 1]];

  for (const [side, colour, alpha] of order) {
    const buffer = AB.buffers[side];
    if (!buffer) continue;
    const env = abEnvelope(buffer, w);
    g.globalAlpha = alpha;
    g.fillStyle = colour;
    g.beginPath();
    for (let x = 0; x < w; x++) g.lineTo(x, mid - env[x] * (mid - 2));
    for (let x = w - 1; x >= 0; x--) g.lineTo(x, mid + env[x] * (mid - 2));
    g.closePath();
    g.fill();
  }
  g.globalAlpha = 1;

  AB.canvasWidth = w;
  AB.canvasHeight = h;
  abPaint();
}

// The playhead moves every frame; redrawing the envelope every frame would
// burn a core for nothing. It is kept on its own overlay element instead.
function abPaint() {
  const span = abDuration();
  const at = abPosition();
  const head = $('ab-head');
  if (head) head.style.left = span ? `${(at / span) * 100}%` : '0%';

  const clock = $('ab-clock');
  if (clock) clock.textContent = `${abClock(at)} / ${abClock(span)}`;

  const play = $('ab-play');
  if (play) {
    play.classList.toggle('playing', AB.playing);
    play.setAttribute('aria-label', AB.playing ? 'pause' : 'play');
  }
}

function abClock(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

// -- wiring ---------------------------------------------------------------

function wireAbPlayer() {
  const canvas = $('ab-wave');

  // The playhead is a DOM element over the canvas rather than a redraw, so
  // moving it costs a style change instead of a full envelope pass.
  const head = document.createElement('i');
  head.id = 'ab-head';
  head.className = 'ab-head';
  canvas.parentNode.insertBefore(head, canvas.nextSibling);

  $('ab-play').addEventListener('click', (event) => {
    abToggle();
    event.currentTarget.blur();
  });

  $('ab-switch').addEventListener('click', (event) => {
    const btn = event.target.closest('button[data-side]');
    if (!btn) return;
    abSetSide(btn.dataset.side);
    btn.blur();
  });

  $('ab-loop').addEventListener('change', (event) => {
    AB.loop = event.target.checked;
    if (AB.playing) abStart(abPosition());
  });

  // Scrubbing restarts two buffer sources, so doing it per pointermove would
  // rebuild the graph sixty times a second and stutter. The drag moves the
  // playhead only; playback picks up again where the pointer is let go.
  let dragging = false;
  let resume = false;

  const at = (event) => {
    const span = abDuration();
    const box = canvas.getBoundingClientRect();
    const x = (event.clientX - box.left) / box.width;
    return Math.max(0, Math.min(span, x * span));
  };

  canvas.addEventListener('pointerdown', (event) => {
    if (!abDuration()) return;
    dragging = true;
    resume = AB.playing;
    canvas.setPointerCapture(event.pointerId);
    canvas.classList.add('dragging');
    if (AB.playing) abPause(at(event));
    else { AB.offset = at(event); abPaint(); }
  });

  canvas.addEventListener('pointermove', (event) => {
    if (!dragging) return;
    AB.offset = at(event);
    abPaint();
  });

  const drop = (event) => {
    if (!dragging) return;
    dragging = false;
    canvas.classList.remove('dragging');
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
    if (resume) abStart(AB.offset);
  };
  canvas.addEventListener('pointerup', drop);
  canvas.addEventListener('pointercancel', drop);

  window.addEventListener('keydown', (event) => {
    if ($('audio-test-players').hidden) return;
    if (document.querySelector('.panel[data-panel="audio"]').hidden) return;
    const tag = event.target.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    if (event.code === 'Space') { event.preventDefault(); abToggle(); }
    else if (event.key === 'a' || event.key === 'A') {
      abSetSide(AB.side === 'after' ? 'before' : 'after');
    }
  });

  window.addEventListener('resize', () => abDrawWave());
}

// Anything that changes what the far end hears re-renders the call side from
// the take already on the server. The button that used to do this by hand is
// gone: a comparison you have to remember to ask for is a comparison nobody
// makes.
// The take lives on the server, not in the page. A reload forgets the player
// and nothing else: the recording it was showing is still held, so it comes
// back on its own rather than making someone hold the button again to see
// where they had got to.
async function abRestore() {
  try {
    const take = await (await fetch('/api/audio-test/status')).json();
    if (!take.ready) return;
    $('audio-test-players').hidden = false;
    await abLoad({ both: true });
    $('audio-test-note').textContent =
      `${take.seconds.toFixed(1)}s still held from before, mic peak ${take.peak_in.toFixed(3)}`;
  } catch (err) {
    // Nothing recorded yet, or the audio chain is down. Either way the panel
    // is correct as it stands, so this stays quiet.
  }
}

function abRefreshSoon() {
  if ($('audio-test-players').hidden) return;
  clearTimeout(AB.pending);
  AB.pending = setTimeout(async () => {
    try {
      await abLoad();
      const note = $('audio-test-note');
      note.classList.remove('bad');
      note.textContent = 'call side rebuilt from the same take with the settings as they are now';
    } catch (err) {
      showError(err.message);
    }
  }, 180);
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
  buildAudioPresets(state.audio_presets || []);
  markPreset(state.preset);
  markAudioPreset(state.audio_preset);
  render();
  wireTabs();
  wirePedal();
  wireAudioTest();
  wireAbPlayer();
  wireStatus();
  abRestore();
  $('hotkey-hint').textContent =
    `Hold ${settings.pedal.record_key} to record, ${settings.pedal.live_key} to go live. `
    + 'The hotkeys work while another window has focus.';
})();
