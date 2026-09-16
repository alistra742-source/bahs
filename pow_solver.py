"""Solve DeepSeek's proof of work for chat.deepseek.com.

Every message to `/api/v0/chat/completion` has to carry an `x-ds-pow-response` header. Without it
the API answers `40300 MISSING_HEADER`, and with a wrong answer it answers `40301
INVALID_POW_RESPONSE` -- so the header is not optional, and a guess is worse than nothing.

What the work is: `/api/v0/chat/create_pow_challenge` hands out a challenge, and

    challenge == DeepSeekHashV1(f"{salt}_{expire_at}_" + str(w))   for some w in [0, difficulty)

so the challenge is a hash the server already computed over a small integer, and the work is
recovering that integer. It was measured against the module: difficulty 144000 takes ~10 ms, and a
challenge built from a known `w` comes back as exactly that `w` (and not, off by one, when `w`
equals difficulty -- the range is half open).

**Why the wasm and not a reimplementation.** DeepSeekHashV1 is neither SHA3-256 nor Keccak-256:
it is a 256-bit-capacity sponge (rate 168 bytes, not 136), and its digest for a given input matches
neither (checked against `hashlib.sha3_256` and pycryptodome's `keccak`, both of which are
themselves correct -- Keccak-256 of the empty string is the published vector). A sponge that is
subtly wrong would earn `40301`, which looks exactly like the header being useless, so the site's
own module is used.

The module is the `sha3_wasm_bg.wasm` chat.deepseek.com loads (26,612 bytes; the two open-source
mirrors are byte-identical). It is fetched at build time by the Dockerfile and, if the image does
not carry it, on first use; `wasmtime` is imported lazily so a missing wheel only disables this
file rather than the service.
"""

import base64
import json
import os
import struct
import threading
from pathlib import Path
from typing import Optional

# Where the module is looked for. POW_WASM points somewhere else (tests, a pinned copy).
MODULE_PATH = Path(os.environ.get("POW_WASM") or Path(__file__).parent / "sha3_wasm_bg.wasm")

# The published copies. The first is the one that was measured; the second is the same build.
MODULE_URLS = (
    "https://raw.githubusercontent.com/xtekky/deepseek4free/main/"
    "dsk/wasm/sha3_wasm_bg.7b9ca65ddd.wasm",
    "https://raw.githubusercontent.com/sums001/Deepseek-API/main/deepseek/sha3_wasm_bg.wasm",
)

# The challenge names the sponge it was made with. Only this one is known.
ALGORITHM = "DeepSeekHashV1"

# A difficulty is the number of candidates the module will try, so a big one is a stall rather than
# work. 144000 is what the site hands out; anything past this is not solved, and is reported.
MAX_TRIES = int(os.environ.get("POW_MAX_TRIES") or "5000000")


class PowSolver:
    """The site's own sha3 module, driven the way its JS glue drives it.

    The two calls are wbindgen-shaped: a return slot is reserved on the shadow stack, the strings
    are written into linear memory through the allocator export, and the answer comes back as an
    i32 status followed by an f64 (which is where the integer is, exactly as in the reference
    client).
    """

    def __init__(self, module: bytes):
        import wasmtime  # lazy: a missing wheel must not take the service down

        engine = wasmtime.Engine()
        self.store = wasmtime.Store(engine)
        linker = wasmtime.Linker(engine)
        linker.define_wasi()
        self.exports = linker.instantiate(
            self.store, wasmtime.Module(engine, module)).exports(self.store)
        self.memory = self.exports["memory"]

    def _write(self, text: str) -> tuple:
        data = text.encode("utf-8")
        ptr = self.exports["__wbindgen_export_0"](self.store, len(data), 1)
        try:
            self.memory.write(self.store, data, ptr)
        except (AttributeError, TypeError):
            view = self.memory.data_ptr(self.store)
            for i, byte in enumerate(data):
                view[ptr + i] = byte
        return ptr, len(data)

    def _read(self, ptr: int, size: int) -> bytes:
        try:
            return self.memory.read(self.store, ptr, ptr + size)
        except (AttributeError, TypeError):
            return bytes(self.memory.data_ptr(self.store)[ptr:ptr + size])

    def digest(self, text: str) -> str:
        """DeepSeekHashV1 of one string, as the 64-character hex the challenge is made of."""
        ptr, length = self._write(text)
        retptr = self.exports["__wbindgen_add_to_stack_pointer"](self.store, -16)
        try:
            self.exports["wasm_deepseek_hash_v1"](self.store, retptr, ptr, length)
            out_ptr = int.from_bytes(self._read(retptr, 4), "little")
            out_len = int.from_bytes(self._read(retptr + 4, 4), "little")
            return self._read(out_ptr, out_len).decode("utf-8", "replace")
        finally:
            self.exports["__wbindgen_add_to_stack_pointer"](self.store, 16)

    def answer(self, challenge: str, prefix: str, difficulty: int) -> Optional[int]:
        """The `w` the challenge was made from, or None if it is not below `difficulty`."""
        retptr = self.exports["__wbindgen_add_to_stack_pointer"](self.store, -16)
        try:
            cp, cl = self._write(challenge)
            pp, pl = self._write(prefix)
            self.exports["wasm_solve"](
                self.store, retptr, cp, cl, pp, pl, float(difficulty))
            status = int.from_bytes(self._read(retptr, 4), "little", signed=True)
            if status != 1:
                return None
            return int(struct.unpack("<d", self._read(retptr + 8, 8))[0])
        finally:
            self.exports["__wbindgen_add_to_stack_pointer"](self.store, 16)


def prefix_for(salt, expire_at) -> str:
    """`{salt}_{expire_at}_`, with the values spelled the way the challenge carries them.

    An f-string is exactly what the site's own client uses, so an integer stays an integer and a
    string stays a string; a float is the one shape that would print differently, and it is
    normalised away.
    """
    if isinstance(expire_at, float) and expire_at.is_integer():
        expire_at = int(expire_at)
    return f"{salt}_{expire_at}_"


def fetch_module(timeout: float = 30.0) -> Optional[bytes]:
    """Download the sha3 module from the published copies, or None."""
    import httpx

    for url in MODULE_URLS:
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0),
                              follow_redirects=True) as client:
                response = client.get(url)
            if response.status_code != 200 or len(response.content) < 4096:
                print(f"[deepseek] {url} answered {response.status_code} "
                      f"({len(response.content)} bytes); trying the next copy", flush=True)
                continue
            return response.content
        except httpx.HTTPError as e:
            print(f"[deepseek] cannot reach {url} ({e.__class__.__name__})", flush=True)
    return None


_lock = threading.Lock()
_state: dict = {"tried": False, "solver": None}


def load() -> Optional[PowSolver]:
    """The solver, built once: from the image, or from a copy fetched on first use."""
    with _lock:
        if _state["tried"]:
            return _state["solver"]
        _state["tried"] = True
        module = None
        try:
            if MODULE_PATH.exists():
                module = MODULE_PATH.read_bytes()
        except OSError as e:
            print(f"[deepseek] {MODULE_PATH} could not be read ({e.__class__.__name__})", flush=True)
        if module is None:
            module = fetch_module()
        if module is None:
            print("[deepseek] no proof-of-work module available: a review will be refused with "
                  "40300 MISSING_HEADER until one is", flush=True)
            return None
        try:
            _state["solver"] = PowSolver(module)
        except Exception as e:  # a broken wheel here must not be fatal to the service
            print(f"[deepseek] the proof-of-work module would not load "
                  f"({e.__class__.__name__}: {e}); wasmtime is in requirements.txt", flush=True)
            return None
        print(f"[deepseek] proof of work: the sha3 module loaded ({len(module)} bytes)", flush=True)
        return _state["solver"]


def solve(challenge: dict) -> str:
    """The `x-ds-pow-response` value for one challenge, or '' when it cannot be produced.

    '' means the request goes out without the header, which the API answers with 40300 -- reported
    as it is rather than hidden, so a missing module or an unknown algorithm is visible.
    """
    if not isinstance(challenge, dict) or not challenge:
        return ""
    algorithm = str(challenge.get("algorithm") or "")
    if algorithm and algorithm != ALGORITHM:
        print(f"[deepseek] the challenge is {algorithm}, and only {ALGORITHM} is known here",
              flush=True)
        return ""
    raw_difficulty = challenge.get("difficulty")
    try:
        difficulty = int(raw_difficulty or 0)
    except (TypeError, ValueError):
        print(f"[deepseek] the challenge's difficulty is {raw_difficulty!r}, which cannot be used",
              flush=True)
        return ""
    if difficulty <= 0 or difficulty > MAX_TRIES:
        print(f"[deepseek] the challenge asks for {difficulty} candidates, which is outside the "
              f"{MAX_TRIES} this service will attempt", flush=True)
        return ""
    solver = load()
    if solver is None:
        return ""
    prefix = prefix_for(challenge.get("salt"), challenge.get("expire_at"))
    try:
        answer = solver.answer(str(challenge.get("challenge") or ""), prefix, difficulty)
    except Exception as e:  # the module must never take a turn down with it
        print(f"[deepseek] solving the proof of work failed "
              f"({e.__class__.__name__}: {e})", flush=True)
        return ""
    if answer is None:
        print(f"[deepseek] no answer below {difficulty} for this challenge "
              f"(the module found none)", flush=True)
        return ""
    # Every field the challenge carries goes back, with the answer: a parser that requires one of
    # them is then not a reason to fail, and an unknown extra field is ignored by any of them.
    body = dict(challenge)
    body["answer"] = answer
    body.setdefault("algorithm", ALGORITHM)
    print(f"[deepseek] solved the proof of work: {answer} of {difficulty}", flush=True)
    return base64.b64encode(json.dumps(body).encode()).decode()


if __name__ == "__main__":
    # The Dockerfile step: put the module in the image, and say what happened if it cannot.
    data = MODULE_PATH.read_bytes() if MODULE_PATH.exists() else fetch_module()
    if not data:
        raise SystemExit("could not fetch the sha3 module; the service will still run, but "
                         "reviews will be refused for a missing header")
    MODULE_PATH.write_bytes(data)
    print(f"{MODULE_PATH} ({len(data)} bytes)")
