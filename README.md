# Contact Enrichment Webhook

İş ajansı için şirketlerdeki işe alım karar vericilerini bulan webhook servisi.  
Şirket adı + lokasyon verilince; çalışan sayısını tahmin eder, uygun kişileri birden fazla kaynaktan bulur, email/telefon ile zenginleştirir.

## Hızlı Başlangıç

```bash
# 1. Virtual environment
python3 -m venv venv && source venv/bin/activate

# 2. Bağımlılıklar
pip install -r requirements.txt
playwright install chromium   # LinkedIn scraping için

# 3. Environment
cp .env.example .env
# .env içinde API key'leri doldur (aşağıya bak)

# 4. Çalıştır
python main.py
# veya
uvicorn main:app --host 0.0.0.0 --port 8000
```

## API Kullanımı

### `POST /enrich` — Şirket zenginleştirme

```bash
curl -X POST http://localhost:8000/enrich \
  -H "Content-Type: application/json" \
  -d '{
    "company_name": "Messe Frankfurt GmbH",
    "location": "Frankfurt, Germany",
    "job_category": "event",
    "max_contacts": 5
  }'
```

**Örnek yanıt:**

```json
{
  "company_name": "Messe Frankfurt GmbH",
  "domain": "messefrankfurt.com",
  "employee_count": 2700,
  "company_contact_info": {
    "phone": "+496975750",
    "email": "info@messefrankfurt.com",
    "website": "https://messefrankfurt.com"
  },
  "contacts": [
    {
      "full_name": "Anna Müller",
      "title": "HR Manager",
      "company": "Messe Frankfurt GmbH",
      "email": "a.mueller@messefrankfurt.com",
      "email_verified": true,
      "linkedin_url": "https://linkedin.com/in/anna-mueller",
      "source": "linkedin",
      "rating": 3,
      "rating_reason": "HR Manager level — day-to-day recruitment decisions",
      "confidence": 85,
      "employment_confirmed": true
    }
  ],
  "total_found": 6,
  "sources_used": ["gemini_initial", "linkedin", "xing", "pdl", "bundesanzeiger", "hunter"],
  "errors": [],
  "status": "completed"
}
```

**Request parametreleri:**

| Parametre | Tip | Açıklama |
|---|---|---|
| `company_name` | string | Şirket adı (zorunlu) |
| `location` | string | Şehir, ülke (ör. "Berlin, Germany") |
| `job_category` | string | Sektör ipucu (ör. "event", "hr", "staffing") |
| `max_contacts` | int | Maksimum kaç kişi dönsün (varsayılan: 5) |
| `domain` | string | Bilinen domain (opsiyonel, tahmin atlanır) |
| `callback_url` | string | Async mod için webhook URL (202 döner, biter bitmez POST eder) |
| `find_direct_lines` | bool | Direkt hat araması (daha yavaş) |

### `POST /match_person` — Tek kişi email/telefon bulma

Başka kaynaktan (LinkedIn, sheet, CRM) zaten bildiğin bir kişinin mail + telefonunu bulur.

```bash
curl -X POST http://localhost:8000/match_person \
  -H "Content-Type: application/json" \
  -d '{
    "full_name": "Jennifer Kandetzki",
    "company_name": "cip marketing GmbH",
    "domain": "cip-marketing.com"
  }'
```

---

## Akıllı Contact Stratejisi (Çalışan Sayısına Göre)

Gemini ilk adımda şirketin yaklaşık çalışan sayısını tahmin eder.  
Bu sayıya göre farklı contact arama stratejisi uygulanır:

| Şirket Boyutu | Strateji |
|---|---|
| **< 200 çalışan** | Önce event/HR/recruit title'ları arar. Bulamazsa tüm title'lara (CEO, Geschäftsführer vb.) döner. |
| **≥ 200 çalışan** | Sadece staffing-relevant title'lar: event, human resources, people, staff, recruit, operations, assist |

Büyük şirketlerde CEO/CFO bulmanın anlamı yok — ajansa gerçekten ulaşacak olan HR/event ekipleri.

---

## Derecelendirme Sistemi (1–5)

| Rating | Açıklama | Örnek Unvanlar |
|---|---|---|
| **1** | En yüksek karar yetkisi | CEO, Founder, Managing Director, Geschäftsführer, Inhaber |
| **2** | VP/Direktör seviyesi İK | HR Director, Chief People Officer, Head of HR, Personalleiter |
| **3** | Müdür seviyesi İK | HR Manager, TA Manager, HR Business Partner, Personalreferent |
| **4** | Uzman seviyesi | Recruiter, HR Specialist, HR Generalist |
| **5** | Destek rolü | HR Coordinator, HR Assistant |

---

## Veri Kaynakları

### Şirket Bilgisi (Initial Search)

| Kaynak | Ne Sağlar | API Key |
|---|---|---|
| **Gemini 2.5 Flash** (Google) | Çalışan sayısı, sektör, website, lokasyon | `GEMINI_API_KEY` |

### Kişi Bulma

| Kaynak | Kapsam | API Key |
|---|---|---|
| **LinkedIn** | Site:linkedin.com/in araması | Yok (scraping) |
| **XING** | DACH şirketleri | Yok (scraping) |
| **PDL** (People Data Labs) | Title bazlı kişi araması | `PDL_API_KEY` (1.000/ay ücretsiz) |
| **Hunter.io** | Domain-based email bulma | `HUNTER_API_KEY` (25/ay ücretsiz) |
| **Google/Bing SERP** | SERP'ten kişi çıkarma | Yok |
| **Crunchbase** | Leadership bilgisi | Yok (scraping) |
| **Companies House** | UK şirket yetkilileri | Yok |
| **Northdata** | Handelsregister (DACH) | Yok (scraping) |
| **Bundesanzeiger** | Resmi Alman federal gazetesi, tescil ilanları | Yok (scraping) |
| **XING / Kununu** | DACH işveren profili, HR yanıt imzaları | Yok (scraping) |
| **Almanya dizinleri** | gelbeseiten, 11880, wlw, cylex | Yok (scraping) |
| **Alman ticaret sicili** | openregister.de | Yok (scraping) |
| **Basın/Haberler** | presseportal.de, haber arşivleri | Yok (scraping) |
| **İş ilanları** | stepstone, indeed, monster | Yok (scraping) |
| **Şirket websitesi** | /team, /about, Impressum sayfaları | Yok (scraping) |

### Email Zenginleştirme

| Kaynak | Açıklama | API Key |
|---|---|---|
| **Icypeas** | İsim + domain → email (Apollo'nun yerini tutar) | `ICYPEAS_API_KEY` (1.000/ay ücretsiz) |
| **Email Hunter** | Domain pattern tespiti + SMTP doğrulama | Yok |
| **SMTP Verifier** | Bulk email doğrulama | Yok |

### Telefon

| Kaynak | Açıklama | API Key |
|---|---|---|
| **Google Places API** | Şirket ana hattı | `GOOGLE_MAPS_API_KEY` |
| **OpenStreetMap** | Fallback lokasyon/tel | Yok |
| **Gelbe Seiten / 11880** | Alman telefon rehberleri | Yok (scraping) |

---

## Konfigürasyon (.env)

```env
# Zorunlu
ANTHROPIC_API_KEY=sk-ant-...    # Claude ile SERP parsing ve contact scoring

# Önerilir (ücretsiz plan yeterli)
GEMINI_API_KEY=AIzaSy...        # Şirket initial search — aistudio.google.com
PDL_API_KEY=...                 # Title bazlı kişi araması — 1.000 ücretsiz/ay
ICYPEAS_API_KEY=...             # Email enrichment — 1.000 ücretsiz/ay
HUNTER_API_KEY=...              # Domain email bulma — 25 ücretsiz/ay
GOOGLE_MAPS_API_KEY=AIzaSy...   # Şirket telefonu — $200/ay ücretsiz kredi

# Opsiyonel
SCRAPER_API_KEY=...             # Google/Bing IP engeli varsa — scraperapi.com
JINA_API_KEY=...                # Yüksek rate limit için — jina.ai

# Sunucu
PORT=8000
REQUEST_TIMEOUT=15
DELAY_BETWEEN_REQUESTS=1.5
LARGE_COMPANY_THRESHOLD=200     # Bu değer ve üzeri = büyük şirket modu
```

---

## Proje Yapısı

```
contact-enrichment/
├── main.py                         # FastAPI app + endpoint'ler
├── enricher.py                     # Ana orkestrasyon (tüm kaynakları yönetir)
├── config.py                       # API key'ler + rating title'ları + staffing keyword'leri
├── models.py                       # Pydantic modelleri (EnrichmentResult, Contact...)
├── requirements.txt
│
├── scrapers/
│   ├── gemini_scraper.py           # ★ Gemini initial company search
│   ├── linkedin_scraper.py         # LinkedIn site:search
│   ├── xing_scraper.py             # XING (DACH)
│   ├── pdl_scraper.py              # ★ People Data Labs API
│   ├── bundesanzeiger_scraper.py   # ★ Bundesanzeiger tescil ilanları
│   ├── kununu_scraper.py           # ★ Kununu HR yanıt imzaları
│   ├── google_scraper.py           # Google/Bing SERP + Crunchbase
│   ├── hunter_scraper.py           # Hunter.io API
│   ├── website_scraper.py          # Şirket sitesi scraping
│   ├── news_scraper.py             # Haber arşivi
│   ├── press_scraper.py            # Basın bültenleri (presseportal.de)
│   ├── job_portal_scraper.py       # İş ilanı portalları
│   ├── german_directories.py       # Alman iş dizinleri
│   ├── openregister.py             # Handelsregister
│   ├── companies_house.py          # UK Companies House
│   └── apollo_scraper.py           # ⚠ Devre dışı (kod referans olarak duruyor)
│
├── email_hunter/                   # Domain pattern tespiti + SMTP doğrulama
├── phone_hunter/                   # Şirket ana hattı + direkt hat bulma
└── utils/
    ├── claude_extractor.py         # Claude ile SERP parsing + contact scoring
    ├── rater.py                    # Title → rating (1-5)
    ├── domain_finder.py            # Şirket domain tahmini
    └── http_client.py              # Session yönetimi + ScraperAPI fallback
```

---

## Railway Deploy

```bash
git push origin main
```

Gerekli environment variable'ları Railway dashboard'dan ekle.  
`PORT` otomatik atanır, uygulama `$PORT`'u okur.
