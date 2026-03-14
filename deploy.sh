#!/usr/bin/env bash
# DGC SMS - Ubuntu Server Deployment Script
# Run as root or with sudo: sudo bash deploy.sh

set -e

APP_NAME="dgc_sms"
APP_DIR="/opt/dgc_sms"
APP_USER="dgc_sms"
VENV_DIR="$APP_DIR/venv"
DOMAIN="${1:-_}"  # Pass domain as argument, or default to any

echo "=== DGC SMS Deployment ==="

# 1. System packages
echo "[1/7] Installing system packages..."
apt update
apt install -y python3 python3-venv python3-pip nginx git

# 2. Create app user
echo "[2/7] Creating application user..."
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --shell /bin/false --home "$APP_DIR" "$APP_USER"
fi

# 3. Set up application directory
echo "[3/7] Setting up application directory..."
mkdir -p "$APP_DIR"
cp -r . "$APP_DIR/"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"

# 4. Create .env file if not exists
if [ ! -f "$APP_DIR/.env" ]; then
    echo "[4/7] Creating .env configuration..."
    SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    cat > "$APP_DIR/.env" <<EOF
SECRET_KEY=$SECRET
FLASK_CONFIG=production
DATABASE_URL=sqlite:///$APP_DIR/instance/dgc_sms.db

# Email - configure these for your mail server
MAIL_SERVER=smtp.gmail.com
MAIL_PORT=587
MAIL_USE_TLS=true
MAIL_USERNAME=your-email@example.com
MAIL_PASSWORD=your-app-password
MAIL_DEFAULT_SENDER=your-email@example.com
EOF
    echo "  >> Edit $APP_DIR/.env with your actual mail settings"
else
    echo "[4/7] .env already exists, skipping..."
fi

# 5. Initialize database
echo "[5/7] Initializing database..."
mkdir -p "$APP_DIR/instance" "$APP_DIR/uploads"
cd "$APP_DIR"
"$VENV_DIR/bin/python" -c "
from dotenv import load_dotenv
load_dotenv()
from app import create_app, db
app = create_app('production')
with app.app_context():
    db.create_all()
    print('  >> Database tables created')
"
"$VENV_DIR/bin/python" seed.py

# 6. Set permissions
echo "[6/7] Setting file permissions..."
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

# 7. Install systemd service + nginx
echo "[7/7] Installing systemd service and nginx config..."
cp "$APP_DIR/deployment/dgc_sms.service" /etc/systemd/system/dgc_sms.service

# Generate nginx config with the provided domain
sed "s/YOUR_DOMAIN_OR_IP/$DOMAIN/g" \
    "$APP_DIR/deployment/nginx_dgc_sms.conf" > /etc/nginx/sites-available/dgc_sms

ln -sf /etc/nginx/sites-available/dgc_sms /etc/nginx/sites-enabled/dgc_sms
rm -f /etc/nginx/sites-enabled/default

# Start services
systemctl daemon-reload
systemctl enable dgc_sms
systemctl start dgc_sms
nginx -t && systemctl restart nginx

echo ""
echo "=== Deployment Complete ==="
echo "App running at: http://$DOMAIN"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status dgc_sms    # Check app status"
echo "  sudo journalctl -u dgc_sms -f    # View app logs"
echo "  sudo systemctl restart dgc_sms   # Restart app"
echo "  sudo nano $APP_DIR/.env          # Edit configuration"
