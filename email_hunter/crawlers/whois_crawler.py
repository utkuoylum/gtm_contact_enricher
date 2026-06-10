from __future__ import annotations
"""
WHOIS & DNS-based email discovery.
- WHOIS registrant email (often a real contact or forwarding address)
- SPF/TXT records sometimes reveal email infrastructure hints
- DNS MX records for validating the domain accepts mail
"""
import re
import logging
import socket
import dns.resolver
import requests
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url

logger = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def get_whois_emails(domain: str) -> dict:
    """
    Returns {
        emails: set[str],
        mx_records: list[str],
        registrant_name: str | None,
        registrant_org: str | None,
    }
    """
    result = {
        "emails": set(),
        "mx_records": [],
        "registrant_name": None,
        "registrant_org": None,
    }

    # Method 1: python-whois library
    try:
        import whois as python_whois
        w = python_whois.whois(domain)
        if w:
            # Emails can be a string or list
            raw_emails = w.emails or []
            if isinstance(raw_emails, str):
                raw_emails = [raw_emails]
            for e in raw_emails:
                if e and "@" in e:
                    result["emails"].add(e.lower().strip())

            result["registrant_name"] = w.name or w.registrant_name
            result["registrant_org"] = w.org or w.registrant

    except Exception as e:
        logger.debug(f"python-whois error: {e}")

    # Method 2: RDAP (modern WHOIS replacement) — returns JSON
    rdap_emails = _query_rdap(domain)
    result["emails"].update(rdap_emails)

    # Method 3: Public WHOIS web services
    web_emails = _query_whois_web(domain)
    result["emails"].update(web_emails)

    # Get MX records (needed for SMTP verification)
    try:
        mx = dns.resolver.resolve(domain, "MX")
        result["mx_records"] = sorted(str(r.exchange).rstrip(".") for r in mx)
    except Exception:
        pass

    logger.info(f"[whois] {domain}: {len(result['emails'])} emails, {len(result['mx_records'])} MX")
    return result


def _query_rdap(domain: str) -> set[str]:
    """RDAP is the modern replacement for WHOIS — structured JSON."""
    emails: set[str] = set()
    try:
        resp = requests.get(f"https://rdap.org/domain/{domain}", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            _extract_emails_from_rdap(data, emails)
    except Exception:
        pass
    return emails


def _extract_emails_from_rdap(obj, emails: set[str]):
    if isinstance(obj, dict):
        # vCard in entities
        for entity in obj.get("entities", []):
            _extract_emails_from_rdap(entity, emails)
        vcardarray = obj.get("vcardArray", [])
        if vcardarray and len(vcardarray) > 1:
            for item in vcardarray[1]:
                if isinstance(item, list) and len(item) >= 4:
                    if item[0] == "email":
                        e = str(item[3]).lower().strip()
                        if "@" in e:
                            emails.add(e)
    elif isinstance(obj, list):
        for item in obj:
            _extract_emails_from_rdap(item, emails)


def _query_whois_web(domain: str) -> set[str]:
    """Fallback: scrape public WHOIS lookup tools."""
    emails: set[str] = set()
    session = get_session()

    sources = [
        f"https://www.whois.com/whois/{domain}",
        f"https://who.is/whois/{domain}",
    ]
    for url in sources:
        html = fetch_url(url, session)
        if not html:
            continue
        text = BeautifulSoup(html, "html.parser").get_text(separator=" ")
        for m in EMAIL_RE.finditer(text):
            e = m.group(0).lower().strip(".,;")
            # Filter out service emails like abuse@registrar.com
            if not any(x in e for x in ["abuse@", "noreply@", "hostmaster@", "postmaster@", "webmaster@"]):
                emails.add(e)
        if emails:
            break

    return emails


def check_catch_all(domain: str, mx_records: list[str] = None) -> bool:
    """
    Returns True if the domain is a catch-all (accepts any email address).
    This is critical: if catch-all, SMTP verification always says "valid"
    even for made-up addresses.
    """
    if not mx_records:
        try:
            mx = dns.resolver.resolve(domain, "MX")
            mx_records = [str(r.exchange).rstrip(".") for r in mx]
        except Exception:
            return False

    import smtplib
    test_email = f"zzz_definitelynonexistent_xyzxyz_1234@{domain}"
    probe_from = "probe@enrichment.local"

    for mx_host in mx_records[:2]:
        try:
            with smtplib.SMTP(timeout=10) as smtp:
                smtp.connect(mx_host, 25)
                smtp.ehlo_or_helo_if_needed()
                smtp.mail(probe_from)
                code, _ = smtp.rcpt(test_email)
                if code == 250:
                    return True  # catch-all
                if code == 550:
                    return False  # proper rejection
        except Exception:
            continue
    return False
