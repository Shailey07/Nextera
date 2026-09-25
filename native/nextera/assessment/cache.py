"""
Answer cache — local database of solved quiz questions.
Keyed by question text hash (order-independent options).
"""
import json
import hashlib
from pathlib import Path
from loguru import logger

CACHE_DIR = Path.home() / ".nextera"
CACHE_FILE = CACHE_DIR / "answer_cache.json"


def _hash_question(text: str, options: list) -> str:
    """Order-independent hash of question + options."""
    normalized = text.strip().lower()
    opt_str = "|".join(sorted(
        str(opt.get("value", "")).strip().lower() for opt in options
    ))
    combined = f"{normalized}||{opt_str}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def _hash_question_only(text: str) -> str:
    """Fallback hash — only question text."""
    normalized = text.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Cache load failed: {e}")
        return {}


def save_cache(cache: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        CACHE_FILE.write_text(
            json.dumps(cache, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"Cache save failed: {e}")


def lookup_cached(question_text: str, options: list) -> dict | None:
    """Check if this question is already solved."""
    cache = load_cache()

    key1 = _hash_question(question_text, options)
    entry = cache.get(key1)

    if not entry:
        key2 = _hash_question_only(question_text)
        entry = cache.get(key2)
        if entry:
            logger.debug(f"Cache hit (question-only): {question_text[:50]}...")

    if not entry:
        return None

    cached_opt_values = set(
        str(opt.get("value", "")).strip().lower() for opt in options
    )
    cached_opt_snapshot = set(
        str(v).strip().lower() for v in entry.get("option_snapshot", [])
    )

    if cached_opt_snapshot and cached_opt_values:
        overlap = cached_opt_values & cached_opt_snapshot
        if len(overlap) == 0:
            logger.debug("Cache has different options — skipping")
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
    answer: str | None = None,
    verified: bool = True
) -> None:
    """Save solved question to cache (stores TWO keys)."""
    if not verified:
        return

    cache = load_cache()

    key1 = _hash_question(question_text, options)
    key2 = _hash_question_only(question_text)

    entry = {
        "question": question_text[:500],
        "option_snapshot": [
            str(opt.get("value", "")) for opt in options
        ],
        "chosen": chosen,
        "answer": answer,
    }

    cache[key1] = entry
    cache[key2] = entry

    save_cache(cache)
    logger.debug(f"Cached: {question_text[:60]}...")


def delete_cached(question_text: str, options: list) -> None:
    """
    Remove a question from cache (used when answer was INCORRECT).
    Deletes both keys (question+options AND question-only).
    """
    cache = load_cache()
    key1 = _hash_question(question_text, options)
    key2 = _hash_question_only(question_text)

    removed = False
    if key1 in cache:
        del cache[key1]
        removed = True
    if key2 in cache:
        del cache[key2]
        removed = True

    if removed:
        save_cache(cache)
        logger.debug(f"Removed from cache: {question_text[:60]}...")


def get_cache_count() -> int:
    """Return number of unique entries."""
    cache = load_cache()
    return len(cache)