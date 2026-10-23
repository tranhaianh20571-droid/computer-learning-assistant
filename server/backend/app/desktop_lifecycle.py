"""Observe owner-pipe closure without holding a Windows CRT stream lock."""
import os
import sys
import time


def wait_for_owner():
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        peek = kernel.PeekNamedPipe
        peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        peek.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
        while peek(handle, None, 0, None, None, None):
            time.sleep(.2)
        if ctypes.get_last_error() not in (109, 232, 6):
            raise RuntimeError("OWNER_PIPE_FAILED")
    else:
        while os.read(sys.stdin.fileno(), 1):
            pass
