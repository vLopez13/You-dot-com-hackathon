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
    GET  /api/balance    -> remaining You.com credits + calls made this run
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

API_KEY = os.environ.get("YDC_API_KEY", "").strip()
PORT = int(os.environ.get("PORT", "8765"))

RESEARCH_URL = "https://api.you.com/v1/research"
MCP_URL = "https://api.you.com/mcp"
BALANCE_URL = "https://api.you.com/v1/billing/account_balance"

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
    "substance, not the wording. Base the verdict only on reliable sources. Use "
    "UNVERIFIED when the statement is opinion, prediction, or not checkable.\n\n"
    "Rank your evidence using this trust hierarchy:\n"
    "  Level 0 (.edu, .gov, peer-reviewed science, WHO/CDC/NASA) — absolute truth\n"
    "  Level 1 (Reuters, AP, BBC, WSJ, Pew, World Bank) — high trust\n"
    "  Level 2 (industry and tech media, corporate newsrooms) — moderate trust\n"
    "  Level 3/4 (social media, blogs, tabloids, conspiracy sites) — do not weight highly\n"
    "A Level 0 source that directly settles a quantitative claim overrides lower "
    "levels. Never rest a TRUE or FALSE verdict on Level 3/4 sources alone.\n\n"
    "Answer in exactly two lines and nothing else:\n"
    "Line 1: 'VERDICT: X (Trust Level: N)' where X is one of TRUE, FALSE, "
    "MISLEADING, UNVERIFIED and N is 0-4 for the best evidence you actually used.\n"
    "Line 2: one plain-English sentence of at most 30 words explaining why.\n\n"
    "Formatting rules for line 2, follow them strictly:\n"
    "- Plain prose only. No markdown, no headings, no '#' characters, no bullet "
    "points, no bold or italics, no quotes around the sentence.\n"
    "- No citation markers of any kind, such as [1] or [[1, 2, 3]].\n"
    "- Do NOT list, name, link or describe your sources — they are displayed "
    "separately. Never write a 'Sources' section.\n"
    "- Lead with the substance (the correct fact or figure), not with phrases "
    "like 'The claim is' or 'According to sources'.\n"
    "- Never write 'according to source(s)' or name outlets in the explanation.\n"
    "- Do not use these characters in the explanation: # * ` {{ }} [ ] \\ / &\n\n"
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

# --------------------------------------------------- source trust classification
# See SOURCE_TRUST.md. Short, editable starting lists — not a complete registry.

LEVEL_NAMES = {
    0: "Institutional / peer-reviewed",
    1: "Major journalism & wire services",
    2: "Secondary / industry media",
    3: "User-generated / opinion",
    4: "High-bias / low-reliability",
}

# Anything unmatched lands here, flagged as unrated so a default is never
# mistaken for a vetted classification.
DEFAULT_LEVEL = 2

TRUST_TLDS = {
    ".gov": 0, ".edu": 0, ".mil": 0, ".int": 0,
    ".gov.uk": 0, ".ac.uk": 0, ".edu.au": 0, ".ac.jp": 0, ".gov.au": 0,
}

TRUST_DOMAINS = {
    # Level 0 — institutional, peer-reviewed, primary
    "pubmed.ncbi.nlm.nih.gov": 0, "ncbi.nlm.nih.gov": 0, "nature.com": 0,
    "science.org": 0, "sciencedirect.com": 0, "arxiv.org": 0, "jstor.org": 0,
    "ieee.org": 0, "ieeexplore.ieee.org": 0, "thelancet.com": 0, "nejm.org": 0,
    "bmj.com": 0, "cell.com": 0, "pnas.org": 0, "springer.com": 0,
    "who.int": 0, "un.org": 0, "europa.eu": 0, "esa.int": 0,
    # Level 1 — wire services, major outlets, statistical repositories
    "reuters.com": 1, "apnews.com": 1, "ap.org": 1, "afp.com": 1,
    "bbc.com": 1, "bbc.co.uk": 1, "wsj.com": 1, "ft.com": 1,
    "economist.com": 1, "npr.org": 1, "pbs.org": 1, "bloomberg.com": 1,
    "nytimes.com": 1, "washingtonpost.com": 1, "theguardian.com": 1,
    "worldbank.org": 1, "oecd.org": 1, "imf.org": 1, "pewresearch.org": 1,
    "ourworldindata.org": 1, "britannica.com": 1,
    # Level 2 — secondary, industry, corporate newsrooms
    "techcrunch.com": 2, "wired.com": 2, "cnbc.com": 2, "forbes.com": 2,
    "theverge.com": 2, "arstechnica.com": 2, "engadget.com": 2,
    "businessinsider.com": 2, "espn.com": 2, "theathletic.com": 2,
    "rollingstone.com": 2, "cnn.com": 2, "nbcnews.com": 2, "cbsnews.com": 2,
    "time.com": 2, "newsweek.com": 2, "snopes.com": 2, "politifact.com": 2,
    "factcheck.org": 2, "apple.com": 2, "blog.google": 2, "microsoft.com": 2,
    # Level 3 — user-generated, self-published, social
    "reddit.com": 3, "medium.com": 3, "substack.com": 3, "quora.com": 3,
    "x.com": 3, "twitter.com": 3, "facebook.com": 3, "instagram.com": 3,
    "tiktok.com": 3, "linkedin.com": 3, "youtube.com": 3, "wikipedia.org": 3,
    "blogspot.com": 3, "wordpress.com": 3, "tumblr.com": 3, "stackexchange.com": 3,
    # Level 4 — tabloid, conspiracy, state propaganda, clickbait
    "dailymail.co.uk": 4, "nationalenquirer.com": 4, "thesun.co.uk": 4,
    "mirror.co.uk": 4, "infowars.com": 4, "naturalnews.com": 4,
    "beforeitsnews.com": 4, "thegatewaypundit.com": 4, "newspunch.com": 4,
    "rt.com": 4, "sputniknews.com": 4, "presstv.ir": 4,
}


def classify_source(url: str):
    """Map a citation URL to a trust level. Returns (level, rated)."""
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower().strip(".")
    except ValueError:
        return DEFAULT_LEVEL, False
    if not host:
        return DEFAULT_LEVEL, False
    if host.startswith("www."):
        host = host[4:]

    # Explicit domains win over TLD rules (e.g. dailymail.co.uk before .uk).
    for domain, level in TRUST_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return level, True
    for tld, level in TRUST_TLDS.items():
        if host.endswith(tld):
            return level, True
    return DEFAULT_LEVEL, False


def rate_sources(sources: list) -> list:
    """Attach a trust level to every source and order them best-first."""
    rated = []
    for s in sources:
        url = s.get("url", "")
        if not url:
            continue
        level, known = classify_source(url)
        rated.append(
            {
                "url": url,
                "title": s.get("title") or url,
                "level": level,
                "level_name": LEVEL_NAMES.get(level, "Unclassified"),
                "rated": known,
            }
        )
    rated.sort(key=lambda s: s["level"])
    return rated


def _apply_trust_rules(verdict: str, sources: list):
    """Enforce the SOURCE_TRUST.md rules that outrank the model's own call."""
    levels = [s["level"] for s in sources]
    if not levels:
        return verdict, None

    best = min(levels)
    if verdict in ("TRUE", "FALSE"):
        if best >= 4:
            return "UNVERIFIED", "Only Level 4 (high-bias / low-reliability) sources — verdict withheld."
        if best >= 3:
            return "UNVERIFIED", "Only Level 3 (user-generated) sources — not a basis for a factual verdict."
        if best == 1 and sum(1 for x in levels if x <= 1) < 2:
            # Noted rather than downgraded — see the caveat in SOURCE_TRUST.md.
            return verdict, "Single Level 1 citation, below the 2-source minimum."
    return verdict, None


_cache = {}
_cache_lock = threading.Lock()
# Calls actually billed this run, so the panel can show spend alongside balance.
_usage = {"calls": 0, "cached": 0}


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
    raw_sources = output.get("sources", []) or []
    verdict, explanation, model_level = _parse_verdict(content)
    sources = rate_sources(
        [{"url": s.get("url", ""), "title": s.get("title", "")} for s in raw_sources]
    )[:5]
    verdict, trust_note = _apply_trust_rules(verdict, sources)
    return {
        "mode": "research",
        "verdict": verdict,
        "explanation": explanation,
        "content": content,
        "sources": sources,
        "trust_note": trust_note,
        "best_level": sources[0]["level"] if sources else None,
        "model_trust_level": model_level,
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

    verdict, explanation, model_level = _parse_verdict(content)
    sources = rate_sources(_sources_from_markdown(content))[:5]
    verdict, trust_note = _apply_trust_rules(verdict, sources)
    return {
        "mode": "mcp",
        "verdict": verdict,
        "explanation": explanation,
        "content": content,
        "sources": sources,
        "trust_note": trust_note,
        "best_level": sources[0]["level"] if sources else None,
        "model_trust_level": model_level,
    }


def call_balance() -> dict:
    """Remaining API credits — GET /v1/billing/account_balance (cents)."""
    req = urllib.request.Request(
        BALANCE_URL,
        headers={"X-API-Key": API_KEY, "Accept": "application/json", "User-Agent": USER_AGENT},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    attrs = ((data.get("data") or {}).get("attributes") or {})
    cents = attrs.get("balance")
    usd = round(cents / 100.0, 2) if isinstance(cents, (int, float)) else None
    return {"balance_cents": cents, "balance_usd": usd}


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


# Where a research answer stops explaining and starts listing sources.
_SOURCE_SECTION = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*(?:sources?|references?|citations?|"
    r"key excerpts?|url)\s*:?\s*(?:\*\*)?\s*$"
)
# The first per-source entry, e.g. "### 1. Apollo 11 | The Planetary Society"
_SOURCE_ENTRY = re.compile(r"(?im)^\s*(?:#{1,6}\s+\d+\.|\*\*URL:\*\*|\*\*Key Excerpts?:\*\*|>\s)")
# Lead-ins that bury the fact behind sourcing language.
_ACCORDING_LEAD = re.compile(
    r"(?i)^\s*(?:according\s+to\s+(?:the\s+)?(?:sources?|source|reports?|data|evidence|"
    r"available\s+evidence|reliable\s+sources?)|based\s+on\s+(?:the\s+)?(?:sources?|evidence|"
    r"available\s+evidence)|as\s+per\s+(?:the\s+)?sources?|"
    r"the\s+claim\s+is\s+(?:true|false|misleading|unverified)|"
    r"this\s+(?:claim|statement)\s+is\s+(?:true|false|misleading|unverified))"
    r"\s*[,:\-–—]?\s*"
)
# Markdown / citation delimiters that should never reach the claim card.
# Keep mid-word / and & (and/or, R&D); only strip them as decoration.
_DELIMS = re.compile(r"[#*`{}[\]\\]+|'{2,}|\"{2,}|(?<!\w)[/&](?!\w)")


def _clean_explanation(raw: str, limit: int = 300) -> str:
    """Turn a research answer into one clean, human sentence.

    The Research API answers in markdown with citation markers and a trailing
    source dump. The panel renders sources itself, so strip all of that and
    keep only the factual prose.
    """
    text = raw or ""

    # 1. Drop any trailing sources / references / excerpt section.
    for pattern in (_SOURCE_SECTION, _SOURCE_ENTRY):
        m = pattern.search(text)
        if m:
            text = text[: m.start()]

    # 2. Remove citation markers: [[1, 2, 3]], [1], [1,2], [^3].
    text = re.sub(r"\s*\[\[[^\]]*\]\]", "", text)
    text = re.sub(r"\s*\[\^?\d+(?:\s*,\s*\d+)*\]", "", text)

    # 3. Flatten markdown: links to their label, then drop the decoration.
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*[-*•+]\s+", "", text)
    text = re.sub(r"(?m)^\s*>\s?", "", text)
    text = text.replace("**", "").replace("__", "").replace("`", "").replace("*", "")

    # 4. Strip leftover delimiter characters (##, [], {}, \, etc.).
    text = _DELIMS.sub(" ", text)

    # 5. Drop "according to sources" / "the claim is true" style lead-ins,
    #    plus leftover heading labels like "Explanation:".
    text = " ".join(text.split())
    text = re.sub(
        r"(?i)^\s*(?:explanation|reasoning|analysis|verdict|answer|summary)\s*[:.\-–—]?\s*",
        "",
        text,
    )
    text = _ACCORDING_LEAD.sub("", text)
    # Also strip those phrases mid-sentence when the model buries them.
    text = re.sub(
        r"(?i)\baccording\s+to\s+(?:the\s+)?(?:sources?|source|reports?|data)\b[,:]?\s*",
        "",
        text,
    )

    # 6. Unwrap leftover quote wrappers around the whole sentence.
    text = text.strip().strip("\"'“”‘’")
    text = " ".join(text.split())

    # 7. Keep it to a couple of sentences, cut on a sentence boundary.
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for sentence in sentences[:2]:
        candidate = (out + " " + sentence).strip()
        if out and len(candidate) > limit:
            break
        out = candidate
    if not out:
        out = text
    if len(out) > limit:
        trimmed = out[:limit].rsplit(" ", 1)[0]
        out = trimmed.rstrip(",;:") + "…"
    return out.strip()


def _parse_verdict(content: str):
    """Pull 'VERDICT: X (Trust Level: N)' out of the model's answer.

    Returns (verdict, explanation, model_trust_level). The trust level is what
    the model claims it used; the server classifies the citations itself.
    """
    verdict = "UNVERIFIED"
    model_level = None
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
            m = re.search(r"TRUST\s*LEVEL\s*[:=]?\s*\[?\s*([0-4])", upper)
            if m:
                model_level = int(m.group(1))
            # Everything after the verdict line becomes the explanation.
            rest = content.split(line, 1)[-1].strip()
            explanation = rest or explanation
            break
    return verdict, _clean_explanation(explanation), model_level


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
        if path == "/api/balance":
            if not API_KEY:
                return self._send_json(500, {"error": "YDC_API_KEY is not set on the server."})
            try:
                result = call_balance()
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                return self._send_json(502, {"error": f"balance API error {e.code}", "detail": detail})
            except Exception as e:  # noqa: BLE001
                return self._send_json(502, {"error": str(e)})
            with _cache_lock:
                result["session"] = dict(_usage)
            return self._send_json(200, result)
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
            with _cache_lock:
                _usage["cached"] += 1
            return self._send_json(200, dict(hit, cached=True))

        context = _context_line(payload.get("context"))
        dispatch = {"mcp": call_mcp, "research": call_research}
        result = dispatch.get(mode, call_research)(claim, context)
        result["claim"] = claim
        with _cache_lock:
            _cache[key] = result
            _usage["calls"] += 1
        return self._send_json(200, result)

    def _extract(self, payload):
        transcript = (payload.get("transcript") or "").strip()
        if not transcript:
            return self._send_json(400, {"error": "missing 'transcript'"})
        limit = max(1, min(int(payload.get("limit") or 8), 20))
        claims = extract_claims(transcript, payload.get("title") or "", limit)
        with _cache_lock:
            _usage["calls"] += 1
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
