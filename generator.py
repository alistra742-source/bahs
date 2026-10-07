"""Candidate username generation.

Four patterns, each taking a length:

* ``letters`` -- ``a-z``, so 26**n candidates
* ``alnum``   -- ``a-z`` and ``0-9``, 36**n
* ``numbers`` -- ``0-9``, so 10**n
* ``words``   -- "OG" dictionary words of exactly n letters, from the
                 configurable list (``OG_WORDS`` / ``OG_WORDS_FILE``, plus the
                 bundled ``wordlists/og.txt``)

Lower-case only, deliberately. All four platforms treat a handle as
case-insensitive for uniqueness, and TikTok's oEmbed does not even resolve a
mixed-case one -- measured: ``check_tiktok("nike")`` returns ``taken`` while
``check_tiktok("NIKE")`` returns the ``400`` that means free. Generating both
cases would double every run's work to ask the same question twice, and half of
the answers would be wrong.

By default a request enumerates **every** name of that length, not a sample:
``limit`` omitted or 0 means all of them. That is the useful default for short
names -- "all of length 4 in a-z" is 456,976 and fits -- but the space grows
fast enough that the honest answer for the next length up is "no":

===========  =========  =========  ============
length       letters    numbers    alphanumeric
===========  =========  =========  ============
1                    26         10            36
2                   676        100         1,296
3                17,576      1,000        46,656
4               456,976     10,000     1,679,616
5            11,881,376    100,000    60,466,176
6           308,915,776  1,000,000 2,176,782,336
===========  =========  =========  ============

``MAX_ENUMERATION`` (1,000,000 by default) is the line, and it is a memory
limit as much as a time one. Measured peak RSS while building the lists:

===========================  ===========  ==========
space                        names        peak RSS
===========================  ===========  ==========
``a-z``, length 4               456,976      57 MB
``a-z0-9``, length 4          1,679,616    ~200 MB
``A-Za-z``, length 4          7,311,616     429 MB
``A-Za-z0-9``, length 4      14,776,336     843 MB
===========================  ===========  ==========

Past the limit the request is refused with the real size rather than quietly
sampled, so "every name of this length" always means exactly that or nothing -- a
scan that silently checked a thousandth of the space and reported "done" would
be the worst of the three answers.

A caller that genuinely wants a sample from a huge space passes ``limit`` and
gets ``limit`` distinct names drawn at random (``mode="random"``, reproducible
with ``seed``) or the first ``limit`` in order (``mode="sequential"``).
"""

import itertools
from collections.abc import Iterable, Iterator
import os
import random
import re
import string

import config

PATTERNS = ("letters", "alnum", "numbers", "words")
MODES = ("random", "sequential")

# Lower case only: a handle is case-insensitive for uniqueness on every target
# here, and TikTok's oEmbed will not resolve a mixed-case one at all. Both cases
# would mean checking each name twice, with half the answers wrong.
CHARSETS = {
    "letters": string.ascii_lowercase,
    "alnum": string.ascii_lowercase + string.digits,
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


def words_by_length() -> dict[int, int]:
    """How many dictionary words exist at each length, for the size preview."""
    counts: dict[int, int] = {}
    for word in load_words():
        counts[len(word)] = counts.get(len(word), 0) + 1
    return dict(sorted(counts.items()))


def space_size(pattern: str, length: int, words: list[str] | None = None) -> int:
    """How many names exist for this pattern at this length."""
    pattern = (pattern or "").strip().lower()
    if pattern == "words":
        pool = words if words is not None else load_words()
        return sum(1 for w in pool if len(w) == int(length))
    if pattern not in CHARSETS:
        raise ValueError(f"pattern must be one of {PATTERNS}")
    return len(CHARSETS[pattern]) ** int(length)


def enumerate_all(pattern: str, length: int, words: list[str] | None = None) -> list[str]:
    """Every name of this pattern and length, in order."""
    if pattern == "words":
        pool = [w for w in (words if words is not None else load_words()) if len(w) == length]
        return list(dict.fromkeys(pool))
    alphabet = CHARSETS[pattern]
    return ["".join(pick) for pick in itertools.product(alphabet, repeat=length)]


def _take(items: list[str], limit: int, seed: int | None, mode: str) -> list[str]:
    if limit >= len(items):
        return list(items)
    if mode == "sequential":
        return list(items[:limit])
    return random.Random(seed).sample(items, limit)


def _resolve(
    pattern: str, length: int, limit: int | None, mode: str, words: list[str] | None
) -> tuple[str, int, str, list[str] | None]:
    pattern = (pattern or "").strip().lower()
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    if pattern not in PATTERNS:
        raise ValueError(f"pattern must be one of {PATTERNS}")
    length = int(length)
    if length < 1 or length > 32:
        raise ValueError("length must be between 1 and 32")
    return pattern, length, mode, words


def generate(
    pattern: str,
    length: int,
    limit: int | None = None,
    seed: int | None = None,
    mode: str = "random",
    words: list[str] | None = None,
) -> list[str]:
    """Names of ``pattern`` with exactly ``length`` characters.

    ``limit`` of None or 0 means **every** name of that length, and is refused
    with the real size when the space is over ``MAX_ENUMERATION``. Any other
    ``limit`` draws that many from the space instead.
    """
    pattern, length, mode, words = _resolve(pattern, length, limit, mode, words)
    size = space_size(pattern, length, words)

    if limit is None or int(limit) <= 0:
        if size > config.MAX_ENUMERATION:
            raise ValueError(
                f"{pattern} at length {length} is {size:,} names, which is over the "
                f"{config.MAX_ENUMERATION:,} the service will enumerate in one request. "
                "Use a shorter length, a narrower pattern, or send a limit to sample it."
            )
        return enumerate_all(pattern, length, words)

    limit = int(limit)
    if limit < 1:
        raise ValueError("limit must be positive")

    if pattern == "words":
        pool = [w for w in (words if words is not None else load_words()) if len(w) == length]
        return _take(pool, limit, seed, mode)

    alphabet = CHARSETS[pattern]
    if size <= limit * _ENUMERATE_FACTOR:
        return _take(enumerate_all(pattern, length), limit, seed, mode)
    if mode == "sequential":
        out: list[str] = []
        for pick in itertools.product(alphabet, repeat=length):
            out.append("".join(pick))
            if len(out) >= limit:
                break
        return out
    # Huge space with a small limit: draw distinct candidates directly. `limit`
    # is far below `space` here, so rejection is vanishingly rare.
    rng = random.Random(seed)
    drawn: dict[str, None] = {}
    while len(drawn) < limit:
        drawn["".join(rng.choice(alphabet) for _ in range(length))] = None
    return list(drawn)


def generate_many(
    requests: list[dict],
    limit: int | None = None,
    seed: int | None = None,
    words: list[str] | None = None,
) -> list[str]:
    """Run several pattern requests and return the de-duplicated union, in order.

    Each request's own ``limit`` wins; ``limit`` here is the fallback and 0/None
    means "as many as the request asks for, and all of them if it asks for none".
    The merged union is capped at ``MAX_ENUMERATION``.
    """
    out: list[str] = []
    for request in requests:
        pattern = str(request.get("pattern") or "")
        length = int(request.get("length") or 0)
        per = request.get("limit")
        if per is None:
            per = limit
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
        if len(out) > config.MAX_ENUMERATION:
            break
    merged = list(dict.fromkeys(out))
    return merged[: config.MAX_ENUMERATION]


# --- buckets ---------------------------------------------------------------
# The named selection the dashboard offers: a kind and a length. `l` is letters,
# `n` digits, `c` letters+digits, `og` dictionary words -- spelled out here so
# the labels and the sizes the UI shows cannot drift from what is generated.
KINDS = ("l", "n", "c", "og")
KIND_PATTERN = {"l": "letters", "n": "numbers", "c": "alnum", "og": "words"}
KIND_LABEL = {
    "l": "letters a-z",
    "n": "digits 0-9",
    "c": "letters + digits",
    "og": "dictionary words",
}


def kind_size(kind: str, length: int, words: list[str] | None = None) -> int:
    if kind not in KIND_PATTERN:
        raise ValueError(f"kind must be one of {KINDS}")
    return space_size(KIND_PATTERN[kind], int(length), words)


def iter_kind(kind: str, length: int, words: list[str] | None = None) -> Iterator[str]:
    """Every name of this kind and length, generated lazily.

    Lazy is the whole point. "All of length 5 in a-z" is 11,881,376 names, which
    as a list is most of a gigabyte and as a generator is a few hundred bytes --
    itertools.product walks the space while the run consumes it, so a run can be
    bigger than memory without the space ever being materialised.
    """
    if kind not in KIND_PATTERN:
        raise ValueError(f"kind must be one of {KINDS}")
    pattern = KIND_PATTERN[kind]
    length = int(length)
    if length < 1 or length > 32:
        raise ValueError("length must be between 1 and 32")
    if pattern == "words":
        pool = words if words is not None else load_words()
        for word in dict.fromkeys(w for w in pool if len(w) == length):
            yield word
        return
    alphabet = CHARSETS[pattern]
    for pick in itertools.product(alphabet, repeat=length):
        yield "".join(pick)


def effective_buckets(buckets: list[dict]) -> list[tuple[str, int]]:
    """Drop buckets whose every name is already inside another selected bucket.

    `og` words are lowercase letters, so every 3-letter word is already in `3l`;
    `l` and `n` are both inside `c` at the same length. Left in, they are not
    extra coverage, they are the same names checked twice -- which is the one
    thing "every combination" must not mean.
    """
    chosen: list[tuple[str, int]] = []
    for bucket in buckets:
        kind = str(bucket.get("kind") or "").strip().lower()
        try:
            length = int(bucket.get("length") or 0)
        except (TypeError, ValueError):
            continue
        if kind in KINDS and 1 <= length <= 32 and (kind, length) not in chosen:
            chosen.append((kind, length))
    kinds = {kind for kind, _ in chosen}
    lengths = {length for _, length in chosen}
    out: list[tuple[str, int]] = []
    for kind, length in chosen:
        if kind == "og" and (("c", length) in chosen or ("l", length) in chosen):
            continue
        if kind in ("l", "n") and ("c", length) in chosen:
            continue
        out.append((kind, length))
    del kinds, lengths
    return out


def iter_buckets(buckets: list[dict], words: list[str] | None = None) -> Iterator[str]:
    """Every name in the selection, lazily, each exactly once."""
    for kind, length in effective_buckets(buckets):
        yield from iter_kind(kind, length, words)


def buckets_total(buckets: list[dict], words: list[str] | None = None) -> int:
    """How many names the effective selection holds, without building it."""
    return sum(kind_size(kind, length, words) for kind, length in effective_buckets(buckets))


def menu(lengths: Iterable[int] = (3, 4, 5)) -> list[dict]:
    """The size of every bucket the dashboard offers, for the picker."""
    word_counts = words_by_length()
    out: list[dict] = []
    for length in lengths:
        for kind in KINDS:
            if kind == "og":
                size = word_counts.get(length, 0)
            else:
                size = kind_size(kind, length)
            out.append(
                {
                    "kind": kind,
                    "length": length,
                    "label": f"{length}{kind}",
                    "detail": KIND_LABEL[kind],
                    "size": size,
                }
            )
    return out
