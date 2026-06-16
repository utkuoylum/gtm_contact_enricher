from __future__ import annotations
"""
Bundesanzeiger scraper — resmi Alman federal gazetesi.

Handelsregister tescil duyurularından Geschäftsführer / Prokurist isimlerini çıkarır.
Northdata'nın indekslemediği güncel atamalar için değerlidir.

Yaklaşım:
  1. SERP araması: site:bundesanzeiger.de "{şirket}" Geschäftsführer
  2. Eşleşen ilan sayfaları Jina ile okunur
  3. Tescil metinleri regex ile ayrıştırılır
"""
import re
import logging
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from utils.http_client import get_session, fetch_url, multi_engine_search, polite_sleep

try:
    from utils.http_client import fetch_with_jina
    _JINA_OK = True
except ImportError:
    _JINA_OK = False

logger = logging.getLogger(__name__)

_BANZ_DOMAIN = "bundesanzeiger.de"

# Tescil metinlerinde yetkili kişi rolleri
_OFFICER_ROLES = re.compile(
    r"\b(Geschäftsführer(?:in)?|Prokurist(?:in)?|Vorstand(?:svorsitzender|svorsitzende)?|"
    r"Inhaber(?:in)?|Gesellschafter(?:in)?|Gründer(?:in)?|Liquidator(?:in)?)\b",
    re.IGNORECASE,
)

# "Geschäftsführer: Vorname Nachname" veya "Vorname Nachname, Stadt, *TT.MM.JJJJ"
_NAME_AFTER_ROLE = re.compile(
    r"(?:Geschäftsführer(?:in)?|Prokurist(?:in)?|Vorstand(?:svorsitzender)?|"
    r"Inhaber(?:in)?|Liquidator(?:in)?)"
    r"[:\s]+(?:(?:Dr\.|Prof\.|Dipl\.)\s+)?"
    r"([A-ZÜÖÄ][a-züöäß\-]+(?:\s+[a-züöäß\-]+)?\s+[A-ZÜÖÄ][a-züöäß\-]+(?:\s+[A-ZÜÖÄ][a-züöäß\-]+)?)",
    re.IGNORECASE,
)

# İsimden sonra gelen rol: "Max Mustermann, Geschäftsführer"
_ROLE_AFTER_NAME = re.compile(
    r"([A-ZÜÖÄ][a-züöäß\-]+(?:\s+[a-züöäß\-]+)?\s+[A-ZÜÖÄ][a-züöäß\-]+)"
    r",\s*(?:geb\.\s*[\d.]+,\s*)?(?:\w+,\s*)?"
    r"(Geschäftsführer(?:in)?|Prokurist(?:in)?|Vorstand|Inhaber(?:in)?)",
    re.IGNORECASE,
)


def find_bundesanzeiger_contacts(company_name: str, location: str = "") -> list[dict]:
    """
    Bundesanzeiger'de şirket için tescil ilanlarını arar, yetkili kişileri döndürür.
    """
    session = get_session()
    contacts: list[dict] = []
    seen_names: set[str] = set()

    # 1. SERP ile Bundesanzeiger ilan URL'lerini bul
    query = f'site:{_BANZ_DOMAIN} "{company_name}" Geschäftsführer'
    html = multi_engine_search(query, session)
    if not html:
        return []

    banz_urls = _extract_banz_urls(html, company_name)
    polite_sleep(1.5)

    # 2. İlan metinlerini çek ve ayrıştır
    for url in banz_urls[:4]:
        try:
            page_text = _fetch_announcement_text(url, session)
            if not page_text:
                continue
            for person in _extract_officers(page_text, company_name):
                name_key = person["full_name"].lower()
                if name_key not in seen_names:
                    seen_names.add(name_key)
                    contacts.append(person)
        except Exception as e:
            logger.debug(f"Bundesanzeiger page fetch error ({url}): {e}")
        polite_sleep(1.0)

    # 3. Eğer URL bulunamadıysa SERP snippet'lerinden çıkar
    if not contacts and html:
        soup = BeautifulSoup(html, "html.parser")
        serp_text = soup.get_text(separator="\n")
        for person in _extract_officers(serp_text, company_name):
            name_key = person["full_name"].lower()
            if name_key not in seen_names:
                seen_names.add(name_key)
                contacts.append(person)

    logger.info(f"Bundesanzeiger '{company_name}': {len(contacts)} officer(s) found")
    return contacts


def _extract_banz_urls(html: str, company_name: str) -> list[str]:
    """SERP HTML'inden bundesanzeiger.de URL'lerini çıkar."""
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    company_lower = company_name.lower()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if _BANZ_DOMAIN in href and "pub/de" in href:
            # URL şirket adını içeriyorsa öncelikli al
            link_text = a.get_text(separator=" ").lower()
            if any(w in link_text or w in href.lower()
                   for w in company_lower.split() if len(w) > 3):
                urls.insert(0, href)
            else:
                urls.append(href)
    return list(dict.fromkeys(urls))  # deduplicate, preserve order


def _fetch_announcement_text(url: str, session) -> str:
    """İlan sayfasını metin olarak çek (Jina önce, HTTP fallback)."""
    if _JINA_OK:
        try:
            text = fetch_with_jina(url)
            if text and len(text) > 100:
                return text
        except Exception:
            pass
    html = fetch_url(url, session)
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator="\n")


def _extract_officers(text: str, company_name: str) -> list[dict]:
    """Tescil metninden yetkili kişi isimlerini çıkar."""
    contacts = []
    seen: set[str] = set()

    # Şirket adını içeren bölümü bul — yanlış şirketten isim almamak için
    company_words = [w.lower() for w in company_name.split() if len(w) > 3]
    relevant_text = text
    for i, line in enumerate(text.splitlines()):
        if any(w in line.lower() for w in company_words):
            # Şirket adından sonraki 30 satırı al
            relevant_text = "\n".join(text.splitlines()[max(0, i-2):i+30])
            break

    # "Geschäftsführer: Vorname Nachname" deseni
    for m in _NAME_AFTER_ROLE.finditer(relevant_text):
        name = _clean_name(m.group(1))
        if name and name.lower() not in seen:
            seen.add(name.lower())
            role = _extract_role_word(m.group(0))
            contacts.append({
                "full_name": name,
                "title": role,
                "source": "bundesanzeiger",
            })

    # "Vorname Nachname, Geschäftsführer" deseni
    for m in _ROLE_AFTER_NAME.finditer(relevant_text):
        name = _clean_name(m.group(1))
        role = m.group(2)
        if name and name.lower() not in seen:
            seen.add(name.lower())
            contacts.append({
                "full_name": name,
                "title": role,
                "source": "bundesanzeiger",
            })

    return contacts


def _clean_name(raw: str) -> str:
    name = raw.strip()
    # Doğum tarihi, şehir, "geb." gibi artıkları kaldır
    name = re.sub(r",.*$", "", name).strip()
    name = re.sub(r"\s+", " ", name)
    # En az iki kelime, her biri büyük harfle başlıyor olmalı
    parts = name.split()
    if len(parts) < 2 or len(parts) > 4:
        return ""
    if not all(p[0].isupper() for p in parts if p.lower() not in {"von", "van", "de", "der"}):
        return ""
    return name


def _extract_role_word(text: str) -> str:
    m = _OFFICER_ROLES.search(text)
    return m.group(1) if m else "Geschäftsführer"
