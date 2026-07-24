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
    "substance, not the wording. Your response MUST begin with exactly one line "
    "of the form 'VERDICT: X' where X is one of TRUE, FALSE, MISLEADING, or "
    "UNVERIFIED. On the next line give one concise sentence (max 35 words) "
    "explaining the verdict. Base the verdict only on reliable sources. Use "
    "UNVERIFIED when the statement is opinion, prediction, or not checkable.\n"
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
    sources = output.get("sources", []) or []
    verdict, explanation = _parse_verdict(content)
    return {
        "mode": "research",
        "verdict": verdict,
        "explanation": explanation,
        "content": content,
        "sources": [
            {"url": s.get("url", ""), "title": s.get("title", "") or s.get("url", "")}
            for s in sources[:5]
        ],
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
    return {
        "mode": "mcp",
        "verdict": verdict,
        "explanation": explanation,
        "content": content,
        "sources": _sources_from_markdown(content),
    }


def call_search(claim: str, context: str = "") -> dict:
    """Fast evidence via the You.com Web Search API (no synthesized verdict)."""
    qs = urllib.parse.urlencode({"query": claim[:400], "count": 5})
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

    snippets, sources = [], []
    for r in results[:5]:
        sources.append({"url": r.get("url", ""), "title": r.get("title", "") or r.get("url", "")})
        snips = r.get("snippets") or ([r["description"]] if r.get("description") else [])
        snippets.extend(snips[:1])
    return {
        "mode": "search",
        "verdict": "UNVERIFIED",
        "explanation": " ".join(snippets[:2])[:280] or "See sources below.",
        "content": "\n".join(f"- {s}" for s in snippets),
        "sources": sources,
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
