#!/bin/bash
# VPS ilk kurulum scripti (Ubuntu 22.04)
# Kullanım: sudo bash setup.sh

set -e

APP_DIR="/opt/contact-enrichment"
REPO="https://github.com/utkuoylum/gtm_contact_enricher.git"

echo "==> Sistem güncelleniyor..."
apt-get update -q && apt-get install -y --no-install-recommends \
    python3.11 python3.11-venv python3-pip git nginx curl

echo "==> Repo klonlanıyor → $APP_DIR"
if [ -d "$APP_DIR" ]; then
    cd "$APP_DIR" && git pull
else
    git clone "$REPO" "$APP_DIR"
fi

echo "==> Virtualenv kuruluyor..."
python3.11 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --no-cache-dir -r "$APP_DIR/requirements.txt"

echo "==> .env dosyası kontrol ediliyor..."
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"
    echo ""
    echo "  !! $APP_DIR/.env oluşturuldu."
    echo "  !! API keylerini girmeden önce düzenle: nano $APP_DIR/.env"
    echo ""
fi

echo "==> systemd servisi kuruluyor..."
cp "$APP_DIR/deploy/contact-enrichment.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable contact-enrichment
systemctl restart contact-enrichment

echo "==> nginx kuruluyor..."
cp "$APP_DIR/deploy/nginx.conf" /etc/nginx/sites-available/contact-enrichment
ln -sf /etc/nginx/sites-available/contact-enrichment /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

echo ""
echo "==> Kurulum tamamlandı!"
echo "    Servis durumu: systemctl status contact-enrichment"
echo "    Loglar:        journalctl -u contact-enrichment -f"
echo "    SSL için:      certbot --nginx -d your-domain.com"
