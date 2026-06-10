from __future__ import annotations
"""
Full SMTP email verification with:
- MX record lookup
- Catch-all detection
- Per-email RCPT TO check
- Connection reuse across multiple emails (same domain → same SMTP session)
- Graceful handling of: rate limits, greylisting, TLS-only servers
"""
import smtplib
import ssl
import logging
import time
from dataclasses import dataclass
import dns.resolver

logger = logging.getLogger(__name__)
PROBE_FROM = "probe@enrichment-check.local"
TIMEOUT = 12


@dataclass
class VerifyResult:
    email: str
    status: str           # valid | invalid | catch_all | unknown | no_mx
    deliverable: bool
    catch_all_domain: bool
    mx_host: str | None
    detail: str


def get_mx(domain: str) -> list[str]:
    try:
        records = dns.resolver.resolve(domain, "MX")
        return sorted(str(r.exchange).rstrip(".") for r in records)
    except Exception:
        return []


def verify_emails_bulk(emails: list[str]) -> list[VerifyResult]:
    """
    Verify multiple emails. Groups by domain and reuses SMTP connections
    to reduce overhead and avoid triggering rate limits.
    """
    # Group by domain
    by_domain: dict[str, list[str]] = {}
    for email in emails:
        if "@" not in email:
            continue
        domain = email.split("@")[1].lower()
        by_domain.setdefault(domain, []).append(email)

    results = []
    for domain, domain_emails in by_domain.items():
        domain_results = _verify_domain_group(domain, domain_emails)
        results.extend(domain_results)

    return results


def verify_single(email: str) -> VerifyResult:
    results = verify_emails_bulk([email])
    if results:
        return results[0]
    return VerifyResult(email, "unknown", False, False, None, "Verification failed")


def _verify_domain_group(domain: str, emails: list[str]) -> list[VerifyResult]:
    mx_hosts = get_mx(domain)
    if not mx_hosts:
        return [VerifyResult(e, "no_mx", False, False, None, f"No MX records for {domain}") for e in emails]

    # Try each MX host until one works
    for mx_host in mx_hosts[:3]:
        results = _smtp_verify_on_host(mx_host, domain, emails)
        if results is not None:
            return results
        time.sleep(1)  # Brief pause before trying next MX

    return [VerifyResult(e, "unknown", False, False, mx_hosts[0] if mx_hosts else None,
                         "All MX hosts failed") for e in emails]


def _smtp_verify_on_host(mx_host: str, domain: str, emails: list[str]) -> list[VerifyResult] | None:
    """
    Open ONE SMTP session and RCPT TO all emails.
    Returns None if the connection failed entirely.
    """
    results = []
    catch_all = None  # Will be detected on first probe

    try:
        # Try plain SMTP first, then TLS if needed
        smtp = _connect_smtp(mx_host)
        if smtp is None:
            return None

        with smtp:
            # Step 1: Catch-all probe with obviously fake address
            probe = f"zzz_nonexistent_xyz_789abc@{domain}"
            smtp.mail(PROBE_FROM)
            code, _ = smtp.rcpt(probe)
            catch_all = (code == 250)

            if catch_all:
                logger.debug(f"[smtp] {domain} is catch-all on {mx_host}")

            # Step 2: Verify each email
            for email in emails:
                # Reset sender for each rcpt
                try:
                    smtp.mail(PROBE_FROM)
                    code, msg = smtp.rcpt(email)
                except smtplib.SMTPServerDisconnected:
                    # Server kicked us; mark remaining as unknown
                    results.append(VerifyResult(email, "unknown", False, catch_all, mx_host, "Server disconnected"))
                    continue
                except smtplib.SMTPResponseException as e:
                    code = e.smtp_code

                if code == 250:
                    if catch_all:
                        status = "catch_all"
                        deliverable = True  # Technically yes, but unconfirmed
                    else:
                        status = "valid"
                        deliverable = True
                elif code in (550, 551, 552, 553, 554):
                    status = "invalid"
                    deliverable = False
                elif code in (421, 450, 451, 452):
                    status = "unknown"
                    deliverable = False
                else:
                    status = "unknown"
                    deliverable = False

                results.append(VerifyResult(
                    email=email,
                    status=status,
                    deliverable=deliverable,
                    catch_all_domain=catch_all,
                    mx_host=mx_host,
                    detail=f"SMTP {code}",
                ))

        return results

    except smtplib.SMTPConnectError:
        return None
    except smtplib.SMTPHeloError:
        return None
    except Exception as e:
        logger.debug(f"[smtp] unexpected error on {mx_host}: {e}")
        return None


def _connect_smtp(mx_host: str) -> smtplib.SMTP | None:
    """Try plain SMTP on port 25, then fallback to port 587 STARTTLS."""
    for port in [25, 587]:
        try:
            smtp = smtplib.SMTP(timeout=TIMEOUT)
            smtp.connect(mx_host, port)
            smtp.ehlo_or_helo_if_needed()

            if port == 587:
                try:
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                except Exception:
                    pass  # Continue without TLS

            return smtp
        except (smtplib.SMTPConnectError, ConnectionRefusedError, OSError):
            continue
        except Exception:
            continue

    return None
