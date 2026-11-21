"""Paths and settings. Every setting can be overridden by an IMSEARCH_* env var.

Defaults target a small Linux server (Ubuntu, 2GB RAM, CPU only, local Ollama):
  - chat:  qwen3:1.7b            (~1.4GB, Q4_K_M)
  - embed: qwen3-embedding:0.6b  (~0.6GB, 1024 dim, multilingual)
  - small LLM context window / fewer context chunks / small embedding batches
imsearch.env.example has blocks for 4GB machines and for a remote OpenAI-compatible API
(さくらの AI Engine: gpt-oss-120b + multilingual-e5-large; then 1GB RAM is enough).
Variables can also be put in `imsearch.env` (KEY=VALUE lines) in the project root
or in the file named by IMSEARCH_ENV_FILE; real environment variables win.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_env_file():
    path = Path(os.environ.get("IMSEARCH_ENV_FILE", ROOT / "imsearch.env"))
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        os.environ.setdefault(k, v.strip().strip("'\""))


_load_env_file()


def _env(name: str, default: str) -> str:
    return os.environ.get(f"IMSEARCH_{name}", default)


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


DATA_DIR = Path(_env("DATA_DIR", str(ROOT / "data")))
CONFIG_DIR = ROOT / "config"

DB_PATH = Path(_env("DB", str(DATA_DIR / "imsearch.db")))
MANUAL_CACHE_DIR = DATA_DIR / "manual"
REPOS_DIR = DATA_DIR / "repos"

MANUAL_BASE = "https://inter-mediator.info/ja/"

REPOS = {
    "code": {
        "url": "https://github.com/INTER-Mediator/INTER-Mediator",
        "dir": REPOS_DIR / "INTER-Mediator",
    },
    "samples": {
        "url": "https://github.com/INTER-Mediator/INTER-Mediator_Samples",
        "dir": REPOS_DIR / "INTER-Mediator_Samples",
        "demo": "https://demo.inter-mediator.com/INTER-Mediator_Samples/",
    },
}

# LLM backend (docs/DESIGN.md §13: keep it switchable)
#   "ollama": native Ollama API (/api/chat, /api/embed)
#   "openai": any OpenAI-compatible server (/v1/chat/completions, /v1/embeddings)
LLM_API = _env("LLM_API", "ollama")
LLM_BASE_URL = _env("LLM_BASE_URL", "http://localhost:11434")
LLM_API_KEY = _env("LLM_API_KEY", "")
CHAT_MODEL = _env("CHAT_MODEL", "qwen3:1.7b")
EMBED_MODEL = _env("EMBED_MODEL", "qwen3-embedding:0.6b")
EMBED_DIM = _int("EMBED_DIM", 1024)
# Embedding models such as qwen3-embedding want an instruction on the *query* side only;
# multilingual-e5 wants "query: " / "passage: " on both sides.
# "auto" picks them by model name; set to "" to disable or to your own text.
EMBED_QUERY_PREFIX = _env("EMBED_QUERY_PREFIX", "auto")
EMBED_DOC_PREFIX = _env("EMBED_DOC_PREFIX", "auto")
# OpenAI-compatible servers only: reasoning models (gpt-oss etc.) spend part of max_tokens on
# hidden reasoning; "low" keeps answers fast. Empty = do not send the parameter.
REASONING_EFFORT = _env("REASONING_EFFORT", "")

# Memory knobs. Defaults fit a 2GB RAM machine (one model in RAM at a time, measured peak
# ~1.4GB for Ollama); with 4GB+ raise NUM_CTX/CONTEXT_CHARS (see imsearch.env.example).
NUM_CTX = _int("NUM_CTX", 4096)            # chat context window (tokens): the KV cache grows with it
NUM_PREDICT = _int("NUM_PREDICT", 1024)    # max answer tokens (prompt + answer must fit NUM_CTX)
# Map the model file instead of copying it into RAM: ~280MB less anonymous memory on a 1.7B
# model and no slower (pages stay in the file cache and can be dropped under pressure).
USE_MMAP = _bool("USE_MMAP", True)
EMBED_NUM_CTX = _int("EMBED_NUM_CTX", 2048)
EMBED_BATCH = _int("EMBED_BATCH", 4)       # texts per embedding request during ingest
EMBED_MAX_CHARS = _int("EMBED_MAX_CHARS", 2000)
# CPU threads for Ollama. 0 = auto: use the CPUs actually available to this machine/container
# (cgroup CPU quota / affinity). Without this, llama.cpp may start one thread per host core
# under a CPU quota and become 10x+ slower.
NUM_THREAD = _int("NUM_THREAD", 0)
KEEP_ALIVE = _env("KEEP_ALIVE", "10m")     # how long Ollama keeps a model in RAM after a request
LLM_TIMEOUT = float(_env("LLM_TIMEOUT", "900"))  # CPU inference is slow; be patient

# Retrieval
RRF_K = 60
CANDIDATES = _int("CANDIDATES", 30)          # per retriever
CONTEXT_CHUNKS = _int("CONTEXT_CHUNKS", 4)   # chunks passed to the answer LLM
CONTEXT_CHARS = _int("CONTEXT_CHARS", 3500)  # total characters of those chunks (~2.5K tokens with the prompt; must fit NUM_CTX)
PREREQ_KEYS = _int("PREREQ_KEYS", 3)        # manual explanations of a hit sample's settings (0 = off)
PREREQ_CHARS = _int("PREREQ_CHARS", 1000)   # part of CONTEXT_CHARS reserved for them
# Run the LLM query analysis for Japanese questions too (English ones are always translated).
# Off by default: on CPU it costs several seconds and small models add little.
ANALYZE_JA = _bool("ANALYZE_JA", False)
# "simple": LLM only translates English questions (robust with 1-4B models)
# "json":   one JSON call returns translation + keywords + identifier guesses (needs a larger model)
ANALYZE_MODE = _env("ANALYZE_MODE", "simple")
# Sample descriptions at ingest time: characters of the page file shown to the LLM
DESCRIBE_PAGE_CHARS = _int("DESCRIBE_PAGE_CHARS", 1500)

# Web server (imsearch serve)
WEB_HOST = _env("WEB_HOST", "127.0.0.1")   # put nginx etc. in front to publish it
WEB_PORT = _int("WEB_PORT", 8000)
WEB_CONCURRENCY = _int("WEB_CONCURRENCY", 1)  # answers generated at the same time (1 for 2-4GB RAM)
WEB_MAX_QUEUE = _int("WEB_MAX_QUEUE", 2)      # waiting answers; more requests get "busy"
WEB_MAX_QUESTION = _int("WEB_MAX_QUESTION", 500)  # characters
# Answers per client IP per minute (0 = no limit). For reverse proxies without their own
# rate limiter (Apache). The client IP comes from X-Forwarded-For sent by a proxy on 127.0.0.1.
WEB_ASK_PER_MIN = _int("WEB_ASK_PER_MIN", 0)
# Public path prefix when the proxy serves the app below a sub-path, e.g. "/imsearch"
# (the proxy strips it). Only the API docs page needs it; the UI uses relative URLs.
WEB_ROOT_PATH = _env("WEB_ROOT_PATH", "").rstrip("/")

# SQLite page cache (KiB). Small on purpose; the OS file cache does the rest.
SQLITE_CACHE_KB = _int("SQLITE_CACHE_KB", 8192)


def _available_cpus() -> int | None:
    """CPUs this process may really use, or None if not limited below os.cpu_count()."""
    n = os.cpu_count() or 1
    try:
        n = min(n, len(os.sched_getaffinity(0)))
    except AttributeError:  # macOS
        pass
    try:  # cgroup v2: "max 100000" or "400000 100000"
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            n = min(n, max(1, int(int(quota) / int(period))))
    except (OSError, ValueError):
        pass
    return n if n < (os.cpu_count() or 1) else None


def num_thread() -> int | None:
    """Value for Ollama's options.num_thread (None = let Ollama decide)."""
    if NUM_THREAD > 0:
        return NUM_THREAD
    if LLM_BASE_URL.startswith(("http://localhost", "http://127.0.0.1")):
        return _available_cpus()
    return None  # remote server: its CPUs are not ours


def embed_query_prefix() -> str:
    if EMBED_QUERY_PREFIX != "auto":
        return EMBED_QUERY_PREFIX
    m = EMBED_MODEL.lower()
    if m.startswith("qwen3-embedding"):
        return ("Instruct: Given a question from a developer, retrieve relevant documentation, "
                "sample code or source code of the INTER-Mediator web framework\nQuery: ")
    if "e5" in m:
        return "query: "
    return ""


def embed_doc_prefix() -> str:
    if EMBED_DOC_PREFIX != "auto":
        return EMBED_DOC_PREFIX
    return "passage: " if "e5" in EMBED_MODEL.lower() else ""


@lru_cache
def exclude_config() -> dict:
    return yaml.safe_load((CONFIG_DIR / "exclude.yaml").read_text(encoding="utf-8"))


@lru_cache
def glossary() -> list[dict]:
    return yaml.safe_load((CONFIG_DIR / "glossary.yaml").read_text(encoding="utf-8")) or []
