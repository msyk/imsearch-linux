"""Thin LLM client: native Ollama API or any OpenAI-compatible server."""
from __future__ import annotations

import json
import time
from typing import Iterator

import httpx

from . import config


class LLMError(RuntimeError):
    pass


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(config.LLM_TIMEOUT, connect=10.0)


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.LLM_API_KEY}"} if config.LLM_API_KEY else {}


def _client() -> httpx.Client:
    return httpx.Client(base_url=config.LLM_BASE_URL, timeout=_timeout(), headers=_headers())


def _raise_for(r: httpx.Response, model: str):
    if r.is_success:
        return
    try:
        msg = r.json().get("error", r.text)
        if isinstance(msg, dict):
            msg = msg.get("message", str(msg))
    except Exception:
        msg = r.text
    hint = ""
    low = str(msg).lower()
    if "memory" in low:
        hint = ("\n  → メモリ不足です。swap を追加する、IMSEARCH_NUM_CTX を下げる、"
                "OLLAMA_MAX_LOADED_MODELS=1 にする、より小さいモデルにする、のいずれかを試してください"
                "（README の「メモリ 4GB での運用」参照）。")
    elif "not found" in low:
        hint = f"\n  → モデルがありません: ollama pull {model}"
    raise LLMError(f"LLM server error {r.status_code} ({model}): {msg}{hint}")


def _post(c: httpx.Client, path: str, body: dict, model: str, retries: int = 2) -> httpx.Response:
    """POST with a couple of retries: on a small box Ollama may be busy (re)loading a model."""
    for attempt in range(retries + 1):
        try:
            r = c.post(path, json=body)
        except httpx.ConnectError as e:
            raise LLMError(f"LLM サーバー {config.LLM_BASE_URL} に接続できません（ollama は起動していますか？）: {e}")
        except httpx.TimeoutException:
            if attempt == retries:
                raise LLMError(f"LLM の応答がタイムアウトしました（{config.LLM_TIMEOUT:.0f}s）。"
                               "IMSEARCH_LLM_TIMEOUT を延ばすか、より小さいモデルを使ってください。")
            continue
        if r.status_code in (429, 503) and attempt < retries:
            time.sleep(2 * (attempt + 1))
            continue
        _raise_for(r, model)
        return r
    raise AssertionError("unreachable")


def _options(**kw) -> dict:
    nt = config.num_thread()
    if nt:
        kw["num_thread"] = nt
    return kw


def embed(texts: list[str], query: bool = False) -> list[list[float]]:
    if not texts:
        return []
    if query:
        texts = [config.embed_query_prefix() + t for t in texts]
    texts = [t[: config.EMBED_MAX_CHARS] for t in texts]
    with _client() as c:
        if config.LLM_API == "ollama":
            r = _post(c, "/api/embed", {
                "model": config.EMBED_MODEL, "input": texts, "truncate": True,
                "keep_alive": config.KEEP_ALIVE, "options": _options(num_ctx=config.EMBED_NUM_CTX),
            }, config.EMBED_MODEL)
            return r.json()["embeddings"]
        r = _post(c, "/v1/embeddings", {"model": config.EMBED_MODEL, "input": texts}, config.EMBED_MODEL)
        return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]


def _payload(messages: list[dict], stream: bool, json_mode: bool, temperature: float) -> tuple[str, dict]:
    if config.LLM_API == "ollama":
        body = {
            "model": config.CHAT_MODEL,
            "messages": messages,
            "stream": stream,
            "think": False,            # qwen3 etc.: skip the reasoning trace for speed
            "options": _options(temperature=temperature, num_ctx=config.NUM_CTX),
            "keep_alive": config.KEEP_ALIVE,
        }
        if json_mode:
            body["format"] = "json"
        return "/api/chat", body
    body = {"model": config.CHAT_MODEL, "messages": messages, "stream": stream, "temperature": temperature}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    return "/v1/chat/completions", body


def chat(messages: list[dict], json_mode: bool = False, temperature: float = 0.2) -> str:
    path, body = _payload(messages, False, json_mode, temperature)
    with _client() as c:
        data = _post(c, path, body, config.CHAT_MODEL).json()
    if config.LLM_API == "ollama":
        return data["message"]["content"]
    return data["choices"][0]["message"]["content"]


def chat_stream(messages: list[dict], temperature: float = 0.2) -> Iterator[str]:
    path, body = _payload(messages, True, False, temperature)
    with _client() as c:
        try:
            with c.stream("POST", path, json=body) as r:
                if not r.is_success:
                    r.read()
                    _raise_for(r, config.CHAT_MODEL)
                for line in r.iter_lines():
                    if not line:
                        continue
                    if config.LLM_API == "ollama":
                        d = json.loads(line)
                        if d.get("error"):
                            raise LLMError(f"LLM error ({config.CHAT_MODEL}): {d['error']}")
                        if d.get("message", {}).get("content"):
                            yield d["message"]["content"]
                        if d.get("done"):
                            break
                    else:
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        delta = json.loads(payload)["choices"][0].get("delta", {})
                        if delta.get("content"):
                            yield delta["content"]
        except httpx.ConnectError as e:
            raise LLMError(f"LLM サーバー {config.LLM_BASE_URL} に接続できません: {e}")


def chat_json(messages: list[dict]) -> dict:
    text = chat(messages, json_mode=True, temperature=0.0)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    return {}


def ollama_ps() -> list[dict]:
    """Models currently loaded in Ollama (name, size in RAM)."""
    if config.LLM_API != "ollama":
        return []
    with _client() as c:
        r = c.get("/api/ps")
        return r.json().get("models", []) if r.is_success else []


def ollama_tags() -> list[str]:
    with _client() as c:
        r = c.get("/api/tags")
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
