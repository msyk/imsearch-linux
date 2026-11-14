"""Paths and settings. Every setting can be overridden by an IMSEARCH_* env var.

Defaults target a small Linux server (Ubuntu, 4GB RAM, CPU only):
  - chat:  qwen3:1.7b            (~1.4GB, Q4_K_M)
  - embed: qwen3-embedding:0.6b  (~0.6GB, 1024 dim, multilingual)
  - small LLM context window / fewer context chunks / small embedding batches
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
# Embedding models such as qwen3-embedding want an instruction on the *query* side only.
# "auto" picks one by model name; set to "" to disable or to your own text.
EMBED_QUERY_PREFIX = _env("EMBED_QUERY_PREFIX", "auto")

# Memory knobs (4GB RAM: keep the KV cache and request sizes small)
NUM_CTX = _int("NUM_CTX", 8192)            # chat context window (tokens)
EMBED_NUM_CTX = _int("EMBED_NUM_CTX", 2048)
EMBED_BATCH = _int("EMBED_BATCH", 8)       # texts per embedding request during ingest
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
CONTEXT_CHUNKS = _int("CONTEXT_CHUNKS", 5)   # chunks passed to the answer LLM
CONTEXT_CHARS = _int("CONTEXT_CHARS", 7000)  # total characters of those chunks (must fit NUM_CTX)
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
WEB_CONCURRENCY = _int("WEB_CONCURRENCY", 1)  # answers generated at the same time (1 for 4GB RAM)
WEB_MAX_QUEUE = _int("WEB_MAX_QUEUE", 3)      # waiting answers; more requests get "busy"
WEB_MAX_QUESTION = _int("WEB_MAX_QUESTION", 500)  # characters

# SQLite page cache (KiB). Small on purpose; the OS file cache does the rest.
SQLITE_CACHE_KB = _int("SQLITE_CACHE_KB", 16384)


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
    return ""


@lru_cache
def exclude_config() -> dict:
    return yaml.safe_load((CONFIG_DIR / "exclude.yaml").read_text(encoding="utf-8"))


@lru_cache
def glossary() -> list[dict]:
    return yaml.safe_load((CONFIG_DIR / "glossary.yaml").read_text(encoding="utf-8")) or []
