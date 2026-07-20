"""One-time Mac installer logic for the relay supervisor LaunchAgent (Bite 1.5 A4).

The TESTABLE core behind ``install_relay.sh``:

  * render the LaunchAgent plist from the template, asserting it carries NO secret (the device secret
    lives ONLY in the Keychain — never in the plist, argv, env, or logs);
  * lay out a durable state dir (0700) WITHOUT ever wiping existing state;
  * manage PINNED-COMMIT versions via a ``current`` symlink so upgrade AND rollback preserve durable
    state (the durable state dir is separate from the versioned code and is never touched).

The genuinely Mac-only side effects — writing the secret to the Keychain and loading the LaunchAgent —
are emitted as exact, SECRET-FREE commands (``keychain_add_argv`` uses ``security``'s interactive prompt,
so the secret never appears in argv) that the shell wrapper runs and the runbook documents for approval.
"""

from __future__ import annotations

import os
import stat

LABEL = "com.bruce.relay.supervisor"
KEYCHAIN_SERVICE = "com.bruce.relay.device-secret"   # must match relay/config.py
_TEMPLATE = os.path.join(os.path.dirname(__file__), "launchd", f"{LABEL}.plist")

# Substituted into the template. NONE of these is a secret (paths, a URL, a commit hash).
_PLACEHOLDERS = ("@PYTHON@", "@ENGINE_DIR@", "@STATE_DIR@", "@API_BASE_URL@", "@PINNED_COMMIT@")

# Patterns that must NEVER appear in a rendered plist (belt-and-suspenders secret guard).
_SECRET_MARKERS = ("BRUCE_RELAY_SECRET", "device-secret-value", "Authorization", "Bearer ", "password")


def load_template(path: str | None = None) -> str:
    with open(path or _TEMPLATE) as f:
        return f.read()


def render_plist(*, python: str, engine_dir: str, state_dir: str, api_base_url: str,
                 pinned_commit: str, template: str | None = None) -> str:
    """Render the LaunchAgent plist. Raises if any placeholder is left unfilled or a secret leaks in."""
    out = template if template is not None else load_template()
    for key, val in (("@PYTHON@", python), ("@ENGINE_DIR@", engine_dir), ("@STATE_DIR@", state_dir),
                     ("@API_BASE_URL@", api_base_url), ("@PINNED_COMMIT@", pinned_commit)):
        out = out.replace(key, val)
    leftover = [p for p in _PLACEHOLDERS if p in out]
    if leftover:
        raise ValueError(f"unfilled plist placeholders: {leftover}")
    if not python.startswith("/"):
        raise ValueError(f"python path must be absolute in the plist, got {python!r}")
    assert_plist_secret_free(out)
    assert_plist_safe_paths(out)
    return out


def assert_plist_secret_free(plist: str) -> None:
    low = plist.lower()
    for marker in _SECRET_MARKERS:
        if marker.lower() in low:
            raise ValueError(f"refusing to write a plist containing a secret marker: {marker!r}")


def assert_plist_safe_paths(plist: str) -> None:
    """The LaunchAgent must use ABSOLUTE paths and NO shell interpolation (ProgramArguments is an argv
    array, never a shell string) — so nothing is re-evaluated by a shell at launch."""
    for bad in ("$(", "`", "${"):
        if bad in plist:
            raise ValueError(f"refusing to write a plist with shell interpolation: {bad!r}")
    import re
    args = re.findall(r"<key>ProgramArguments</key>\s*<array>(.*?)</array>", plist, re.S)
    if args:
        first = re.findall(r"<string>(.*?)</string>", args[0], re.S)
        if first and not first[0].startswith("/"):
            raise ValueError(f"ProgramArguments[0] must be an absolute path, got {first[0]!r}")


def verify_extracted_safe(version_dir: str) -> None:
    """Reject a code checkout that contains a path-traversal or an UNSAFE SYMLINK — no file (regular or
    link) may resolve OUTSIDE the version dir. Belt-and-suspenders over `git archive` (which already emits
    only relative, in-tree paths from a trusted commit)."""
    root = os.path.realpath(version_dir)
    for dirpath, dirnames, filenames in os.walk(version_dir, followlinks=False):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            real = os.path.realpath(p)
            if real != root and not real.startswith(root + os.sep):
                raise ValueError(f"unsafe path escapes the version dir: {name!r}")


# durable state files/dirs the installer creates but MUST NEVER overwrite/wipe on re-run.
_DURABLE_FILES = ("checkpoint.json", "outbound_sent.json", "pending_attachments.json")
_DURABLE_DIRS = ("spool",)


def ensure_state_dir(state_dir: str) -> None:
    """Create the durable state dir (0700) and its private subdirs. NEVER wipes or truncates an existing
    file — normal start / upgrade / rollback must preserve checkpoint, ledger, and pending attachments."""
    os.makedirs(state_dir, exist_ok=True)
    os.chmod(state_dir, 0o700)
    for d in _DURABLE_DIRS:
        p = os.path.join(state_dir, d)
        os.makedirs(p, exist_ok=True)
        os.chmod(p, 0o700)
    # touch nothing else — existing durable files are left exactly as they are.


def activate_version(install_dir: str, commit: str, *, current_link: str = "current") -> str:
    """Point ``<install_dir>/current`` at ``<install_dir>/versions/<commit>`` (atomically). The versioned
    code checkout is done by the shell (git worktree/checkout of the approved sha) BEFORE this; here we
    only flip the symlink, so upgrade AND rollback are a symlink swap that never touches durable state.
    Returns the resolved code dir. Raises if that version isn't present."""
    version_dir = os.path.join(install_dir, "versions", commit)
    if not os.path.isdir(version_dir):
        raise FileNotFoundError(f"version not checked out: {version_dir}")
    verify_extracted_safe(version_dir)                 # reject a traversal / unsafe symlink before activating
    try:
        os.chmod(install_dir, 0o700)                   # safe perms on the code root
    except OSError:
        pass
    link = os.path.join(install_dir, current_link)
    tmp = link + ".tmp"
    if os.path.islink(tmp) or os.path.exists(tmp):
        os.remove(tmp)
    os.symlink(version_dir, tmp)
    os.replace(tmp, link)                          # atomic swap of the current pointer
    return version_dir


def active_version(install_dir: str, *, current_link: str = "current") -> str | None:
    link = os.path.join(install_dir, current_link)
    if not os.path.islink(link):
        return None
    target = os.path.realpath(link)
    return os.path.basename(target)                # the <commit> dir name


def launchagent_path(home: str) -> str:
    return os.path.join(home, "Library", "LaunchAgents", f"{LABEL}.plist")


def load_argv(plist_path: str, *, uid: int) -> list[list[str]]:
    """launchctl commands to (re)load the LaunchAgent for the GUI session of ``uid`` — idempotent: boot
    out any existing instance first, then bootstrap. RunAtLoad in the plist starts it (and after login)."""
    domain = f"gui/{uid}"
    return [["launchctl", "bootout", domain, plist_path],           # ok to fail if not loaded
            ["launchctl", "bootstrap", domain, plist_path],
            ["launchctl", "enable", f"{domain}/{LABEL}"]]


def kickstart_argv(uid: int) -> list[str]:
    """Restart the supervisor in place after an upgrade/rollback (picks up the retargeted `current` +
    new pinned commit) — WITHOUT reinstalling or wiping state."""
    return ["launchctl", "kickstart", "-k", f"gui/{uid}/{LABEL}"]


def write_plist(dest_path: str, contents: str) -> None:
    """Write the rendered plist (0644 — it holds no secret) into ~/Library/LaunchAgents, atomically."""
    assert_plist_secret_free(contents)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp = dest_path + ".tmp"
    with open(tmp, "w") as f:
        f.write(contents)
    os.chmod(tmp, 0o644)
    os.replace(tmp, dest_path)


# --------------------------------------------------------------------------- CLI (called by install_relay.sh)


def _poll_health(state_dir: str, *, deadline_s: float = 20.0) -> bool:
    """After (re)load, verify the supervisor came up healthy (running/parked with a fresh status), so a
    bad activation is rolled back. Reads only the content-free status file."""
    import time as _t

    from . import brucectl
    end = _t.time() + deadline_s
    while _t.time() < end:
        st = brucectl.read_status(os.path.join(state_dir, "supervisor-status.json"))
        if st and st.get("state") in ("running", "parked") and (_t.time() - float(st.get("updated_at", 0)) < 60):
            return True
        _t.sleep(0.5)
    return False


def _prepare(args) -> int:
    """Compatibility-checked, atomic install / upgrade / rollback: never wipes durable state, BLOCKS an
    incompatible rollback (on-disk state newer than the target supports), runs forward migrations after a
    privacy-safe backup, flips the `current` symlink atomically, writes the secret-free plist, (re)loads
    the LaunchAgent, and — unless --assume-healthy/--dry-run — verifies health and RESTORES the prior
    version+state if activation or health fails. Returns 0 on success."""
    import subprocess

    from . import state_manifest

    if args.dry_run or args.assume_healthy:
        # file work only; no health gate (dry-run prints; unit tests exercise file ops).
        if args.dry_run:
            print(f"[dry-run] ensure_state_dir {args.state_dir} (0700, no wipe)")
            print(f"[dry-run] compat-check + activate_version -> {os.path.join(args.install_dir, 'versions', args.commit)}")
        else:
            ensure_state_dir(args.state_dir)
            state_manifest.safe_activate(install_dir=args.install_dir, state_dir=args.state_dir,
                                         commit=args.commit,
                                         activate=lambda c: activate_version(args.install_dir, c),
                                         health_check=lambda: True)
            print(f"active version -> {active_version(args.install_dir)}")
    else:
        ensure_state_dir(args.state_dir)

    engine_dir = os.path.join(args.install_dir, "current", "engine")
    plist = render_plist(python=args.python, engine_dir=engine_dir, state_dir=args.state_dir,
                         api_base_url=args.api_base_url, pinned_commit=args.commit)
    dest = launchagent_path(args.home)

    def _reload():
        for c in load_argv(dest, uid=args.uid) + [kickstart_argv(args.uid)]:
            subprocess.run(c, check=False)                 # bootout may fail if not loaded — that's fine

    if args.dry_run:
        print(f"[dry-run] write_plist -> {dest} (0644, secret-free: OK)")
        for c in load_argv(dest, uid=args.uid) + [kickstart_argv(args.uid)]:
            print("[dry-run] " + " ".join(c))
        return 0

    if not args.assume_healthy:
        # full path: activation is gated on post-load health; a failure restores the prior version+state.
        ensure_state_dir(args.state_dir)

        def _activate_and_load(commit):
            activate_version(args.install_dir, commit)
            write_plist(dest, render_plist(python=args.python, engine_dir=engine_dir, state_dir=args.state_dir,
                                           api_base_url=args.api_base_url, pinned_commit=commit))
            _reload()
        state_manifest.safe_activate(install_dir=args.install_dir, state_dir=args.state_dir, commit=args.commit,
                                     activate=_activate_and_load,
                                     health_check=lambda: _poll_health(args.state_dir))
        print(f"active version -> {active_version(args.install_dir)} (healthy)")
    else:
        write_plist(dest, plist)
        _reload()
        print(f"wrote {dest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Relay LaunchAgent installer core (invoked by install_relay.sh).")
    sub = p.add_subparsers(dest="command", required=True)
    pr = sub.add_parser("prepare", help="ensure state dir + activate version + render/write plist + (re)load")
    pr.add_argument("--install-dir", dest="install_dir", required=True)
    pr.add_argument("--state-dir", dest="state_dir", required=True)
    pr.add_argument("--commit", required=True)
    pr.add_argument("--python", required=True)
    pr.add_argument("--api-base-url", dest="api_base_url", required=True)
    pr.add_argument("--home", default=os.path.expanduser("~"))
    pr.add_argument("--uid", type=int, default=os.getuid())
    pr.add_argument("--dry-run", dest="dry_run", action="store_true")
    pr.add_argument("--assume-healthy", dest="assume_healthy", action="store_true",
                    help="skip the post-load health gate (used by tests; the shell uses the real gate)")
    args = p.parse_args(argv)
    if args.command == "prepare":
        return _prepare(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
