/**
 * YouTube Truth Panel — content script.
 *
 * Runs on youtube.com and does three jobs for the side panel:
 *   1. reports which video is on screen (id, title, channel, duration),
 *   2. pulls the video's caption track and returns it as timed segments,
 *   3. reports playback position (for "live follow") and seeks on request.
 *
 * Everything is best-effort: YouTube changes its DOM constantly, so each
 * transcript strategy falls through to the next one.
 */

(() => {
  if (window.__ytTruthPanelLoaded) return;
  window.__ytTruthPanelLoaded = true;

  const TIME_TICK_MS = 1000;
  let timeTimer = null;
  let timeUpdatesActive = false;
  let timeOverlay = null;
  let lastHref = location.href;

  // ---------- time overlay helpers ----------

  function ensureTimeOverlay() {
    if (timeOverlay && timeOverlay.parentNode) return;
    const player = document.querySelector('.html5-video-player') || document.querySelector('#movie_player');
    if (!player) return;

    timeOverlay = document.createElement('div');
    timeOverlay.className = 'yt-truth-panel-time-overlay';
    Object.assign(timeOverlay.style, {
      position: 'absolute',
      top: '20px',
      left: '50%',
      transform: 'translateX(-50%)',
      zIndex: '9999',
      background: 'rgba(20,24,33,0.88)',
      color: '#e8ecf4',
      fontSize: '13px',
      fontWeight: '600',
      padding: '6px 14px',
      borderRadius: '8px',
      border: '1px solid #262d3d',
      display: 'none',
      fontVariantNumeric: 'tabular-nums',
      letterSpacing: '0.02em',
      fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif'
    });
    player.appendChild(timeOverlay);
  }

  function formatTime(sec) {
    const s = Math.max(0, Math.floor(sec || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    const mm = h ? String(m).padStart(2, '0') : String(m);
    return (h ? `${h}:` : '') + `${mm}:${String(r).padStart(2, '0')}`;
  }

  function updateTimeOverlay(currentTime, duration) {
    if (!timeOverlay) return;
    if (!timeUpdatesActive) {
      timeOverlay.style.display = 'none';
      return;
    }
    timeOverlay.style.display = 'block';
    // If it's a livestream, duration might be very large or 0.
    const isLive = duration === 0 || duration > 360000;
    if (isLive) {
      timeOverlay.textContent = `● LIVE · ${formatTime(currentTime)}`;
    } else {
      timeOverlay.textContent = `${formatTime(currentTime)} / ${formatTime(duration)}`;
    }
  }

  // ---------- page helpers ----------

  function videoIdFromUrl(href = location.href) {
    try {
      const u = new URL(href);
      if (u.pathname === '/watch') return u.searchParams.get('v') || '';
      if (u.pathname.startsWith('/shorts/')) return u.pathname.split('/')[2] || '';
      if (u.hostname === 'youtu.be') return u.pathname.slice(1);
    } catch (_) {
      /* malformed URL — fall through */
    }
    return '';
  }

  function videoEl() {
    return (
      document.querySelector('video.html5-main-video') ||
      document.querySelector('#movie_player video') ||
      document.querySelector('video')
    );
  }

  function domTitle() {
    const el =
      document.querySelector('h1.ytd-watch-metadata yt-formatted-string') ||
      document.querySelector('h1.ytd-watch-metadata') ||
      document.querySelector('#title h1');
    const t = el && el.textContent ? el.textContent.trim() : '';
    return t || document.title.replace(/\s*-\s*YouTube\s*$/, '').trim();
  }

  function domChannel() {
    const el =
      document.querySelector('#owner #channel-name a') ||
      document.querySelector('ytd-channel-name#channel-name a') ||
      document.querySelector('#upload-info #channel-name a');
    return el && el.textContent ? el.textContent.trim() : '';
  }

  function getContext() {
    const v = videoEl();
    return {
      videoId: videoIdFromUrl(),
      title: domTitle(),
      channel: domChannel(),
      url: location.href,
      duration: v && isFinite(v.duration) ? v.duration : 0,
      currentTime: v ? v.currentTime : 0,
      paused: v ? v.paused : true,
      isWatchPage: !!videoIdFromUrl()
    };
  }

  // ---------- ytInitialPlayerResponse extraction ----------

  /**
   * Pull a `<key> = { ... }` JSON object out of a page's HTML by brace-matching
   * (string- and escape-aware), which survives the minified inline scripts
   * YouTube ships far better than a regex would.
   */
  function extractJsonAssignment(html, key) {
    let from = 0;
    while (true) {
      const at = html.indexOf(key, from);
      if (at === -1) return null;
      const eq = html.indexOf('=', at + key.length);
      const brace = html.indexOf('{', at + key.length);
      if (eq === -1 || brace === -1 || brace - eq > 4 || eq < at) {
        from = at + key.length;
        continue;
      }
      const json = sliceBalanced(html, brace);
      if (json) {
        try {
          return JSON.parse(json);
        } catch (_) {
          /* truncated or not the assignment we wanted — keep looking */
        }
      }
      from = at + key.length;
    }
  }

  function sliceBalanced(s, start) {
    let depth = 0;
    let inStr = false;
    let quote = '';
    for (let i = start; i < s.length; i++) {
      const c = s[i];
      if (inStr) {
        if (c === '\\') i++;
        else if (c === quote) inStr = false;
        continue;
      }
      if (c === '"' || c === "'") {
        inStr = true;
        quote = c;
      } else if (c === '{') depth++;
      else if (c === '}') {
        depth--;
        if (depth === 0) return s.slice(start, i + 1);
      }
    }
    return null;
  }

  async function getPlayerResponse(videoId) {
    // The inline copy is only trustworthy on a hard load — after an SPA
    // navigation it still describes the previously watched video.
    const inline = extractJsonAssignment(document.documentElement.innerHTML, 'ytInitialPlayerResponse');
    if (inline && inline.videoDetails && inline.videoDetails.videoId === videoId) return inline;

    const res = await fetch(`https://www.youtube.com/watch?v=${encodeURIComponent(videoId)}&hl=en`, {
      credentials: 'include'
    });
    const html = await res.text();
    const fetched = extractJsonAssignment(html, 'ytInitialPlayerResponse');
    if (fetched && (!fetched.videoDetails || fetched.videoDetails.videoId === videoId)) return fetched;
    return null;
  }

  function pickCaptionTrack(player) {
    const list =
      player &&
      player.captions &&
      player.captions.playerCaptionsTracklistRenderer &&
      player.captions.playerCaptionsTracklistRenderer.captionTracks;
    if (!list || !list.length) return null;

    const en = list.filter((t) => (t.languageCode || '').toLowerCase().startsWith('en'));
    // Prefer a human-written English track, then auto-generated English, then
    // anything at all (translated to English further down).
    const manualEn = en.find((t) => t.kind !== 'asr');
    return manualEn || en[0] || list[0];
  }

  // ---------- caption fetching ----------

  /** Set query params on a URL, replacing any that are already there.
   *  Caption baseUrls ship with fmt=srv3 — appending &fmt=json3 is ignored. */
  function withQuery(url, params) {
    try {
      const u = new URL(url, location.origin);
      for (const [k, v] of Object.entries(params)) {
        if (v == null) u.searchParams.delete(k);
        else u.searchParams.set(k, v);
      }
      return u.toString();
    } catch (_) {
      return url;
    }
  }

  function parseJson3(body) {
    let data;
    try {
      data = JSON.parse(body);
    } catch (_) {
      return null;
    }
    if (!data || !Array.isArray(data.events)) return null;
    const segments = [];
    for (const ev of data.events) {
      if (!ev.segs) continue;
      const text = ev.segs.map((s) => s.utf8 || '').join('').replace(/\s+/g, ' ').trim();
      if (!text || text === '[Music]') continue;
      segments.push({ start: (ev.tStartMs || 0) / 1000, dur: (ev.dDurationMs || 0) / 1000, text });
    }
    return segments.length ? segments : null;
  }

  /** srv3/XML captions: <p t="4220" d="1180">text</p> — the shape YouTube
   *  hands back when it ignores the json3 request. */
  function parseSrv3(body) {
    if (!/^\s*</.test(body)) return null;
    let doc;
    try {
      doc = new DOMParser().parseFromString(body, 'text/xml');
    } catch (_) {
      return null;
    }
    if (doc.querySelector('parsererror')) return null;
    const segments = [];
    doc.querySelectorAll('p').forEach((p) => {
      const text = (p.textContent || '').replace(/\s+/g, ' ').trim();
      if (!text || text === '[Music]') return;
      segments.push({
        start: Number(p.getAttribute('t') || 0) / 1000,
        dur: Number(p.getAttribute('d') || 0) / 1000,
        text
      });
    });
    return segments.length ? segments : null;
  }

  async function fetchCaptionTrack(track, notes) {
    if (!track || !track.baseUrl) return null;
    const params = { fmt: 'json3' };
    if (!(track.languageCode || '').toLowerCase().startsWith('en')) params.tlang = 'en';

    const res = await fetch(withQuery(track.baseUrl, params), { credentials: 'omit' });
    const body = await res.text();
    notes.push(`timedtext ${res.status} ${body.length}b ${track.languageCode || '?'}`);
    if (!body) return null; // YouTube answers 200 with an empty body when the URL is stale
    return parseJson3(body) || parseSrv3(body);
  }

  function packageResult(segments, track, details, source) {
    return {
      segments,
      source,
      lang: (track && track.languageCode) || 'en',
      auto: !!track && track.kind === 'asr',
      title: (details && details.title) || domTitle(),
      channel: (details && details.author) || domChannel(),
      duration: Number((details && details.lengthSeconds) || 0)
    };
  }

  // ---------- transcript strategy 1: InnerTube player API ----------

  // Public web key, present in every youtube.com page; only a fallback.
  const FALLBACK_API_KEY = 'AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8';

  function scrapeApiKey() {
    const m = document.documentElement.innerHTML.match(/"INNERTUBE_API_KEY":"([\w-]+)"/);
    return (m && m[1]) || FALLBACK_API_KEY;
  }

  /**
   * Ask InnerTube for the player response using the ANDROID client.
   * The caption URLs embedded in the watch page (WEB client) now come back
   * empty; the ANDROID ones still serve real caption data.
   */
  async function transcriptFromInnerTube(videoId, notes) {
    const res = await fetch(`https://www.youtube.com/youtubei/v1/player?key=${scrapeApiKey()}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'omit',
      body: JSON.stringify({
        context: { client: { clientName: 'ANDROID', clientVersion: '20.10.38', hl: 'en', gl: 'US' } },
        videoId,
        contentCheckOk: true,
        racyCheckOk: true
      })
    });
    const raw = await res.text();
    notes.push(`innertube ${res.status} ${raw.length}b`);
    let player;
    try {
      player = JSON.parse(raw);
    } catch (_) {
      return null;
    }

    const status = (player.playabilityStatus || {}).status;
    const track = pickCaptionTrack(player);
    if (!track) {
      notes.push(`innertube: no caption tracks (playability ${status})`);
      return null;
    }
    const segments = await fetchCaptionTrack(track, notes);
    return segments ? packageResult(segments, track, player.videoDetails, 'captions') : null;
  }

  // ---------- transcript strategy 2: caption URL from the watch page ----------

  async function transcriptFromTimedText(videoId, notes) {
    const player = await getPlayerResponse(videoId);
    if (!player) {
      notes.push('page scrape: no player response');
      return null;
    }
    const track = pickCaptionTrack(player);
    if (!track) {
      notes.push('page scrape: no caption tracks');
      return null;
    }
    const segments = await fetchCaptionTrack(track, notes);
    return segments ? packageResult(segments, track, player.videoDetails, 'captions') : null;
  }

  // ---------- transcript strategy 2: the on-page transcript panel ----------

  function readTranscriptPanel() {
    const rows = document.querySelectorAll('ytd-transcript-segment-renderer');
    if (!rows.length) return null;
    const segments = [];
    rows.forEach((row) => {
      const stamp = row.querySelector('.segment-timestamp');
      const text = row.querySelector('.segment-text');
      if (!text) return;
      const body = text.textContent.replace(/\s+/g, ' ').trim();
      if (!body) return;
      segments.push({ start: parseStamp(stamp ? stamp.textContent : ''), dur: 0, text: body });
    });
    return segments.length ? segments : null;
  }

  function parseStamp(raw) {
    const parts = String(raw || '')
      .trim()
      .split(':')
      .map((p) => parseInt(p, 10));
    if (!parts.length || parts.some(isNaN)) return 0;
    return parts.reduce((acc, p) => acc * 60 + p, 0);
  }

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function transcriptFromPanel() {
    let segments = readTranscriptPanel();
    if (segments) return finishPanel(segments);

    // Expand the description, then click its "Show transcript" button.
    const expand = document.querySelector('#description tp-yt-paper-button#expand, #expand');
    if (expand) {
      expand.click();
      await sleep(400);
    }
    const buttons = Array.from(document.querySelectorAll('button, yt-button-shape button'));
    const showBtn = buttons.find((b) => {
      const label = ((b.getAttribute('aria-label') || '') + ' ' + (b.textContent || '')).toLowerCase();
      return label.includes('transcript');
    });
    if (!showBtn) return null;
    showBtn.click();

    for (let i = 0; i < 20; i++) {
      await sleep(300);
      segments = readTranscriptPanel();
      if (segments) return finishPanel(segments);
    }
    return null;
  }

  function finishPanel(segments) {
    return {
      segments,
      source: 'panel',
      lang: 'unknown',
      auto: true,
      title: domTitle(),
      channel: domChannel(),
      duration: (videoEl() && videoEl().duration) || 0
    };
  }

  // ---------- transcript strategy 3: metadata only ----------

  function metadataFallback() {
    const desc = document.querySelector('#description-inline-expander, #description');
    const text = desc && desc.textContent ? desc.textContent.replace(/\s+/g, ' ').trim().slice(0, 4000) : '';
    const title = domTitle();
    const segments = [];
    if (title) segments.push({ start: 0, dur: 0, text: title });
    if (text) segments.push({ start: 0, dur: 0, text });
    if (!segments.length) return null;
    return {
      segments,
      source: 'metadata',
      lang: 'unknown',
      auto: false,
      title,
      channel: domChannel(),
      duration: (videoEl() && videoEl().duration) || 0
    };
  }

  async function getTranscript(videoId) {
    const id = videoId || videoIdFromUrl();
    if (!id) return { error: 'Not a YouTube video page.' };

    const notes = [];
    const attempts = [
      ['innertube', () => transcriptFromInnerTube(id, notes)],
      ['page-scrape', () => transcriptFromTimedText(id, notes)],
      ['panel', () => transcriptFromPanel()],
      ['metadata', () => metadataFallback()]
    ];
    for (const [name, attempt] of attempts) {
      try {
        const result = await attempt();
        if (result) return Object.assign({ videoId: id, notes, strategy: name }, result);
        notes.push(`${name}: no result`);
      } catch (e) {
        notes.push(`${name}: ${e && e.message ? e.message : String(e)}`);
      }
    }
    return {
      videoId: id,
      error: 'No captions available for this video.',
      notes
    };
  }

  // ---------- playback tracking ----------

  function emit(message) {
    try {
      chrome.runtime.sendMessage(message, () => void chrome.runtime.lastError);
    } catch (_) {
      /* side panel closed — nothing is listening */
    }
  }

  function startTimeUpdates() {
    stopTimeUpdates();
    timeUpdatesActive = true;
    ensureTimeOverlay();
    timeTimer = setInterval(() => {
      const v = videoEl();
      if (!v) return;
      const cTime = v.currentTime;
      const dur = isFinite(v.duration) ? v.duration : 0;
      
      updateTimeOverlay(cTime, dur);
      
      emit({
        type: 'time-update',
        videoId: videoIdFromUrl(),
        time: cTime,
        paused: v.paused,
        duration: dur
      });
    }, TIME_TICK_MS);
  }

  function stopTimeUpdates() {
    timeUpdatesActive = false;
    if (timeOverlay) timeOverlay.style.display = 'none';
    if (timeTimer) clearInterval(timeTimer);
    timeTimer = null;
  }

  function announceNavigation() {
    stopTimeUpdates();
    emit({ type: 'video-changed', context: getContext() });
  }

  document.addEventListener('yt-navigate-finish', () => setTimeout(announceNavigation, 600));
  setInterval(() => {
    if (location.href !== lastHref) {
      lastHref = location.href;
      setTimeout(announceNavigation, 600);
    }
  }, 1000);

  // ---------- message routing ----------

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    switch (message && message.type) {
      case 'ping':
        sendResponse({ ok: true });
        return false;

      case 'get-context':
      // Kept for compatibility with the 1.x panel.
      case 'get-video-context':
        sendResponse(getContext());
        return false;

      case 'get-transcript':
        getTranscript(message.videoId).then(sendResponse, (e) =>
          sendResponse({ error: e && e.message ? e.message : String(e) })
        );
        return true;

      case 'seek': {
        const v = videoEl();
        if (v) {
          v.currentTime = Math.max(0, Number(message.time) || 0);
          if (v.paused && message.play !== false) v.play().catch(() => {});
        }
        sendResponse({ ok: !!v });
        return false;
      }

      case 'set-playing': {
        const v = videoEl();
        if (v) {
          if (message.playing) v.play().catch(() => {});
          else v.pause();
        }
        sendResponse({ ok: !!v });
        return false;
      }

      case 'start-time-updates':
        startTimeUpdates();
        sendResponse({ ok: true });
        return false;

      case 'stop-time-updates':
        stopTimeUpdates();
        sendResponse({ ok: true });
        return false;

      default:
        return false;
    }
  });
})();
