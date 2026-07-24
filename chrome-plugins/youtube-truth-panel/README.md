# YouTube Truth Panel

A Chrome side-panel extension that fact-checks the YouTube video you are
watching. It reads the video's captions, finds the check-worthy claims, and
verifies each one against the web with the **You.com API** — every verdict
comes back with citations and a timestamp you can click to jump to the moment
the claim was spoken.

It's the [LiveCheck podcast detector](../../you.com/) idea moved into the
browser: instead of a microphone, the input is any YouTube video. Citations
use the same **SoT Level 0–4** trust ranking as LiveCheck.

```
YouTube tab ──► content.js ──► caption track (timedtext JSON)
                                     │
                                     ▼
                          claim detection (local, free)
                                     │
                                     ▼
              sidepanel.js ──► server.py ──► You.com Research / MCP / Search
                                     │
                                     ▼
                       verdict + explanation + sources
```

## Run it

**1. Start the backend** (Python 3, standard library only — no `pip install`):

```bash
cd chrome-plugins/youtube-truth-panel
export YDC_API_KEY="your-you.com-api-key"
python3 server.py                 # http://127.0.0.1:8765
```

**2. Load the extension:** open `chrome://extensions`, turn on **Developer
mode**, click **Load unpacked**, and pick this folder.

**3. Use it:** open any YouTube video, click the extension icon to open the
side panel, then hit **Scan video** or **Live follow**.

## What you see

A **now playing** bar sits under the video title showing the transcript line the
video is on this second, whatever tab you are in. Click it to jump to that spot
in the transcript.

Below it, the panel fills in as soon as the captions load — before any
fact-checking happens — across two tabs:

- **Claims** — every detected claim, in timestamp order, marked *Not checked*.
  Hit **Check** on any single one, or fact-check them in bulk with the buttons
  below. Verdicts, explanations and sources replace the placeholder in place.
- **Transcript** — the full transcript in seekable lines. The line playing right
  now is highlighted and scrolls itself into view; lines carrying a detected
  claim are tinted, and take on the verdict colour once checked.

| Button | Behaviour |
|--------|-----------|
| **Scan video** | Ranks every claim in the transcript and fact-checks the top N (5–20, your pick) in parallel. Good for a video you haven't watched yet. |
| **Live follow** | Turns the panel into a live feed: claims and transcript lines past the playhead are hidden, and each claim is revealed *and* fact-checked as the video reaches it. Leave it running while you watch. |

Live follow only reveals what has actually been said — it is a running feed
rather than the whole video at once. Turning it off puts everything back on
screen, so you can scan ahead or review what you missed.

Click any timestamp — on a card or a transcript line — to seek the video there.
The **trust score** is the share of decided verdicts that held up (`MISLEADING`
counts as a partial credit; `UNVERIFIED` is excluded).

## Depth modes

The same three You.com integrations as LiveCheck, switchable in the panel:

- **REST** → `POST https://api.you.com/v1/research` — grounded, cited verdict.
  The backend prompts it to lead with `VERDICT: TRUE|FALSE|MISLEADING|UNVERIFIED`,
  which becomes the badge.
- **MCP** → `POST https://api.you.com/mcp` (JSON-RPC) calling the `you-research`
  tool. Same engine over the Model Context Protocol, `Authorization: Bearer` auth.
- **Fast** → `GET https://ydc-index.io/v1/search` — quick web evidence, no
  synthesized verdict. Use it when latency matters more than a hard verdict.

Repeated claims are cached in the backend, so re-scanning a video is free.

## How claims are found

Captions come back as timed lines. Auto-generated captions have no punctuation,
so the panel splits them at caption-line edges, pauses and discourse markers
(`so`, `but`, `because`…) with a 22-word cap — never mid-phrase — isolating
sponsor/CTA chatter and hedges (`I think`, `maybe`) into their own chunks so
they can't drag a neighbouring factual sentence below threshold. Each chunk
is then scored for check-worthiness — years, figures, percentages, superlatives,
causal verbs and study references score up; hedges (`I think`, `maybe`) and
promos (`like and subscribe`) score down. Only positive-scoring chunks are
candidates, and the highest scorers get checked.

Tick **Smart claim picking** in Settings to hand that job to You.com instead
(`/api/extract`) — better selection, noticeably slower.

## Where the transcript comes from

**YouTube's own caption track — not the audio.** There is no microphone and no
speech recognition: the content script downloads the same captions YouTube
would render as subtitles. A video with captions disabled has nothing to read.

The content script tries, in order:

1. **InnerTube player API** — `POST /youtubei/v1/player` with the **ANDROID**
   client, then downloads the caption track it returns.
2. **The watch page's own caption URL** — parses `ytInitialPlayerResponse` out
   of the page (re-fetching the watch page after SPA navigation).
3. **The on-page transcript panel** — expands the description, clicks *Show
   transcript*, reads `ytd-transcript-segment-renderer` rows.
4. **Metadata only** — title + description, so a video with no captions at all
   still gets something checked.

Strategy 1 exists because strategy 2 stopped working: the caption URLs embedded
in the watch page (WEB client) now answer **HTTP 200 with an empty body**, while
the ANDROID client's URLs still serve real caption data. Two related traps this
code handles:

- Caption URLs already carry `fmt=srv3`, so appending `&fmt=json3` is silently
  ignored — the param has to be *replaced*, not appended.
- When YouTube ignores the `json3` request anyway, it returns srv3 **XML**, so
  both formats are parsed (`parseJson3` → `parseSrv3`).

If nothing works, the panel says so in both tabs and shows a **Technical
details** dump of what each strategy returned (status codes and byte counts) —
that dump is the first thing to look at when a video comes up empty. On success
the same trail is on the status line's tooltip.

## Files

| File | Purpose |
|------|---------|
| `manifest.json` | MV3 manifest — side panel, content script, host permissions |
| `background.js` | Opens the panel; injects the content script into tabs that lack it |
| `content.js` | Caption extraction, playback tracking, seeking |
| `sidepanel.html/.css/.js` | The panel: claim detection, fact-check queue, verdict cards |
| `server.py` | Local backend — You.com proxy, verdict parsing, caching |

## API

```
GET  /health        -> {"ok": true, "has_key": true}
POST /api/factcheck  {claim, mode, context:{title, channel}}
                    -> {verdict, explanation, sources[], mode, cached?}
POST /api/extract    {transcript, title, limit} -> {claims:[{claim, start}]}
```

## Notes & limits

- The API key stays on the backend; the extension only ever talks to
  `127.0.0.1`. Change the port with `PORT=… python3 server.py` and update
  **Backend URL** in the panel's Settings.
- Videos with captions disabled fall back to title + description only.
- `api.you.com` sits behind Cloudflare, which 403s the default `Python-urllib`
  User-Agent (error 1010) — every outbound call sends a browser UA instead.
- Claim detection is heuristic. It aims for *check-worthy*, not *complete* —
  a scan of the top 8 claims is a sample of the video, not an audit of it.
