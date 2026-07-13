"""Email resolution — out of band, grounded, never guessed.

No scholarly API exposes a professor's email, so we resolve it only from real sources, with
provenance, and validate the domain against the institution:

  Tier 1 (implemented): the corresponding-author email inside a paper's open-access PDF
    (OpenAlex `oa_url`). Validated against the institution's ROR email domains when available.

A faculty-page tier needs a web-search API and is added later. If nothing can be grounded we
leave `contact_email = None` and tell the student to check the faculty page — we NEVER construct
a plausible address like first.last@university.edu, because a wrong address sends their outreach
to the wrong or nonexistent person.
"""

from __future__ import annotations

import io
import re

import httpx

from .models import ProfessorCandidate

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_EMAIL_JUNK = ("example.com", "email.com", "domain.com", "sci-hub", "elsevier.com", "wiley.com", "springer.com")
_UA = {"User-Agent": "bruce-research-engine/0.1 (student research outreach)"}


async def _ror_domains(ror: str | None) -> list[str]:
    """Institution email domains from the ROR record (authoritative for validation)."""
    if not ror:
        return []
    rid = ror.rstrip("/").rsplit("/", 1)[-1]
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(f"https://api.ror.org/organizations/{rid}", headers=_UA)
            r.raise_for_status()
            return [d.lower() for d in (r.json().get("domains") or [])]
    except (httpx.HTTPError, ValueError):
        return []


def _last_name(name: str) -> str:
    parts = [p for p in re.split(r"[^A-Za-z]+", name) if len(p) > 1]
    return parts[-1].lower() if parts else ""


async def _pdf_emails(url: str) -> list[str]:
    """Fetch a PDF and return de-duplicated emails from its first two pages (author block)."""
    try:
        async with httpx.AsyncClient(timeout=45, follow_redirects=True) as c:
            r = await c.get(url, headers=_UA)
            r.raise_for_status()
            data = r.content
    except httpx.HTTPError:
        return []
    if data[:5] != b"%PDF-":
        return []  # HTML landing page, not a PDF
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join((pg.extract_text() or "") for pg in pdf.pages[:2])
    except Exception:
        return []
    out, seen = [], set()
    for e in _EMAIL_RE.findall(text):
        el = e.lower().strip(".,;)")
        if el in seen or any(j in el for j in _EMAIL_JUNK):
            continue
        seen.add(el)
        out.append(el)
    return out


def _pick_email(emails: list[str], last_name: str, domains: list[str]) -> tuple[str | None, bool]:
    """Choose the best email and whether its domain is institution-validated. Never fabricate."""
    def domain_ok(dom: str) -> bool:
        return bool(domains) and any(dom == d or dom.endswith("." + d) for d in domains)

    # 1) institution domain + last name in local part (highest confidence)
    for e in emails:
        local, _, dom = e.partition("@")
        if domain_ok(dom) and last_name and last_name in local:
            return e, True
    # 2) institution domain only
    for e in emails:
        _, _, dom = e.partition("@")
        if domain_ok(dom):
            return e, True
    # 3) no ROR domains to check against, but last name matches the local part
    if not domains:
        for e in emails:
            local, _, _ = e.partition("@")
            if last_name and last_name in local:
                return e, False
    return None, False


async def resolve_email(candidate: ProfessorCandidate) -> ProfessorCandidate:
    """Resolve a grounded email for the candidate (mutates and returns it). None if not found."""
    domains = await _ror_domains(candidate.institution_ror)
    last = _last_name(candidate.name)

    for paper in candidate.recent_work:
        if not paper.pdf_url:
            continue
        emails = await _pdf_emails(str(paper.pdf_url))
        email, validated = _pick_email(emails, last, domains)
        if email:
            candidate.contact_email = email
            candidate.email_source = str(paper.pdf_url)
            candidate.email_verified = validated
            if not validated:
                candidate.uncertainties.append(
                    "Email found in a paper PDF but its domain wasn't validated against the "
                    "institution — confirm it's correct before sending."
                )
            return candidate

    candidate.uncertainties.append(
        "No email could be grounded from open-access PDFs — check the professor's faculty page manually."
    )
    return candidate
