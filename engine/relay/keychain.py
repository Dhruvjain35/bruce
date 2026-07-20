"""macOS login-Keychain access via Security.framework (Bite 1.5 A4 gap 1).

Stores/reads the relay device credential through ``SecItemAdd`` / ``SecItemUpdate`` / ``SecItemCopyMatching``
directly, so the raw secret exists ONLY in this process's memory before it is handed to the Keychain — it
is never placed in argv (unlike ``security -w``), in the environment, on disk, or in a log. Used by the
installer's one-command bootstrap so the operator never sees or pastes the permanent credential.

macOS only. On any other platform (or if the frameworks can't load) the calls raise KeychainUnavailable,
so the flow fails closed rather than silently mis-storing the credential. The Python-level behavior is
unit-tested; the real Keychain round-trip is a documented on-device step.
"""

from __future__ import annotations

import ctypes
import sys

SERVICE = "com.bruce.relay.device-secret"   # must match relay/config.py + installer.py


class KeychainError(Exception):
    pass


class KeychainUnavailable(KeychainError):
    """Security.framework / CoreFoundation is not available (non-macOS, or the load failed)."""


def _load():
    if sys.platform != "darwin":
        raise KeychainUnavailable("Keychain access requires macOS (Security.framework)")
    try:
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        sec = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    except OSError as exc:  # pragma: no cover - only on a broken macOS
        raise KeychainUnavailable(str(exc)) from exc
    return cf, sec


def _validate(account: str, secret: str | None = None) -> None:
    if not account or "\x00" in account:
        raise KeychainError("invalid account")
    if secret is not None and (not secret or "\x00" in secret):
        raise KeychainError("invalid secret")


def _cf_str(cf, s: str):
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    return cf.CFStringCreateWithCString(None, s.encode("utf-8"), 0x08000100)   # kCFStringEncodingUTF8


def _cf_data(cf, b: bytes):
    cf.CFDataCreate.restype = ctypes.c_void_p
    cf.CFDataCreate.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long]
    return cf.CFDataCreate(None, b, len(b))


def _cf_dict(cf, pairs):
    keys = (ctypes.c_void_p * len(pairs))(*[k for k, _ in pairs])
    vals = (ctypes.c_void_p * len(pairs))(*[v for _, v in pairs])
    cf.CFDictionaryCreate.restype = ctypes.c_void_p
    cf.CFDictionaryCreate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long,
                                      ctypes.c_void_p, ctypes.c_void_p]
    # kCFTypeDictionaryKeyCallBacks / ValueCallBacks
    kcb = ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")
    vcb = ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryValueCallBacks")
    return cf.CFDictionaryCreate(None, keys, vals, len(pairs), ctypes.byref(kcb), ctypes.byref(vcb))


def _const(sec, name):
    return ctypes.c_void_p.in_dll(sec, name)


def set_password(account: str, secret: str, *, service: str = SERVICE) -> None:
    """Store (or update) the credential. The secret is passed straight to SecItemAdd/Update — never argv."""
    _validate(account, secret)
    cf, sec = _load()
    cls_key = _const(sec, "kSecClass")
    cls_val = _const(sec, "kSecClassGenericPassword")
    a_svc = _const(sec, "kSecAttrService")
    a_acc = _const(sec, "kSecAttrAccount")
    v_data = _const(sec, "kSecValueData")
    svc, acc, data = _cf_str(cf, service), _cf_str(cf, account), _cf_data(cf, secret.encode("utf-8"))
    query = _cf_dict(cf, [(cls_key, cls_val), (a_svc, svc), (a_acc, acc)])
    add = _cf_dict(cf, [(cls_key, cls_val), (a_svc, svc), (a_acc, acc), (v_data, data)])
    sec.SecItemAdd.restype = ctypes.c_int32
    sec.SecItemAdd.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    status = sec.SecItemAdd(add, None)
    if status == -25299:                                   # errSecDuplicateItem -> update in place
        upd = _cf_dict(cf, [(v_data, data)])
        sec.SecItemUpdate.restype = ctypes.c_int32
        sec.SecItemUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        status = sec.SecItemUpdate(query, upd)
    if status != 0:
        raise KeychainError(f"SecItemAdd/Update failed (OSStatus {status})")


def get_password(account: str, *, service: str = SERVICE) -> str | None:
    _validate(account)
    cf, sec = _load()
    a_svc = _const(sec, "kSecAttrService")
    a_acc = _const(sec, "kSecAttrAccount")
    ret_data = _const(sec, "kSecReturnData")
    svc, acc = _cf_str(cf, service), _cf_str(cf, account)
    true_val = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue")
    query = _cf_dict(cf, [(_const(sec, "kSecClass"), _const(sec, "kSecClassGenericPassword")),
                          (a_svc, svc), (a_acc, acc), (ret_data, true_val)])
    out = ctypes.c_void_p()
    sec.SecItemCopyMatching.restype = ctypes.c_int32
    sec.SecItemCopyMatching.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    status = sec.SecItemCopyMatching(query, ctypes.byref(out))
    if status == -25300:                                   # errSecItemNotFound
        return None
    if status != 0:
        raise KeychainError(f"SecItemCopyMatching failed (OSStatus {status})")
    cf.CFDataGetLength.restype = ctypes.c_long
    cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
    cf.CFDataGetBytePtr.restype = ctypes.c_void_p
    cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
    n = cf.CFDataGetLength(out)
    ptr = cf.CFDataGetBytePtr(out)
    return ctypes.string_at(ptr, n).decode("utf-8")


def delete_password(account: str, *, service: str = SERVICE) -> None:
    _validate(account)
    cf, sec = _load()
    query = _cf_dict(cf, [(_const(sec, "kSecClass"), _const(sec, "kSecClassGenericPassword")),
                          (_const(sec, "kSecAttrService"), _cf_str(cf, service)),
                          (_const(sec, "kSecAttrAccount"), _cf_str(cf, account))])
    sec.SecItemDelete.restype = ctypes.c_int32
    sec.SecItemDelete.argtypes = [ctypes.c_void_p]
    sec.SecItemDelete(query)
