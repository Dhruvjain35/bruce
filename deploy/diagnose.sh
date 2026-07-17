#!/usr/bin/env bash
# Classify an Alibaba deploy failure into ONE actionable cause.
#
# Split out of deploy_fc.sh so it is testable in isolation (tests/test_deploy_diagnose.py) — the
# whole value of this file is that the founder never has to guess which of the six plausible walls
# they just hit at 6am. Reads the failure text on stdin, prints the cause, exits with its code.
#
#   echo "$output" | ./deploy/diagnose.sh ; echo $?
#
# Exit codes match deploy_fc.sh:
#   0 no known failure   14 wrong architecture/invalid package   20 FC not activated
#   21 AccessDenied / RAM policy   22 risk control   23 generic deploy failure
#   33 bootstrap failed to start

set -uo pipefail
OUT="$(cat)"

emit() { printf "CAUSE: %s\n" "$1"; printf "FIX:   %s\n" "$2"; exit "$3"; }

# Order matters: most specific first. Risk control is checked before AccessDenied because a
# risk-control body can also contain the string "AccessDenied".
if grep -qE "RISK[._]|RISK_CONTROL" <<<"$OUT"; then
  emit "risk_control_rejection — the account-level hold now covers Function Compute" \
       "This is the same hold blocking Qwen inference. Record the verbatim error in docs/deployment-verification.md and STOP. Not fixable in code." 22
fi
if grep -q "FC service is not enabled" <<<"$OUT"; then
  emit "fc_not_activated — Function Compute has never been activated on this account" \
       "https://fcnext.console.aliyun.com/ -> region Singapore -> activate (needs SMS). Not a code problem." 20
fi
if grep -qE "ImplicitDeny|not authorized to perform" <<<"$OUT"; then
  emit "ram_permission_missing — the AccessKey has no policy for this action" \
       "Attach AliyunFCFullAccess to the RAM user (NOT AdministratorAccess)." 21
fi
if grep -qiE "exec format error|cannot execute binary|invalid ELF" <<<"$OUT"; then
  emit "wrong_architecture — package holds arm64 wheels; FC runs linux/amd64" \
       "Rebuild with deploy/build-package.sh (installs inside an amd64 container)." 14
fi
if grep -qiE "bootstrap.*(not found|permission denied)|exec.*bootstrap" <<<"$OUT"; then
  emit "bootstrap_failed — FC could not execute ./bootstrap" \
       "Check the exec bit (must be 0o755). deploy/build-package.sh zips with the zip CLI because python zipfile drops it." 33
fi
if grep -qiE "InvalidArgument|code package|zip.*invalid|Unzip" <<<"$OUT"; then
  emit "invalid_package — FC rejected the code package" \
       "Rebuild with deploy/build-package.sh and check the base64 size is under 100MB." 14
fi
if grep -qi "AccessDenied" <<<"$OUT"; then
  emit "access_denied — Alibaba refused (not activation, not a known RAM gap)" \
       "Read the response body above; check the RAM policy and the region." 21
fi
if grep -qiE "timeout|timed out|deadline exceeded" <<<"$OUT"; then
  emit "timeout — the deploy call timed out" \
       "Retry. If it recurs, check FC console for a partially created function." 23
fi
if grep -qiE "error|failed|exception" <<<"$OUT"; then
  emit "unknown_deploy_failure" "Read the deploy output above; no known signature matched." 23
fi
exit 0
