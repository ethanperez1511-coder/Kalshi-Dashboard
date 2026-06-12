"""RSA-PKCS1v15-SHA256 request signing for the Kalshi API v2."""
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
    # Cloud env vars (Railway, Render, etc.) often store multiline values with
    # literal \n instead of real newlines — decode both forms.
    pem_content = pem_content.replace("\\n", "\n")
    return _parse_pem(pem_content.encode())


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
        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}\n{method.upper()}\n{path}"
        signature = self._private_key.sign(
            message.encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        }
