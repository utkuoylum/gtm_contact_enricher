# Contact Enrichment Webhook

Job agency için şirketlerdeki işe alım karar vericilerini bulan webhook servisi.

## Hızlı Başlangıç

```bash
# 1. Virtual environment
python3 -m venv venv && source venv/bin/activate

# 2. Bağımlılıklar
pip install -r requirements.txt

# 3. Environment
cp .env.example .env
# .env içinde HUNTER_API_KEY ekle (opsiyonel ama önerilir)

# 4. Çalıştır
python3 main.py
# veya
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Webhook Kullanımı

### Senkron Mod (sonuç bekle)

```bash
curl -X POST http://localhost:8000/enrich \
  -H "Content-Type: application/json" \
  -d '{
    "company_name": "Acme Corp",
    "location": "London",
    "job_category": "Software Engineering",
    "max_contacts": 10
  }'
```

### Asenkron Mod (callback URL ile)

```bash
curl -X POST http://localhost:8000/enrich \
  -H "Content-Type: application/json" \
  -d '{
    "company_name": "Acme Corp",
    "location": "London",
    "job_category": "Software Engineering",
    "callback_url": "https://your-app.com/webhook/result",
    "max_contacts": 10
  }'
```
→ 202 döner, işlem bitince sonuç `callback_url`'e POST edilir.

### Örnek Yanıt

```json
{
  "company_name": "Acme Corp",
  "domain": "acme.com",
  "contacts": [
    {
      "full_name": "Jane Smith",
      "title": "HR Director",
      "company": "Acme Corp",
      "email": "jane.smith@acme.com",
      "email_verified": true,
      "phone": "+44 20 7946 0958",
      "linkedin_url": "https://linkedin.com/in/jane-smith",
      "source": "hunter_domain",
      "rating": 2,
      "rating_reason": "VP/Director HR/People (HR Director) — can authorize agency agreements"
    }
  ],
  "total_found": 8,
  "sources_used": ["hunter", "linkedin", "website", "google", "crunchbase"],
  "errors": [],
  "status": "completed"
}
```

## Derecelendirme Sistemi (1-5)

| Rating | Açıklama | Örnek Unvanlar |
|--------|----------|----------------|
| **1** | En yüksek karar yetkisi | CEO, Founder, Managing Director, Owner, COO |
| **2** | VP/Direktör seviyesi İK | HR Director, Chief People Officer, Head of HR |
| **3** | Müdür seviyesi İK | HR Manager, TA Manager, HR Business Partner |
| **4** | Uzman seviyesi | Recruiter, HR Specialist, HR Generalist |
| **5** | Destek rolü | HR Coordinator, HR Assistant, Receptionist |

## Veri Kaynakları (sıra: öncelik sırasına göre)

1. **Hunter.io API** — Email pattern + doğrulanmış emailler (en güvenilir)
2. **LinkedIn** — Google üzerinden `site:linkedin.com/in` araması
3. **Şirket Websitesi** — /team, /about, /leadership, /contact sayfaları
4. **Google/Bing SERP** — Email ve isim çıkarma
5. **Crunchbase** — Leadership bilgisi

## Email Doğrulama

- **Hunter.io API** varsa: API ile doğrulama (güvenilir)
- **SMTP doğrulama** (ücretsiz): MX record + RCPT TO ile kontrol
- `email_verified: true` → mailbox mevcut
- `email_verified: false` → mailbox yok
- `email_verified: null` → doğrulanamadı (firewall/timeout)

## Ücretli Servis Önerileri

| Servis | Ücret | Ne Zaman Gerekli |
|--------|-------|-----------------|
| **Hunter.io Starter** | $49/ay (500 arama) | >25 şirket/ay taranıyorsa |
| **Hunter.io Growth** | $99/ay (2000 arama) | >100 şirket/ay |
| **ScraperAPI** | $49/ay (100k istek) | Google/Bing IP engeli varsa |

**Hunter.io olmadan:** Script çalışır ama email bulma oranı %30-50 düşer.
**ScraperAPI olmadan:** Google bazen IP'yi engeller; DuckDuckGo fallback'i devreye girer.

## Konfigürasyon (.env)

```env
HUNTER_API_KEY=your_key_here
SCRAPER_API_KEY=optional_key

PORT=8000
REQUEST_TIMEOUT=15
DELAY_BETWEEN_REQUESTS=1.5
SMTP_TIMEOUT=10
```

## Proje Yapısı

```
contact-enrichment/
├── main.py                    # FastAPI webhook app
├── enricher.py                # Ana orkestrasyon mantığı
├── config.py                  # Ayarlar + derecelendirme keyword'leri
├── models.py                  # Pydantic modelleri
├── scrapers/
│   ├── linkedin_scraper.py    # LinkedIn (Google üzerinden)
│   ├── website_scraper.py     # Şirket sitesi scraping
│   └── google_scraper.py      # Google/Bing SERP + Crunchbase
├── apis/
│   └── hunter_api.py          # Hunter.io entegrasyonu
├── verifiers/
│   └── email_verifier.py      # SMTP email doğrulama
└── utils/
    ├── domain_finder.py        # Şirket domain bulma
    ├── rater.py               # Derecelendirme mantığı
    ├── email_patterns.py      # Email pattern üretimi
    └── http_client.py         # Session + ScraperAPI fallback
```
