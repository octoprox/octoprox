#!/bin/bash
# Stealth Squid Proxy Setup Script for Octoprox
# This script installs and configures Squid as a proxy with optional authentication.

set -ex

# Install Squid (works on both yum and apt-based systems)
if command -v dnf &> /dev/null; then
    dnf install -y squid
elif command -v yum &> /dev/null; then
    yum install -y squid
elif command -v apt-get &> /dev/null; then
    apt-get update
    apt-get install -y squid
else
    echo "Unsupported package manager"
    exit 1
fi

# Stop squid if running
systemctl stop squid || true

# Remove ALL squid config files to start clean
rm -f /etc/squid/squid.conf
rm -rf /etc/squid/conf.d/*

# Create stealthy proxy configuration
cat > /etc/squid/squid.conf << SQUIDCONF
# Stealth proxy config for Octoprox

http_port 3128

##OCTOPROX_AUTH_CONFIG##

# === STEALTH SETTINGS ===

# Don't add X-Forwarded-For header (hides client IP)
forwarded_for delete

# Don't add Via header
via off

# Strip outgoing request headers that reveal proxy
request_header_access Via deny all
request_header_access X-Forwarded-For deny all
request_header_access Forwarded deny all
request_header_access Proxy-Connection deny all
request_header_access Cache-Control deny all

# Strip response headers that reveal proxy
reply_header_access X-Squid-Error deny all
reply_header_access X-Cache deny all
reply_header_access X-Cache-Lookup deny all
reply_header_access Via deny all
reply_header_access Server deny all
reply_header_access Proxy-Authenticate deny all

# Hide the proxy hostname
visible_hostname unknown

# Hide Squid version and identity completely
httpd_suppress_version_string on

# Disable custom error pages that reveal Squid
err_page_stylesheet none

# Don't identify as a proxy in error messages
error_default_language en

# Disable caching to avoid cache-related headers
cache deny all

# Disable other protocols that could leak info
icp_port 0
htcp_port 0
snmp_port 0

# Minimal logging (optional - can enable for debugging)
access_log none
cache_store_log none
SQUIDCONF

# Verify the config
echo "=== squid.conf contents ==="
cat /etc/squid/squid.conf
echo "==========================="

# Check syntax
squid -k parse || { echo "Squid config syntax error"; exit 1; }

# Enable and start Squid
systemctl enable squid
systemctl restart squid

# Show status
systemctl status squid --no-pager || true

echo "Squid setup complete"
