from __future__ import annotations
"""
Use Gemini (Google Search grounding) to find @domain email addresses.
Searches the web for real employee email examples to anchor pattern detection.
"""
import re
import logging

logger = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

_GENERIC_LOCALS = frozenset({
    "info", "contact", "kontakt", "noreply", "no-reply", "admin", "support",
    "hello", "hallo", "office", "marketing", "sales", "hr", "jobs", "press",
    "presse", "media", "service", "help", "team", "mail", "post", "web",
})


def find_emails_via_gemini(domain: str, company_name: str = "") -> dict:
    """
    Use Gemini with Google Search grounding to find @domain email addresses.
    Returns {"emails": set[str]}.
    """
    from config import GEMINI_API_KEY
    if not GEMINI_API_KEY:
        return {"emails": set()}

    try:
        from google import genai
        from google.genai.types import Tool, GenerateContentConfig, GoogleSearch
    except ImportError:
        logger.warning("google-genai not installed; skipping Gemini email crawler")
        return {"emails": set()}

    co = company_name or domain
    prompt = (
        f'Search for email addresses of employees at "{co}" (website domain: {domain}).\n'
        f'Look for "@{domain}" in LinkedIn profiles, XING profiles, press releases, '
        f'the company website, GitHub commits, and PDF documents.\n'
        f'List every individual email address you find that ends in @{domain}. '
        f'One email per line. Do not include generic addresses like info@ or contact@. '
        f'No explanations — only the email addresses.'
    )

    emails: set[str] = set()
    _MODELS = ["gemini-2.5-flash", "gemini-2.0-flash-001", "gemini-1.5-flash"]

    for model_name in _MODELS:
        try:
            client = genai.Client(api_key=GEMINI_API_KEY)
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=GenerateContentConfig(
                    tools=[Tool(google_search=GoogleSearch())],
                    temperature=0,
                ),
            )
            text = response.text or ""
            for m in EMAIL_RE.finditer(text):
                email = m.group(0).lower().strip(".,;:")
                if _is_personal_email(email, domain):
                    emails.add(email)
            break
        except Exception as e:
            err_str = str(e)
            if any(code in err_str for code in ("429", "404", "RESOURCE_EXHAUSTED", "NOT_FOUND", "no longer available")):
                logger.warning(f"Gemini {model_name} unavailable for email search, trying next")
                continue
            logger.warning(f"Gemini email search error for {domain}: {e}")
            break

    logger.info(f"[gemini_crawler] {domain}: {len(emails)} personal emails found")
    return {"emails": emails}


def _is_personal_email(email: str, domain: str) -> bool:
    if not email.endswith(f"@{domain}"):
        return False
    local = email.split("@")[0]
    if local in _GENERIC_LOCALS:
        return False
    if len(local) < 3:
        return False
    return True
