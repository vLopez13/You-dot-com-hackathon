/**
 * YouTube Truth Panel — side panel UI.
 *
 * Pipeline: content script → caption segments → transcript view + claim
 * detection (local) → You.com fact-check via the local backend → verdict cards.
 */

const $ = (id) => document.getElementById(id);

const DEFAULTS = {
  endpoint: 'http://127.0.0.1:8765',
  mode: 'research',
  maxClaims: 8,
  smartExtract: false,
  autoCheck: false
};

const state = {
  settings: { ...DEFAULTS },
  tabId: null,
  videoId: '',
  title: '',
  channel: '',
  transcript: null, // { segments, source, auto, ... }
  lines: [], // readable transcript lines: { start, end, text }
  lineEls: [], // one <div class="line"> per entry above
  activeLine: -1,
  claims: [], // [{ id, text, start, score }]
  claimById: new Map(),
  claimLine: new Map(), // claim id → transcript line element
  cards: new Map(), // claim id → card element
  queued: new Set(), // claim ids already sent for checking
  scores: { true: 0, false: 0, misleading: 0, unverified: 0 },
  checked: 0,
  live: false,
  generation: 0, // bumped per video, so stale replies can be dropped
  lastTime: 0,
  revealedClaims: -1, // how much of the video the live window has reached
  revealedLines: -1,
  scanning: false,
  loadingTranscript: false,
  tab: 'claims',
  autoScrolling: false,
  lastUserScroll: 0,
  abort: null
};

// ---------------------------------------------------------------- settings

async function loadSettings() {
  const stored = await chrome.storage.local.get(DEFAULTS);
  state.settings = { ...DEFAULTS, ...stored };
  // 'search' (Fast) was removed — anyone still on it lands on the REST verdict.
  if (state.settings.mode === 'search') saveSettings({ mode: 'research' });
  $('endpoint').value = state.settings.endpoint;
  $('maxClaims').value = String(state.settings.maxClaims);
  $('smartExtract').checked = !!state.settings.smartExtract;
  [...$('modeToggle').children].forEach((b) =>
    b.classList.toggle('active', b.dataset.mode === state.settings.mode)
  );
  setAutoButton();
}

function setAutoButton() {
  const on = !!state.settings.autoCheck;
  $('autoBtn').classList.toggle('on', on);
  $('autoBtn').textContent = on ? '⚡ Auto on' : '⚡ Auto';
}

function saveSettings(patch) {
  state.settings = { ...state.settings, ...patch };
  chrome.storage.local.set(patch);
}

// ---------------------------------------------------------------- backend

function api(path) {
  return state.settings.endpoint.replace(/\/+$/, '') + path;
}

async function checkBackend() {
  setBackend('busy', 'checking…');
  try {
    const res = await fetch(api('/health'), { cache: 'no-store' });
    const data = await res.json();
    if (!data.has_key) setBackend('bad', 'no API key');
    else setBackend('ok', 'connected');
    return !!data.has_key;
  } catch (_) {
    setBackend('bad', 'backend offline');
    return false;
  }
}

function setBackend(kind, text) {
  $('backendDot').className = 'dot ' + kind;
  $('backendText').textContent = text;
}

/** Remaining You.com credits, straight from the billing API. */
async function refreshBalance() {
  const pill = $('creditsPill');
  const label = $('creditsText');
  try {
    const res = await fetch(api('/api/balance'), { cache: 'no-store' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `unavailable (${res.status})`);

    const usd = data.balance_usd;
    const known = typeof usd === 'number';
    label.textContent = known ? `$${usd.toFixed(2)} left` : 'credits ?';
    pill.classList.toggle('low', known && usd > 0 && usd < 5);
    pill.classList.toggle('empty', known && usd <= 0);
    const s = data.session || {};
    pill.title =
      `You.com credits remaining\n` +
      `${s.calls || 0} billed call(s) this backend run, ${s.cached || 0} served from cache\n` +
      `Click to refresh`;
  } catch (e) {
    label.textContent = 'credits —';
    pill.classList.remove('low', 'empty');
    pill.title = `Could not read balance: ${e.message || e}`;
  }
}

let balanceTimer = null;
/** Coalesce refreshes so a burst of checks costs one balance call. */
function scheduleBalanceRefresh() {
  clearTimeout(balanceTimer);
  balanceTimer = setTimeout(refreshBalance, 4000);
}

// ---------------------------------------------------------------- tab wiring

async function findYouTubeTab() {
  const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (active && /youtube\.com/.test(active.url || '')) return active;
  const tabs = await chrome.tabs.query({ url: 'https://www.youtube.com/*' });
  return tabs.find((t) => t.active) || tabs[0] || null;
}

async function sendToTab(message) {
  if (state.tabId == null) throw new Error('No YouTube tab.');
  return chrome.tabs.sendMessage(state.tabId, message);
}

async function ensureContentScript(tabId) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: 'ensure-content-script', tabId }, (res) => {
      void chrome.runtime.lastError;
      resolve(res || { ok: false });
    });
  });
}

/** Point the panel at whatever video the browser is showing right now. */
async function syncVideo({ force = false } = {}) {
  const tab = await findYouTubeTab();
  if (!tab) {
    state.tabId = null;
    setVideoHeader('No YouTube video detected', '', 'open a youtube.com/watch page');
    setControlsEnabled(false);
    return;
  }
  state.tabId = tab.id;
  await ensureContentScript(tab.id);

  let ctx = null;
  try {
    ctx = await sendToTab({ type: 'get-context' });
  } catch (_) {
    setVideoHeader('Reload the YouTube tab', '', 'content script not attached yet');
    showNotice('This tab was open before the extension loaded, so it has no content script yet. Reload the YouTube tab, then hit Retry.', 'Retry', () => syncVideo({ force: true }));
    setControlsEnabled(false);
    return;
  }

  if (!ctx || !ctx.videoId) {
    // Left the watch page — drop the previous video's results too.
    if (state.videoId) {
      stopScan({ abort: true });
      stopLive();
      state.videoId = '';
      resetAll();
    }
    setVideoHeader('No video on this page', '', 'open a youtube.com/watch page');
    setControlsEnabled(false);
    return;
  }

  const changed = ctx.videoId !== state.videoId;
  state.videoId = ctx.videoId;
  state.title = ctx.title || 'YouTube video';
  state.channel = ctx.channel || '';

  if (changed || force) {
    stopScan({ abort: true }); // kill any work still running for the old video
    stopLive();
    resetAll();
    setVideoHeader(state.title, state.channel, 'loading captions…');
    setControlsEnabled(true);
    loadTranscript().catch(() => {});
  }
}

function setVideoHeader(title, channel, transcriptState) {
  $('videoTitle').textContent = title;
  $('videoChannel').textContent = channel || '';
  $('videoSep').textContent = channel && transcriptState ? ' ' : '';
  $('transcriptState').textContent = transcriptState || '';
}

function setControlsEnabled(on) {
  $('scanBtn').disabled = !on;
  $('liveBtn').disabled = !on;
}

// ---------------------------------------------------------------- transcript

async function loadTranscript() {
  if (state.loadingTranscript || !state.videoId) return state.transcript;
  state.loadingTranscript = true;
  setVideoHeader(state.title, state.channel, 'loading captions…');
  showNotice('Reading captions…');
  try {
    const result = await sendToTab({ type: 'get-transcript', videoId: state.videoId });
    if (!result || result.error) {
      const why = (result && result.error) || 'no captions';
      setVideoHeader(state.title, state.channel, why);
      state.transcript = null;
      const notes = (result && result.notes) || [];
      showNotice(
        `${why} This video may have captions turned off, or YouTube may have refused the caption request.`,
        'Retry',
        () => loadTranscript(),
        notes
      );
      setTranscriptMessage('No transcript could be read for this video.', notes);
      return null;
    }

    state.transcript = result;
    if (result.title) state.title = result.title;
    if (result.channel) state.channel = result.channel;
    state.claims = buildClaims(result.segments);
    state.claimById = new Map(state.claims.map((c) => [c.id, c]));

    renderTranscript(result.segments);
    renderClaimList();
    updateNow(state.lastTime);
    applyLiveWindow(state.lastTime);

    setVideoHeader(state.title, state.channel, transcriptLabel());
    // Keep the strategy trail on the status line for troubleshooting.
    $('transcriptState').title = [`strategy: ${result.strategy || result.source}`]
      .concat(result.notes || [])
      .join('\n');

    // Follow playback so the transcript highlights along with the video.
    sendToTab({ type: 'start-time-updates' }).catch(() => {});

    // Auto mode fact-checks a new video without being asked. Live follow does
    // its own checking as claims are spoken, so don't double up.
    if (state.settings.autoCheck && !state.live && state.claims.length && !state.scanning) {
      scanVideo();
    }
    return result;
  } catch (e) {
    setVideoHeader(state.title, state.channel, 'could not read captions');
    showNotice('Could not read the captions from this tab.', 'Retry', () => loadTranscript());
    return null;
  } finally {
    state.loadingTranscript = false;
  }
}

/** The steady-state status line under the video title. */
function transcriptLabel() {
  const t = state.transcript;
  if (!t) return 'no captions';
  if (t.source === 'metadata') return 'no captions — using title + description';
  return (
    `${state.lines.length} lines · ${state.claims.length} claims` + (t.auto ? ' · auto-captions' : '')
  );
}

/** Merge caption fragments into readable, seekable transcript lines. */
function buildLines(segments) {
  const lines = [];
  let cur = null;
  for (const seg of segments) {
    const text = String(seg.text || '').trim();
    if (!text) continue;
    if (!cur) cur = { start: seg.start, end: seg.start + (seg.dur || 0), text };
    else {
      cur.text += ' ' + text;
      cur.end = seg.start + (seg.dur || 0);
    }
    const words = cur.text.split(/\s+/).length;
    if (words >= 14 || cur.end - cur.start >= 9 || /[.!?]["')\]]?$/.test(text)) {
      lines.push(cur);
      cur = null;
    }
  }
  if (cur) lines.push(cur);
  return lines;
}

function renderTranscript(segments) {
  const pane = $('transcript');
  pane.textContent = '';
  state.lines = buildLines(segments);
  state.lineEls = [];
  state.claimLine.clear();
  state.activeLine = -1;

  const frag = document.createDocumentFragment();
  state.lines.forEach((line) => {
    const el = document.createElement('div');
    el.className = 'line';
    el.dataset.start = String(line.start);

    const t = document.createElement('span');
    t.className = 't';
    t.textContent = fmtTime(line.start);

    const txt = document.createElement('span');
    txt.className = 'txt';
    txt.textContent = line.text;

    el.append(txt, t);
    el.addEventListener('click', () => seekTo(line.start));
    frag.appendChild(el);
    state.lineEls.push(el);
  });
  pane.appendChild(frag);

  // Mark which lines carry a detected claim.
  state.claims.forEach((claim) => {
    const idx = lineIndexAt(claim.start);
    if (idx < 0) return;
    const el = state.lineEls[idx];
    el.classList.add('claim');
    el.title = 'Detected claim — check it in the Claims tab';
    if (!state.claimLine.has(claim.id)) state.claimLine.set(claim.id, el);
  });

  $('lineCount').textContent = state.lines.length ? String(state.lines.length) : '';
}

function lineIndexAt(time) {
  let idx = -1;
  for (let i = 0; i < state.lines.length; i++) {
    if (state.lines[i].start <= time + 0.01) idx = i;
    else break;
  }
  return idx;
}

function highlightLine(time) {
  const idx = lineIndexAt(time);
  if (idx === state.activeLine) return;
  if (state.lineEls[state.activeLine]) state.lineEls[state.activeLine].classList.remove('active');
  state.activeLine = idx;
  const el = state.lineEls[idx];
  if (!el) return;
  el.classList.add('active');

  // Auto-scroll only while the user is actually reading the transcript, and
  // back off for a few seconds after they scroll it themselves.
  if (state.tab !== 'transcript') return;
  if (Date.now() - state.lastUserScroll < 4000) return;
  state.autoScrolling = true;
  el.scrollIntoView({ block: 'center', behavior: 'smooth' });
  setTimeout(() => (state.autoScrolling = false), 900);
}

function seekTo(time) {
  sendToTab({ type: 'seek', time }).catch(() => {});
}

// ------------------------------------------------------------ claim detection

const MAX_WINDOW_WORDS = 22; // a spoken sentence is rarely longer
const MIN_WINDOW_WORDS = 10; // below this, a break cue is probably mid-thought
const PAUSE_BREAK = 0.8; // seconds of silence that likely ends a sentence
// Discourse markers that start a new thought in unpunctuated auto-captions.
const BREAK_WORDS = /^(so|but|and|because|now|then|well|okay|ok|anyway|however|actually|meanwhile|plus|also|first|second|finally)$/;
// Sponsor/CTA chatter — isolated into its own chunk so it never bleeds into a
// neighbouring claim (a chunk of pure filler then scores itself out).
const FILLER_CUE = /^(hey|guys|subscribe|subscribed|patreon|sponsor|sponsors|sponsored|channel)$/;
// Hedges get the same treatment, so an opinion aside can't drag the verifiable
// sentence next to it below the scoring threshold.
const HEDGE_CUE = /^(think|believe|feel|guess|maybe|probably|honestly|personally|opinion)$/;

/** Caption lines → timestamped, roughly sentence-sized, scored claims. */
function buildClaims(segments) {
  const words = [];
  let prevEnd = null;
  for (const seg of segments) {
    const parts = String(seg.text || '').split(/\s+/).filter(Boolean);
    if (!parts.length) continue;
    const per = seg.dur > 0 ? seg.dur / parts.length : 0;
    const gap = prevEnd == null ? 0 : seg.start - prevEnd;
    parts.forEach((w, i) =>
      words.push({ w, t: seg.start + per * i, seg0: i === 0, brk: i === 0 && gap > PAUSE_BREAK })
    );
    prevEnd = seg.start + (seg.dur || 0);
  }
  if (!words.length) return [];

  const joined = words.map((x) => x.w).join(' ');
  const stops = (joined.match(/[.!?]/g) || []).length;
  // Auto-captions arrive unpunctuated; without sentence stops we window instead.
  const punctuated = stops >= joined.length / 400;

  const chunks = [];
  let buf = [];
  let startT = words[0].t;
  const flush = () => {
    if (!buf.length) return;
    chunks.push({ text: buf.map((x) => x.w).join(' ').trim(), start: startT });
    buf = [];
  };

  for (const item of words) {
    if (punctuated) {
      if (!buf.length) startT = item.t;
      buf.push(item);
      if (/[.!?]["')\]]?$/.test(item.w)) flush();
      continue;
    }
    // Unpunctuated: cut at a caption-line edge, a pause, a discourse marker or
    // sponsor chatter — never mid-phrase — and hard-cap the window so one
    // chunk stays one thought.
    const bare = item.w.toLowerCase().replace(/[^a-z']/g, '');
    const filler = FILLER_CUE.test(bare) || HEDGE_CUE.test(bare);
    const cue = item.brk || item.seg0 || filler || BREAK_WORDS.test(bare);
    const minWords = filler ? 3 : item.brk ? 5 : MIN_WINDOW_WORDS;
    if (buf.length >= minWords && cue) flush();
    if (!buf.length) startT = item.t;
    buf.push(item);
    if (filler || buf.length >= MAX_WINDOW_WORDS) flush();
  }
  flush();

  const seen = new Set();
  const claims = [];
  chunks.forEach((c, i) => {
    const text = tidy(c.text, !punctuated);
    const wordCount = text.split(/\s+/).length;
    // Short lines are usually filler — unless they carry a figure worth checking.
    if (text.length < 30 || (wordCount < 7 && !/\d/.test(text))) return;
    const key = text.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
    if (seen.has(key)) return;
    seen.add(key);
    const score = scoreClaim(text);
    if (score <= 0) return;
    claims.push({ id: `c${i}`, text, start: c.start, score });
  });
  return claims;
}

/**
 * Tidy a chunk's edges: window cuts leave dangling connectives behind.
 * The leading trim only applies to windowed (unpunctuated) chunks — in a real
 * sentence a leading "That"/"So" is the actual subject, not a cut artifact.
 */
function tidy(text, trimLead) {
  let out = text.replace(/\s+/g, ' ').trim();
  if (trimLead) out = out.replace(/^(which|and|but|so|then|because|that|of)\s+/i, '');
  return out.replace(/\s+(i|a|an|the|of|to|and|but|so|or|which|that|is|was|it|in|on|at|for|with)$/i, '').trim();
}

/** Cheap check-worthiness heuristic — verifiable assertions score highest. */
function scoreClaim(s) {
  let score = 0;
  const lower = s.toLowerCase();
  if (/\b(19|20)\d{2}\b/.test(s)) score += 3;
  if (/\d/.test(s)) score += 2;
  if (/\b(percent|%|million|billion|trillion|thousand)\b/.test(lower)) score += 2;
  if (/\b(first|largest|biggest|smallest|most|highest|lowest|only|never|always|worst|best)\b/.test(lower)) score += 2;
  if (/\b(causes?|caused|cures?|prevents?|increases?|decreases?|reduces?|doubles?|kills?|leads to)\b/.test(lower)) score += 2;
  if (/\b(study|studies|research|researchers|scientists|data|report|according to|survey|statistics)\b/.test(lower)) score += 2;
  if (/\b(more than|less than|fewer than|higher than|lower than|twice|half of)\b/.test(lower)) score += 1;
  if (/\b(is|are|was|were|has|have|had|will)\b/.test(lower)) score += 1;
  if (/\b(i think|i believe|in my opinion|maybe|probably|i guess|i feel like)\b/.test(lower)) score -= 4;
  if (/\b(subscribe|like and subscribe|sponsor|patreon|comment below|channel)\b/.test(lower)) score -= 4;
  if (s.trim().endsWith('?')) score -= 2;
  return score;
}

/** Optional: let You.com pick the claims instead of the local heuristic. */
async function smartClaims(limit) {
  const transcript = state.transcript.segments
    .map((s) => `[${Math.round(s.start)}] ${s.text}`)
    .join('\n')
    .slice(0, 12000);
  const res = await fetch(api('/api/extract'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ transcript, title: state.title, limit })
  });
  const data = await res.json();
  if (!res.ok || !Array.isArray(data.claims) || !data.claims.length) throw new Error(data.error || 'no claims');
  return data.claims
    .map((c, i) => ({
      id: `s${i}`,
      text: String(c.claim || c.text || '').trim(),
      start: Number(c.start || c.time || 0),
      score: 99
    }))
    .filter((c) => c.text.length > 15);
}

// ---------------------------------------------------------------- scanning

/** How many claims a scan checks — Auto means every one of them. */
function claimLimit() {
  if (state.settings.autoCheck) return Infinity;
  const v = state.settings.maxClaims;
  return String(v).toLowerCase() === 'all' ? Infinity : Number(v) || 8;
}

async function scanVideo() {
  if (state.scanning) return stopScan();

  const transcript = state.transcript || (await loadTranscript());
  if (!transcript) return;

  const limit = claimLimit();
  let picks = [];
  if (state.settings.smartExtract) {
    setVideoHeader(state.title, state.channel, 'asking You.com to pick claims…');
    try {
      picks = await smartClaims(Number.isFinite(limit) ? limit : 20); // backend caps at 20
      picks.forEach((c) => state.claimById.set(c.id, c));
    } catch (_) {
      picks = [];
    }
  }
  if (!picks.length) {
    picks = [...state.claims].sort((a, b) => b.score - a.score || a.start - b.start).slice(0, limit);
  }
  picks.sort((a, b) => a.start - b.start);

  if (!picks.length) {
    setVideoHeader(state.title, state.channel, 'no check-worthy claims found');
    return;
  }

  state.scanning = true;
  $('scanBtn').textContent = '■ Stop';
  $('scanBtn').classList.remove('primary');
  $('progress').hidden = false;
  showTab('claims');

  let done = 0;
  const total = picks.length;
  const tick = () => {
    done++;
    $('progressFill').style.width = `${Math.round((done / total) * 100)}%`;
    setVideoHeader(state.title, state.channel, `checking ${done}/${total} claims…`);
  };
  setVideoHeader(state.title, state.channel, `checking 0/${total} claims…`);

  picks.forEach((claim) => setCardStatus(addCard(claim), 'QUEUED'));

  const concurrency = 2;
  const queue = picks.slice();
  const workers = Array.from({ length: Math.min(concurrency, queue.length) }, async () => {
    while (queue.length && state.scanning) {
      const claim = queue.shift();
      await checkClaim(claim);
      tick();
    }
  });
  await Promise.all(workers);
  stopScan({ abort: false }); // finished on its own — nothing to cancel
}

/** Cancel every fact-check still in flight and arm a fresh controller. */
function abortInFlight() {
  if (state.abort) state.abort.abort();
  state.abort = new AbortController();
}

function stopScan({ abort = true } = {}) {
  const wasScanning = state.scanning;
  state.scanning = false;
  // Cancel requests still in flight, then arm a fresh controller so later
  // checks (a single Check click, live follow) still work.
  if (abort && wasScanning) abortInFlight();
  // Claims that never left the queue go back to being plain detected claims.
  for (const [id, card] of state.cards) {
    if (card.querySelector('.verdict.QUEUED')) {
      state.queued.delete(id);
      setCardStatus(card, 'NEW');
    }
  }
  $('scanBtn').textContent = 'Scan video';
  $('scanBtn').classList.add('primary');
  $('progress').hidden = true;
  $('progressFill').style.width = '0';
  if (state.transcript) setVideoHeader(state.title, state.channel, transcriptLabel());
}

// ---------------------------------------------------------------- live follow

const LIVE_WINDOW = 20; // seconds behind the playhead we still care about

async function toggleLive() {
  if (state.live) return stopLive();
  const transcript = state.transcript || (await loadTranscript());
  if (!transcript) return;

  state.live = true;
  document.body.classList.add('live-mode');
  $('nowBox').classList.add('pulse');
  $('liveBtn').textContent = '■ Stop live';
  $('liveBtn').classList.add('live');
  applyLiveWindow(state.lastTime);
  try {
    await sendToTab({ type: 'start-time-updates' });
  } catch (_) {
    stopLive();
  }
}

function stopLive() {
  if (!state.live) return;
  state.live = false;
  document.body.classList.remove('live-mode');
  $('nowBox').classList.remove('pulse');
  $('liveBtn').textContent = '▶ Live follow';
  $('liveBtn').classList.remove('live');
  revealEverything();
}

/** Show the transcript line the video is on right now. */
function updateNow(time) {
  const line = state.lines[lineIndexAt(time)];
  const box = $('nowBox');
  if (!line) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  $('nowTime').textContent = fmtTime(line.start);
  $('nowText').textContent = line.text;
}

/**
 * In live mode the panel only shows what has actually been said so far —
 * claims and transcript lines past the playhead stay hidden until reached.
 */
function applyLiveWindow(time) {
  if (!state.live) return;

  let n = 0;
  while (n < state.claims.length && state.claims[n].start <= time) n++;
  if (n !== state.revealedClaims) {
    state.revealedClaims = n;
    state.claims.forEach((claim, i) => {
      const card = state.cards.get(claim.id);
      if (card) card.classList.toggle('future', i >= n);
    });
    if (!n) {
      showNotice('Live — waiting for the first claim to be spoken…');
    } else {
      hideNotice();
      const newest = state.cards.get(state.claims[n - 1].id);
      if (newest && state.tab === 'claims') scrollIntoViewSoftly(newest);
    }
  }

  const m = lineIndexAt(time) + 1;
  if (m !== state.revealedLines) {
    state.revealedLines = m;
    state.lineEls.forEach((el, i) => el.classList.toggle('future', i >= m));
  }
}

/** Leaving live mode puts the whole video back on screen. */
function revealEverything() {
  state.revealedClaims = -1;
  state.revealedLines = -1;
  state.cards.forEach((card) => card.classList.remove('future'));
  state.lineEls.forEach((el) => el.classList.remove('future'));
  if (state.claims.length) hideNotice();
}

function scrollIntoViewSoftly(el) {
  state.autoScrolling = true;
  el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  setTimeout(() => (state.autoScrolling = false), 900);
}

function onTimeUpdate(time) {
  state.lastTime = time;
  updateNow(time);
  highlightLine(time);
  if (!state.live) return;
  applyLiveWindow(time);
  // Auto mode checks everything that goes by; otherwise only the strong claims.
  const auto = state.settings.autoCheck;
  const minScore = auto ? 1 : 3;
  const due = state.claims.filter(
    (c) => !state.queued.has(c.id) && c.score >= minScore && c.start <= time && c.start >= time - LIVE_WINDOW
  );
  for (const claim of due.slice(0, auto ? 3 : 2)) checkClaim(claim);
}

// ---------------------------------------------------------------- fact check

async function checkClaim(claim) {
  if (state.queued.has(claim.id)) return;
  state.queued.add(claim.id);

  if (!state.abort) state.abort = new AbortController();
  const gen = state.generation; // the video this check belongs to
  const signal = state.abort.signal;
  const card = addCard(claim);
  setCardStatus(card, 'CHECKING');

  try {
    const res = await fetch(api('/api/factcheck'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        claim: claim.text,
        mode: state.settings.mode,
        context: { title: state.title, channel: state.channel }
      }),
      signal
    });
    const data = await res.json();
    if (gen !== state.generation) return; // video changed — drop the stale reply
    if (!res.ok) throw new Error(data.error || `request failed (${res.status})`);
    renderResult(card, claim, data);
  } catch (e) {
    if (gen !== state.generation) return;
    if (e.name === 'AbortError') {
      state.queued.delete(claim.id);
      setCardStatus(card, 'NEW');
      return;
    }
    renderError(card, claim, e.message || String(e));
  }
}

// ---------------------------------------------------------------- rendering

function fmtTime(sec) {
  const s = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  const mm = h ? String(m).padStart(2, '0') : String(m);
  return (h ? `${h}:` : '') + `${mm}:${String(r).padStart(2, '0')}`;
}

/** Remove result cards without touching the notice element living alongside them. */
function clearCards() {
  $('cards').querySelectorAll('.card').forEach((el) => el.remove());
  state.cards.clear();
}

function renderClaimList() {
  clearCards();
  
  const limit = claimLimit();
  let displayClaims = [...state.claims].sort((a, b) => b.score - a.score || a.start - b.start).slice(0, limit);
  displayClaims.sort((a, b) => a.start - b.start);

  $('claimCount').textContent = displayClaims.length ? String(displayClaims.length) : '';

  if (!displayClaims.length) {
    showNotice(
      state.transcript
        ? 'No check-worthy claims found in this video — it may be mostly opinion, chat or music.'
        : 'No captions loaded yet.'
    );
    return;
  }
  hideNotice();
  displayClaims.forEach((claim) => addCard(claim));
}

function addCard(claim) {
  hideNotice();
  const existing = state.cards.get(claim.id);
  if (existing) return existing;

  const card = document.createElement('div');
  card.className = 'card';
  card.dataset.start = String(claim.start);
  card.innerHTML = `
    <div class="card-top">
      <button class="stamp"></button>
      <span class="verdict NEW">Not checked</span>
      <button class="check-one">Check</button>
    </div>
    <div class="claim"></div>`;
  card.querySelector('.stamp').textContent = fmtTime(claim.start);
  card.querySelector('.claim').textContent = claim.text;
  card.querySelector('.stamp').addEventListener('click', () => seekTo(claim.start));
  card.querySelector('.check-one').addEventListener('click', () => checkClaim(claim));

  insertByTime(card, claim.start);
  state.cards.set(claim.id, card);
  return card;
}

function insertByTime(card, start) {
  const cards = $('cards');
  const siblings = [...cards.querySelectorAll('.card')];
  const after = siblings.find((el) => Number(el.dataset.start) > start);
  if (after) cards.insertBefore(card, after);
  else cards.appendChild(card);
}

/** Move a card between the pre-verdict states: NEW → QUEUED → CHECKING. */
function setCardStatus(card, status) {
  const top = card.querySelector('.card-top');
  const badge = top.querySelector('.verdict');
  const label = { NEW: 'Not checked', QUEUED: 'Queued', CHECKING: 'Checking…' }[status] || status;
  badge.className = `verdict ${status}`;
  badge.textContent = label;

  const spinner = top.querySelector('.spinner');
  if (status === 'CHECKING' && !spinner) {
    top.insertBefore(Object.assign(document.createElement('span'), { className: 'spinner' }), badge);
  } else if (status !== 'CHECKING' && spinner) {
    spinner.remove();
  }

  const btn = top.querySelector('.check-one');
  if (btn) btn.hidden = status !== 'NEW';
}

function renderResult(card, claim, data) {
  const verdict = String(data.verdict || 'UNVERIFIED').toUpperCase();
  card.className = `card ${verdict}`;

  const tally = {
    TRUE: ['sTrue', 'true'],
    FALSE: ['sFalse', 'false'],
    MISLEADING: ['sMisleading', 'misleading'],
    UNVERIFIED: ['sUnverified', 'unverified']
  };
  const [tileId, key] = tally[verdict] || tally.UNVERIFIED;
  state.scores[key]++;
  state.checked++;
  $(tileId).textContent = state.scores[key];
  $('sChecked').textContent = state.checked;
  bump(tileId);
  bump('sChecked');
  updateTrust();

  const line = state.claimLine.get(claim.id);
  if (line) {
    line.classList.remove('TRUE', 'FALSE', 'MISLEADING', 'UNVERIFIED');
    line.classList.add(verdict);
    line.title = `${verdict} — ${data.explanation || ''}`;
  }

  const tag = data.cached
    ? 'cached'
    : data.mode === 'mcp'
      ? 'via MCP · grounded'
      : 'grounded verdict';
  if (!data.cached) scheduleBalanceRefresh();

  card.innerHTML = `
    <div class="card-top">
      <button class="stamp"></button>
      <span class="verdict ${verdict}">${verdict}</span>
      <span class="tag">${tag}</span>
    </div>
    <div class="claim"></div>
    <div class="explain"></div>
    <div class="sources"></div>`;
  card.querySelector('.stamp').textContent = fmtTime(claim.start);
  card.querySelector('.claim').textContent = claim.text;
  card.querySelector('.explain').textContent = data.explanation || '';
  card.querySelector('.stamp').addEventListener('click', () => seekTo(claim.start));

  // A rule from SOURCE_TRUST.md fired (verdict withheld / weak sourcing).
  if (data.trust_note) {
    const note = document.createElement('div');
    note.className = 'trust-note';
    note.textContent = '⚠ ' + data.trust_note;
    card.querySelector('.explain').after(note);
  }

  const sources = card.querySelector('.sources');
  (data.sources || [])
    .filter((s) => s && s.url)
    .slice(0, 5)
    .forEach((s) => sources.appendChild(sourceRow(s)));
}

const LEVEL_NAMES = {
  0: 'Institutional / peer-reviewed',
  1: 'Major journalism & wire services',
  2: 'Secondary / industry media',
  3: 'User-generated / opinion',
  4: 'High-bias / low-reliability'
};

/** One citation: trust-level badge, then the link. */
function sourceRow(s) {
  const row = document.createElement('div');
  row.className = 'source';

  const hasLevel = typeof s.level === 'number' && s.level >= 0 && s.level <= 4;
  const badge = document.createElement('span');
  badge.className = hasLevel ? `lvl lvl${s.level}` : 'lvl lvl2 unrated';
  badge.textContent = hasLevel ? `L${s.level}` : 'L?';
  badge.title = hasLevel
    ? `Trust Level ${s.level} — ${s.level_name || LEVEL_NAMES[s.level]}` +
      (s.rated === false ? '\n(domain not in the trust list — default)' : '')
    : 'Unclassified source';
  if (s.rated === false) badge.classList.add('unrated');

  const a = document.createElement('a');
  a.href = s.url;
  a.target = '_blank';
  a.rel = 'noopener';
  a.textContent = s.title || s.url;
  a.title = s.url;

  row.append(badge, a);
  return row;
}

function renderError(card, claim, message) {
  card.className = 'card ERROR';
  card.innerHTML = `
    <div class="card-top">
      <button class="stamp"></button>
      <span class="verdict ERROR">Error</span>
      <button class="check-one">Retry</button>
    </div>
    <div class="claim"></div>
    <div class="explain"></div>`;
  card.querySelector('.stamp').textContent = fmtTime(claim.start);
  card.querySelector('.claim').textContent = claim.text;
  card.querySelector('.explain').textContent = message;
  card.querySelector('.stamp').addEventListener('click', () => seekTo(claim.start));
  state.queued.delete(claim.id); // allow a retry
  card.querySelector('.check-one').addEventListener('click', () => checkClaim(claim));
}

function bump(id) {
  const tile = $(id).parentElement;
  tile.classList.remove('bump');
  void tile.offsetWidth; // restart the animation
  tile.classList.add('bump');
}

/** Share of decided verdicts that held up, MISLEADING counting as a partial. */
function updateTrust() {
  const { true: t, false: f, misleading: m } = state.scores;
  const decided = t + f + m;
  if (!decided) {
    $('trustValue').textContent = '—';
    $('trustFill').style.width = '0';
    return;
  }
  const pct = Math.round(((t + m * 0.4) / decided) * 100);
  $('trustValue').textContent = `${pct}%`;
  const fill = $('trustFill');
  fill.style.width = `${pct}%`;
  fill.style.background = pct >= 70 ? 'var(--true)' : pct >= 40 ? 'var(--misleading)' : 'var(--false)';
}

/** Collapsible dump of what each transcript strategy actually returned. */
function noteDetails(notes) {
  if (!notes || !notes.length) return null;
  const d = document.createElement('details');
  d.className = 'diag';
  const summary = document.createElement('summary');
  summary.textContent = 'Technical details';
  const body = document.createElement('div');
  body.className = 'diag-body';
  body.textContent = notes.join('\n');
  d.append(summary, body);
  return d;
}

function showNotice(text, actionLabel, onAction, notes) {
  const empty = $('emptyState');
  empty.textContent = text;
  empty.className = 'notice';
  empty.style.display = '';
  if (actionLabel) {
    const btn = document.createElement('button');
    btn.className = 'btn';
    btn.textContent = actionLabel;
    btn.addEventListener('click', onAction);
    empty.append(document.createElement('br'), btn);
  }
  const diag = noteDetails(notes);
  if (diag) empty.appendChild(diag);
}

function setTranscriptMessage(text, notes) {
  const pane = $('transcript');
  pane.textContent = '';
  const box = document.createElement('div');
  box.className = 'notice';
  box.textContent = text;
  const diag = noteDetails(notes);
  if (diag) box.appendChild(diag);
  pane.appendChild(box);
}

function hideNotice() {
  $('emptyState').style.display = 'none';
}

/** Clear verdicts but keep the detected claims listed. */
function resetResults() {
  state.queued.clear();
  state.scores = { true: 0, false: 0, misleading: 0, unverified: 0 };
  state.checked = 0;
  ['sChecked', 'sTrue', 'sFalse', 'sMisleading', 'sUnverified'].forEach((id) => ($(id).textContent = '0'));
  updateTrust();
  state.lineEls.forEach((el) => el.classList.remove('TRUE', 'FALSE', 'MISLEADING', 'UNVERIFIED'));
  renderClaimList();
}

/** Full reset — wipes every trace of the previous video. */
function resetAll() {
  state.generation++; // anything still in flight now belongs to a dead video
  abortInFlight();
  state.transcript = null;
  state.claims = [];
  state.claimById.clear();
  state.claimLine.clear();
  state.lines = [];
  state.lineEls = [];
  state.activeLine = -1;
  state.lastTime = 0;
  state.revealedClaims = -1;
  state.revealedLines = -1;
  $('nowBox').hidden = true;
  $('transcript').textContent = '';
  $('lineCount').textContent = '';
  $('transcriptState').title = '';
  $('progress').hidden = true;
  $('progressFill').style.width = '0';
  clearCards();
  resetResults();
  $('cards').scrollTop = 0;
  $('transcript').scrollTop = 0;
}

function showTab(name) {
  state.tab = name;
  [...$('tabs').children].forEach((b) => b.classList.toggle('active', b.dataset.tab === name));
  $('cards').hidden = name !== 'claims';
  $('transcript').hidden = name !== 'transcript';
}

// ---------------------------------------------------------------- events

$('scanBtn').addEventListener('click', () => scanVideo());
$('liveBtn').addEventListener('click', () => toggleLive());
$('autoBtn').addEventListener('click', () => {
  saveSettings({ autoCheck: !state.settings.autoCheck });
  setAutoButton();
  if (state.settings.autoCheck && state.transcript && !state.scanning && !state.live) scanVideo();
});
$('clearBtn').addEventListener('click', () => {
  stopScan();
  resetResults();
});

$('tabs').addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (b) showTab(b.dataset.tab);
});

// The "now playing" line jumps you to that point in the transcript.
$('nowBox').addEventListener('click', () => {
  showTab('transcript');
  const el = state.lineEls[state.activeLine];
  if (el) {
    state.autoScrolling = true;
    el.scrollIntoView({ block: 'center' });
    setTimeout(() => (state.autoScrolling = false), 900);
  }
});

$('transcript').addEventListener('scroll', () => {
  if (!state.autoScrolling) state.lastUserScroll = Date.now();
});

$('modeToggle').addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (!b) return;
  saveSettings({ mode: b.dataset.mode });
  [...$('modeToggle').children].forEach((c) => c.classList.toggle('active', c === b));
});

$('maxClaims').addEventListener('change', (e) => {
  saveSettings({ maxClaims: e.target.value });
  if (state.transcript && !state.scanning && !state.live) {
    renderClaimList();
  }
});
$('smartExtract').addEventListener('change', (e) => saveSettings({ smartExtract: e.target.checked }));
$('endpoint').addEventListener('change', (e) => {
  saveSettings({ endpoint: e.target.value.trim() || DEFAULTS.endpoint });
  $('endpoint').value = state.settings.endpoint;
  checkBackend();
  refreshBalance();
});
$('backendPill').addEventListener('click', () => checkBackend());
$('creditsPill').addEventListener('click', () => refreshBalance());

chrome.runtime.onMessage.addListener((message) => {
  if (!message) return;
  if (message.type === 'time-update' && message.videoId === state.videoId) onTimeUpdate(message.time);
  if (message.type === 'video-changed' || message.type === 'tab-updated') syncVideo();
});

chrome.tabs.onActivated.addListener(() => syncVideo());

// ---------------------------------------------------------------- boot

(async function init() {
  await loadSettings();
  await checkBackend();
  refreshBalance();
  await syncVideo({ force: true });
})();
