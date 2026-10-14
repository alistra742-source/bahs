"""Wallet resolution, pinned to published vectors.

Every layer that turns a recovery phrase into a payout address is checked
against the document that defines it: RIPEMD-160, Keccak-256, BIP-39 (Trezor
seed vector), BIP-32 (test vector 1), BIP-173 (bech32), RFC 8032 (Ed25519) and
SLIP-0010. The end-to-end BTC/ETH addresses are the widely published ones for
the ``abandon ... about`` mnemonic, so a mistake in any single layer shows up
here rather than in a payout nobody can explain.

Run: ``python -m unittest tests.test_wallets`` (or discovery from the repo root).
"""
import hashlib
import hmac
import os
import unittest
from unittest.mock import patch

import wallets

PHRASE = "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
SEED_VECTOR = bytes.fromhex("000102030405060708090a0b0c0d0e0f")


def hexed(value):
    return value.hex()


class PrimitiveTests(unittest.TestCase):
    def test_ripemd160_published_vectors(self):
        # ISO/IEC 10118-3 test vectors for RIPEMD-160.
        self.assertEqual(hexed(wallets.ripemd160(b"")), "9c1185a5c5e9fc54612808977ee8f548b2258d31")
        self.assertEqual(hexed(wallets.ripemd160(b"abc")), "8eb208f7e05d987a9b044a8e98c6b087f15a0bfc")
        self.assertEqual(hexed(wallets.ripemd160(b"message digest")), "5d0689ef49d2fae572b881b123a85ffa21595f36")
        self.assertEqual(hexed(wallets.ripemd160(b"a")), "0bdc9d2d256b3ee9daae347be6f4dc835a467ffe")
        # 56 bytes: crosses the padding boundary a 64-byte block would hide.
        self.assertEqual(hexed(wallets.ripemd160(b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq")),
                         "12a053384a9c0c88e405a06c27dcf49ada62eb2b")
        self.assertEqual(hexed(wallets.ripemd160(b"a" * 1000000)), "52783243c1697bdbe16d37f97f68f08325dc1528")

    def test_keccak256_is_keccak_not_sha3(self):
        self.assertEqual(hexed(wallets.keccak256(b"")), "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470")
        self.assertEqual(hexed(wallets.keccak256(b"abc")), "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45")

    def test_base58(self):
        self.assertEqual(wallets.b58encode(b"hello world"), "StV1DL6CwTryKyV")
        self.assertEqual(wallets.b58encode(b"\x00\x00\x01"), "112")

    def test_bech32_bip173_example(self):
        program = wallets.hash160(bytes.fromhex("0279BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798"))
        self.assertEqual(wallets.segwit_v0("bc", program), "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4")

    def test_secp256k1_generator(self):
        point = wallets._point_mul(1, wallets._G)
        self.assertEqual(wallets._compressed(point).hex(),
                         "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798")
        # n*G is the point at infinity.
        self.assertIsNone(wallets._point_mul(wallets._N, wallets._G))

    def test_ed25519_rfc8032_vector_1(self):
        secret = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        self.assertEqual(hexed(wallets.ed25519_public(secret)),
                         "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")

    def test_eip55_checksum(self):
        # EIP-55's own example.
        self.assertEqual(wallets.eip55(bytes.fromhex("5aaeb6053f3e94c9b9a09f33669435e7ef1beaed")),
                         "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed")


class DerivationTests(unittest.TestCase):
    def test_bip39_seed_vector(self):
        self.assertEqual(hexed(wallets.mnemonic_seed(PHRASE, "TREZOR")),
                         "c55257c360c07c72029aebc1b53c05ed0362ada38ead3e3e9efa3708e5349553"
                         "1f09a6987599d18264c1e1c92f2cf141630c7a3c4ab7c81b2f001698e7463b04")

    def test_bip32_test_vector_1(self):
        # BIP-32 test vector 1, seed 000102...0f.
        key, chain = wallets.bip32_master(SEED_VECTOR)
        self.assertEqual(hexed(key), "e8f32e723decf4051aefac8e2c93c9c5b214313817cdb01a1494b917c8436b35")
        self.assertEqual(hexed(chain), "873dff81c02f525623fd1fe5167eac3a55a049de3d314bb42ee227ffed37d508")
        # m/0'/1/2'/2/1000000000 -- the non-hardened steps must stay unhardened.
        for index in (0x80000000, 1, 0x80000002, 2, 1000000000):
            key, chain = wallets.bip32_child(key, chain, index)
        self.assertEqual(hexed(wallets._compressed(wallets._point_mul(int.from_bytes(key, "big"), wallets._G))),
                         "022a471424da5e657499d1ff51cb43c47481a03b1e77f951fe64cec9f5a48f7011")

    def test_slip10_ed25519_vector_1(self):
        # SLIP-0010 Ed25519 vector 1: seed 000102...0f.
        key, chain = wallets.slip10_master_ed(SEED_VECTOR)
        self.assertEqual(hexed(key), "2b4be7f19ee27bbf30c667b642d5f4aa69fd169872f8fc3059c08ebae2eb19e7")
        self.assertEqual(hexed(chain), "90046a93de5380a72b5e45010748567d5ea02bbf6522f979e05c0d8d8ca9fffb")

    def test_slip10_ed25519_child_vector(self):
        key, chain = wallets.slip10_master_ed(SEED_VECTOR)
        digest = hmac.new(chain, b"\x00" + key + (0x80000000).to_bytes(4, "big"), hashlib.sha512).digest()
        self.assertEqual(hexed(digest[:32]), "68e0fe46dfb67e368c75379acec591dad19df3cde26e63b93a8e704f1dade7a3")
        self.assertEqual(hexed(digest[32:]), "8b59aa11380b624e81507a27fedda59fea6d0b779a778918a2fd3590e16e9c69")

    def test_derived_addresses_for_the_test_mnemonic(self):
        self.assertEqual(wallets.derive("BTC", PHRASE), "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu")
        self.assertEqual(wallets.derive("ETH", PHRASE), "0x9858EfFD232B4033E47d90003D41EC34EcaEda94")
        self.assertEqual(wallets.derive("USDC", PHRASE), "0x9858EfFD232B4033E47d90003D41EC34EcaEda94")

    def test_litecoin_is_its_own_account_under_its_own_hrp(self):
        # LTC BIP84 is coin type 2, so the account differs from BTC's. What is
        # asserted here is the encoder wiring: ltc1q + a 20-byte P2WPKH program.
        litecoin = wallets.derive("LTC", PHRASE)
        self.assertTrue(wallets._HASH160_LTC.match(litecoin), litecoin)

    def test_solana_is_a_32_byte_base58_account(self):
        address = wallets.derive("SOL", PHRASE)
        self.assertEqual(wallets.derive("USDT", PHRASE), address)
        # Base58 of a 32-byte Ed25519 key: 43 or 44 characters, no padding.
        self.assertIn(len(address), (43, 44))
        self.assertTrue(wallets._BASE58_32.match(address))


class ResolutionTests(unittest.TestCase):
    def test_missing_variable_names_itself(self):
        with patch.dict(os.environ, {}, clear=True):
            wallets._cache.clear()
            with self.assertRaises(Exception) as caught:
                wallets.address("BTC")
            self.assertIn("BTC", str(caught.exception))
            self.assertEqual(wallets.available(), [])

    def test_literal_address_is_used_verbatim(self):
        with patch.dict(os.environ, {"BTC": "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"}, clear=True):
            wallets._cache.clear()
            self.assertEqual(wallets.address("BTC"), "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu")

    def test_phrase_is_derived(self):
        with patch.dict(os.environ, {"BTC": PHRASE, "ETH": PHRASE}, clear=True):
            wallets._cache.clear()
            self.assertEqual(wallets.address("BTC"), "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu")
            self.assertEqual(wallets.address("ETH"), "0x9858EfFD232B4033E47d90003D41EC34EcaEda94")
            self.assertEqual(sorted(wallets.available()), ["BTC", "ETH"])

    def test_quotes_and_whitespace_are_stripped(self):
        with patch.dict(os.environ, {"ETH": '  "0x9858EfFD232B4033E47d90003D41EC34EcaEda94"\n'}, clear=True):
            wallets._cache.clear()
            self.assertEqual(wallets.address("ETH"), "0x9858EfFD232B4033E47d90003D41EC34EcaEda94")

    def test_junk_fails_closed_instead_of_becoming_an_address(self):
        with patch.dict(os.environ, {"BTC": "not a wallet"}, clear=True):
            wallets._cache.clear()
            with self.assertRaises(Exception) as caught:
                wallets.address("BTC")
            self.assertIn("neither", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
