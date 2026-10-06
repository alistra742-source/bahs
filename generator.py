"""Candidate username generation.

Four patterns, each taking a length:

* ``letters`` -- ``a-z`` and ``A-Z``, so 52**n candidates
* ``alnum``   -- ``a-z``, ``A-Z`` and ``0-9``, the widest set all three
                 platforms accept
* ``numbers`` -- ``0-9``, so 10**n
* ``words``   -- "OG" dictionary words of exactly n letters, from the
                 configurable list (``OG_WORDS`` / ``OG_WORDS_FILE``, plus the
                 bundled ``wordlists/og.txt``)

A space too large to enumerate is sampled at random instead of counted out, so
``limit`` is always what sets the cost: 52**5 is 380M candidates and nobody
wants to generate them in order. ``seed`` makes a random sample reproducible,
which is what lets the same scan be rerun later without re-checking names that
were already answered.
"""

import itertools
import os
import random
import re
import string

import config

PATTERNS = ("letters", "alnum", "numbers", "words")
MODES = ("random", "sequential")

CHARSETS = {
    "letters": string.ascii_letters,
    "alnum": string.ascii_letters + string.digits,
    "numbers": string.digits,
}

BUNDLED_WORDS_FILE = "wordlists/og.txt"

# A space at or below this multiple of the limit is enumerated and then sampled,
# because drawing `limit` distinct items one at a time from a nearly exhausted
# space turns into a rejection loop.
_ENUMERATE_FACTOR = 4

_word_cache: dict[str, list[str]] = {}


WORD_RE = re.compile(r"^[a-z]+$")


def _words_from_file(path: str) -> list[str]:
    """Lower-cased ASCII words from one file.

    Split on any whitespace, so both a one-word-per-line list and a
    space-separated one read the same, and only ``a-z`` survives -- a stray
    non-ASCII token would otherwise sail through ``str.isalpha()`` and become a
    candidate no platform can accept.
    """
    words: list[str] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip().lower()
                if not line or line.startswith("#"):
                    continue
                words.extend(w for w in line.split() if WORD_RE.match(w))
    except OSError:
        return []
    return words


def load_words(path: str | None = None, extra: list[str] | None = None) -> list[str]:
    """The configured dictionary: bundled list + OG_WORDS_FILE + OG_WORDS + extra.

    Cached per file path, so a scan over a big list does not re-read it per name.
    """
    words: list[str] = []
    files = [path or os.path.join(os.path.dirname(os.path.abspath(__file__)), BUNDLED_WORDS_FILE)]
    if not path and config.OG_WORDS_FILE:
        files.append(config.OG_WORDS_FILE)
    for name in files:
        if name not in _word_cache:
            _word_cache[name] = _words_from_file(name)
        words.extend(_word_cache[name])
    if not path:
        words.extend(w.strip().lower() for w in config.OG_WORDS.split(","))
    if extra:
        words.extend(str(w).strip().lower() for w in extra)
    return [w for w in dict.fromkeys(words) if WORD_RE.match(w)]


def _take(items: list[str], limit: int, seed: int | None, mode: str) -> list[str]:
    if limit >= len(items):
        return list(items)
    if mode == "sequential":
        return list(items[:limit])
    return random.Random(seed).sample(items, limit)


def generate(
    pattern: str,
    length: int,
    limit: int,
    seed: int | None = None,
    mode: str = "random",
    words: list[str] | None = None,
) -> list[str]:
    """Up to ``limit`` candidates of ``pattern`` with exactly ``length`` characters."""
    pattern = (pattern or "").strip().lower()
    mode = (mode or "random").strip().lower()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if pattern not in PATTERNS:
        raise ValueError(f"pattern must be one of {PATTERNS}")
    length = int(length)
    limit = int(limit)
    if length < 1 or length > 32:
        raise ValueError("length must be between 1 and 32")
    if limit < 1:
        raise ValueError("limit must be positive")

    if pattern == "words":
        pool = [w for w in (words if words is not None else load_words()) if len(w) == length]
        return _take(pool, limit, seed, mode)

    alphabet = CHARSETS[pattern]
    space = len(alphabet) ** length
    if space <= limit * _ENUMERATE_FACTOR:
        everything = ["".join(pick) for pick in itertools.product(alphabet, repeat=length)]
        return _take(everything, limit, seed, mode)
    if mode == "sequential":
        out: list[str] = []
        for pick in itertools.product(alphabet, repeat=length):
            out.append("".join(pick))
            if len(out) >= limit:
                break
        return out
    # Huge space: draw distinct candidates until the limit is met. `limit` is
    # far below `space` here, so rejection is vanishingly rare.
    rng = random.Random(seed)
    drawn: dict[str, None] = {}
    while len(drawn) < limit:
        drawn["".join(rng.choice(alphabet) for _ in range(length))] = None
    return list(drawn)


def generate_many(
    requests: list[dict],
    limit: int = 0,
    seed: int | None = None,
    words: list[str] | None = None,
) -> list[str]:
    """Run several pattern requests and return the de-duplicated union.

    ``limit`` caps the total (0 = no cap); each request's own ``limit`` caps its
    own contribution. Order is preserved so a scan is reproducible.
    """
    out: list[str] = []
    for request in requests:
        pattern = str(request.get("pattern") or "")
        length = int(request.get("length") or 0)
        per = int(request.get("limit") or limit or 0)
        if per < 1:
            per = 1000
        out.extend(
            generate(
                pattern,
                length,
                per,
                seed=seed,
                mode=str(request.get("mode") or "random"),
                words=words,
            )
        )
    merged = list(dict.fromkeys(out))
    if limit > 0:
        merged = merged[:limit]
    return merged
