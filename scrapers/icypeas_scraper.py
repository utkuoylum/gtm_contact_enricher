from __future__ import annotations
"""
Icypeas — email enrichment API.
Bilinen kişi (isim + domain) için email bulur.
Ücretsiz plan: 1.000 kredi/ay, GDPR-uyumlu.

API dok: https://icypeas.com/documentation
API key: https://app.icypeas.com/ → Settings → API
"""
import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

logger = logging.getLogger(__name__)

ICYPEAS_API_KEY = os.getenv("ICYPEAS_API_KEY", "")
_BASE = "https://app.icypeas.com/api"
_TIMEOUT = 20


def icypeas_available() -> bool:
    return bool(ICYPEAS_API_KEY)


def find_email_icypeas(
    first_name: str,
    last_name: str,
    domain_or_company: str,
) -> dict | None:
    """
    İsim + domain/şirket adından email bulur.

    Döner: {email, email_status} veya None.
    email_status: "valid" | "unverified"
    """
    if not icypeas_available():
        return None
    if not first_name or not last_name or not domain_or_company:
        return None

    headers = {
        "Authorization": f"Bearer {ICYPEAS_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "firstname": first_name.strip(),
        "lastname": last_name.strip(),
        "domainOrCompany": domain_or_company.strip(),
    }

    try:
        resp = requests.post(
            f"{_BASE}/email-search",
            json=payload,
            headers=headers,
            timeout=_TIMEOUT,
        )
        if resp.status_code == 429:
            logger.warning("Icypeas: rate limit reached")
            return None
        if resp.status_code == 402:
            logger.warning("Icypeas: credit limit reached")
            return None
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.debug(f"Icypeas request error ({first_name} {last_name}): {e}")
        return None

    item = data.get("item") or {}
    emails = item.get("emails") or []
    if not emails:
        return None

    best = emails[0]
    if isinstance(best, dict):
        email = best.get("email")
        validity = best.get("validity") or ""
    elif isinstance(best, str):
        email = best
        validity = ""
    else:
        return None

    if not email:
        return None

    return {
        "email": email,
        "email_status": "valid" if validity.upper() in ("VERIFIED", "VALID") else "unverified",
    }


def enrich_missing_emails_icypeas(
    contacts: list[dict],
    domain: str,
    verified_email_map: dict[str, str],
    errors: list[str],
    top_n: int = 5,
) -> None:
    """
    Email'i eksik olan en iyi top_n kişi için Icypeas ile email arar.
    Sonuçları contacts listesine in-place yazar.
    """
    if not icypeas_available():
        return

    candidates = [
        c for c in contacts
        if not c.get("email")
        and len((c.get("full_name") or "").split()) >= 2
    ][:top_n]

    if not candidates:
        return

    def _enrich_one(c: dict):
        parts = (c.get("full_name") or "").split()
        first, last = parts[0], parts[-1]
        return c, find_email_icypeas(first, last, domain)

    enriched = 0
    with ThreadPoolExecutor(max_workers=min(4, len(candidates))) as ex:
        futures = [ex.submit(_enrich_one, c) for c in candidates]
        for fut in as_completed(futures, timeout=40):
            try:
                c, result = fut.result()
            except Exception as e:
                errors.append(f"icypeas: {e}")
                continue
            if not result or not result.get("email"):
                continue
            c["email"] = result["email"]
            if result.get("email_status") == "valid":
                verified_email_map[result["email"]] = "valid"
            enriched += 1

    logger.info(f"Icypeas: enriched {enriched}/{len(candidates)} emails")
