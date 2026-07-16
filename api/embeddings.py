import os
import re
import math
import hashlib
from openai import OpenAI

LITELLM_BASE = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000")
EMBED_MODEL  = os.environ.get("EMBED_MODEL", "text-embedding-3-small")
EMBED_DIM    = 384

_client = None

def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=LITELLM_BASE + "/v1",
            api_key=os.environ.get("LITELLM_API_KEY", "sk-1234"),
        )
    return _client


def embed(text: str) -> list:
    try:
        resp = _get_client().embeddings.create(model=EMBED_MODEL, input=text[:8000])
        return resp.data[0].embedding
    except Exception:
        return _fallback_embed(text)


def _fallback_embed(text: str) -> list:
    words = re.findall(r'\w+', text.lower())
    vec = [0.0] * EMBED_DIM
    for word in words:
        h = int(hashlib.md5(word.encode()).hexdigest(), 16)
        idx = h % EMBED_DIM
        vec[idx] += 1.0
    mag = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / mag for x in vec]


def extract_tags(text: str) -> list:
    words = re.findall(r'\b[a-zA-Z]{4,}\b', text.lower())
    stopwords = {
        'this','that','with','from','have','been','will','would','could','should',
        'they','them','their','what','when','where','which','while','about','into',
        'through','during','before','after','above','below','between','each','more',
        'most','other','some','such','than','then','there','these','those',
    }
    freq: dict = {}
    for w in words:
        if w not in stopwords:
            freq[w] = freq.get(w, 0) + 1
    return [w for w, _ in sorted(freq.items(), key=lambda x: -x[1])[:8]]


def summarize(text: str) -> str:
    if len(text) <= 200:
        return text
    try:
        resp = _get_client().chat.completions.create(
            model=os.environ.get("INNER_MODEL", "claude-3-haiku-20240307"),
            messages=[
                {"role": "system", "content": "Summarize the following in one sentence (max 100 words):"},
                {"role": "user", "content": text[:4000]},
            ],
            max_tokens=150,
        )
        return resp.choices[0].message.content or text[:200]
    except Exception:
        return text[:200] + ("..." if len(text) > 200 else "")

