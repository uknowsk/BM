"""Tiny OpenAI-compatible client for the local LM Studio server."""
import json
import re
import sys
import time
from typing import Optional

import requests

URL = "http://localhost:1234/v1/chat/completions"
MODEL = "qwen/qwen3-8b"
TIMEOUT_S = 300
VISION_MODEL = "qwen2.5-vl-7b-instruct"
VISION_TIMEOUT_S = 600


def _strip(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"<think>.*$", "", text, flags=re.S)  # unterminated think block
    return re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip(), flags=re.I)


def parse_json(text: str):
    """Parse JSON from an LLM reply (fences/think stripped); None if impossible."""
    text = _strip(text)
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"(\[.*\]|\{.*\})", text, flags=re.S)  # outermost array/object inside prose
    if m:
        try:
            return json.loads(m.group(1))
        except ValueError:
            pass
    return None


def chat_json(prompt: str):
    """Send prompt, return parsed JSON, or None (with stderr log) on any failure."""
    body = {"model": MODEL, "temperature": 0.1,
            "messages": [{"role": "user", "content": prompt + " /no_think"}]}
    try:
        r = requests.post(URL, json=body, timeout=TIMEOUT_S)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
    except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
        print(f"[llm] request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    parsed = parse_json(content)
    if parsed is None:
        print(f"[llm] unparseable reply: {content[:200]!r}", file=sys.stderr)
    return parsed


def chat_vision(prompt: str, png_b64: str, timeout: int = VISION_TIMEOUT_S) -> str:
    """Send prompt + base64 PNG to the vision model; return plain text, or '' (with stderr log) on failure."""
    body = {"model": VISION_MODEL, "temperature": 0, "max_tokens": 3000,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png_b64}"}}]}]}
    for attempt in (1, 2):  # LM Studio may 400 while swapping models in; retry once
        try:
            r = requests.post(URL, json=body, timeout=timeout)
            r.raise_for_status()
            return str(r.json()["choices"][0]["message"]["content"] or "").strip()
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            print(f"[llm] vision request failed (try {attempt}): {type(exc).__name__}: {exc}", file=sys.stderr)
            if attempt == 1:
                time.sleep(5)
    return ""


EMBED_URL = "http://localhost:1234/v1/embeddings"
MODELS_URL = "http://localhost:1234/v1/models"
EMBED_MODELS = ("text-embedding-bge-m3", "text-embedding-qwen3-embedding-0.6b")  # multilingual first, then fallback
EMBED_MODEL = ""  # id of the embedding model that served the last successful embed() call
_embed_model_cache: Optional[str] = None


def _pick_embed_model() -> Optional[str]:
    """First EMBED_MODELS id that LM Studio lists in GET /v1/models (cached per process); None if unreachable."""
    global _embed_model_cache
    if _embed_model_cache:
        return _embed_model_cache
    try:
        r = requests.get(MODELS_URL, timeout=(3, 10))
        r.raise_for_status()
        ids = {m.get("id") for m in r.json().get("data", [])}
    except (requests.RequestException, ValueError, AttributeError) as exc:
        print(f"[llm] model list failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    _embed_model_cache = next((m for m in EMBED_MODELS if m in ids), None)
    return _embed_model_cache


def embed(texts: list[str], model: Optional[str] = None) -> Optional[list[list[float]]]:
    """Embed `texts` with the local multilingual embedding model (one batched request).
    Returns one vector per text, or None (with stderr log) when LM Studio / the model is unavailable."""
    global EMBED_MODEL
    if not texts:
        return []
    model = model or _pick_embed_model()
    if not model:
        return None
    vectors = None
    for attempt in (1, 2, 3):  # LM Studio answers 400 while it (re)loads an unloaded embedding model: wait and retry
        try:
            r = requests.post(EMBED_URL, json={"model": model, "input": list(texts)}, timeout=(3, 120))
            if r.status_code == 400 and attempt < 3:
                time.sleep(4)
                continue
            r.raise_for_status()
            rows = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
            vectors = [list(map(float, d["embedding"])) for d in rows]
            break
        except (requests.RequestException, KeyError, ValueError, TypeError) as exc:
            print(f"[llm] embedding request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return None
    if vectors is None:
        return None
    if len(vectors) != len(texts):
        return None
    EMBED_MODEL = model
    return vectors
