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
| **⚡ Auto** | Sticky toggle: every video you open is checked automatically as soon as its captions load, with no click — and it checks **every** detected claim, ignoring the claim limit. Off by default, since it spends credits without asking. |

**Scan checks the top N claims by score, not all of them** — that is what the
claim dropdown controls (5/8/12/20/**All**). If a scan seems to skip claims,
either raise that to **All** or turn on **⚡ Auto**, which always checks
everything. During a scan the status line counts `checking 7/23 claims…`.

Live follow only reveals what has actually been said — it is a running feed
rather than the whole video at once. Turning it off puts everything back on
screen, so you can scan ahead or review what you missed.

**Switching videos wipes the panel.** Cards, transcript, now-playing line,
scoreboard, trust score and progress all reset, any scan or live follow stops,
and requests still in flight are cancelled. Each video gets a generation token,
so a verdict that arrives after you have moved on is discarded instead of
scoring the new video. Navigating off a watch page clears it too.

Click any timestamp — on a card or a transcript line — to seek the video there.
The **trust score** is the share of decided verdicts that held up (`MISLEADING`
counts as a partial credit; `UNVERIFIED` is excluded).

## Credits

The header pill shows the credits left on your You.com account, read straight
from `GET https://api.you.com/v1/billing/account_balance` (the API reports
cents; the panel shows dollars). It refreshes on open, a few seconds after a
batch of checks settles, and whenever you click it. Its tooltip adds how many
calls this backend run actually billed versus how many were served from cache.
It turns amber below $5 and red at zero.

## Depth modes

Two You.com integrations, switchable in the panel:

- **REST** → `POST https://api.you.com/v1/research` — grounded, cited verdict.
  The backend prompts it to lead with `VERDICT: TRUE|FALSE|MISLEADING|UNVERIFIED`,
  which becomes the badge.
- **MCP** → `POST https://api.you.com/mcp` (JSON-RPC) calling the `you-research`
  tool. Same engine over the Model Context Protocol, `Authorization: Bearer` auth.

Repeated claims are cached in the backend, so re-scanning a video is free —
cached cards are tagged `cached` and cost nothing. The cache lives in memory,
so restarting the backend clears it.

### Source trust levels

Every citation is classified **Level 0–4** per
[`SOURCE_TRUST.md`](SOURCE_TRUST.md) and each link is prefixed with its badge:

| Badge | Meaning | Examples |
|---|---|---|
| `L0` | Institutional / peer-reviewed | `.gov`, `.edu`, `.mil`, `.int`, PubMed, Nature, NASA, WHO |
| `L1` | Major journalism & wire services | Reuters, AP, BBC, WSJ, Pew, World Bank |
| `L2` | Secondary / industry media | TechCrunch, Forbes, ESPN, corporate newsrooms |
| `L3` | User-generated / opinion | Reddit, Medium, Substack, X, Wikipedia |
| `L4` | High-bias / low-reliability | Tabloids, conspiracy domains, state propaganda |

Sources are sorted best-first, and a dashed badge means the domain is not in
the trust list and defaulted to L2. Classification is done by the backend from
the URL, so it does not depend on the model being honest about its sourcing.

Two rules can **override the model's verdict**: a `TRUE`/`FALSE` resting only
on L3 sources, or only on L4 sources, is downgraded to `UNVERIFIED` with a
warning on the card. A lone L1 citation is flagged but not downgraded — see the
caveat in `SOURCE_TRUST.md`.

### Keeping the explanation readable

The Research API answers in markdown: citation markers like `[[1, 2, 3]]`, a
`## Sources` section and `**Key Excerpts:**` blocks. None of that belongs in a
one-line verdict, so the prompt forbids it *and* `_clean_explanation()` strips
it anyway — cutting everything from the first sources heading, removing
citation markers, flattening links to their label, dropping heading/bullet/bold
syntax and trimming to at most two sentences on a sentence boundary. Sources
are rendered separately as links, so nothing is lost.

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
| `server.py` | Local backend — You.com proxy, verdict parsing, source trust classification, caching |
| `SOURCE_TRUST.md` | The Level 0–4 source trust matrix and how it is enforced |

## API

```
GET  /health        -> {"ok": true, "has_key": true}
GET  /api/balance   -> {balance_cents, balance_usd, session:{calls, cached}}
POST /api/factcheck  {claim, mode, context:{title, channel}}
                    -> {verdict, explanation, sources[], mode, cached?}
POST /api/extract    {transcript, title, limit} -> {claims:[{claim, start}]}
```

## Troubleshooting

- **Credits pill shows `credits —` or nothing changes after editing
  `server.py`** — restart the backend. Python does not hot-reload, so a server
  started before an endpoint existed keeps answering `{"error": "not found"}`
  no matter how many times you reload the extension. `Ctrl+C`, then re-run it.
  Check with `curl http://127.0.0.1:8765/api/balance`.
- **Reloading the extension is not enough for content-script changes** — reload
  the YouTube tab too; the old script stays in the page until you do.
- **A scan appears to skip claims** — that is the top-N limit, see above.

## Notes & limits

- The API key stays on the backend; the extension only ever talks to
  `127.0.0.1`. Change the port with `PORT=… python3 server.py` and update
  **Backend URL** in the panel's Settings.
- Videos with captions disabled fall back to title + description only.
- `api.you.com` sits behind Cloudflare, which 403s the default `Python-urllib`
  User-Agent (error 1010) — every outbound call sends a browser UA instead.
- Claim detection is heuristic. It aims for *check-worthy*, not *complete* —
  a scan of the top 8 claims is a sample of the video, not an audit of it.
