"""Tests for Kalshi RSA auth signing."""
from __future__ import annotations

import base64
import tempfile

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.kalshi.auth import KalshiAuth, load_private_key


@pytest.fixture
def rsa_key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key


@pytest.fixture
def pem_file(rsa_key_pair):
    pem_bytes = rsa_key_pair.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as f:
        f.write(pem_bytes)
        return f.name


def test_load_private_key(pem_file):
    key = load_private_key(pem_file)
    assert isinstance(key, rsa.RSAPrivateKey)


def test_sign_request_returns_correct_headers(pem_file):
    auth = KalshiAuth(api_key="test-key-123", private_key_path=pem_file)
    headers = auth.sign_request("GET", "/trade-api/v2/markets")

    assert headers["KALSHI-ACCESS-KEY"] == "test-key-123"
    assert "KALSHI-ACCESS-SIGNATURE" in headers
    assert "KALSHI-ACCESS-TIMESTAMP" in headers


def test_signature_is_valid_base64(pem_file):
    auth = KalshiAuth(api_key="test-key", private_key_path=pem_file)
    headers = auth.sign_request("POST", "/trade-api/v2/orders")
    sig_bytes = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    assert len(sig_bytes) > 0


def test_signature_verifies(pem_file, rsa_key_pair):
    auth = KalshiAuth(api_key="key", private_key_path=pem_file)
    headers = auth.sign_request("GET", "/trade-api/v2/markets")

    timestamp = headers["KALSHI-ACCESS-TIMESTAMP"]
    message = f"{timestamp}\nGET\n/trade-api/v2/markets".encode()
    signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])

    # Should not raise
    rsa_key_pair.public_key().verify(
        signature, message, padding.PKCS1v15(), hashes.SHA256()
    )


def test_timestamp_is_milliseconds(pem_file):
    auth = KalshiAuth(api_key="key", private_key_path=pem_file)
    headers = auth.sign_request("GET", "/markets")
    ts = int(headers["KALSHI-ACCESS-TIMESTAMP"])
    # Millisecond timestamps are > 1e12
    assert ts > 1_000_000_000_000
