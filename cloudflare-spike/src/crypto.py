"""Web Crypto primitives exercised by the isolated Cloudflare Worker spike."""

from __future__ import annotations

from js import Uint8Array, crypto
from pyodide.ffi import run_sync, to_js


def _bytes(value):
    """Copy an ArrayBuffer/TypedArray result into Python bytes."""
    return Uint8Array.new(value).to_bytes()


def _view(value: bytes):
    """Create a JavaScript Uint8Array from a Python bytes-like value."""
    return Uint8Array.new(value)


def sha256(data: bytes) -> bytes:
    digest = run_sync(crypto.subtle.digest("SHA-256", _view(data)))
    return _bytes(digest)


def _import_aes_key(raw_key: bytes):
    return run_sync(
        crypto.subtle.importKey(
            "raw",
            _view(raw_key),
            to_js({"name": "AES-GCM"}),
            False,
            to_js(["encrypt", "decrypt"]),
        )
    )


def aes_gcm_encrypt(
    raw_key: bytes, nonce: bytes, plaintext: bytes, additional_data: bytes
) -> bytes:
    if len(raw_key) != 32:
        raise ValueError("AES-256-GCM requires a 32-byte key")
    if len(nonce) != 12:
        raise ValueError("AES-GCM requires a 12-byte nonce")
    key = _import_aes_key(raw_key)
    algorithm = to_js(
        {
            "name": "AES-GCM",
            "iv": _view(nonce),
            "additionalData": _view(additional_data),
            "tagLength": 128,
        }
    )
    ciphertext = run_sync(crypto.subtle.encrypt(algorithm, key, _view(plaintext)))
    return _bytes(ciphertext)


def aes_gcm_decrypt(
    raw_key: bytes, nonce: bytes, ciphertext: bytes, additional_data: bytes
) -> bytes:
    if len(raw_key) != 32:
        raise ValueError("AES-256-GCM requires a 32-byte key")
    if len(nonce) != 12:
        raise ValueError("AES-GCM requires a 12-byte nonce")
    key = _import_aes_key(raw_key)
    algorithm = to_js(
        {
            "name": "AES-GCM",
            "iv": _view(nonce),
            "additionalData": _view(additional_data),
            "tagLength": 128,
        }
    )
    plaintext = run_sync(crypto.subtle.decrypt(algorithm, key, _view(ciphertext)))
    return _bytes(plaintext)
