#!/usr/bin/env bash
# DGC SMS - Update script from Git into /opt/dgc_sms
# Run as root or with sudo: sudo bash update_from_git.sh
# Optional args: sudo bash update_from_git.sh <repo_url> <branch>

set -euo pipefail

APP_DIR="/opt/dgc_sms"
APP_USER="dgc_sms"
VENV_DIR="$APP_DIR/venv"
REPO_URL="${1:-https://github.com/dsitservicesja-lab/DGC_SMS.git}"
BRANCH="${2:-main}"

echo "=== DGC SMS Update ==="
echo "Repo: $REPO_URL"
echo "Branch: $BRANCH"
echo "Target: $APP_DIR"

mkdir -p "$APP_DIR"

if [ -d "$APP_DIR/.git" ]; then
    echo "[1/5] Existing git repository found. Pulling latest changes..."
    git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true
    git -C "$APP_DIR" fetch origin "$BRANCH"
    git -C "$APP_DIR" checkout "$BRANCH"
    # Reset hard so that any locally-modified tracked files (e.g. deployment/nginx_dgc_sms.conf)
    # do not block the update.  Runtime/state files (.env, instance/, uploads/, venv/) are
    # already excluded from git tracking so they are unaffected.
    git -C "$APP_DIR" reset --hard origin/"$BRANCH"
else
    echo "[1/5] No git repository in $APP_DIR. Syncing code from remote..."
    TMP_DIR="$(mktemp -d)"
    trap 'rm -rf "$TMP_DIR"' EXIT

    git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$TMP_DIR/repo"

    # Keep runtime/state files in place while updating application code.
    rsync -a --delete \
        --exclude '.env' \
        --exclude 'instance/' \
        --exclude 'uploads/' \
        --exclude 'venv/' \
        "$TMP_DIR/repo/" "$APP_DIR/"
fi

echo "[2/5] Ensuring Python virtual environment..."
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi

echo "[3/5] Installing/updating Python dependencies..."
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "[4/6] Running database migrations..."
"$VENV_DIR/bin/python" "$APP_DIR/migrate_db.py" "$APP_DIR/instance/dgc_sms.db"

echo "[5/6] Setting ownership and permissions..."
if id "$APP_USER" &>/dev/null; then
    chown -R "$APP_USER:$APP_USER" "$APP_DIR"
fi
if [ -f "$APP_DIR/.env" ]; then
    chmod 600 "$APP_DIR/.env"
fi

echo "[6/6] Updating nginx config and restarting services..."
NGINX_CONF="$APP_DIR/deployment/nginx_dgc_sms.conf"
if [ ! -f "$NGINX_CONF" ]; then
    echo "WARNING: nginx config not found at $NGINX_CONF, skipping nginx update"
else
    # Preserve the server_name from the already-deployed config so the domain
    # placeholder is replaced with whatever was set during the initial deploy.
    CURRENT_SERVER_NAME="_"
    if [ -f /etc/nginx/sites-available/dgc_sms ]; then
        CURRENT_SERVER_NAME=$(grep -oP 'server_name\s+\K[^;[:space:]]+' /etc/nginx/sites-available/dgc_sms \
            | head -1 | xargs)
        : "${CURRENT_SERVER_NAME:=_}"
    fi
    sed "s/YOUR_DOMAIN_OR_IP/${CURRENT_SERVER_NAME}/g" "$NGINX_CONF" \
        > /etc/nginx/sites-available/dgc_sms
    nginx -t && systemctl reload nginx
fi
systemctl daemon-reload
systemctl restart dgc_sms
systemctl status dgc_sms --no-pager -l || true

echo ""
echo "=== Update Complete ==="
echo "Useful commands:"
echo "  sudo journalctl -u dgc_sms -f"
echo "  sudo systemctl restart dgc_sms"
