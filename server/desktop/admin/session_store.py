"""Keep the desktop refresh token in the current Windows user's credential vault."""

from __future__ import annotations

import ctypes
from ctypes import wintypes


_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


class WindowsSessionStore:
    def __init__(self, server_url: str):
        self.target = "Eye-O-T/AI-CCTV-Desktop/" + server_url.strip().rstrip("/").casefold()
        self._api = ctypes.WinDLL("Advapi32", use_last_error=True)
        self._api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_Credential))]
        self._api.CredReadW.restype = wintypes.BOOL
        self._api.CredWriteW.argtypes = [ctypes.POINTER(_Credential), wintypes.DWORD]
        self._api.CredWriteW.restype = wintypes.BOOL
        self._api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self._api.CredDeleteW.restype = wintypes.BOOL
        self._api.CredFree.argtypes = [ctypes.c_void_p]
        self._api.CredFree.restype = None

    def read(self) -> tuple[str, str] | None:
        credential = ctypes.POINTER(_Credential)()
        if not self._api.CredReadW(self.target, _CRED_TYPE_GENERIC, 0, ctypes.byref(credential)):
            if ctypes.get_last_error() == _ERROR_NOT_FOUND:
                return None
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            record = credential.contents
            token = ctypes.string_at(record.CredentialBlob, record.CredentialBlobSize).decode("utf-8")
            return record.UserName or "", token
        finally:
            self._api.CredFree(credential)

    def write(self, username: str, refresh_token: str) -> None:
        value = refresh_token.encode("utf-8")
        blob = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        credential = _Credential(
            Type=_CRED_TYPE_GENERIC,
            TargetName=self.target,
            CredentialBlobSize=len(value),
            CredentialBlob=blob,
            Persist=_CRED_PERSIST_LOCAL_MACHINE,
            UserName=username,
        )
        if not self._api.CredWriteW(ctypes.byref(credential), 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def delete(self) -> None:
        if not self._api.CredDeleteW(self.target, _CRED_TYPE_GENERIC, 0):
            if ctypes.get_last_error() != _ERROR_NOT_FOUND:
                raise ctypes.WinError(ctypes.get_last_error())
