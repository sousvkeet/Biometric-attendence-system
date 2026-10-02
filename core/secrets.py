"""Encryption of stored credentials (backup destination passwords, keys) with a key derived from SECRET_KEY.

Note: if SECRET_KEY changes, stored credentials can no longer be read and must be entered again.
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    key = hashlib.sha256(("bioattend-credentials:" + settings.SECRET_KEY).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(value):
    if not value:
        return ""
    return _fernet().encrypt(value.encode()).decode()


def decrypt_secret(token):
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return ""
