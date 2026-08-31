#!/bin/bash
# Generates a self-signed HTTPS certificate for dev-cert/, so other devices
# on the LAN can access the camera (getUserMedia requires a secure origin —
# localhost is exempt, but any other IP needs HTTPS).
#
# Re-run this whenever the server PC's IP changes (a different WiFi/venue) —
# browsers require the cert's IP to exactly match the address being visited.
#
# Usage:
#   ./gen-cert.sh            # auto-detect this machine's LAN IPv4
#   ./gen-cert.sh 10.1.2.3   # or pass the IP explicitly
set -e
cd "$(dirname "$0")"

IP="$1"
if [ -z "$IP" ]; then
    IP=$(ipconfig | grep "IPv4 Address" | grep -v "169.254" | head -1 | sed -E 's/.*: *//')
fi
if [ -z "$IP" ]; then
    echo "Could not auto-detect a LAN IP. Run: ./gen-cert.sh <server-ip>"
    exit 1
fi

mkdir -p dev-cert
# MSYS_NO_PATHCONV stops Git Bash from mangling the leading "/" in -subj
# into a Windows path (e.g. "/CN=..." -> "C:/Program Files/Git/CN=...").
MSYS_NO_PATHCONV=1 openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout dev-cert/key.pem -out dev-cert/cert.pem \
    -days 397 -subj "/CN=$IP" \
    -addext "subjectAltName=IP:$IP,IP:127.0.0.1"

echo
echo "Certificate generated for IP $IP"
echo "Server will be reachable at:"
echo "  https://$IP:8010   (other devices on the LAN)"
echo "  https://localhost:8010   (this PC)"
echo
echo "First visit on each device shows a browser warning (self-signed cert)"
echo "— click Advanced -> Proceed anyway / Accept the Risk. Only needed once"
echo "per device (browsers remember the exception)."
