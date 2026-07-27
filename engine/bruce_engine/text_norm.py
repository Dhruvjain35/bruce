"""One text normalizer, applied before ANY matcher runs.

Every deterministic matcher in this codebase was written with straight apostrophes (`can'?t`, `don'?t`)
and then fed raw text from a phone, which sends curly ones. The result is a class of silent failure that
has now bitten three separate times in one session:

  * `_CHATTER_RE` (messaging_inbound) missed "i'm saying yo" -> a greeting became an intake mission.
  * `_NEG` (decision_resolver) missed "don't" -> a refusal was read as approval, the P0 incident.
  * `_CAL_DENIAL_RE` (capability_truth) missed "i can't add it to your calendar" -> the guard that
    exists to correct a FALSE capability denial never fired, and Bruce told a student it could not touch
    a calendar it has full write access to.

Each was invisible until a real message hit it, and each looked like a reasoning failure rather than a
punctuation one. Folding here is not another phrase rule — it removes a whole category of miss from
every matcher at once, and it is a precondition for measuring a true routing miss rate: without it,
"the router did not understand" and "the router saw a smart quote" are indistinguishable.
"""

from __future__ import annotations

import re
import unicodedata

# Unicode punctuation a phone/keyboard substitutes silently.
_FOLD = {
    "’": "'", "‘": "'", "ʼ": "'", "′": "'",     # curly / modifier apostrophes
    "“": '"', "”": '"',                                    # curly quotes
    "–": "-", "—": "-", "−": "-",                     # en/em dash, minus
    "…": "...",                                                 # ellipsis
    " ": " ", "​": "", "﻿": "",                       # nbsp, zero-width
}

_WS = re.compile(r"\s+")


def fold(text: str | None) -> str:
    """Canonical form for MATCHING (never for display or storage).

    NFKC, unicode punctuation folded to ASCII, whitespace collapsed. Case and apostrophes are preserved
    so a caller can still distinguish "It's" from "its" if it needs to; use `fold_match` when you want
    the aggressive form used for intent matching.
    """
    t = unicodedata.normalize("NFKC", text or "")
    for k, v in _FOLD.items():
        t = t.replace(k, v)
    return _WS.sub(" ", t).strip()


def fold_match(text: str | None) -> str:
    """The aggressive form for intent/negation matching: `fold` plus lowercase and apostrophes removed,
    so "Don't" / "don’t" / "dont" are one token. Contractions collapse rather than needing `'?` in every
    pattern — the omission of which caused all three incidents above."""
    return fold(text).lower().replace("'", "")
