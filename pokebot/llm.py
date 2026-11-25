"""Optional small local LLM (llama.cpp GGUF) that phrases answers from Pokédex facts."""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

from pokebot import config

SYSTEM_PROMPT = """You are Pokédex, a friendly Pokémon expert running fully offline.
Answer the user's question using ONLY the FACTS provided. If the facts do not contain the
answer, say you don't have that information. Be concise: 1-4 sentences, or a short list when
listing things. Do not mention "facts", "context" or the database."""

CONTINUE_PROMPT = """The answer already begins with this verified sentence, shown to the user:
"{key}"
Write 1-2 more sentences with useful, related details from the FACTS. Do not repeat or
contradict that sentence, and do not start with "Yes" or "No"."""

_silent_log = None


def _silence_llama_logs() -> None:
    """llama.cpp logs every Metal kernel it compiles; keep the terminal readable."""
    global _silent_log
    import ctypes

    import llama_cpp

    if _silent_log is None:
        # Module-level so the C callback outlives every model (it is called during interpreter shutdown).
        _silent_log = llama_cpp.llama_log_callback(lambda level, text, user_data: None)
        llama_cpp.llama_log_set(_silent_log, ctypes.c_void_p(0))


def model_path(size: str | None = None) -> Path | None:
    """The configured model if present, else any .gguf in the models folder."""
    env = os.environ.get("POKEBOT_MODEL")
    if env:
        return Path(env) if Path(env).exists() else None
    preferred = config.MODELS_DIR / config.MODELS[size or config.DEFAULT_MODEL][1]
    if preferred.exists():
        return preferred
    if size is None and config.MODELS_DIR.exists():
        found = sorted(config.MODELS_DIR.glob("*.gguf"))
        if found:
            return found[0]
    return None


def download_model(size: str = config.DEFAULT_MODEL) -> Path:
    url, filename = config.MODELS[size]
    dest = config.MODELS_DIR / filename
    if dest.exists():
        print(f"Model already downloaded: {dest}")
        return dest
    config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part")
    print(f"Downloading {filename} (one time) ...")
    try:
        _download_urllib(url, part)
    except urllib.error.URLError as exc:
        # python.org builds on macOS ship without root certificates; curl uses the system store.
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc) or not shutil.which("curl"):
            raise
        subprocess.run(["curl", "-L", "--fail", "--progress-bar", "-o", str(part), url], check=True)
    part.replace(dest)
    print(f"Saved to {dest}")
    return dest


def _download_urllib(url: str, part: Path) -> None:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "pokebot"}), timeout=60) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        with open(part, "wb") as out:
            while chunk := resp.read(1 << 20):
                out.write(chunk)
                done += len(chunk)
                if total:
                    sys.stdout.write(f"\r  {done / 1e6:7.1f} / {total / 1e6:.1f} MB ({done / total:.0%})")
                    sys.stdout.flush()
    print()


class LocalLLM:
    def __init__(self, path: Path, n_ctx: int = 4096):
        from llama_cpp import Llama

        _silence_llama_logs()
        self.path = path
        self.name = path.stem
        self.llm = Llama(model_path=str(path), n_ctx=n_ctx, n_gpu_layers=-1, verbose=False)
        self.lock = threading.Lock()
        atexit.register(self.close)

    def close(self) -> None:
        if self.llm is not None:
            self.llm.close()
            self.llm = None

    def stream(self, question: str, facts: str, history: list[dict] | None = None,
               key_fact: str | None = None, max_tokens: int = 300) -> Iterator[str]:
        system = SYSTEM_PROMPT + ("\n\n" + CONTINUE_PROMPT.format(key=key_fact) if key_fact else "")
        messages = [{"role": "system", "content": system}]
        for turn in (history or [])[-4:]:
            if turn.get("role") in ("user", "assistant") and turn.get("content"):
                messages.append({"role": turn["role"], "content": str(turn["content"])[:600]})
        messages.append({"role": "user", "content": f"FACTS:\n{facts}\n\nQUESTION: {question}"})
        with self.lock:
            for chunk in self.llm.create_chat_completion(messages=messages, stream=True, temperature=0.3,
                                                         top_p=0.9, max_tokens=max_tokens):
                text = chunk["choices"][0]["delta"].get("content")
                if text:
                    yield text


def load(size: str | None = None, quiet: bool = False) -> LocalLLM | None:
    """Load the local model, or return None (the app then answers from templates)."""
    path = model_path(size)
    if path is None:
        if not quiet:
            print("No local model found - answering from Pokédex templates. "
                  "Run `python -m pokebot download-model` to enable the LLM.")
        return None
    try:
        import llama_cpp  # noqa: F401
    except ImportError:
        if not quiet:
            print('llama-cpp-python is not installed - answering from templates. Install with: pip install -e ".[llm]"')
        return None
    if not quiet:
        print(f"Loading local model {path.name} ...")
    return LocalLLM(path)
