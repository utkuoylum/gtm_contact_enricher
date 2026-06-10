from __future__ import annotations
"""
Email pattern inference engine.
Given a set of known emails from a domain, detect the naming convention.

Supported patterns (Hunter.io uses the same set):
  {first}.{last}        john.smith@acme.com
  {first}{last}         johnsmith@acme.com
  {f}{last}             jsmith@acme.com
  {first}               john@acme.com
  {first}_{last}        john_smith@acme.com
  {last}.{first}        smith.john@acme.com
  {last}{first}         smithjohn@acme.com
  {last}                smith@acme.com
  {f}.{last}            j.smith@acme.com
  {first}.{l}           john.s@acme.com
  {first}{l}            johns@acme.com
  {f}{l}                js@acme.com  (rare)
"""
import re
import unicodedata
import logging
from collections import Counter
from dataclasses import dataclass

logger = logging.getLogger(__name__)

PATTERNS = [
    "{first}.{last}",
    "{first}{last}",
    "{f}{last}",
    "{first}",
    "{first}_{last}",
    "{last}.{first}",
    "{last}{first}",
    "{last}",
    "{f}.{last}",
    "{first}.{l}",
    "{first}{l}",
    "{f}{l}",
    "{last}.{f}",
    "{last}_{first}",
    "{last}-{first}",
    "{first}-{last}",
]


@dataclass
class PatternResult:
    pattern: str          # e.g. "{first}.{last}"
    confidence: float     # 0.0 – 1.0
    sample_count: int     # how many emails confirmed this pattern
    example: str          # e.g. "john.smith@acme.com"


def normalize(name: str) -> str:
    """Lowercase, strip accents, remove non-alpha."""
    nfkd = unicodedata.normalize("NFKD", name)
    return re.sub(r"[^a-z]", "", nfkd.encode("ascii", "ignore").decode("ascii").lower())


def detect_pattern(emails: set[str], known_people: list[dict] = None) -> PatternResult | None:
    """
    Detect the dominant email pattern for a domain.

    emails: raw emails found for the domain (local parts only or full)
    known_people: list of {"first_name": ..., "last_name": ...} dicts
                  (optional — used to cross-validate pattern matches)

    Returns the most likely PatternResult, or None if not enough data.
    """
    if not emails:
        return None

    # Extract local parts
    locals_ = [e.split("@")[0].lower() for e in emails if "@" in e]
    if not known_people:
        # Try to infer names from email locals alone
        known_people = _infer_names_from_locals(locals_)

    if not known_people:
        # Can't detect pattern without name hints
        return _statistical_fallback(locals_)

    # Count which pattern explains the most emails
    votes: Counter = Counter()
    examples: dict[str, str] = {}

    for person in known_people:
        full_parts = (person.get("full_name") or "").split()
        first = normalize(person.get("first_name") or (full_parts[0] if full_parts else ""))
        last = normalize(person.get("last_name") or (full_parts[-1] if len(full_parts) >= 2 else ""))
        if not first or not last:
            continue

        fi = first[0] if first else ""
        li = last[0] if last else ""

        for pat in PATTERNS:
            candidate = (
                pat
                .replace("{first}", first)
                .replace("{last}", last)
                .replace("{f}", fi)
                .replace("{l}", li)
            )
            if candidate in locals_:
                votes[pat] += 1
                if pat not in examples:
                    for e in emails:
                        if e.split("@")[0].lower() == candidate:
                            examples[pat] = e
                            break

    if not votes:
        return _statistical_fallback(locals_)

    best_pat, best_count = votes.most_common(1)[0]
    confidence = best_count / max(len(known_people), 1)
    confidence = min(confidence, 1.0)

    return PatternResult(
        pattern=best_pat,
        confidence=round(confidence, 2),
        sample_count=best_count,
        example=examples.get(best_pat, ""),
    )


def _infer_names_from_locals(locals_: list[str]) -> list[dict]:
    """
    Try to infer first/last names from email local parts.
    Works for patterns like john.smith, j.smith, jsmith etc.
    Not 100% accurate but useful as a hint.
    """
    people = []
    for local in locals_:
        # Split on separator
        for sep in [".", "_", "-"]:
            parts = local.split(sep)
            if len(parts) == 2 and all(len(p) >= 2 for p in parts):
                people.append({"first_name": parts[0], "last_name": parts[1]})
                break
    return people


def _statistical_fallback(locals_: list[str]) -> PatternResult | None:
    """
    When no name data available, guess pattern statistically:
    - Most locals with '.' separator → {first}.{last}
    - Most locals with '_' separator → {first}_{last}
    - Short locals → {f}{last}
    - Long locals no sep → {first}{last}
    """
    dot_count = sum(1 for l in locals_ if "." in l)
    underscore_count = sum(1 for l in locals_ if "_" in l)
    dash_count = sum(1 for l in locals_ if "-" in l)
    total = len(locals_) or 1

    if dot_count / total > 0.5:
        return PatternResult("{first}.{last}", round(dot_count / total, 2), dot_count, "")
    if underscore_count / total > 0.3:
        return PatternResult("{first}_{last}", round(underscore_count / total, 2), underscore_count, "")
    if dash_count / total > 0.3:
        return PatternResult("{first}-{last}", round(dash_count / total, 2), dash_count, "")

    # Check average length: short → {f}{last}, long → {first}{last}
    avg_len = sum(len(l) for l in locals_) / total
    if avg_len <= 7:
        return PatternResult("{f}{last}", 0.3, 0, "")
    return PatternResult("{first}{last}", 0.3, 0, "")


def apply_pattern(pattern: str, first_name: str, last_name: str) -> str:
    """Generate a single email local part from pattern + names."""
    first = normalize(first_name)
    last = normalize(last_name)
    fi = first[0] if first else ""
    li = last[0] if last else ""
    return (
        pattern
        .replace("{first}", first)
        .replace("{last}", last)
        .replace("{f}", fi)
        .replace("{l}", li)
    )


# German .de domains prioritize {f}.{last} higher than other locales
# Research: vorname.nachname > v.nachname > vnachname > vorname (for German SMEs)
_GERMAN_PRIORITY_PATTERNS = [
    "{first}.{last}",   # hans.mueller@firma.de — most common overall
    "{f}.{last}",       # h.mueller@firma.de   — very common in German SMEs
    "{first}{last}",    # hansmueller@firma.de
    "{f}{last}",        # hmueller@firma.de
    "{first}",          # hans@firma.de         — small companies
    "{last}",           # mueller@firma.de       — some professional services
    "{last}.{first}",   # mueller.hans@firma.de — financial sector
    "{first}_{last}",   # hans_mueller@firma.de
    "{last}{first}",    # muellerhanss@firma.de
    "{first}.{l}",      # hans.m@firma.de
    "{first}{l}",       # hansm@firma.de
    "{last}.{f}",       # mueller.h@firma.de
    "{f}{l}",           # hm@firma.de (rare)
    "{last}_{first}",   # mueller_hans@firma.de
    "{first}-{last}",   # hans-mueller@firma.de
    "{last}-{first}",   # mueller-hans@firma.de
]

_STANDARD_PRIORITY_PATTERNS = [
    "{first}.{last}", "{first}{last}", "{f}{last}",
    "{first}", "{f}.{last}", "{first}_{last}",
    "{last}.{first}", "{last}{first}", "{last}",
    "{first}.{l}", "{first}{l}", "{last}.{f}",
    "{f}{l}", "{last}_{first}", "{first}-{last}", "{last}-{first}",
]


def _is_german_domain(domain: str) -> bool:
    """Heuristic: .de, .at, .ch domains use German email conventions."""
    tld = domain.lower().split(".")[-1] if "." in domain else ""
    return tld in ("de", "at", "ch")


def generate_all_candidates(first_name: str, last_name: str, domain: str,
                            preferred_pattern: str | None = None) -> list[str]:
    """
    Generate all possible email candidates for a person, ranked by likelihood.
    For German (.de/.at/.ch) domains, prioritizes {f}.{last} higher.
    preferred_pattern (from detect_pattern) is put first if provided.
    """
    first = normalize(first_name)
    last = normalize(last_name)
    if not first or not last:
        return []

    fi = first[0]
    li = last[0]

    seen = set()
    candidates = []

    def add(local: str):
        email = f"{local}@{domain}"
        if email not in seen:
            seen.add(email)
            candidates.append(email)

    # Preferred pattern first (from detect_pattern)
    if preferred_pattern:
        preferred_local = apply_pattern(preferred_pattern, first_name, last_name)
        add(preferred_local)

    # Select pattern order based on domain locale
    priority_patterns = _GERMAN_PRIORITY_PATTERNS if _is_german_domain(domain) else _STANDARD_PRIORITY_PATTERNS

    for pat in priority_patterns:
        local = apply_pattern(pat, first_name, last_name)
        add(local)

    return candidates
