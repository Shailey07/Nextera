"""
Answer cache — local database of solved quiz questions.
Keyed by question text hash. Reused across users on same machine.
"""
import json
import hashlib
from pathlib import Path
from loguru import logger

CACHE_DIR = Path.home() / ".nextera"
CACHE_FILE = CACHE_DIR / "answer_cache.json"


def _hash_question(text: str, options: list) -> str:
    """Stable hash of question + options for cache lookup."""
    normalized = text.strip().lower()
    opt_str = "|".join(sorted(
        str(opt.get("value", "")).strip().lower() for opt in options
    ))
    combined = f"{normalized}||{opt_str}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def load_cache() -> dict:
    """Load cache from disk."""
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Cache load failed: {e}")
        return {}


def save_cache(cache: dict) -> None:
    """Persist cache to disk."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CACHE_FILE.write_text(
            json.dumps(cache, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"Cache save failed: {e}")


def lookup_cached(question_text: str, options: list) -> dict | None:
    """
    Check if this question is already solved.
    Returns cached response dict, or None.
    """
    cache = load_cache()
    key = _hash_question(question_text, options)
    entry = cache.get(key)
    if not entry:
        return None

    cached_opt_values = set(
        str(opt.get("value", "")).strip().lower() for opt in options
    )
    cached_opt_snapshot = set(
        str(v).strip().lower() for v in entry.get("option_snapshot", [])
    )
    if cached_opt_values != cached_opt_snapshot:
        logger.debug("Cache hit but options changed — skipping")
        return None

    return {
        "chosen": entry.get("chosen"),
        "answer": entry.get("answer"),
        "source": "cache"
    }


def store_answer(
    question_text: str,
    options: list,
    chosen: list | None = None,
    answer: str | None = None
) -> None:
    """
    Save a solved question to cache.
    Only stores when correctness is confirmed (caller responsibility).
    """
    cache = load_cache()
    key = _hash_question(question_text, options)
    cache[key] = {
        "question": question_text[:500],
        "option_snapshot": [
            str(opt.get("value", "")) for opt in options
        ],
        "chosen": chosen,
        "answer": answer,
    }
    save_cache(cache)
    logger.debug(f"Cached answer for: {question_text[:60]}...")


def get_cache_count() -> int:
    """Return number of cached entries."""
    return len(load_cache())