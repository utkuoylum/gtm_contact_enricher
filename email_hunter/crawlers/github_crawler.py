from __future__ import annotations
"""
GitHub email discovery — two methods:
1. Org members API: list org members → fetch public profile email
2. Code search API: search for "@domain.com" in code → extract emails from results
3. Commit email scan: find repos with company contributors, read commit emails

GitHub free API: 60 req/hr unauthenticated, 5000/hr with token (GITHUB_TOKEN env var).
Commit emails are public and appear in the git log for public repos.
"""
import os
import re
import logging
import requests
from utils.http_client import polite_sleep

logger = logging.getLogger(__name__)

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
BASE = "https://api.github.com"
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if GITHUB_TOKEN:
        h["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return h


def _get(path: str, params: dict = None) -> dict | list | None:
    try:
        resp = requests.get(f"{BASE}{path}", headers=_headers(), params=params or {}, timeout=12)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 403:
            logger.warning("GitHub rate limit hit")
        elif resp.status_code == 422:
            logger.debug(f"GitHub 422 for {path}: {resp.text[:200]}")
    except requests.RequestException as e:
        logger.debug(f"GitHub request error: {e}")
    return None


def find_emails_via_github(domain: str) -> set[str]:
    """Return all corporate emails discovered on GitHub for this domain."""
    emails: set[str] = set()

    # 1. Try to find the GitHub org matching this domain
    org_slug = _guess_org_slug(domain)
    if org_slug:
        org_emails = _scrape_org_members(org_slug, domain)
        emails.update(org_emails)
        logger.info(f"[github] org members: {len(org_emails)} emails")

    # 2. Code search: "@domain.com" in GitHub code index
    code_emails = _code_search(domain)
    emails.update(code_emails)
    logger.info(f"[github] code search: {len(code_emails)} emails")

    # 3. User search: users with company domain in profile email
    user_emails = _user_search(domain)
    emails.update(user_emails)
    logger.info(f"[github] user search: {len(user_emails)} emails")

    return {e for e in emails if e.endswith("@" + domain) or e.endswith("." + domain)}


def _guess_org_slug(domain: str) -> str | None:
    """Extract probable GitHub org name from domain (e.g. acme.com → acme)."""
    slug = domain.split(".")[0]
    slug = re.sub(r"[^a-z0-9\-]", "", slug.lower())

    # Verify org exists
    result = _get(f"/orgs/{slug}")
    if result and isinstance(result, dict) and result.get("login"):
        return result["login"]

    # Try with common variations
    for variant in [slug + "-hq", slug + "-inc", slug + "-team"]:
        result = _get(f"/orgs/{variant}")
        if result and isinstance(result, dict) and result.get("login"):
            return result["login"]

    return None


def _scrape_org_members(org: str, domain: str) -> set[str]:
    emails: set[str] = set()
    page = 1
    while page <= 3:  # max 3 pages = 90 members
        members = _get(f"/orgs/{org}/members", {"per_page": 30, "page": page})
        if not members or not isinstance(members, list) or len(members) == 0:
            break
        for member in members:
            login = member.get("login")
            if login:
                # Fetch public profile
                user = _get(f"/users/{login}")
                if user and isinstance(user, dict):
                    email = user.get("email")
                    if email and "@" in email:
                        e = email.lower().strip()
                        if e.endswith("@" + domain) or e.endswith("." + domain):
                            emails.add(e)
                polite_sleep(0.3)
        page += 1
    return emails


def _code_search(domain: str) -> set[str]:
    """Search GitHub code for email addresses containing the domain."""
    emails: set[str] = set()
    query = f'"@{domain}"'
    result = _get("/search/code", {"q": query, "per_page": 30})
    if not result or not isinstance(result, dict):
        return emails

    items = result.get("items", [])
    for item in items[:15]:  # check first 15 results
        # Fetch the raw file content
        raw_url = item.get("html_url", "").replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
        if raw_url:
            try:
                resp = requests.get(raw_url, headers=_headers(), timeout=8)
                if resp.status_code == 200:
                    for m in EMAIL_RE.finditer(resp.text):
                        e = m.group(0).lower().strip(".,;")
                        if domain in e:
                            emails.add(e)
            except Exception:
                pass
        polite_sleep(0.4)

    return emails


def _user_search(domain: str) -> set[str]:
    """Search GitHub users whose email contains the company domain."""
    emails: set[str] = set()
    result = _get("/search/users", {"q": f"@{domain} in:email", "per_page": 30})
    if not result or not isinstance(result, dict):
        return emails

    for user in result.get("items", [])[:20]:
        login = user.get("login")
        if login:
            profile = _get(f"/users/{login}")
            if profile and isinstance(profile, dict):
                email = profile.get("email")
                if email and "@" in email:
                    e = email.lower().strip()
                    if domain in e:
                        emails.add(e)
            polite_sleep(0.3)

    return emails
