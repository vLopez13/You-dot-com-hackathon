# LiveCheck — Real-Time Podcast Fact Detector

Speak into your mic like a podcast host. Your speech is transcribed live, and
factual claims are automatically fact-checked against the web using the
**You.com API** — each check comes back with a **verdict** and **citations**.

Built for the You.com hackathon. Three moving parts:

1. **Speech-to-text** — the browser's Web Speech API (real-time, no setup).
2. **You.com API** — a tiny Python backend proxies claims to the You.com
   Research / Search API (keeps your key secret, avoids CORS).
3. **Podcast video UI** — your webcam is the "podcast video", with live
   captions and a fact-check feed.

## Run it

Requires only Python 3 — **no `pip install` needed** (standard library only).

```bash
export YDC_API_KEY="your-you.com-api-key"   # get one at https://you.com/docs
python3 server.py
```

Open **http://localhost:8000** in **Chrome or Edge** (they support the Web
Speech API), click **● Go Live**, allow the mic + camera, and start talking.

## How it works

- Finalized sentences run through a lightweight **claim detector** (numbers,
  dates, comparatives, is/are assertions) — claim-like sentences are checked
  automatically. You can also force-check the last sentence with the button.
- Each check aims for **2–3 real web citations** with **factual excerpts**
  (stats, dates, named findings). Research/MCP boost trusted domains
  (`.gov` / `.edu` / `.int`, CDC, WHO, NIH, Al Jazeera, National Geographic,
  Forbes, Washington Post, wire services, encyclopedias), drop low-quality
  forums/social hits from the main list, and enrich thin citations with Search
  snippets that carry concrete data. When weaker hits exist (Reddit, Quora,
  rumor/opinion pages), up to **2** appear in a secondary “less-reliable” list
  under the trusted sources for contrast.

Three interchangeable You.com integrations, switchable in the UI:

- **REST** → `POST https://api.you.com/v1/research`, a grounded, cited answer
  with `source_control` boosts. Prompted to lead with `VERDICT:
  TRUE|FALSE|MISLEADING|UNVERIFIED` and cite 2–3 reliable sources.
- **MCP** → `POST https://api.you.com/mcp` (JSON-RPC), calling the server's
  `you-research` tool with the same prompt + source control. Source URLs are
  extracted from the answer markdown, then ranked/padded.
- **Fast** → `GET https://ydc-index.io/v1/search` for quick web evidence
  (no synthesized verdict), ranked toward trusted domains.

All requests send a browser `User-Agent` — `api.you.com` sits behind
Cloudflare, which 403s the default `Python-urllib` agent (error 1010).

## Files

| File | Purpose |
|------|---------|
| `server.py` | Stdlib HTTP server: serves the UI + proxies to You.com |
| `index.html` | Podcast UI: video, live captions, fact-check feed |

## Live feeds

Switch to **Live Feed**, paste a URL from **YouTube**, **Kick**, **TikTok**,
**Instagram**, or **Facebook**, then **Load stream**. Press **● Go Live**, pick
**this Chrome tab**, and enable **Share tab audio** — LiveCheck captures the
podcaster’s voice from the stream (not the mic) and checks claims against the
**You.com** web index.

- YouTube & Kick usually embed in-app.
- TikTok / Instagram Live often need “Open stream” (platform embed limits).
- Keep speakers on so speech recognition can pick up what is said.
