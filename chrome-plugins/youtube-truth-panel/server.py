#!/usr/bin/env python3
"""
YouTube Truth Panel — backend.

Fact-checks claims pulled out of a YouTube video by the Chrome side panel,
proxying to the You.com API so the key never reaches the browser and the
extension never has to deal with CORS on api.you.com.

Standard library only — no pip install required.

Usage:
    export YDC_API_KEY="your-you.com-api-key"
    python3 server.py            # listens on http://127.0.0.1:8765

Endpoints:
    GET  /health         -> {"ok": true, "has_key": bool}
    POST /api/factcheck  -> {claim, mode, context} -> verdict + sources
    POST /api/extract    -> {transcript, title, limit} -> check-worthy claims
"""

import json
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_env():
    """Load API key from local .env or the sibling you.com/.env."""
    candidates = (
        os.path.join(HERE, ".env"),
        os.path.join(HERE, "..", "..", "you.com", ".env"),
    )
    for env_path in candidates:
        if not os.path.exists(env_path):
            continue
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    key, val = key.strip(), val.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = val
        except OSError as e:
            sys.stderr.write(f"  Warning: could not read {env_path}: {e}\n")
        break


_load_env()
API_KEY = os.environ.get("YDC_API_KEY", "").strip()
PORT = int(os.environ.get("PORT", "8765"))

RESEARCH_URL = "https://api.you.com/v1/research"
SEARCH_URL = "https://ydc-index.io/v1/search"
MCP_URL = "https://api.you.com/mcp"

# api.you.com sits behind Cloudflare, which 403s the default Python-urllib
# User-Agent (error 1010). Present a normal browser UA on every outbound call.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

FACTCHECK_PROMPT = (
    "You are a fact checker for claims spoken in an online video. Fact-check "
    "the statement below. It comes from an auto-generated transcript, so it may "
    "be missing punctuation or contain small transcription errors — judge the "
    "substance, not the wording.\n\n"
    "TRUST HIERARCHY (SoT):\n"
    "Level 0: .edu/.gov/.int, peer-reviewed science, WHO/CDC/NIH — highest.\n"
    "Level 1: wire services & major news (Reuters, AP, BBC, WSJ).\n"
    "Level 2: niche/tech/business media.\n"
    "Level 3/4: social, blogs, tabloids — do not weight highly.\n"
    "Prefer Level 0/1 sources; if they refute the claim, they override lower levels.\n\n"
    "Your response MUST begin with exactly one line of the form 'VERDICT: X' "
    "where X is one of TRUE, FALSE, MISLEADING, or UNVERIFIED. On the next line "
    "give one concise sentence (max 35 words) explaining the verdict. Cite "
    "concrete facts (stats, dates, named findings). Use UNVERIFIED when the "
    "statement is opinion, prediction, or not checkable.\n"
    "{context}"
    'Statement: "{claim}"'
)

EXTRACT_PROMPT = (
    "Below is a timestamped transcript of a video. Identify the {limit} most "
    "check-worthy factual claims — specific, verifiable assertions (statistics, "
    "historical facts, causal claims, superlatives), not opinions or filler. "
    "Respond with ONLY a JSON array, no prose, where each element is "
    '{{"claim": "<the claim as a clean sentence>", "start": <seconds as a number>}}.'
    "\n\nVideo title: {title}\n\nTranscript:\n{transcript}"
)

_cache = {}
_cache_lock = threading.Lock()

LEVEL0_DOMAINS = (
    "ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov", "jstor.org", "arxiv.org",
    "ieee.org", "nature.com", "science.org", "who.int", "cdc.gov", "nasa.gov",
    "nih.gov", "noaa.gov", "fda.gov", "epa.gov",
)
LEVEL1_DOMAINS = (
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "wsj.com",
    "bloomberg.com", "ft.com", "economist.com", "npr.org", "pbs.org",
    "nytimes.com", "washingtonpost.com", "theguardian.com", "pewresearch.org",
    "factcheck.org", "politifact.com", "snopes.com",
)
LEVEL3_DOMAINS = (
    "reddit.com", "medium.com", "twitter.com", "x.com", "substack.com",
    "quora.com", "tiktok.com", "youtube.com", "facebook.com", "instagram.com",
)
LEVEL4_DOMAINS = (
    "dailymail.co.uk", "thesun.co.uk", "nationalenquirer.com",
    "naturalnews.com", "infowars.com",
)


def _host(url: str) -> str:
    try:
        h = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return ""
    return h.lower().removeprefix("www.")


def _matches(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) or d in host for d in domains)


def _classify_source(url: str) -> dict:
    """SoT Level 0–4 trust classification (shared with LiveCheck)."""
    host = _host(url)
    path = ""
    try:
        path = (urllib.parse.urlparse(url).path or "").lower()
    except ValueError:
        pass
    is_file = any(path.endswith(ext) for ext in (".pdf", ".doc", ".docx", ".csv", ".xls", ".xlsx"))
    is_edu = host.endswith(".edu") or ".edu." in host
    is_gov = host.endswith(".gov") or ".gov." in host or host.endswith(".mil")
    is_int = host.endswith(".int")

    if is_edu or is_gov or is_int or is_file or _matches(host, LEVEL0_DOMAINS):
        if is_file:
            badge, label = "L0 · File", "Level 0: Primary File"
        elif is_edu:
            badge, label = "L0 · .edu", "Level 0: .edu Academic"
        elif is_gov:
            badge, label = "L0 · .gov", "Level 0: .gov Institutional"
        elif is_int:
            badge, label = "L0 · .int", "Level 0: International Org"
        else:
            badge, label = "L0 · Science", "Level 0: Academic & Scientific"
        return {"level": 0, "badge": badge, "label": label, "trusted": True}

    if _matches(host, LEVEL1_DOMAINS):
        return {"level": 1, "badge": "L1 · Wire / News", "label": "Level 1: Major News", "trusted": True}
    if _matches(host, LEVEL4_DOMAINS):
        return {"level": 4, "badge": "L4 · Low Trust", "label": "Level 4: Low Reliability", "trusted": False}
    if _matches(host, LEVEL3_DOMAINS):
        return {"level": 3, "badge": "L3 · Social", "label": "Level 3: Social / UGC", "trusted": False}
    return {"level": 2, "badge": "L2 · Media", "label": "Level 2: Secondary Media", "trusted": False}


def _best_snippet(snippets) -> str:
    if not snippets:
        return ""
    if isinstance(snippets, str):
        snippets = [snippets]
    texts = [" ".join(str(s).split()).strip() for s in snippets if str(s).strip()]
    if not texts:
        return ""

    def score(t: str) -> int:
        s = 0
        if re.search(r"\d+(\.\d+)?\s*%", t):
            s += 3
        if re.search(r"\d", t):
            s += 2
        if len(t) > 40:
            s += 1
        return s

    texts.sort(key=score, reverse=True)
    best = texts[0]
    return best[:217] + "..." if len(best) > 220 else best


def _process_sources(raw_sources: list) -> list:
    """Attach SoT levels + snippets; sort Level 0 first; cap at 5."""
    seen, out = set(), []
    for s in raw_sources or []:
        if isinstance(s, str):
            url, title, snippet = s, s, ""
        else:
            url = (s.get("url") or "").strip()
            title = (s.get("title") or "").strip() or url
            snips = s.get("snippets") or s.get("snippet") or []
            if not snips and s.get("description"):
                snips = [s["description"]]
            snippet = _best_snippet(snips) if not isinstance(snips, str) else _best_snippet([snips])
            if isinstance(s.get("snippet"), str) and not snippet:
                snippet = s.get("snippet") or ""
        if not url or not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        cls = _classify_source(url)
        out.append(
            {
                "url": url,
                "title": title,
                "snippet": snippet,
                "level": cls["level"],
                "badge": cls["badge"],
                "label": cls["label"],
                "trusted": cls["trusted"],
            }
        )
    out.sort(key=lambda x: (x["level"], 0 if x.get("snippet") else 1))
    return out[:5]

# --------------------------------------------------------------- You.com calls


def _research(prompt: str, effort: str = "standard", timeout: int = 120) -> dict:
    """POST a prompt to the You.com Research API and return the parsed body."""
    payload = json.dumps({"input": prompt, "research_effort": effort}).encode("utf-8")
    req = urllib.request.Request(
        RESEARCH_URL,
        data=payload,
        headers={
            "X-API-Key": API_KEY,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def call_research(claim: str, context: str = "") -> dict:
    """Grounded verdict via the You.com Research API (slower, higher quality)."""
    data = _research(FACTCHECK_PROMPT.format(claim=claim, context=context))
    output = data.get("output", {}) or {}
    content = output.get("content", "") or ""
    sources = _process_sources(output.get("sources", []) or [])
    verdict, explanation = _parse_verdict(content)
    return {
        "mode": "research",
        "verdict": verdict,
        "explanation": explanation,
        "content": content,
        "sources": sources,
        "has_level0": any(s.get("level") == 0 for s in sources),
    }


def call_mcp(claim: str, context: str = "") -> dict:
    """Grounded verdict via the You.com MCP server (you-research tool).

    The MCP HTTP endpoint is stateless here (no session id returned on
    initialize), so a single tools/call request is enough. Responses come back
    as JSON, but we also tolerate an SSE-framed body just in case.
    """
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "you-research",
                "arguments": {
                    "input": FACTCHECK_PROMPT.format(claim=claim, context=context),
                    "research_effort": "standard",
                },
            },
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        MCP_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8")

    envelope = _parse_jsonrpc(raw)
    if "error" in envelope:
        raise RuntimeError(envelope["error"].get("message", "MCP error"))

    result = envelope.get("result", {}) or {}
    parts = result.get("content", []) or []
    content = "\n".join(p.get("text", "") for p in parts if p.get("type") == "text") or ""
    if result.get("isError"):
        raise RuntimeError(content or "MCP tool returned an error")

    verdict, explanation = _parse_verdict(content)
    sources = _process_sources(_sources_from_markdown(content))
    return {
        "mode": "mcp",
        "verdict": verdict,
        "explanation": explanation,
        "content": content,
        "sources": sources,
        "has_level0": any(s.get("level") == 0 for s in sources),
    }


def call_search(claim: str, context: str = "") -> dict:
    """Fast evidence via the You.com Web Search API (no synthesized verdict)."""
    qs = urllib.parse.urlencode({"query": claim[:400], "count": 8})
    req = urllib.request.Request(
        f"{SEARCH_URL}?{qs}",
        headers={"X-API-Key": API_KEY, "User-Agent": USER_AGENT},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    # Search API shape: {"results": {"web": [...]}}; fall back to older shapes.
    block = data.get("results", data)
    results = block.get("web", []) if isinstance(block, dict) else []
    if not results:
        results = data.get("web", []) or []

    sources = _process_sources(results)
    snippets = [s["snippet"] for s in sources if s.get("snippet")][:2]
    return {
        "mode": "search",
        "verdict": "UNVERIFIED",
        "explanation": " ".join(snippets)[:280] or "See sources below.",
        "content": "\n".join(f"- {s}" for s in snippets),
        "sources": sources,
        "has_level0": any(s.get("level") == 0 for s in sources),
    }


def extract_claims(transcript: str, title: str, limit: int) -> list:
    """Ask You.com to pick the most check-worthy claims out of a transcript."""
    prompt = EXTRACT_PROMPT.format(limit=limit, title=title or "Untitled", transcript=transcript[:12000])
    data = _research(prompt, timeout=150)
    content = (data.get("output", {}) or {}).get("content", "") or ""
    claims = _parse_json_array(content)
    out = []
    for c in claims[:limit]:
        if not isinstance(c, dict):
            continue
        text = str(c.get("claim") or c.get("text") or "").strip()
        if len(text) < 15:
            continue
        try:
            start = float(c.get("start") or c.get("time") or 0)
        except (TypeError, ValueError):
            start = 0.0
        out.append({"claim": text, "start": start})
    return out


# ------------------------------------------------------------------- parsing


def _parse_verdict(content: str):
    """Pull the leading 'VERDICT: X' line out of the model's answer."""
    verdict = "UNVERIFIED"
    explanation = content.strip()
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        # Models like to dress the line up as "**VERDICT: FALSE**".
        upper = line.lstrip("*#>_- ").upper()
        if upper.startswith("VERDICT"):
            for cand in ("MISLEADING", "UNVERIFIED", "FALSE", "TRUE"):
                if cand in upper:
                    verdict = cand
                    break
            # Everything after the verdict line becomes the explanation.
            rest = content.split(line, 1)[-1].strip()
            explanation = rest or explanation
            break
    explanation = " ".join(explanation.split())
    explanation = re.sub(r"\s*\[\d+\]", "", explanation)
    explanation = explanation.lstrip("*#>_- ").strip()
    if len(explanation) > 400:
        explanation = explanation[:397] + "..."
    return verdict, explanation


def _parse_jsonrpc(raw: str) -> dict:
    """Return the JSON-RPC envelope from a JSON or SSE-framed response body."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    envelope = {}
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            chunk = line[5:].strip()
            try:
                envelope = json.loads(chunk)
            except json.JSONDecodeError:
                continue
    return envelope


def _parse_json_array(content: str) -> list:
    """Find the first JSON array in a model answer that may be fenced or chatty."""
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", content, re.S)
    candidates = [fenced.group(1)] if fenced else []
    start = content.find("[")
    end = content.rfind("]")
    if start != -1 and end > start:
        candidates.append(content[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            continue
    return []


def _sources_from_markdown(content: str) -> list:
    """Extract bare URLs from the research markdown as source links."""
    seen, out = set(), []
    for url in re.findall(r"https?://[^\s\)\]\">]+", content):
        url = url.rstrip(".,);]")
        if url not in seen:
            seen.add(url)
            out.append({"url": url, "title": url})
        if len(out) >= 5:
            break
    return out


def _context_line(context: dict) -> str:
    """Give the model the video's title/channel so pronouns resolve."""
    if not isinstance(context, dict):
        return ""
    title = str(context.get("title") or "").strip()[:200]
    channel = str(context.get("channel") or "").strip()[:100]
    if not title and not channel:
        return ""
    bits = []
    if title:
        bits.append(f'video titled "{title}"')
    if channel:
        bits.append(f"published by {channel}")
    return "Context: the statement was spoken in a " + ", ".join(bits) + ".\n"


# -------------------------------------------------------------------- server


class Handler(BaseHTTPRequestHandler):
    server_version = "TruthPanel/2.0"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            return self._send_json(200, {"ok": True, "has_key": bool(API_KEY)})
        return self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/api/factcheck", "/api/extract"):
            return self._send_json(404, {"error": "not found"})
        if not API_KEY:
            return self._send_json(
                500, {"error": "YDC_API_KEY is not set on the server. Export it and restart."}
            )
        try:
            payload = self._read_json()
        except (ValueError, json.JSONDecodeError):
            return self._send_json(400, {"error": "invalid JSON body"})

        try:
            if path == "/api/factcheck":
                return self._factcheck(payload)
            return self._extract(payload)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            return self._send_json(502, {"error": f"You.com API error {e.code}", "detail": detail})
        except urllib.error.URLError as e:
            return self._send_json(502, {"error": f"network error: {e.reason}"})
        except Exception as e:  # noqa: BLE001 — surface anything to the panel
            return self._send_json(500, {"error": str(e)})

    def _factcheck(self, payload):
        claim = (payload.get("claim") or "").strip()
        mode = (payload.get("mode") or "research").strip().lower()
        if not claim:
            return self._send_json(400, {"error": "missing 'claim'"})

        key = (mode, claim.lower())
        with _cache_lock:
            hit = _cache.get(key)
        if hit:
            return self._send_json(200, dict(hit, cached=True))

        context = _context_line(payload.get("context"))
        dispatch = {"search": call_search, "mcp": call_mcp, "research": call_research}
        result = dispatch.get(mode, call_research)(claim, context)
        result["claim"] = claim
        with _cache_lock:
            _cache[key] = result
        return self._send_json(200, result)

    def _extract(self, payload):
        transcript = (payload.get("transcript") or "").strip()
        if not transcript:
            return self._send_json(400, {"error": "missing 'transcript'"})
        limit = max(1, min(int(payload.get("limit") or 8), 20))
        claims = extract_claims(transcript, payload.get("title") or "", limit)
        return self._send_json(200, {"claims": claims})

    def log_message(self, fmt, *args):
        sys.stderr.write("  " + (fmt % args) + "\n")


def main():
    if not API_KEY:
        print("\n  WARNING: YDC_API_KEY is not set — fact-checks will fail.")
        print("  Set it with:  export YDC_API_KEY='your-key'  then restart.\n")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"  YouTube Truth Panel backend →  http://127.0.0.1:{PORT}")
    print("  Load the extension, open a YouTube video, and hit Scan.")
    print("  Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
