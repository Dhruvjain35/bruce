"""Email resolution — out of band, grounded, never guessed.

No scholarly API exposes a professor's email, so we resolve it only from real sources, with
provenance and domain validation:

  Tier 1: the corresponding-author email inside a paper's open-access PDF (OpenAlex `oa_url`).
  Tier 2: the email published on the professor's official faculty page, found via OpenAI's
          built-in web search (uses the existing OpenAI key — no separate SERP subscription),
          then VERIFIED by fetching that page and confirming the address literally appears on
          it. Runs only when Tier 1 fails, to keep web-search cost low.

We NEVER construct a plausible address (no first.last@university.edu). If nothing can be
grounded, `contact_email` stays None and we tell the student to check the faculty page.
"""

from __future__ import annotations

import io
import re

import httpx

from .models import ProfessorCandidate

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_EMAIL_JUNK = ("example.com", "email.com", "domain.com", "sci-hub", "elsevier.com", "wiley.com", "springer.com")
_UA = {"User-Agent": "bruce-research-engine/0.1 (student research outreach)"}
WEBSEARCH_MODEL = "gpt-5.4-mini"  # cheap; web-search tool fee applies per call, gated to Tier-1 misses


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


def _domain_ok(dom: str, domains: list[str]) -> bool:
    return bool(domains) and any(dom == d or dom.endswith("." + d) for d in domains)


def _deobfuscate(text: str) -> str:
    t = text
    for a in ("[at]", "(at)", " at ", " AT ", "&#64;", "&#x40;"):
        t = t.replace(a, "@")
    for d in ("[dot]", "(dot)", " dot ", " DOT "):
        t = t.replace(d, ".")
    return t


# ---------- Tier 1: open-access PDF ----------

async def _pdf_emails(url: str) -> list[str]:
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
    for e in emails:  # 1) institution domain + last name in local part
        local, _, dom = e.partition("@")
        if _domain_ok(dom, domains) and last_name and last_name in local:
            return e, True
    for e in emails:  # 2) institution domain only
        _, _, dom = e.partition("@")
        if _domain_ok(dom, domains):
            return e, True
    if not domains:  # 3) no ROR domains to check, but last name matches local part
        for e in emails:
            local, _, _ = e.partition("@")
            if last_name and last_name in local:
                return e, False
    return None, False


# ---------- Tier 2: faculty page via OpenAI web search, page-verified ----------

async def _fetch_page_text(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
            r = await c.get(url, headers=_UA)
            r.raise_for_status()
            return r.text
    except httpx.HTTPError:
        return ""


def _emails_on_page(html: str) -> set[str]:
    return {e.lower().strip(".,;)") for e in _EMAIL_RE.findall(_deobfuscate(html))}


def _parse_ws(text: str) -> tuple[str | None, str | None]:
    """Pull an email + source URL from the web-search response (prose or JSON)."""
    email_m = _EMAIL_RE.search(_deobfuscate(text))
    url_m = re.search(r"https?://[^\s)\"'\]]+", text)
    return (email_m.group(0) if email_m else None), (url_m.group(0) if url_m else None)


async def _websearch_email(candidate: ProfessorCandidate, domains: list[str]) -> tuple[str, str, bool] | None:
    try:
        from openai import AsyncOpenAI
    except Exception:
        return None
    client = AsyncOpenAI()  # reads OPENAI_API_KEY
    prompt = (
        f"Find the official university faculty or department page for {candidate.name}, a "
        f"researcher at {candidate.institution}, and the email address published on that page. "
        f"Give the email address and the exact source page URL. Only report an email that "
        f"literally appears on an official institutional page; if you can't find one, say so."
    )
    try:
        r = await client.responses.create(model=WEBSEARCH_MODEL, tools=[{"type": "web_search"}], input=prompt)
        text = r.output_text or ""
    except Exception:
        return None

    email, source = _parse_ws(text)
    if not email or "@" not in email:
        return None
    email = email.lower().strip(".,;)")
    dom = email.split("@")[-1]
    domain_ok = _domain_ok(dom, domains)

    # Do NOT trust the model — confirm the address is actually on the cited page.
    page_ok = False
    if source and source.startswith("http"):
        page_ok = email in _emails_on_page(await _fetch_page_text(source))

    if not (page_ok or domain_ok):
        return None  # unconfirmable -> never emit
    return email, (source or "openai_web_search"), (page_ok and domain_ok)


# ---------- orchestration ----------

async def resolve_email(candidate: ProfessorCandidate) -> ProfessorCandidate:
    """Resolve a grounded email (mutates + returns the candidate). Leaves None if not found."""
    domains = await _ror_domains(candidate.institution_ror)
    last = _last_name(candidate.name)

    # Tier 1: open-access PDF corresponding-author email
    for paper in candidate.recent_work:
        if not paper.pdf_url:
            continue
        email, validated = _pick_email(await _pdf_emails(str(paper.pdf_url)), last, domains)
        if email:
            candidate.contact_email = email
            candidate.email_source = str(paper.pdf_url)
            candidate.email_verified = validated
            if not validated:
                candidate.uncertainties.append(
                    "Email from a paper PDF but domain unvalidated — confirm before sending."
                )
            return candidate

    # Tier 2: faculty page via web search (only reached if Tier 1 found nothing)
    ws = await _websearch_email(candidate, domains)
    if ws:
        email, source, verified = ws
        candidate.contact_email = email
        candidate.email_source = source
        candidate.email_verified = verified
        if not verified:
            candidate.uncertainties.append(
                "Email found via web search but not fully page-confirmed — verify before sending."
            )
        return candidate

    candidate.uncertainties.append(
        "No email could be grounded (PDF + faculty-page search) — check the professor's faculty page manually."
    )
    return candidate
