"""Payout wallets, read from the environment -- one variable per coin.

Railway variables ``SOL``, ``LTC``, ``BTC``, ``ETH``, ``USDT`` and ``USDC`` hold
the destination the processor forwards a settled payment to. A variable may hold
either shape:

* a deposit address -- used exactly as written (``bc1q...``, ``ltc1q...``,
  ``0x...``, a base58 Solana address). Nothing is derived; the operator's own
  string is what the invoice is paid out to.
* a BIP-39 recovery phrase. The first standard receiving account is derived:

  ==== ============================= ================================
  coin path                          address form
  ==== ============================= ================================
  BTC  m/84'/0'/0'/0/0                bech32 P2WPKH (``bc1q``)
  LTC  m/84'/2'/0'/0/0                bech32 P2WPKH (``ltc1q``)
  ETH  m/44'/60'/0'/0/0               EIP-55 checksummed (``0x``)
  USDC m/44'/60'/0'/0/0               the same Ethereum account
  SOL  m/44'/501'/0'/0'               base58 Ed25519 (Phantom/Solflare account 0)
  USDT m/44'/501'/0'/0'               the same Solana account (SPL mint)
  ==== ============================= ================================

Only public addresses are produced. A phrase never leaves this process, is never
logged, and is never returned to a client -- :func:`address` hands back the
payout address and nothing else.

Everything here is pure Python on the standard library: secp256k1 and Ed25519
point arithmetic, BIP-32/44/84 and SLIP-0010 derivation, base58, bech32 and
Keccak-256. ``tests/test_wallets.py`` pins each layer to its published vector,
so the dependency list stays at four packages and a deploy cannot pick up a
wallet library's breaking change.

BIP-39 word checksum validation is deliberately absent: it needs the 2048-word
list (~13 KB) and a typo in a phrase still derives *a* valid address, just not
the intended one. Compare the derived address with your wallet once before
taking money on it -- ``python -c "import wallets; print(wallets.address('BTC'))"``.
"""

import hashlib
import hmac
import os
import re
import unicodedata

from fastapi import HTTPException

# Environment variable per coin. Named in every error message, because "which
# variable is missing" is the only question an operator ever asks here.
ENV = {"SOL": "SOL", "LTC": "LTC", "BTC": "BTC", "ETH": "ETH", "USDT": "USDT", "USDC": "USDC"}

# coin -> (curve, path, encoder). Paths are written the way the standards write
# them -- ``'`` is a hardened index, and the last two steps of a BIP-44/84 path
# are deliberately not hardened. For the secp256k1 coins the encoder is the
# bech32 human-readable part, or "eth" for the Keccak/EIP-55 branch.
_PATHS = {
    "BTC": ("secp256k1", "84'/0'/0'/0/0", "bc"),
    "LTC": ("secp256k1", "84'/2'/0'/0/0", "ltc"),
    "ETH": ("secp256k1", "44'/60'/0'/0/0", "eth"),
    "USDC": ("secp256k1", "44'/60'/0'/0/0", "eth"),
    "SOL": ("ed25519", "44'/501'/0'/0'", "sol"),
    "USDT": ("ed25519", "44'/501'/0'/0'", "sol"),
}

_HRP = {"bc": "bc", "ltc": "ltc"}

_BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_HASH160 = re.compile(r"^bc1q[02-9ac-hj-np-z]{38}$")
_HASH160_LTC = re.compile(r"^ltc1q[02-9ac-hj-np-z]{38}$")
_HEX20 = re.compile(r"^0x[0-9a-fA-F]{40}$")
_BASE58_32 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_WORDS = {12, 15, 18, 21, 24}


# --- base58 ---------------------------------------------------------------
def b58encode(payload: bytes) -> str:
    number = int.from_bytes(payload, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = _BASE58[remainder] + encoded
    return "1" * (len(payload) - len(payload.lstrip(b"\x00"))) + encoded


# --- bech32 (BIP-173), version 0 only: every address derived here is P2WPKH ----
def _polymod(values) -> int:
    generator = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    checksum = 1
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index, poly in enumerate(generator):
            if (top >> index) & 1:
                checksum ^= poly
    return checksum


def _hrp_expand(hrp: str):
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convertbits(data, from_bits: int, to_bits: int):
    accumulator = bits = 0
    out = []
    max_value = (1 << to_bits) - 1
    for value in data:
        accumulator = (accumulator << from_bits) | value
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            out.append((accumulator >> bits) & max_value)
    if bits:
        out.append((accumulator << (to_bits - bits)) & max_value)
    return out


def segwit_v0(hrp: str, program: bytes) -> str:
    data = [0] + _convertbits(program, 8, 5)
    polymod = _polymod(_hrp_expand(hrp) + data + [0] * 6) ^ 1
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH32[d] for d in data + checksum)


# --- RIPEMD-160 -----------------------------------------------------------
# Ahead of secp256k1 because Bitcoin's hash160 needs it, and because
# ``hashlib.new("ripemd160")`` depends on the OpenSSL legacy provider -- absent
# from some build images. Implemented here so address derivation never depends
# on which provider the base image happens to ship.
_RL = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15),
    (7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8),
    (3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12),
    (1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2),
    (4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13),
)
_RR = (
    (5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12),
    (6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2),
    (15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13),
    (8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14),
    (12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11),
)
_SL = (
    (11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8),
    (7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12),
    (11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5),
    (11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12),
    (9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6),
)
_SR = (
    (8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6),
    (9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11),
    (9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5),
    (15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8),
    (8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11),
)
_KL = (0x00000000, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E)
_KR = (0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0x00000000)
_MASK32 = 0xFFFFFFFF


def _f(j: int, x: int, y: int, z: int) -> int:
    if j < 16:
        return x ^ y ^ z
    if j < 32:
        return (x & y) | (~x & _MASK32 & z)
    if j < 48:
        return (x | (~y & _MASK32)) ^ z
    if j < 64:
        return (x & z) | (y & (~z & _MASK32))
    return x ^ (y | (~z & _MASK32))


def _rol32(value: int, bits: int) -> int:
    return ((value << bits) | (value >> (32 - bits))) & _MASK32


def ripemd160(data: bytes) -> bytes:
    """RIPEMD-160 as specified by Dobbertin, Bosselaers and Preneel."""
    padded = bytearray(data) + b"\x80"
    padded += b"\x00" * ((56 - len(padded)) % 64)
    padded += (len(data) * 8).to_bytes(8, "little")
    h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
    for offset in range(0, len(padded), 64):
        x = [int.from_bytes(padded[offset + 4 * i : offset + 4 * i + 4], "little") for i in range(16)]
        a, b, c, d, e = h
        aa, bb, cc, dd, ee = h
        for j in range(80):
            round_index, slot = divmod(j, 16)
            t = (_rol32((a + _f(j, b, c, d) + x[_RL[round_index][slot]] + _KL[round_index]) & _MASK32, _SL[round_index][slot]) + e) & _MASK32
            a, e, d, c, b = e, d, _rol32(c, 10), b, t
            t = (_rol32((aa + _f(79 - j, bb, cc, dd) + x[_RR[round_index][slot]] + _KR[round_index]) & _MASK32, _SR[round_index][slot]) + ee) & _MASK32
            aa, ee, dd, cc, bb = ee, dd, _rol32(cc, 10), bb, t
        h = [(h[1] + c + dd) & _MASK32, (h[2] + d + ee) & _MASK32, (h[3] + e + aa) & _MASK32,
             (h[4] + a + bb) & _MASK32, (h[0] + b + cc) & _MASK32]
    return b"".join(word.to_bytes(4, "little") for word in h)


def hash160(payload: bytes) -> bytes:
    return ripemd160(hashlib.sha256(payload).digest())


# --- secp256k1 ------------------------------------------------------------
_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)


def _point_add(first, second):
    if first is None:
        return second
    if second is None:
        return first
    x1, y1 = first
    x2, y2 = second
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if first == second:
        slope = 3 * x1 * x1 * pow(2 * y1, -1, _P) % _P
    else:
        slope = (y2 - y1) * pow(x2 - x1, -1, _P) % _P
    x3 = (slope * slope - x1 - x2) % _P
    return x3, (slope * (x1 - x3) - y1) % _P


def _point_mul(scalar: int, point):
    result = None
    addend = point
    while scalar:
        if scalar & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        scalar >>= 1
    return result


def _compressed(point) -> bytes:
    x, y = point
    return bytes([0x02 + (y & 1)]) + x.to_bytes(32, "big")


def _uncompressed(point) -> bytes:
    x, y = point
    return b"\x04" + x.to_bytes(32, "big") + y.to_bytes(32, "big")


# --- BIP-39 / BIP-32 ------------------------------------------------------
def mnemonic_seed(phrase: str, passphrase: str = "") -> bytes:
    """BIP-39 seed: PBKDF2-HMAC-SHA512, 2048 rounds, salt ``"mnemonic"+passphrase``."""
    salt = "mnemonic" + unicodedata.normalize("NFKD", passphrase)
    return hashlib.pbkdf2_hmac(
        "sha512",
        unicodedata.normalize("NFKD", phrase).encode("utf-8"),
        salt.encode("utf-8"),
        2048,
        64,
    )


def bip32_master(seed: bytes):
    digest = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    return digest[:32], digest[32:]


def bip32_child(key: bytes, chain_code: bytes, index: int):
    if index >= 0x80000000:
        data = b"\x00" + key + index.to_bytes(4, "big")
    else:
        data = _compressed(_point_mul(int.from_bytes(key, "big"), _G)) + index.to_bytes(4, "big")
    digest = hmac.new(chain_code, data, hashlib.sha512).digest()
    child = (int.from_bytes(digest[:32], "big") + int.from_bytes(key, "big")) % _N
    return child.to_bytes(32, "big"), digest[32:]


def parse_path(text: str) -> list[int]:
    """``44'/60'/0'/0/0`` -> BIP-32 indices, hardened ones with the high bit set."""
    indices = []
    for step in text.split("/"):
        hardened = step.endswith("'")
        number = int(step.rstrip("'"))
        if not 0 <= number < 0x80000000:
            raise ValueError(f"index out of range: {step}")
        indices.append(number | 0x80000000 if hardened else number)
    return indices


def derive_secp(seed: bytes, path: str):
    key, chain = bip32_master(seed)
    for index in parse_path(path):
        key, chain = bip32_child(key, chain, index)
    return _point_mul(int.from_bytes(key, "big"), _G)


# --- Ed25519 / SLIP-0010 --------------------------------------------------
_Q = 2**255 - 19
_D = -121665 * pow(121666, _Q - 2, _Q) % _Q
_SQRT_M1 = pow(2, (_Q - 1) // 4, _Q)
_BASE_Y = 4 * pow(5, _Q - 2, _Q) % _Q


def _recover_x(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, _Q - 2, _Q) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q:
        x = x * _SQRT_M1 % _Q
    if x & 1:
        x = _Q - x
    return x


_BASE = (_recover_x(_BASE_Y), _BASE_Y)


def _ed_add(first, second):
    x1, y1 = first
    x2, y2 = second
    product = _D * x1 * x2 * y1 * y2 % _Q
    x3 = (x1 * y2 + x2 * y1) * pow(1 + product, _Q - 2, _Q) % _Q
    y3 = (y1 * y2 + x1 * x2) * pow(1 - product, _Q - 2, _Q) % _Q
    return x3, y3


def _ed_mul(scalar: int, point):
    result = (0, 1)
    while scalar:
        if scalar & 1:
            result = _ed_add(result, point)
        point = _ed_add(point, point)
        scalar >>= 1
    return result


def ed25519_public(secret: bytes) -> bytes:
    """RFC 8032 public key: clamp the hash, multiply the base point, encode y."""
    scalar = int.from_bytes(hashlib.sha512(secret).digest()[:32], "little")
    scalar = (scalar & ((1 << 254) - 8)) | (1 << 254)
    x, y = _ed_mul(scalar, _BASE)
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def slip10_master_ed(seed: bytes):
    digest = hmac.new(b"ed25519 seed", seed, hashlib.sha512).digest()
    return digest[:32], digest[32:]


def derive_ed(seed: bytes, path: str):
    """SLIP-0010 for Ed25519: every index hardened, no public derivation."""
    indices = parse_path(path)
    if any(index < 0x80000000 for index in indices):
        # Fail closed: a non-hardened step has no Ed25519 definition, and
        # silently hardening it would derive an account the wallet never shows.
        raise ValueError(f"Ed25519 paths must be fully hardened: {path}")
    key, chain = slip10_master_ed(seed)
    for index in indices:
        digest = hmac.new(chain, b"\x00" + key + index.to_bytes(4, "big"), hashlib.sha512).digest()
        key, chain = digest[:32], digest[32:]
    return ed25519_public(key)


# --- Keccak-256 (original padding, not SHA3) ------------------------------
_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000,
    0x000000000000808B, 0x0000000080000001, 0x8000000080008081, 0x8000000000008009,
    0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
    0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
    0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)
_ROT = (
    0, 1, 62, 28, 27,
    36, 44, 6, 55, 20,
    3, 10, 43, 25, 39,
    41, 45, 15, 21, 8,
    18, 2, 61, 56, 14,
)
_MASK64 = 0xFFFFFFFFFFFFFFFF


def _rotl(value: int, bits: int) -> int:
    return ((value << bits) | (value >> (64 - bits))) & _MASK64


def _keccak_f(lanes) -> None:
    for round_constant in _RC:
        column = [lanes[x] ^ lanes[x + 5] ^ lanes[x + 10] ^ lanes[x + 15] ^ lanes[x + 20] for x in range(5)]
        delta = [column[(x + 4) % 5] ^ _rotl(column[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(0, 25, 5):
                lanes[y + x] ^= delta[x]
        rotated = [0] * 25
        for x in range(5):
            for y in range(5):
                rotated[y + 5 * ((2 * x + 3 * y) % 5)] = _rotl(lanes[x + 5 * y], _ROT[x + 5 * y])
        for x in range(5):
            for y in range(0, 25, 5):
                lanes[y + x] = rotated[y + x] ^ (rotated[y + (x + 1) % 5] ^ _MASK64) & rotated[y + (x + 2) % 5]
        lanes[0] ^= round_constant


def keccak256(data: bytes) -> bytes:
    rate = 136
    padded = bytearray(data) + b"\x01"
    padded += b"\x00" * (-len(padded) % rate)
    padded[-1] ^= 0x80
    lanes = [0] * 25
    for offset in range(0, len(padded), rate):
        block = padded[offset : offset + rate]
        for lane in range(rate // 8):
            lanes[lane] ^= int.from_bytes(block[lane * 8 : lane * 8 + 8], "little")
        _keccak_f(lanes)
    out = bytearray()
    while len(out) < 32:
        for lane in range(rate // 8):
            out += lanes[lane].to_bytes(8, "little")
        if len(out) < 32:
            _keccak_f(lanes)
    return bytes(out[:32])


def eip55(address20: bytes) -> str:
    body = address20.hex()
    digest = keccak256(body.encode()).hex()
    return "0x" + "".join(c.upper() if c.isalpha() and int(digest[i], 16) >= 8 else c for i, c in enumerate(body))


# --- resolution -----------------------------------------------------------
def is_phrase(value: str) -> bool:
    """A recovery phrase, not an address: single-spaced words, standard count."""
    words = value.split()
    return len(words) in _WORDS and all(word.isalpha() for word in words)


def derive(coin: str, phrase: str) -> str:
    curve, path, encoder = _PATHS[coin]
    seed = mnemonic_seed(phrase)
    if curve == "ed25519":
        # Solana pays to the raw Ed25519 account, not a hash of it.
        return b58encode(derive_ed(seed, path))
    point = derive_secp(seed, path)
    if encoder == "eth":
        return eip55(keccak256(_uncompressed(point)[1:])[-20:])
    return segwit_v0(_HRP[encoder], hash160(_compressed(point)))


def looks_like_address(coin: str, value: str) -> bool:
    if coin in ("BTC", "LTC", "ETH", "USDC"):
        return bool((_HASH160 if coin == "BTC" else _HASH160_LTC if coin == "LTC" else _HEX20).match(value))
    return bool(_BASE58_32.match(value))


_cache: dict[tuple[str, str], str] = {}


def address(coin: str) -> str:
    """The payout address for ``coin``, derived from its variable when needed.

    Raises 503 naming the variable whenever the coin cannot be paid to, so a
    checkout fails loudly instead of generating an invoice nobody can settle.
    """
    name = ENV.get(coin)
    if name is None:
        raise HTTPException(503, f"{coin} is not a supported payout currency")
    raw = os.getenv(name, "").strip().strip('"').strip("'").strip()
    if not raw:
        raise HTTPException(503, f"Crypto checkout is not configured for {coin}: set the {name} variable to a {coin} address or recovery phrase.")
    cached = _cache.get((coin, raw))
    if cached:
        return cached
    if is_phrase(raw):
        try:
            resolved = derive(coin, raw)
        except Exception:
            raise HTTPException(503, f"The {name} phrase did not derive a {coin} address. Check the word count and spelling, or paste the address instead.")
    else:
        resolved = raw
        if not looks_like_address(coin, resolved):
            raise HTTPException(503, f"The {name} variable is neither a {coin} address nor a recovery phrase.")
    _cache[(coin, raw)] = resolved
    return resolved


def available() -> list[str]:
    """Coins with a usable variable right now, in checkout order."""
    ready = []
    for coin in ENV:
        try:
            address(coin)
        except HTTPException:
            continue
        ready.append(coin)
    return ready
