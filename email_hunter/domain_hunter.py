from __future__ import annotations
"""
Hunter.io replacement — full domain email intelligence.

Pipeline:
  1. Crawl company website deeply (site_crawler)
  2. Search GitHub for org member emails (github_crawler)
  3. Google/Bing SERP email mining (google_crawler)
  4. WHOIS registrant email (whois_crawler)
  5. Job board recruiter emails (job_board_crawler)
  6. Deduplicate + classify all found emails
  7. Detect email pattern from collected data (pattern_detector)
  8. SMTP-verify discovered emails in bulk (smtp_verifier)

Output mirrors Hunter.io domain-search response shape.
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutTimeout
from dataclasses import dataclass, field

from email_hunter.crawlers.site_crawler import crawl_domain
from email_hunter.crawlers.github_crawler import find_emails_via_github
from email_hunter.crawlers.google_crawler import find_emails_via_search
from email_hunter.crawlers.whois_crawler import get_whois_emails
from email_hunter.crawlers.job_board_crawler import find_emails_in_job_postings
from email_hunter.crawlers.gemini_crawler import find_emails_via_gemini
from email_hunter.crawlers.claude_email_extractor import extract_emails_from_text
from email_hunter.pattern_detector import detect_pattern, generate_all_candidates, PatternResult
from email_hunter.smtp_verifier import verify_emails_bulk, VerifyResult

logger = logging.getLogger(__name__)


@dataclass
class EmailContact:
    email: str
    local_part: str
    domain: str
    sources: list[str] = field(default_factory=list)
    smtp_status: str = "unverified"   # valid | invalid | catch_all | unknown | unverified
    deliverable: bool | None = None
    catch_all_domain: bool = False
    confidence: int = 0               # 0-100, computed from sources + smtp


@dataclass
class DomainHuntResult:
    domain: str
    company_name: str
    pattern: PatternResult | None
    contacts: list[EmailContact]
    catch_all: bool
    mx_records: list[str]
    sources_used: list[str]
    errors: list[str]
    total_raw_found: int


def hunt_domain(domain: str, company_name: str = "") -> DomainHuntResult:
    """
    Main entry point. Returns full email intelligence for a domain.
    Runs all crawlers in parallel (90s timeout each).
    """
    errors: list[str] = []
    all_emails: set[str] = set()
    sources_used: list[str] = []
    mx_records: list[str] = []

    # --- Run all crawlers in parallel ---
    tasks = {
        "site":      lambda: crawl_domain(domain, max_pages=15),
        "github":    lambda: {"emails": find_emails_via_github(domain)},
        "google":    lambda: {"emails": find_emails_via_search(domain)},
        "whois":     lambda: get_whois_emails(domain),
        "job_board": lambda: {"emails": find_emails_in_job_postings(company_name or domain, domain)},
        "gemini":    lambda: find_emails_via_gemini(domain, company_name),
    }

    site_text: str = ""  # collected for Claude obfuscation pass

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(fn): name for name, fn in tasks.items()}
        try:
            for future in as_completed(futures, timeout=35):
                name = futures[future]
                try:
                    res = future.result()
                    batch = res.get("emails", set()) if isinstance(res, dict) else set()
                    if isinstance(batch, set):
                        all_emails.update(batch)
                    if name == "whois" and isinstance(res, dict):
                        mx_records = res.get("mx_records", [])
                    if name == "site" and isinstance(res, dict):
                        site_text = res.get("text", "")
                    sources_used.append(name)
                    logger.info(f"[hunt_domain] {name} done, total emails so far: {len(all_emails)}")
                except Exception as e:
                    errors.append(f"{name}: {e}")
                    logger.error(f"[hunt_domain] {name} error: {e}", exc_info=True)
        except FutTimeout:
            for future, name in futures.items():
                if not future.done():
                    future.cancel()
                    errors.append(f"{name}: timed out")
                elif future.done():
                    try:
                        res = future.result()
                        batch = res.get("emails", set()) if isinstance(res, dict) else set()
                        if isinstance(batch, set):
                            all_emails.update(batch)
                        if name == "site" and isinstance(res, dict):
                            site_text = res.get("text", "")
                        sources_used.append(name)
                    except Exception:
                        pass

    # Claude obfuscation pass — runs after site_crawler, catches German "punkt/at" formats
    if site_text:
        try:
            claude_emails = extract_emails_from_text(site_text, domain)
            if claude_emails:
                all_emails.update(claude_emails)
                sources_used.append("claude_email")
                logger.info(f"[hunt_domain] claude_email: {len(claude_emails)} obfuscated emails decoded")
        except Exception as e:
            errors.append(f"claude_email: {e}")

    total_raw = len(all_emails)
    logger.info(f"[hunt_domain] total raw emails: {total_raw}")

    # --- Detect email pattern ---
    pattern = detect_pattern(all_emails)
    logger.info(f"[hunt_domain] pattern: {pattern}")

    # --- SMTP verify all found emails in bulk ---
    email_list = list(all_emails)
    verify_results: list[VerifyResult] = []
    if email_list:
        try:
            verify_results = verify_emails_bulk(email_list)
        except Exception as e:
            errors.append(f"smtp_verify: {e}")
            logger.error(f"[hunt_domain] SMTP verify error: {e}")

    catch_all = any(r.catch_all_domain for r in verify_results)

    # --- Build EmailContact objects ---
    verify_map: dict[str, VerifyResult] = {r.email: r for r in verify_results}
    contacts: list[EmailContact] = []

    for email in all_emails:
        local = email.split("@")[0]
        vr = verify_map.get(email)

        smtp_status = "unverified"
        deliverable = None
        if vr:
            smtp_status = vr.status
            deliverable = vr.deliverable

        confidence = _compute_confidence(email, smtp_status, pattern)

        contacts.append(EmailContact(
            email=email,
            local_part=local,
            domain=domain,
            sources=_which_sources(email, tasks),  # approximate
            smtp_status=smtp_status,
            deliverable=deliverable,
            catch_all_domain=catch_all,
            confidence=confidence,
        ))

    # Sort: deliverable first, then by confidence desc
    contacts.sort(key=lambda c: (0 if c.deliverable else 1, -c.confidence))

    return DomainHuntResult(
        domain=domain,
        company_name=company_name,
        pattern=pattern,
        contacts=contacts,
        catch_all=catch_all,
        mx_records=mx_records,
        sources_used=list(set(sources_used)),
        errors=errors,
        total_raw_found=total_raw,
    )


def find_person_email(
    first_name: str,
    last_name: str,
    domain: str,
    pattern: PatternResult | None = None,
    max_verify: int = 6,
) -> dict | None:
    """
    Find and verify the email for a specific person at a company.
    Returns {email, confidence, smtp_status} or None.
    """
    if not pattern:
        # Quick domain scan to get pattern (site-only, 20 pages)
        result = crawl_domain(domain, max_pages=20)
        found_emails = result.get("emails", set())
        pattern = detect_pattern(found_emails)

    candidates = generate_all_candidates(
        first_name, last_name, domain,
        preferred_pattern=pattern.pattern if pattern else None,
    )[:max_verify]

    if not candidates:
        return None

    verify_results = verify_emails_bulk(candidates)
    for vr in verify_results:
        if vr.deliverable:
            return {
                "email": vr.email,
                "confidence": 90 if vr.status == "valid" else 60,
                "smtp_status": vr.status,
            }

    # No verified hit — return top candidate with low confidence
    if candidates:
        return {
            "email": candidates[0],
            "confidence": 20,
            "smtp_status": "unverified",
        }
    return None


def _compute_confidence(email: str, smtp_status: str, pattern: PatternResult | None) -> int:
    score = 0
    if smtp_status == "valid":
        score += 60
    elif smtp_status == "catch_all":
        score += 30
    elif smtp_status == "invalid":
        return 0

    # Bonus: email matches detected pattern
    if pattern and pattern.example:
        local = email.split("@")[0]
        pattern_local = pattern.example.split("@")[0] if "@" in pattern.example else ""
        if _locals_match_pattern(local, pattern.pattern):
            score += 30

    return min(score, 100)


def _locals_match_pattern(local: str, pattern: str) -> bool:
    """Rough check: does this local part look like it follows the pattern?"""
    if "{first}.{last}" in pattern and "." in local:
        return True
    if "{first}{last}" in pattern and "." not in local and "_" not in local:
        return True
    if "{f}{last}" in pattern and len(local.split(".")[0]) == 1:
        return True
    return False


def _which_sources(email: str, tasks: dict) -> list[str]:
    # This is a rough approximation — real implementation would tag per email
    return ["discovered"]
