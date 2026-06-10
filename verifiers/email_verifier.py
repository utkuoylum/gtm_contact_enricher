from __future__ import annotations
"""
SMTP-based email verification (free).
Connects to the MX server and uses RCPT TO to check if mailbox exists
without actually sending an email.
"""
import smtplib
import dns.resolver
import logging
from config import SMTP_TIMEOUT, SMTP_FROM_EMAIL

logger = logging.getLogger(__name__)

DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "tempmail.com", "throwam.com",
    "yopmail.com", "sharklasers.com", "maildrop.cc", "trashmail.com",
}


def get_mx_records(domain: str) -> list[str]:
    try:
        records = dns.resolver.resolve(domain, "MX")
        return sorted(str(r.exchange).rstrip(".") for r in records)
    except Exception:
        return []


def verify_email_smtp(email: str) -> tuple[bool, str]:
    """
    Returns (is_valid, reason).
    Uses catch-all detection: if the server accepts a random address, it's a catch-all.
    """
    if "@" not in email:
        return False, "Invalid email format"

    local, domain = email.rsplit("@", 1)

    if domain.lower() in DISPOSABLE_DOMAINS:
        return False, "Disposable email domain"

    mx_hosts = get_mx_records(domain)
    if not mx_hosts:
        return False, f"No MX records found for {domain}"

    for mx_host in mx_hosts[:2]:  # try first two MX servers
        result = _check_smtp(mx_host, email, local, domain)
        if result is not None:
            return result
    return False, "SMTP check failed for all MX hosts"


def _check_smtp(mx_host: str, email: str, local: str, domain: str) -> tuple[bool, str] | None:
    try:
        with smtplib.SMTP(timeout=SMTP_TIMEOUT) as smtp:
            smtp.connect(mx_host, 25)
            smtp.ehlo_or_helo_if_needed()
            smtp.mail(SMTP_FROM_EMAIL)
            code, _ = smtp.rcpt(email)

            if code == 250:
                # Check for catch-all: test a random address
                random_test = f"zzz_nonexistent_xyz_123@{domain}"
                catch_code, _ = smtp.rcpt(random_test)
                if catch_code == 250:
                    return True, "Valid (catch-all domain — cannot confirm individual mailbox)"
                return True, "Verified via SMTP"
            elif code == 550:
                return False, "Mailbox does not exist (550)"
            elif code in (421, 450, 451, 452):
                return None  # Temporary failure, try next MX
            else:
                return False, f"SMTP code {code}"
    except smtplib.SMTPConnectError:
        return None  # Network/port issue, try next MX
    except smtplib.SMTPServerDisconnected:
        return None
    except Exception as e:
        logger.debug(f"SMTP check error for {mx_host}: {e}")
        return None
