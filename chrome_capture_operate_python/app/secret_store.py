"""Windows DPAPI helpers for encrypted login account values.

密文格式：``dpapi:v1:<base64>``。
使用当前 Windows 用户的 DPAPI（CryptProtectData/CryptUnprotectData），
不需要额外的主密钥文件；换 Windows 用户或换机器后无法解密。
"""
import base64
import ctypes
import os
from ctypes import wintypes

PREFIX = "dpapi:v1:"
_ENTROPY = b"chrome_capture_operate.login_accounts.v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class SecretStoreError(Exception):
    """加密/解密失败。"""


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _load_dlls():
    if os.name != "nt":
        raise SecretStoreError("密码字段加密仅支持 Windows（DPAPI）")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), wintypes.LPCWSTR,
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wintypes.DWORD, ctypes.POINTER(_DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _blob(data):
    buf = ctypes.create_string_buffer(data)
    blob = _DATA_BLOB(
        len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


def _raise_last_error(action):
    code = ctypes.get_last_error()
    if code:
        raise SecretStoreError("%s 失败（Windows 错误 %d）" % (action, code))
    raise SecretStoreError("%s 失败" % action)


def is_encrypted(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def protect(text):
    """加密 UTF-8 文本，返回 dpapi:v1:... 字符串。"""
    crypt32, kernel32 = _load_dlls()
    data = str(text).encode("utf-8")
    in_blob, _in_buf = _blob(data)
    entropy_blob, _entropy_buf = _blob(_ENTROPY)
    out_blob = _DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, ctypes.byref(entropy_blob),
        None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob))
    if not ok:
        _raise_last_error("CryptProtectData")
    try:
        raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(out_blob.pbData)
    return PREFIX + base64.b64encode(raw).decode("ascii")


def unprotect(text):
    """解密 dpapi:v1:... 字符串；失败抛 SecretStoreError。"""
    if not is_encrypted(text):
        raise SecretStoreError("不是本程序生成的密码密文")
    try:
        raw = base64.b64decode(text[len(PREFIX):], validate=True)
    except Exception as e:
        raise SecretStoreError("密码密文格式错误：%s" % e)
    crypt32, kernel32 = _load_dlls()
    in_blob, _in_buf = _blob(raw)
    entropy_blob, _entropy_buf = _blob(_ENTROPY)
    out_blob = _DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, ctypes.byref(entropy_blob),
        None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob))
    if not ok:
        _raise_last_error("CryptUnprotectData")
    try:
        plain = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(out_blob.pbData)
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SecretStoreError("密码明文编码错误：%s" % e)