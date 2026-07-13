"""Tests for the pure email-resolution helpers (no network).

Email grounding is the highest-stakes anti-hallucination surface: we never fabricate an
address and we validate domains against the institution's ROR record. These lock in the
tiered picking logic, domain/subdomain matching, deobfuscation, and prose parsing — all of
which are deterministic and offline.
"""

from __future__ import annotations

from bruce_engine.email_resolver import (
    _deobfuscate,
    _domain_ok,
    _emails_on_page,
    _last_name,
    _parse_ws,
    _pick_email,
)


# ---------- _pick_email ----------


def test_pick_email_tier1_domain_plus_name():
    emails = ["random@rochester.edu", "smith@rochester.edu"]
    email, validated = _pick_email(emails, "smith", ["rochester.edu"])
    assert email == "smith@rochester.edu"
    assert validated is True


def test_pick_email_tier1_prefers_name_match_over_earlier_domain_only():
    # a domain-only address appears first, but the name-matching one must win Tier 1
    emails = ["lab-office@rochester.edu", "jsmith@rochester.edu"]
    email, validated = _pick_email(emails, "smith", ["rochester.edu"])
    assert email == "jsmith@rochester.edu"
    assert validated is True


def test_pick_email_tier2_domain_only():
    # no local part matches the last name, but the domain is institution-validated
    emails = ["frontdesk@rochester.edu"]
    email, validated = _pick_email(emails, "smith", ["rochester.edu"])
    assert email == "frontdesk@rochester.edu"
    assert validated is True


def test_pick_email_tier3_name_only_no_domains():
    # no ROR domains to check, but the last name is in the local part -> unvalidated hit
    emails = ["smith@gmail.com"]
    email, validated = _pick_email(emails, "smith", [])
    assert email == "smith@gmail.com"
    assert validated is False


def test_pick_email_none_when_no_domain_and_no_name_match():
    emails = ["jdoe@gmail.com"]
    email, validated = _pick_email(emails, "smith", [])
    assert email is None
    assert validated is False


def test_pick_email_none_for_empty_list():
    email, validated = _pick_email([], "smith", ["rochester.edu"])
    assert email is None
    assert validated is False


def test_pick_email_does_not_use_name_match_when_domains_present_but_wrong():
    # domains provided but none match, and name-only tier is gated behind `not domains`
    emails = ["smith@gmail.com"]
    email, validated = _pick_email(emails, "smith", ["rochester.edu"])
    assert email is None
    assert validated is False


# ---------- _domain_ok ----------


def test_domain_ok_exact_match():
    assert _domain_ok("rochester.edu", ["rochester.edu"]) is True


def test_domain_ok_subdomain_match():
    # ur.rochester.edu is a legitimate subdomain of rochester.edu
    assert _domain_ok("ur.rochester.edu", ["rochester.edu"]) is True


def test_domain_ok_rejects_suffix_hijack():
    # evil-rochester.edu must NOT be accepted just because it ends in "rochester.edu"
    assert _domain_ok("evil-rochester.edu", ["rochester.edu"]) is False
    assert _domain_ok("notrochester.edu", ["rochester.edu"]) is False


def test_domain_ok_parent_not_ok_for_subdomain_allowlist():
    # a parent domain is not covered by a subdomain-only allowlist
    assert _domain_ok("rochester.edu", ["ur.rochester.edu"]) is False


def test_domain_ok_empty_domains_is_false():
    assert _domain_ok("mit.edu", []) is False


# ---------- _deobfuscate ----------


def test_deobfuscate_at_variants():
    assert _deobfuscate("john[at]mit.edu") == "john@mit.edu"
    assert _deobfuscate("john(at)mit.edu") == "john@mit.edu"
    assert _deobfuscate("john&#64;mit.edu") == "john@mit.edu"


def test_deobfuscate_dot_variants():
    assert _deobfuscate("john at mit dot edu") == "john@mit.edu"
    assert _deobfuscate("jane[at]cs[dot]stanford[dot]edu") == "jane@cs.stanford.edu"


def test_deobfuscate_leaves_clean_email_untouched():
    assert _deobfuscate("john@mit.edu") == "john@mit.edu"


# ---------- _emails_on_page ----------


def test_emails_on_page_extracts_and_lowercases():
    html = "<p>Contact: John.Smith@MIT.EDU</p>"
    assert _emails_on_page(html) == {"john.smith@mit.edu"}


def test_emails_on_page_deobfuscates_then_extracts():
    # classic spelled-out obfuscation: "name at domain dot edu"
    html = "Write to jane at rochester dot edu today."
    assert _emails_on_page(html) == {"jane@rochester.edu"}


def test_emails_on_page_finds_multiple_and_dedups():
    html = "a@x.edu and b@y.edu and A@X.EDU"
    assert _emails_on_page(html) == {"a@x.edu", "b@y.edu"}


def test_emails_on_page_empty_when_none_present():
    assert _emails_on_page("no addresses here") == set()


# ---------- _last_name ----------


def test_last_name_basic():
    assert _last_name("John Smith") == "smith"


def test_last_name_drops_single_letter_middle_and_initials():
    assert _last_name("Jane A. Doe") == "doe"


def test_last_name_single_token():
    assert _last_name("Cher") == "cher"


def test_last_name_empty_string():
    assert _last_name("") == ""


# ---------- _parse_ws ----------


def test_parse_ws_extracts_email_and_url_from_prose():
    text = "You can reach Dr. Smith at smith@mit.edu — see https://mit.edu/~smith for more."
    email, url = _parse_ws(text)
    assert email == "smith@mit.edu"
    assert url == "https://mit.edu/~smith"


def test_parse_ws_deobfuscates_email():
    text = "Contact: smith[at]mit.edu (faculty page unavailable)"
    email, url = _parse_ws(text)
    assert email == "smith@mit.edu"
    assert url is None


def test_parse_ws_none_when_nothing_found():
    email, url = _parse_ws("No email address could be located.")
    assert email is None
    assert url is None
