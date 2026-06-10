from __future__ import annotations
"""
Given a person's name and company domain + email pattern from Hunter.io,
generate likely email addresses.
"""
import re
import unicodedata


def normalize_name(name: str) -> str:
    """Lowercase, remove accents, keep only ASCII letters."""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_str = nfkd.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z]", "", ascii_str.lower())


def generate_email_candidates(full_name: str, domain: str, pattern: str | None = None) -> list[str]:
    parts = full_name.strip().split()
    if len(parts) < 2:
        return []

    first = normalize_name(parts[0])
    last = normalize_name(parts[-1])
    fi = first[0] if first else ""
    li = last[0] if last else ""

    # All common patterns
    candidates = [
        f"{first}.{last}@{domain}",
        f"{first}{last}@{domain}",
        f"{fi}{last}@{domain}",
        f"{first}@{domain}",
        f"{first}_{last}@{domain}",
        f"{last}.{first}@{domain}",
        f"{last}{fi}@{domain}",
        f"{fi}.{last}@{domain}",
        f"{first}.{li}@{domain}",
    ]

    # If Hunter told us the pattern, put that first
    if pattern:
        template = (
            pattern
            .replace("{first}", first)
            .replace("{last}", last)
            .replace("{f}", fi)
            .replace("{l}", li)
            .replace("{first_name}", first)
            .replace("{last_name}", last)
        )
        if "@" in template and template not in candidates:
            candidates.insert(0, template)

    # Deduplicate while preserving order
    seen = set()
    result = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result
