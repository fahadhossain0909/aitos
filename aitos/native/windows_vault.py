"""Windows Credential Manager generic credentials, current-user persistence.

No file/environment fallback. Other processes running as the same Windows user
may read generic credentials; this is not protection from a compromised account.
"""

from __future__ import annotations

import ctypes
import re
import sys
from ctypes import wintypes


class VaultUnavailable(RuntimeError):
    pass


class Credential(ctypes.Structure):
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


class WindowsCredentialAPI:
    def __init__(self):
        if sys.platform != "win32":
            raise VaultUnavailable("Windows Credential Manager required")
        self.dll = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self.dll.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
        self.dll.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(Credential)),
        ]
        self.dll.CredDeleteW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        for method in ("CredWriteW", "CredReadW", "CredDeleteW"):
            getattr(self.dll, method).restype = wintypes.BOOL
        self.dll.CredFree.argtypes = [ctypes.c_void_p]
        self.dll.CredFree.restype = None

    def write(self, target, data):
        blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        record = Credential(
            Type=1,
            TargetName=target,
            CredentialBlobSize=len(data),
            CredentialBlob=blob,
            Persist=2,
            UserName="AITOS operator",
        )
        try:
            if not self.dll.CredWriteW(ctypes.byref(record), 0):
                raise VaultUnavailable("Native credential write failed")
        finally:
            ctypes.memset(blob, 0, len(data))

    def read(self, target):
        pointer = ctypes.POINTER(Credential)()
        if not self.dll.CredReadW(target, 1, 0, ctypes.byref(pointer)):
            if ctypes.get_last_error() == 1168:
                return None
            raise VaultUnavailable("Native credential read failed")
        try:
            size = pointer.contents.CredentialBlobSize
            if not 0 < size <= 2560:
                raise VaultUnavailable("Invalid native credential size")
            return ctypes.string_at(pointer.contents.CredentialBlob, size)
        finally:
            self.dll.CredFree(pointer)

    def delete(self, target):
        if self.dll.CredDeleteW(target, 1, 0):
            return True
        if ctypes.get_last_error() == 1168:
            return False
        raise VaultUnavailable("Native credential deletion failed")


class WindowsVault:
    def __init__(self, *, api=None):
        self._api = api if api is not None else WindowsCredentialAPI()

    @staticmethod
    def _target(slot):
        if (
            type(slot) is not str
            or not re.fullmatch(r"[a-zA-Z0-9_.:/-]{1,120}", slot)
            or slot.startswith("/")
            or any(part in {".", ".."} for part in slot.split("/"))
        ):
            raise ValueError("invalid vault slot")
        return "AITOS/v1/" + slot

    def put(self, slot, secret):
        target = self._target(slot)
        if type(secret) is not str or not secret or "\0" in secret:
            raise ValueError("nonempty secret required")
        data = secret.encode("utf-16-le")
        if len(data) > 2560:
            raise ValueError("native secret size exceeded")
        self._api.write(target, data)

    def get(self, slot):
        data = self._api.read(self._target(slot))
        if data is None:
            return None
        try:
            return data.decode("utf-16-le")
        except UnicodeError:
            raise VaultUnavailable("Native credential encoding invalid") from None

    def delete(self, slot):
        return self._api.delete(self._target(slot))
