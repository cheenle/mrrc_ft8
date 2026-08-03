#!/bin/bash
# MRRC-FT8 Website — Deploy to www.vlsc.net/mrrc_ft8/
set -e

# Configuration
LOCAL_DIR="/Users/cheenle/HAM/ft8/website"
REMOTE_HOST="www.vlsc.net"
REMOTE_USER="cheenle"
REMOTE_WEBROOT="/var/www/vlsc.net/mrrc_ft8"
BACKUP_DIR="/var/tmp/mrrc_ft8_backup_$(date +%Y%m%d_%H%M%S)"

echo "=========================================="
echo "MRRC-FT8 Website Deployment"
echo "=========================================="
echo ""

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

if [ ! -d "$LOCAL_DIR" ]; then
    echo -e "${RED}Error: Local directory not found: $LOCAL_DIR${NC}"
    exit 1
fi

echo "Local directory: $LOCAL_DIR"
echo "Remote host: $REMOTE_HOST"
echo "Remote path: $REMOTE_WEBROOT"
echo ""

cd "$LOCAL_DIR"

echo "Checking required files..."
REQUIRED_FILES=(
    "index.html"
    "zh/index.html"
    "css/octen.css"
    "css/ft8.css"
    "sdd.html"
    "zh/sdd.html"
    "sdd/index.html"
    "sdd/01-executive-summary.html"
    "sdd/15-ptt-safety-architecture.html"
    "js/global-nav.js"
)
for file in "${REQUIRED_FILES[@]}"; do
    if [ ! -f "$file" ]; then
        echo -e "${RED}Error: Required file missing: $file${NC}"
        exit 1
    fi
    echo -e "${GREEN}✓${NC} $file"
done
echo ""

DEPLOY_PACKAGE="/tmp/mrrc_ft8_website_$(date +%Y%m%d_%H%M%S).tar.gz"
tar -czf "$DEPLOY_PACKAGE" --exclude='deploy.sh' --exclude='build_sdd.py' --exclude='.DS_Store' --exclude='__pycache__' -C "$LOCAL_DIR" .
echo -e "${GREEN}✓${NC} Package created: $DEPLOY_PACKAGE"
echo ""

echo "This will:"
echo "  1. Backup current site"
echo "  2. Upload new files to $REMOTE_WEBROOT"
echo "  3. Set permissions and reload nginx"
echo ""

read -p "Continue with deployment? (y/N): " confirm
if [[ $confirm != [yY] ]]; then
    echo "Deployment cancelled."
    rm "$DEPLOY_PACKAGE"
    exit 0
fi

ssh "$REMOTE_USER@$REMOTE_HOST" << 'REMOTE_EOF'
    set -e
    if [ -d "/var/www/vlsc.net/mrrc_ft8" ] && [ "$(ls -A /var/www/vlsc.net/mrrc_ft8 2>/dev/null)" ]; then
        sudo mkdir -p /var/tmp
        sudo cp -r /var/www/vlsc.net/mrrc_ft8 /var/tmp/mrrc_ft8_backup_$(date +%Y%m%d_%H%M%S)
        echo "Backup created."
    fi
    sudo mkdir -p /var/www/vlsc.net/mrrc_ft8
    sudo chown -R www-data:www-data /var/www/vlsc.net/mrrc_ft8
    sudo chmod -R 755 /var/www/vlsc.net/mrrc_ft8
REMOTE_EOF

scp "$DEPLOY_PACKAGE" "$REMOTE_USER@$REMOTE_HOST:/var/tmp/"

ssh "$REMOTE_USER@$REMOTE_HOST" << 'REMOTE_EOF'
    set -e
    sudo tar -xzf "/var/tmp/mrrc_ft8_website_"*.tar.gz -C /var/www/vlsc.net/mrrc_ft8 --overwrite
    sudo chown -R www-data:www-data /var/www/vlsc.net/mrrc_ft8
    sudo chmod -R 755 /var/www/vlsc.net/mrrc_ft8
    find /var/www/vlsc.net/mrrc_ft8 -type f -name "*.html" -exec sudo chmod 644 {} \;
    find /var/www/vlsc.net/mrrc_ft8 -type f -name "*.css" -exec sudo chmod 644 {} \;
    find /var/www/vlsc.net/mrrc_ft8 -type f -name "*.js" -exec sudo chmod 644 {} \; 2>/dev/null || true
    rm -f "/var/tmp/mrrc_ft8_website_"*.tar.gz
    sudo nginx -t
    sudo systemctl reload nginx
    echo ""
    echo "MRRC-FT8 deployment complete!"
    echo "URL: https://www.vlsc.net/mrrc_ft8/"
REMOTE_EOF

rm -f "$DEPLOY_PACKAGE"

echo ""
echo -e "${GREEN}Deployment Complete!${NC}"
echo "https://www.vlsc.net/mrrc_ft8/"
echo ""
echo "Rollback: ssh $REMOTE_USER@$REMOTE_HOST"
echo "  sudo rm -rf $REMOTE_WEBROOT && sudo cp -r /var/tmp/mrrc_ft8_backup_* $REMOTE_WEBROOT"
