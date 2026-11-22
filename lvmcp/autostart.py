"""Start lvmcp_server.vi when it is not running: open it in LabVIEW (it runs when opened) and wait until it
listens. Used by the MCP server and the selftest (on Windows by default)."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

SERVER_VI = Path(__file__).resolve().parent.parent / "labview" / "lvmcp_server.vi"


def listening(host: str, port: int, timeout=2.0) -> bool:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def enabled(mode: str, host: str) -> bool:
    """mode auto/on/off; auto = Windows and a local server."""
    if mode == "off":
        return False
    return mode == "on" or (sys.platform == "win32" and host in ("127.0.0.1", "localhost"))


def start(host: str, port: int, server_vi: str | Path = SERVER_VI, labview: str = "", wait: float = 150) -> bool:
    """Open the server VI (with `labview`, else the program Windows opens .vi files with) and wait up to `wait`
    seconds (a cold LabVIEW 2014 start takes ~30-60 s) until it listens. True if it does."""
    t0 = time.monotonic()
    try:
        if labview:
            subprocess.Popen([labview, str(server_vi)], close_fds=True)
        elif sys.platform == "win32":
            os.startfile(str(server_vi))                       # noqa: S606  (the .vi file association)
        else:
            return False
    except OSError:
        return False
    while time.monotonic() - t0 < wait:
        if listening(host, port):
            time.sleep(1)                                      # let the server VI finish starting
            return True
        time.sleep(1)
    return False
