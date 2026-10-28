#!/usr/bin/env bash
# Install the HTTPS proxy; the Python backend stays on 127.0.0.1:8000.
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: sudo bash deploy/install-direct-ip.sh PUBLIC_IPV4" >&2
    exit 2
fi
public_ip="$(python3 -c 'import ipaddress, sys; ip = ipaddress.IPv4Address(sys.argv[1]); assert ip.is_global, "A public IPv4 address is required"; print(ip)' "$1")"
if [[ $EUID -ne 0 ]]; then
    echo "Run this installer with sudo; ports 80/443 and systemd require administrator access." >&2
    exit 1
fi
if [[ "$(uname -m)" != x86_64 ]]; then
    echo "This installer supports Linux x86_64." >&2
    exit 1
fi
deploy_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
for deploy_tool in curl tar sha256sum systemctl install useradd groupadd getent runuser; do
    command -v "$deploy_tool" >/dev/null
done
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8000/health >/dev/null
python3 - <<'PY'
from urllib.error import HTTPError
from urllib.request import urlopen
try:
    with urlopen("http://127.0.0.1:8000/api/tools/dice", timeout=5):
        raise SystemExit("Start the Python backend with --access-token-file before exposing it.")
except HTTPError as error:
    if error.code != 401:
        raise
    error.close()
PY
if ! systemctl is-active --quiet iirpg-caddy.service; then
    python3 - <<'PY'
import socket
for port in (80, 443, 2019):
    with socket.socket() as listener:
        listener.bind(("0.0.0.0" if port != 2019 else "127.0.0.1", port))
PY
fi

deploy_tmp="$(mktemp -d)"
trap 'rm -rf -- "$deploy_tmp"' EXIT
caddy_archive="$deploy_tmp/caddy.tar.gz"
curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
    --max-time 30 --output "$caddy_archive" \
    https://github.com/caddyserver/caddy/releases/download/v2.11.7/caddy_2.11.7_linux_amd64.tar.gz
printf '%s  %s\n' 727b91701a392de6ebc5027509f548bf39979e5216340d0faed8fa5e69c84f8b "$caddy_archive" | sha256sum --check --status
tar -xzf "$caddy_archive" --no-same-owner --no-same-permissions -C "$deploy_tmp" caddy
"$deploy_tmp/caddy" version

if ! getent group iirpg-web >/dev/null; then
    groupadd --system iirpg-web
fi
if ! id iirpg-web >/dev/null 2>&1; then
    useradd --system --gid iirpg-web --home-dir /var/lib/iirpg-caddy \
        --shell /usr/sbin/nologin --comment 'IIRPG HTTPS proxy' iirpg-web
fi
install -d -m 0755 /etc/iirpg-caddy
install -d -m 0700 -o iirpg-web -g iirpg-web /var/lib/iirpg-caddy
install -m 0755 "$deploy_tmp/caddy" /usr/local/bin/iirpg-caddy
install -m 0644 "$deploy_dir/Caddyfile" /etc/iirpg-caddy/Caddyfile
printf 'IIRPG_PUBLIC_IP=%s\n' "$public_ip" >/etc/iirpg-caddy/environment
chmod 0644 /etc/iirpg-caddy/environment
install -m 0644 "$deploy_dir/iirpg-caddy.service" /etc/systemd/system/iirpg-caddy.service
runuser -u iirpg-web -- env "IIRPG_PUBLIC_IP=$public_ip" \
    XDG_CONFIG_HOME=/var/lib/iirpg-caddy/config \
    /usr/local/bin/iirpg-caddy validate --config /etc/iirpg-caddy/Caddyfile --adapter caddyfile

if command -v ufw >/dev/null; then
    deploy_ufw_status="$(LC_ALL=C ufw status)"
    if [[ "$deploy_ufw_status" == 'Status: active'* ]]; then
        ufw allow 80/tcp comment 'IIRPG certificate validation and HTTPS redirect'
        ufw allow 443/tcp comment 'IIRPG HTTPS'
    fi
fi
systemctl daemon-reload
systemctl enable iirpg-caddy.service
systemctl restart iirpg-caddy.service
echo "Waiting for a publicly trusted certificate for $public_ip..."
for deploy_attempt in {1..10}; do
    if curl --noproxy '*' --fail --silent --show-error --max-time 3 \
        --connect-to "$public_ip:443:127.0.0.1:443" "https://$public_ip/health" >/dev/null 2>&1; then
        echo "HTTPS is ready: https://$public_ip/"
        echo "Keep the Python backend running. Verify access from another device before stopping the old tunnel."
        exit 0
    fi
    sleep 2
done
echo "HTTPS is not ready. Check inbound TCP 80/443 in the provider firewall and the service log:" >&2
echo "sudo journalctl -u iirpg-caddy.service -n 50 --no-pager" >&2
exit 1
