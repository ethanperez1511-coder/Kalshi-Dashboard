"""RSA-PSS-SHA256 request signing for the Kalshi API v2."""
from __future__ import annotations

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def load_private_key(pem_path: str) -> rsa.RSAPrivateKey:
    pem_bytes = Path(pem_path).read_bytes()
    return _parse_pem(pem_bytes)


def load_private_key_from_string(pem_content: str) -> rsa.RSAPrivateKey:
    # Cloud env vars store multiline PEMs three ways; accept all of them:
    #   1. raw PEM with real newlines
    #   2. literal \n instead of real newlines (Railway-style)
    #   3. base64 of the whole PEM (robust transport — dodges newline corruption
    #      entirely; the preferred form for GitHub Actions secrets).
    s = pem_content.strip()
    if "-----BEGIN" not in s:
        # No PEM header present → assume base64-encoded PEM.
        s = base64.b64decode(s).decode()
    s = s.replace("\\n", "\n")
    return _parse_pem(s.encode())


def _parse_pem(pem_bytes: bytes) -> rsa.RSAPrivateKey:
    key = serialization.load_pem_private_key(pem_bytes, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError(f"Expected RSA private key, got {type(key).__name__}")
    return key


class KalshiAuth:
    def __init__(self, api_key: str, private_key_path: str = "", private_key_pem: str = ""):
        self.api_key = api_key
        if private_key_pem:
            self._private_key = load_private_key_from_string(private_key_pem)
        elif private_key_path:
            self._private_key = load_private_key(private_key_path)
        else:
            raise ValueError("Must provide either private_key_path or private_key_pem")

    def sign_request(self, method: str, path: str) -> dict[str, str]:
        """Sign a request the way Kalshi actually verifies it.

        Two things here were wrong until 2026-08-11 and every authenticated
        endpoint had been returning 401 the whole time:

          padding  must be PSS, not PKCS1v15
          message  must be timestamp + METHOD + path CONCATENATED, with no
                   newline separators

        Verified against the live API on /portfolio/balance — PSS+concat
        returns 200, and all three other combinations return
        INCORRECT_API_KEY_SIGNATURE.

        This was invisible because every market-data endpoint Kalshi serves is
        PUBLIC, so ingest, scoring and settlement all worked. Only balance,
        positions, fills, place_order and cancel_order were affected — none of
        which paper mode calls. The first thing a live flip would have done is
        fail.
        """
        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method.upper()}{path}"
        signature = self._private_key.sign(
            message.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256().digest_size,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        }
