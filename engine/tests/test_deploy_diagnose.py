"""deploy/diagnose.sh — does it name the RIGHT wall?

Every fixture below is a REAL error string this account produced (or Alibaba documents), not an
invented one. The point of the script is that at 6am the founder gets one accurate cause instead of
guessing between activation, RAM policy, risk control, architecture and a bad zip — so a
misclassification is a real defect, and these tests are the guard on it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

DIAGNOSE = Path(__file__).resolve().parents[2] / "deploy" / "diagnose.sh"

# Exit codes (must match deploy_fc.sh)
OK, PACKAGE, NOT_ACTIVATED, ACCESS_DENIED, RISK, GENERIC, BOOTSTRAP = 0, 14, 20, 21, 22, 23, 33


def run(text: str) -> tuple[int, str]:
    p = subprocess.run([str(DIAGNOSE)], input=text, capture_output=True, text=True)
    return p.returncode, p.stdout


# --- REAL strings captured from this account -------------------------------------------------

FC_NOT_ACTIVATED = (
    'ERROR: SDK.ServerError\nErrorCode: AccessDenied\n'
    'Message: FC service is not enabled for current user.'
)
RAM_IMPLICIT_DENY = (
    "ErrorCode: AccessDenied\nMessage: the caller is not authorized to perform 'fc:ListFunctions' "
    "on resource 'acs:fc:ap-southeast-1:5550384261126497:functions/*'\n"
    "AccessDeniedDetail: map[NoPermissionType:ImplicitDeny PolicyType:AccountLevelIdentityBasedPolicy]"
)
RISK_CONTROL = (
    'Error code: RISK.RISK_CONTROL_REJECTION\n'
    'Error message: To keep your account secure, your order is suspended.'
)
QWEN_STYLE_ACCESS_DENIED = (
    '{"error":{"code":"AccessDenied.Unpurchased","message":"Access to model denied."}}'
)


def test_fc_not_activated_is_identified_precisely():
    """The blocker as of 2026-07-17. Must NOT be confused with a RAM policy gap."""
    rc, out = run(FC_NOT_ACTIVATED)
    assert rc == NOT_ACTIVATED
    assert "fc_not_activated" in out and "fcnext.console" in out


def test_ram_implicit_deny_is_identified_precisely():
    rc, out = run(RAM_IMPLICIT_DENY)
    assert rc == ACCESS_DENIED
    assert "ram_permission_missing" in out and "AliyunFCFullAccess" in out


def test_risk_control_wins_over_the_accessdenied_substring():
    """A risk-control body can ALSO contain 'AccessDenied'. Ordering must not mislabel it as RAM —
    the two have completely different remedies (one is a policy click, one is unfixable in code)."""
    rc, out = run(RISK_CONTROL + "\nAccessDenied")
    assert rc == RISK
    assert "risk_control_rejection" in out


def test_fc_not_activated_is_not_mistaken_for_ram():
    """Both bodies say ErrorCode: AccessDenied. Only one is fixed by attaching a policy."""
    rc, out = run(FC_NOT_ACTIVATED)
    assert "ram_permission_missing" not in out
    assert rc == NOT_ACTIVATED


@pytest.mark.parametrize(
    "text,code,needle",
    [
        ("standard_init_linux.go: exec format error", PACKAGE, "wrong_architecture"),
        ("/code/bootstrap: permission denied", BOOTSTRAP, "bootstrap_failed"),
        ("InvalidArgument: code package is invalid", PACKAGE, "invalid_package"),
        ("Error: request timed out after 60s", GENERIC, "timeout"),
        (QWEN_STYLE_ACCESS_DENIED, ACCESS_DENIED, "access_denied"),
    ],
)
def test_known_failure_signatures(text, code, needle):
    rc, out = run(text)
    assert rc == code, f"{needle}: expected exit {code}, got {rc} — {out}"
    assert needle in out


def test_success_output_is_not_diagnosed_as_a_failure():
    """A clean deploy must exit 0 — a false alarm here would send the founder chasing nothing."""
    rc, _ = run('{"functions": []}\nDeploy success\nhttps://x.ap-southeast-1.fcapp.run')
    assert rc == OK


def test_every_branch_prints_both_a_cause_and_a_fix():
    """A cause without a fix is just a nicer error message. Each branch must be actionable."""
    for text in (FC_NOT_ACTIVATED, RAM_IMPLICIT_DENY, RISK_CONTROL, "exec format error"):
        rc, out = run(text)
        assert rc != 0
        assert "CAUSE:" in out and "FIX:" in out, f"missing CAUSE/FIX for: {text[:40]}"


def test_diagnose_never_echoes_credentials():
    """Deploy output can contain an AccessKey. The diagnosis must not re-print it."""
    rc, out = run(f"AccessDenied\nAccessKeyId: LTAI5tFAKEKEY123\nsecret: abcdef123456")
    assert "LTAI5tFAKEKEY123" not in out and "abcdef123456" not in out
