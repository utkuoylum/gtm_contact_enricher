from __future__ import annotations
"""
Use Claude to extract obfuscated email addresses from website text.

German SME websites often hide emails to avoid spam bots:
  "vorname punkt nachname at firma punkt de"
  "max(at)firma.de"
  "max.mustermann[at]firma[dot]de"
  JavaScript-assembled addresses, CSS-reversed text, etc.

Standard regex misses many of these; Claude handles natural-language obfuscation well.
"""
import re
import logging

logger = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Max chars sent to Claude — balance coverage vs token cost
_MAX_TEXT_CHARS = 6000


def extract_emails_from_text(text: str, domain: str) -> set[str]:
    """
    Pass scraped website text to Claude to find obfuscated @domain emails.
    Returns a set of email strings. Empty set on failure or when disabled.
    """
    if not text or not domain:
        return set()

    from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
    if not ANTHROPIC_API_KEY:
        return set()

    try:
        import anthropic
    except ImportError:
        return set()

    truncated = text[:_MAX_TEXT_CHARS]

    prompt = (
        f"Find all email addresses in this website text that belong to the domain @{domain}.\n"
        f"Check for obfuscated formats including:\n"
        f"- German: 'vorname punkt nachname at firma punkt de'\n"
        f"- 'firstname(at){domain}', 'name[at]{domain}', 'mail[at]firma[dot]de'\n"
        f"- Spaced out: 'max . mustermann @ firma . de'\n"
        f"- HTML entity encoded or Unicode-separated characters\n\n"
        f"Return ONLY valid email addresses, one per line. "
        f"Skip generic addresses (info@, kontakt@, office@, etc.). "
        f"If none found, return an empty response.\n\n"
        f"Text:\n{truncated}"
    )

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        model = CLAUDE_MODEL or "claude-haiku-4-5-20251001"
        # Use Haiku for cost efficiency — extraction is a simple task
        if "opus" in model or "sonnet" in model:
            model = "claude-haiku-4-5-20251001"

        response = client.messages.create(
            model=model,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        result_text = response.content[0].text if response.content else ""

        emails: set[str] = set()
        for m in EMAIL_RE.finditer(result_text):
            email = m.group(0).lower().strip(".,;:")
            if email.endswith(f"@{domain}") and _is_personal(email):
                emails.add(email)

        if emails:
            logger.info(f"[claude_extractor] {domain}: {len(emails)} obfuscated emails found")
        return emails

    except Exception as e:
        logger.warning(f"Claude email extractor error for {domain}: {e}")
        return set()


_GENERIC = frozenset({
    "info", "contact", "kontakt", "noreply", "no-reply", "admin", "support",
    "hello", "hallo", "office", "marketing", "sales", "hr", "jobs", "press",
    "presse", "media", "service", "help", "team", "mail", "post", "web",
})


def _is_personal(email: str) -> bool:
    local = email.split("@")[0]
    return local not in _GENERIC and len(local) >= 3
