"""Fix-forward hardening of the auto-merged Bite 1.5 relay slice (review findings):
  A2  crash-durable restart-safe stores (fsync file+dir) — prevents a reverted phase double-sending
  A3  the watch() child suppresses stderr — message content can't leak to the plist log file
  B1  the audit actor is derived from the real uid, not env-spoofable $LOGNAME/$USER
"""

from __future__ import annotations

import asyncio
import json
import os

from relay.durable import write_json_durable


def test_write_json_durable_roundtrip_and_atomic(tmp_path):
    p = str(tmp_path / "s.json")
    write_json_durable(p, {"a": 1, "b": [1, 2, 3]})
    assert json.load(open(p)) == {"a": 1, "b": [1, 2, 3]}
    assert not os.path.exists(p + ".tmp")            # temp cleaned by the atomic replace


def test_write_json_durable_fsyncs_file_and_dir(tmp_path, monkeypatch):
    seen = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (seen.append(fd), real(fd))[1])
    write_json_durable(str(tmp_path / "t.json"), {"k": 1})
    assert len(seen) >= 2                            # the file AND its directory are fsynced


def test_login_name_ignores_env_spoof(monkeypatch):
    import pwd

    from relay.brucectl import _login_name
    real = pwd.getpwuid(os.getuid()).pw_name
    monkeypatch.setenv("LOGNAME", "spoofed-ceo")
    monkeypatch.setenv("USER", "spoofed-ceo")
    assert _login_name() == real                     # pwd-by-uid, not the forged env value


def test_watch_child_suppresses_stderr(monkeypatch):
    import relay.imsg as im

    captured = {}

    class _Stdin:
        def write(self, b): pass
        async def drain(self): pass

    class _Stdout:
        def __init__(self): self.n = 0
        async def readline(self):
            self.n += 1
            return b'{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n' if self.n == 1 else b''

    class _Proc:
        def __init__(self): self.stdin, self.stdout = _Stdin(), _Stdout()

    async def fake_exec(*a, **k):
        captured.update(k)
        return _Proc()

    monkeypatch.setattr(im.asyncio, "create_subprocess_exec", fake_exec)

    async def drive():
        async for _ in im.SubprocessImsg("imsg").watch():
            pass

    asyncio.run(drive())
    assert captured.get("stderr") == im.asyncio.subprocess.DEVNULL   # not inherited -> no content on disk
