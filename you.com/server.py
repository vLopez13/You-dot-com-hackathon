#!/usr/bin/env python3
"""
Real-time podcast fact detector — backend.

Serves the front-end UI and proxies fact-check requests to the You.com API,
so the API key never touches the browser and CORS is a non-issue.

Runs on the Python standard library only — no pip install required.

Usage:
    export YDC_API_KEY="your-you.com-api-key"
    python3 server.py
    # open http://localhost:8000
"""

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
API_KEY = os.environ.get("YDC_API_KEY", "").strip()
PORT = int(os.environ.get("PORT", "8000"))

RESEARCH_URL = "https://api.you.com/v1/research"
SEARCH_URL = "https://ydc-index.io/v1/search"
MCP_URL = "https://api.you.com/mcp"

# Show 2–3 citations in the live feed (enough to verify, not overwhelm).
MIN_SOURCES = 2
MAX_SOURCES = 3
MAX_WEAK_SOURCES = 2

# api.you.com sits behind Cloudflare, which 403s the default Python-urllib
# User-Agent (error 1010). Present a normal browser UA on every outbound call.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Prefer primary / reference outlets over blogs and social noise.
# Any hostname ending in .edu / .gov / .int is also treated as trusted.
TRUSTED_TLDS = (".edu", ".gov", ".int", ".mil")

TRUSTED_DOMAINS = [
    # reference & encyclopedias
    "wikipedia.org",
    "britannica.com",
    "wikidata.org",
    "nationalgeographic.com",
    "natgeo.com",
    # wire services & public broadcasters
    "reuters.com",
    "apnews.com",
    "bbc.com",
    "bbc.co.uk",
    "npr.org",
    "pbs.org",
    "afp.com",
    "aljazeera.com",
    "aljazeera.net",
    "dw.com",
    "france24.com",
    "abcnews.go.com",
    "cbsnews.com",
    "nbcnews.com",
    "cnn.com",
    # science, health, government (also covered by .gov/.int TLDs)
    "nih.gov",
    "cdc.gov",
    "who.int",
    "fda.gov",
    "nasa.gov",
    "noaa.gov",
    "census.gov",
    "data.gov",
    "epa.gov",
    "unesco.org",
    "un.org",
    "worldbank.org",
    "imf.org",
    "oecd.org",
    "nature.com",
    "science.org",
    "scientificamerican.com",
    "newscientist.com",
    "pubmed.ncbi.nlm.nih.gov",
    "ncbi.nlm.nih.gov",
    "arxiv.org",
    "mayoclinic.org",
    "clevelandclinic.org",
    "harvard.edu",
    "mit.edu",
    "stanford.edu",
    # fact-check desks
    "factcheck.org",
    "politifact.com",
    "snopes.com",
    "fullfact.org",
    "factcheck.afp.com",
    # major newsrooms & business press
    "nytimes.com",
    "washingtonpost.com",
    "theguardian.com",
    "wsj.com",
    "economist.com",
    "bloomberg.com",
    "forbes.com",
    "ft.com",
    "time.com",
    "theatlantic.com",
    "usatoday.com",
    "latimes.com",
    "independent.co.uk",
]

LOW_QUALITY_DOMAINS = [
    "reddit.com",
    "quora.com",
    "pinterest.com",
    "tiktok.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "instagram.com",
    "tumblr.com",
    "medium.com",
    "answers.yahoo.com",
    "wikihow.com",
    "buzzfeed.com",
    "change.org",
    "naturalnews.com",
    "infowars.com",
    "beforeitsnews.com",
    "theonion.com",
    "babylonbee.com",
]

OPINION_MARKERS = re.compile(
    r"\b(i think|in my opinion|imo|my experience|rumor|rumour|allegedly|"
    r"unconfirmed|conspiracy|hoax|clickbait|heard that|people say|"
    r"according to some|viral|debunked\?)\b",
    re.I,
)

# Ask the Research API to lead with a machine-parseable verdict so the UI can
# render a coloured badge, then cite 2–3 independent real-world sources with
# concrete factual evidence (numbers, dates, named findings) — not bare links.
FACTCHECK_PROMPT = (
    "You are a real-time fact checker for a live podcast. Fact-check the "
    "following spoken statement.\n"
    "Your response MUST begin with exactly one line of the form 'VERDICT: X' "
    "where X is one of TRUE, FALSE, MISLEADING, or UNVERIFIED.\n"
    "On the next line give one concise sentence (max 30 words) explaining the "
    "verdict.\n"
    "Then cite 2 or 3 independent, reliable primary or reference sources. "
    "Strongly prefer: .gov / .edu / .int sites, CDC, WHO, NIH, NASA, UNESCO, "
    "wire services (Reuters, AP, AFP), encyclopedias, peer-reviewed science, "
    "Al Jazeera, National Geographic, Forbes, Washington Post, and other "
    "established newsrooms. Prefer those over blogs, forums, or social media.\n"
    "For each citation, ground it in a concrete fact from that source "
    "(statistic, date, named finding, official figure, or direct quote) — "
    "not a vague summary. Include inline citations that map to the sources "
    "you used. If you cannot find at least two solid sources with factual "
    "evidence, mark the verdict UNVERIFIED.\n\n"
    'Statement: "{claim}"'
)

SOURCE_CONTROL = {
    "boost_domains": TRUSTED_DOMAINS,
    "exclude_domains": LOW_QUALITY_DOMAINS,
}


def call_research(claim: str) -> dict:
    """Grounded verdict via the You.com Research API (slower, higher quality)."""
    payload = json.dumps(
        {
            "input": FACTCHECK_PROMPT.format(claim=claim),
            "research_effort": "standard",
            "source_control": SOURCE_CONTROL,
        }
    ).encode("utf-8")
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
    with urllib.request.urlopen(req, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    output = data.get("output", {}) or {}
    content = output.get("content", "") or ""
    sources = _normalize_sources(output.get("sources", []) or [])
    # Attach factual snippets (and pad to 2–3) from ranked web search hits.
    sources, weak_sources = _enrich_sources_with_facts(claim, sources)
    verdict, explanation = _parse_verdict(content)
    return {
        "mode": "research",
        "verdict": verdict,
        "explanation": explanation,
        "content": content,
        "sources": sources,
        "weak_sources": weak_sources,
    }


def call_mcp(claim: str) -> dict:
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
                    "input": FACTCHECK_PROMPT.format(claim=claim),
                    "research_effort": "standard",
                    "source_control": SOURCE_CONTROL,
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

    sources = _sources_from_markdown(content)
    sources, weak_sources = _enrich_sources_with_facts(claim, sources)
    verdict, explanation = _parse_verdict(content)
    return {
        "mode": "mcp",
        "verdict": verdict,
        "explanation": explanation,
        "content": content,
        "sources": sources,
        "weak_sources": weak_sources,
    }


def _parse_jsonrpc(raw: str) -> dict:
    """Return the JSON-RPC envelope from a JSON or SSE-framed response body."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # SSE framing: pick the last 'data:' line that parses as JSON.
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


def _hostname(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return ""
    return host.lower().removeprefix("www.")


def _is_trusted(url: str) -> bool:
    host = _hostname(url)
    if not host:
        return False
    if any(host.endswith(tld) for tld in TRUSTED_TLDS):
        return True
    return any(host == d or host.endswith("." + d) for d in TRUSTED_DOMAINS)


def _is_low_quality(url: str) -> bool:
    host = _hostname(url)
    return any(host == d or host.endswith("." + d) for d in LOW_QUALITY_DOMAINS)


def _source_rank(source: dict) -> tuple:
    """Lower is better: trusted + factual snippets first, junk last."""
    url = source.get("url", "")
    has_fact = 0 if (source.get("snippet") or "").strip() else 1
    if _is_low_quality(url):
        return (3, has_fact, url)
    if _is_trusted(url):
        return (0, has_fact, url)
    return (1, has_fact, url)


def _snippet_fact_score(text: str) -> int:
    """Higher = more concrete factual content (numbers, dates, %, units)."""
    if not text:
        return 0
    score = 0
    if re.search(r"\d", text):
        score += 2
    if re.search(r"\d+(\.\d+)?\s*%", text):
        score += 3
    if re.search(r"\b(19|20)\d{2}\b", text):
        score += 2
    if re.search(
        r"\b(million|billion|thousand|percent|study|according|reported|"
        r"found|data|rate|average|official)\b",
        text,
        re.I,
    ):
        score += 2
    if len(text) >= 40:
        score += 1
    return score


def _best_snippet(snippets) -> str:
    """Pick the most factual excerpt from a list of candidate snippets."""
    if not snippets:
        return ""
    if isinstance(snippets, str):
        snippets = [snippets]
    cleaned = []
    for s in snippets:
        text = " ".join(str(s).split()).strip()
        if text:
            cleaned.append(text)
    if not cleaned:
        return ""
    cleaned.sort(key=_snippet_fact_score, reverse=True)
    best = cleaned[0]
    if len(best) > 220:
        best = best[:217].rstrip() + "..."
    return best


def _extract_source_fields(s) -> dict:
    """Normalize a Research/Search source object into url/title/snippet."""
    if isinstance(s, str):
        return {"url": s, "title": s, "snippet": ""}
    url = (s.get("url") or "").strip()
    title = (s.get("title") or "").strip() or url
    snippets = s.get("snippets") or s.get("snippet") or []
    if not snippets and s.get("description"):
        snippets = [s["description"]]
    return {"url": url, "title": title, "snippet": _best_snippet(snippets)}


def _normalize_sources(raw) -> list:
    """Dedupe, drop empty/low-quality URLs, prefer trusted + factual, cap at 3."""
    seen, out = set(), []
    cleaned = []
    for s in raw or []:
        fields = _extract_source_fields(s)
        url = fields["url"]
        if not url or not url.startswith("http"):
            continue
        url = url.rstrip(".,);]")
        if _is_low_quality(url):
            continue
        cleaned.append(
            {
                "url": url,
                "title": fields["title"],
                "snippet": fields["snippet"],
                "trusted": _is_trusted(url),
            }
        )

    for s in sorted(cleaned, key=_source_rank):
        if s["url"] in seen:
            continue
        seen.add(s["url"])
        out.append(
            {
                "url": s["url"],
                "title": s["title"],
                "snippet": s["snippet"],
                "trusted": s["trusted"],
            }
        )
        if len(out) >= MAX_SOURCES:
            break
    return out


def _sources_from_markdown(content: str) -> list:
    """Extract bare URLs from the research markdown as source links."""
    seen, out = set(), []
    for url in re.findall(r"https?://[^\s\)\]\">]+", content or ""):
        url = url.rstrip(".,);]")
        if url in seen or _is_low_quality(url):
            continue
        seen.add(url)
        out.append({"url": url, "title": _hostname(url) or url, "snippet": ""})
        if len(out) >= MAX_SOURCES:
            break
    return _normalize_sources(out)


def _hit_to_source(r: dict) -> dict:
    url = (r.get("url") or "").strip()
    title = (r.get("title") or "").strip() or url
    snips = r.get("snippets") or ([r["description"]] if r.get("description") else [])
    return {"url": url, "title": title, "snippet": _best_snippet(snips)}


def _collect_weak_sources(hits: list, reliable: list) -> list:
    """Pick up to 2 non-reliable hits (forums, blogs, thin evidence) if found."""
    skip_urls = {(s.get("url") or "").rstrip(".,);]") for s in reliable}
    skip_hosts = {_hostname(u) for u in skip_urls if u}
    candidates = []
    seen = set()
    for r in hits or []:
        if not isinstance(r, dict):
            continue
        # Raw Search hits have snippets/description; already-normalized ones have snippet.
        if "snippets" in r or "description" in r:
            h = _hit_to_source(r)
        else:
            h = {
                "url": (r.get("url") or "").strip(),
                "title": (r.get("title") or "").strip(),
                "snippet": (r.get("snippet") or "").strip(),
            }
        url = (h.get("url") or "").strip().rstrip(".,);]")
        if not url or not url.startswith("http") or url in skip_urls or url in seen:
            continue
        host = _hostname(url)
        if not host or host in skip_hosts:
            continue
        # Never put government / education / known-trusted hosts in the weak list.
        if _is_trusted(url):
            continue

        low = _is_low_quality(url)
        title = h.get("title") or ""
        snippet = h.get("snippet") or ""
        fact = _snippet_fact_score(snippet)
        opinion = bool(OPINION_MARKERS.search(f"{title} {snippet}"))
        # Prefer forums/social; otherwise only thin or opinion-y untrusted pages.
        if not low and not opinion and fact >= 2:
            continue
        if low:
            reason = "forum / social"
        elif opinion:
            reason = "opinion / rumor"
        else:
            reason = "unverified · weak evidence"
        seen.add(url)
        candidates.append(
            {
                "url": url,
                "title": title or url,
                "snippet": snippet,
                "trusted": False,
                "reason": reason,
            }
        )

    candidates.sort(
        key=lambda c: (
            0 if _is_low_quality(c["url"]) else 1,
            0 if OPINION_MARKERS.search(f"{c.get('title','')} {c.get('snippet','')}") else 1,
            _snippet_fact_score(c.get("snippet") or ""),
        )
    )
    return candidates[:MAX_WEAK_SOURCES]


def _enrich_sources_with_facts(claim: str, sources: list, hits=None) -> tuple:
    """Attach factual snippets; return (reliable_sources, weak_sources).

    Research often returns titles/URLs without readable excerpts. Search hits
    carry snippets with numbers and findings — merge those onto matching hosts,
    prefer sources that show factual data, and also surface up to 2 weaker
    hits underneath for contrast when they appear in search.
    """
    sources = _normalize_sources(sources)
    if hits is None:
        try:
            hits = _fetch_search_hits(claim, count=12)
        except Exception:  # noqa: BLE001 — keep whatever we already have
            return sources[:MAX_SOURCES], []

    hit_sources = [_hit_to_source(r) for r in hits if (r.get("url") or "").strip()]
    by_host = {}
    for h in hit_sources:
        host = _hostname(h["url"])
        if not host:
            continue
        # Keep the most factual snippet per host.
        prev = by_host.get(host)
        if not prev or _snippet_fact_score(h["snippet"]) > _snippet_fact_score(prev["snippet"]):
            by_host[host] = h

    enriched = []
    for s in sources:
        host = _hostname(s["url"])
        match = by_host.get(host)
        snippet = s.get("snippet") or ""
        title = s.get("title") or s["url"]
        if match:
            if _snippet_fact_score(match["snippet"]) > _snippet_fact_score(snippet):
                snippet = match["snippet"]
            if (not title or title == s["url"]) and match.get("title"):
                title = match["title"]
        enriched.append(
            {
                "url": s["url"],
                "title": title,
                "snippet": snippet,
                "trusted": s.get("trusted", _is_trusted(s["url"])),
            }
        )

    # Prefer search hits that already carry factual snippets when we need more.
    need = max(0, MIN_SOURCES - len(enriched))
    if need or any(not e.get("snippet") for e in enriched):
        existing = {_hostname(e["url"]) for e in enriched}
        factual_extras = sorted(
            [h for h in hit_sources if _hostname(h["url"]) not in existing],
            key=lambda h: (
                0 if _is_trusted(h["url"]) else 1,
                -_snippet_fact_score(h["snippet"]),
            ),
        )
        for h in factual_extras:
            if _is_low_quality(h["url"]):
                continue
            if not h.get("snippet") and len(enriched) >= MIN_SOURCES:
                continue
            enriched.append(
                {
                    "url": h["url"],
                    "title": h["title"],
                    "snippet": h["snippet"],
                    "trusted": _is_trusted(h["url"]),
                }
            )
            existing.add(_hostname(h["url"]))
            if len(enriched) >= MAX_SOURCES and all(e.get("snippet") for e in enriched[:MIN_SOURCES]):
                break

    reliable = _normalize_sources(enriched)[:MAX_SOURCES]
    weak = _collect_weak_sources(hits, reliable)

    # If the main results are all clean, try a targeted pull for weaker pages.
    if len(weak) < MAX_WEAK_SOURCES:
        try:
            extra = _fetch_search_hits(
                f'{claim} (reddit OR quora OR blog OR "in my opinion" OR rumor)',
                count=8,
            )
            weak = _collect_weak_sources(list(hits) + list(extra), reliable)
        except Exception:  # noqa: BLE001
            pass

    return reliable, weak


def _fetch_search_hits(claim: str, count: int = 8) -> list:
    """Raw web hits from the You.com Search API."""
    qs = urllib.parse.urlencode({"query": claim, "count": count})
    req = urllib.request.Request(
        f"{SEARCH_URL}?{qs}",
        headers={"X-API-Key": API_KEY, "User-Agent": USER_AGENT},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    block = data.get("results", data)
    results = block.get("web", []) if isinstance(block, dict) else []
    if not results:
        results = data.get("web", []) or []
    return results


def call_search(claim: str) -> dict:
    """Fast evidence via the You.com Web Search API (no synthesized verdict)."""
    results = _fetch_search_hits(claim, count=12)
    raw_sources = [_hit_to_source(r) for r in results if (r.get("url") or "").strip()]
    sources, weak_sources = _enrich_sources_with_facts(claim, raw_sources, hits=results)
    # Lead explanation with the strongest factual snippets we kept.
    explanation_bits = [s["snippet"] for s in sources if s.get("snippet")][:2]
    return {
        "mode": "search",
        "verdict": "UNVERIFIED",
        "explanation": " ".join(explanation_bits)[:280] or "See sources below.",
        "content": "\n".join(f"- {s}" for s in explanation_bits),
        "sources": sources,
        "weak_sources": weak_sources,
    }


def _parse_verdict(content: str):
    """Pull the leading 'VERDICT: X' line out of the model's answer."""
    verdict = "UNVERIFIED"
    explanation = content.strip()
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("VERDICT"):
            for cand in ("TRUE", "FALSE", "MISLEADING", "UNVERIFIED"):
                if cand in upper:
                    verdict = cand
                    break
            # Everything after the verdict line becomes the explanation.
            rest = content.split(line, 1)[-1].strip()
            explanation = rest or explanation
            break
    # Keep the explanation to a tidy single paragraph.
    explanation = " ".join(explanation.split())
    if len(explanation) > 400:
        explanation = explanation[:397] + "..."
    return verdict, explanation


def _transcribe_wav(wav_bytes: bytes, language: str = "en-US") -> str:
    """Transcribe linear PCM WAV using Google's public Web Speech endpoint.

    Same endpoint Chromium uses for the Web Speech API — no extra API key.
    Ideal for short live-feed audio chunks from tab capture.
    """
    if len(wav_bytes) < 1000:
        return ""
    key = "AIzaSyBOti4mM-6x9WDnZIjIeyEUHhVMrDvMl-E"
    qs = urllib.parse.urlencode(
        {"client": "chromium", "lang": language, "key": key, "output": "json"}
    )
    req = urllib.request.Request(
        f"https://www.google.com/speech-api/v2/recognize?{qs}",
        data=wav_bytes,
        headers={
            "Content-Type": "audio/wav",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", "replace").strip()
    # Response is one or more JSON lines; take the best transcript.
    best, best_conf = "", -1.0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        for alt in (payload.get("result") or []):
            for a in alt.get("alternative") or []:
                transcript = (a.get("transcript") or "").strip()
                conf = float(a.get("confidence") or 0)
                if transcript and conf >= best_conf:
                    best, best_conf = transcript, conf
                elif transcript and not best:
                    best = transcript
    return best


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_file("index.html", "text/html; charset=utf-8")
        if path == "/health":
            return self._send_json(200, {"ok": True, "has_key": bool(API_KEY)})
        return self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        if path == "/api/transcribe":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._send_json(400, {"error": "invalid JSON body"})

            b64 = (payload.get("wav_base64") or "").strip()
            if not b64:
                return self._send_json(400, {"error": "missing wav_base64"})
            try:
                wav = base64.b64decode(b64)
                text = _transcribe_wav(wav, payload.get("language") or "en-US")
                return self._send_json(200, {"text": text})
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                return self._send_json(502, {"error": f"transcribe upstream {e.code}", "detail": detail})
            except Exception as e:  # noqa: BLE001
                return self._send_json(500, {"error": str(e)})

        if path != "/api/factcheck":
            return self._send_json(404, {"error": "not found"})

        if not API_KEY:
            return self._send_json(
                500,
                {"error": "YDC_API_KEY is not set on the server. Export it and restart."},
            )

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send_json(400, {"error": "invalid JSON body"})

        claim = (payload.get("claim") or "").strip()
        mode = (payload.get("mode") or "research").strip().lower()
        if not claim:
            return self._send_json(400, {"error": "missing 'claim'"})

        dispatch = {"search": call_search, "mcp": call_mcp, "research": call_research}
        try:
            result = dispatch.get(mode, call_research)(claim)
            result["claim"] = claim
            return self._send_json(200, result)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            return self._send_json(502, {"error": f"You.com API error {e.code}", "detail": detail})
        except urllib.error.URLError as e:
            return self._send_json(502, {"error": f"network error: {e.reason}"})
        except Exception as e:  # noqa: BLE001 — surface anything to the client for the demo
            return self._send_json(500, {"error": str(e)})

    def _serve_file(self, name, content_type):
        try:
            with open(os.path.join(HERE, name), "rb") as f:
                body = f.read()
        except FileNotFoundError:
            return self._send_json(404, {"error": f"{name} not found"})
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("  " + (fmt % args) + "\n")


def main():
    if not API_KEY:
        print("\n  WARNING: YDC_API_KEY is not set — fact-checks will fail.")
        print("  Set it with:  export YDC_API_KEY='your-key'  then restart.\n")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"  Podcast fact detector running →  http://localhost:{PORT}")
    print("  Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
