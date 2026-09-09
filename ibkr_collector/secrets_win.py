"""Windows Credential Manager helpers for the ingest bearer token."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

from ibkr_collector import CREDENTIAL_TARGET

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def read_ingest_token(target: str = CREDENTIAL_TARGET) -> str | None:
    env = (os.environ.get("IBKR_INGEST_TOKEN") or "").strip()
    if env:
        return env
    if os.name != "nt":
        return None
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    cred_ptr = ctypes.c_void_p()
    if not advapi.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(cred_ptr)):
        return None
    try:
        cred = ctypes.cast(cred_ptr, ctypes.POINTER(_CREDENTIAL)).contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        return blob.decode("utf-16le").rstrip("\x00")
    finally:
        advapi.CredFree(cred_ptr)


def write_ingest_token(token: str, target: str = CREDENTIAL_TARGET) -> None:
    if os.name != "nt":
        raise RuntimeError("Windows Credential Manager is only available on Windows")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    blob = token.encode("utf-16le")
    cred = _CREDENTIAL()
    cred.Type = CRED_TYPE_GENERIC
    cred.TargetName = target
    cred.CredentialBlobSize = len(blob)
    blob_buf = ctypes.create_string_buffer(blob)
    cred.CredentialBlob = ctypes.cast(blob_buf, ctypes.POINTER(ctypes.c_char))
    cred.Persist = CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = "ibkr-ingest"
    if not advapi.CredWriteW(ctypes.byref(cred), 0):
        raise ctypes.WinError(ctypes.get_last_error())
