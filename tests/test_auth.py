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
    """SUPERSEDED 2026-08-11: this asserted PKCS1v15 over a newline-delimited
    message, which is what the code did and what Kalshi rejects. Verified
    against the live API: that combination returns 401
    INCORRECT_API_KEY_SIGNATURE, PSS over a concatenated message returns 200.
    The test passed for four months while every authenticated endpoint was
    dead, because it only ever checked the code against itself."""
    auth = KalshiAuth(api_key="key", private_key_path=pem_file)
    headers = auth.sign_request("GET", "/trade-api/v2/markets")

    timestamp = headers["KALSHI-ACCESS-TIMESTAMP"]
    message = f"{timestamp}GET/trade-api/v2/markets".encode()
    signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])

    # Should not raise
    rsa_key_pair.public_key().verify(
        signature, message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=hashes.SHA256().digest_size),
        hashes.SHA256(),
    )


def test_timestamp_is_milliseconds(pem_file):
    auth = KalshiAuth(api_key="key", private_key_path=pem_file)
    headers = auth.sign_request("GET", "/markets")
    ts = int(headers["KALSHI-ACCESS-TIMESTAMP"])
    # Millisecond timestamps are > 1e12
    assert ts > 1_000_000_000_000


# --------------------------------------------------------------------------
# Signing scheme — verified against the live API 2026-08-11
# --------------------------------------------------------------------------

class TestSigningScheme:
    """Kalshi verifies RSA-PSS over a CONCATENATED message.

    Until 2026-08-11 this signed PKCS1v15 over a newline-delimited message and
    every authenticated endpoint returned 401. It stayed invisible because all
    market-data endpoints are public, so ingest, scoring and settlement worked
    — only balance, positions, fills, place_order and cancel_order were dead,
    none of which paper mode calls. The first thing a live flip would have done
    is fail.
    """

    def test_message_has_no_newline_separators(self, pem_file, rsa_key_pair):
        """The exact defect: newlines in the signed message."""
        import base64
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        auth = KalshiAuth(api_key="k", private_key_path=pem_file)
        headers = auth.sign_request("GET", "/trade-api/v2/portfolio/balance")
        ts = headers["KALSHI-ACCESS-TIMESTAMP"]
        expected = f"{ts}GET/trade-api/v2/portfolio/balance"

        # Verifying with the public key proves what was actually signed.
        public = rsa_key_pair.public_key()
        public.verify(
            base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
            expected.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )

    def test_pkcs1v15_signature_is_rejected(self, pem_file, rsa_key_pair):
        """Guard against a revert to the old padding."""
        import base64
        import pytest
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        auth = KalshiAuth(api_key="k", private_key_path=pem_file)
        headers = auth.sign_request("GET", "/x")
        ts = headers["KALSHI-ACCESS-TIMESTAMP"]
        with pytest.raises(InvalidSignature):
            rsa_key_pair.public_key().verify(
                base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
                f"{ts}GET/x".encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
