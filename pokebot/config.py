import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("POKEBOT_DB", PACKAGE_DIR / "data" / "pokedex.sqlite"))
MODELS_DIR = Path(os.environ.get("POKEBOT_MODELS", PACKAGE_DIR.parent / "models"))

ENGLISH = 9

MODELS = {
    "0.5b": (
        "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf",
        "qwen2.5-0.5b-instruct-q4_k_m.gguf",
    ),
    "1.5b": (
        "https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "qwen2.5-1.5b-instruct-q4_k_m.gguf",
    ),
    "3b": (
        "https://huggingface.co/Qwen/Qwen2.5-3B-Instruct-GGUF/resolve/main/qwen2.5-3b-instruct-q4_k_m.gguf",
        "qwen2.5-3b-instruct-q4_k_m.gguf",
    ),
}
DEFAULT_MODEL = "1.5b"
