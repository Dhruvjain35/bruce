"""Generate a short-lived internal-test sign-in link (operator tool — NOT a public endpoint).

The authorized-operator flow behind the E1 zero-terminal test surface: mint a SHORT-LIVED, e1_magic-scoped
magic link for an INTERNAL user so the founder can sign in normally in the browser — no JWT to paste, no
curl, no dev tools, no Terminal. The link is exchanged for a fresh HttpOnly session cookie and expires
quickly. The operator is authenticated by holding the signing secret (BRUCE_JWT_SECRET); the target user
must also be on the server-side internal allowlist (BRUCE_INTERNAL_USER_IDS) for the link to work.

    BRUCE_JWT_SECRET=... [BRUCE_JWT_AUDIENCE=...] \
      python -m scripts.internal_magic_link mint --user <uuid> --base-url https://<api-host> [--ttl 600]

Prints ONE URL. It is short-lived; treat it like a one-time link (deliver it privately).
"""

from __future__ import annotations

import argparse
import uuid

from bruce_engine import internal_test


def _run(args: argparse.Namespace) -> None:
    if args.command == "mint":
        token = internal_test.mint_magic_link_token(uuid.UUID(args.user), ttl_seconds=args.ttl)
        base = args.base_url.rstrip("/")
        print(f"internal test sign-in link for user={args.user} (expires in {args.ttl}s):")
        print(f"  {base}/internal/test/auth?t={token}")
        print("Open it in the browser once; it establishes a short-lived HttpOnly session and expires.")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bruce internal-test magic sign-in link (operator tool).")
    sub = p.add_subparsers(dest="command", required=True)
    m = sub.add_parser("mint", help="mint a short-lived internal-test sign-in link")
    m.add_argument("--user", required=True, help="internal user id (UUID) — must be on the allowlist")
    m.add_argument("--base-url", required=True, help="the API base URL (e.g. https://bruce-api-...run.app)")
    m.add_argument("--ttl", type=int, default=internal_test.MAGIC_DEFAULT_TTL, help="link lifetime in seconds")
    return p


def main() -> None:
    _run(_build_parser().parse_args())


if __name__ == "__main__":
    main()
