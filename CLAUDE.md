# CLAUDE.md — Cowork çalışma notları (Contact Enrichment)

Bu dosya, bu klasörde tekrarlayan "Google Sheets lead enrichment" işini yapan
asistan (Claude / Cowork) içindir. Amaç: her seferinde sıfırdan keşfetmeden
işi hızlı ve tutarlı yapmak.

## İş ne? (tekrar eden görev)

Kullanıcı "Enriched B2B Leads" Google Sheet'ine yeni şirket satırları ekler ve
şunu ister (genelde Türkçe, "yine ..." diye):

1. **Duplicate şirketleri temizle** (yeni eklenen satırlar arasında). İlk
   kaydı tut, sonraki duplicate satır(lar)ı sil. Aynı şirketin farklı
   şehir/ilan varyasyonları da duplicate sayılır (ör. "FRIENDS&CO" vs
   "FRIENDS CO", aynı iş ilanı).
2. **Chrome ile her şirkette işe alımda etkili kişilerin LinkedIn profillerini
   bul**, sheet'e contact olarak yaz (şirket başına **max 5**).
3. **Railway app'in `/enrich` endpoint'i ile enrichment** yap.

Kullanıcı tercihi her seferinde sorulabilir ama varsayılan: ilkini tut+sil;
boş şirket adı olan satıra dokunma; kontakları bul, sonra enrich et.

## Sheet bilgisi

- ID: `1xgcBLbgQJbF0J8Zk7adjGWrQIpCknvqt2YbuekBbaGM` (gid=0, "Sheet1")
- Bağlı Google connector adı: "Enriched B2B Leads".
- **Satır 1 = başlık.** Kolonlar (A–K):
  `Company | Location | Job Category | Company Web | Company Email | Company Phone | Contact 1 | Contact 2 | Contact 3 | Contact 4 | Contact 5`
- Contact hücre formatı (mevcut satırlarla aynı tut):
  `Ad Soyad (Rol) - linkedin.com/in/slug - email - +49telefon`
  (olmayan parçayı atla; sadece şirket sahibi/yönetici için telefon+email de olabilir)
- İşe alım yetkisi rating mantığı: 1=CEO/Geschäftsführer/Inhaber/MD,
  2=HR Director/Head of People, 3=HR/TA Manager, 4=Recruiter/HRBP, 5=destek.
  Sheet'e en yüksek yetkili + en alakalı 5 kişiyi yaz.

## ÖNEMLİ ortam kısıtları (zaman kazandırır)

- **Railway endpoint'leri terminalde `curl` ile çağrılabilir** (sandbox kısıtı kalktı).
  Örnek: `curl -s -X POST https://web-production-b9da8.up.railway.app/enrich -H "Content-Type: application/json" -d '{...}'`
- **Google Sheet'i fetch ile okuyamazsın** (editor sayfası CSP `connect-src` engeli).
  Okumak için ayrı sekmede `.../gviz/tq?tqx=out:html&gid=0&headers=0` aç,
  `get_page_text` ile oku (satır 1 = başlık dahil gelir).
- **Sheet'e yazma/silme = Chrome UI ile.** Name Box'a (sol üst hücre referansı)
  `A<satır>` yazıp Enter → hücre seçilir. Satır silmek için: Name Box ile git,
  `shift+space` (tüm satırı seç), sağ tık → "Delete row".
  **Satır silerken alttan üste doğru sil** ki numaralar kaymasın.
  Hücreye yazmak için: hücreyi seç, yaz, `Tab` ile sağa geç, `Enter` ile bitir.
- **Enrich uzun sürer (~60–90 sn/şirket, sunucu 4 paralel).** Hepsini arka planda
  fire-and-store yap: `window.__jobs[key]` içine sonucu yaz, sonra poll et.
  Fetch'ler o sekmenin window'unda yaşar → **o sekmeyi başka yere navigate etme**,
  yoksa istekler iptal olur. LinkedIn aramaları için AYRI sekme kullan.

## Endpoint'ler (Railway: https://web-production-b9da8.up.railway.app)

### POST /enrich  (şirket → karar verici kontaklar)
```json
{"company_name":"...","location":"Stadt, Germany","job_category":"...","max_contacts":5}
```
Dönen: `domain`, `company_contact_info{phone,email,website}`, `contacts[]`
(her biri: `full_name,title,email,email_verified,phone,direct_phone,linkedin_url,source,rating,confidence`),
`total_found`, `sources_used`, `errors`.
→ Company Web/Email/Phone kolonlarını ve contact'ların email/telefonunu bundan doldur.

## Önerilen akış (verimli)

1. Sheet'i gviz ile oku, yeni satırları + duplicate'leri tespit et.
2. Duplicate satırları Chrome UI'da sil (alttan üste).
3. Tüm yeni şirketler için `/enrich`'i arka planda fire et (`window.__jobs`).
4. Enrich biterken, sonucu zayıf olan küçük şirketler için ayrı sekmede
   Google `site:linkedin.com/in "<şirket>" (Geschäftsführer OR Recruiter OR HR)`
   araması yapıp LinkedIn profillerini topla.
5. Her şirket için: company web/email/phone + en iyi 5 kontağı birleştir, sheet satırına yaz.
6. gviz ile tekrar okuyup doğrula.

Son güncelleme: 2026-06-15 çalışmasında oluşturuldu/teyit edildi.
