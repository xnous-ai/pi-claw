#!/bin/sh
set -eu

PI_VERSION=0.84.2
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "请使用 sudo ./install.sh" >&2
    exit 1
fi

for command in python3 node npm nmcli systemctl; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "缺少依赖：$command" >&2
        exit 1
    }
done

node -e 'const [major, minor] = process.versions.node.split(".").map(Number); process.exit(major > 22 || (major === 22 && minor >= 19) ? 0 : 1)' || {
    echo "Pi 0.84.2 需要 Node.js >= 22.19.0" >&2
    exit 1
}

id clawpi >/dev/null 2>&1 || useradd --system --home-dir /var/lib/clawpi --shell /usr/sbin/nologin clawpi
install -d -m 0755 /opt/clawpi
install -d -o clawpi -g clawpi -m 0750 /var/lib/clawpi /var/lib/clawpi/workspace /var/lib/clawpi/sessions /var/lib/clawpi/pi-config
install -d -m 0750 /etc/clawpi

install -m 0644 "$SCRIPT_DIR/daemon.py" /opt/clawpi/daemon.py
install -m 0644 "$SCRIPT_DIR/simulator.py" /opt/clawpi/simulator.py
install -m 0644 "$SCRIPT_DIR/requirements.txt" /opt/clawpi/requirements.txt
python3 -m venv /opt/clawpi/venv
/opt/clawpi/venv/bin/pip install --disable-pip-version-check --no-cache-dir -r /opt/clawpi/requirements.txt
npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@$PI_VERSION"

if [ ! -f /etc/clawpi/clawpi.env ]; then
    install -m 0600 "$SCRIPT_DIR/clawpi.env.example" /etc/clawpi/clawpi.env
fi
install -m 0644 "$SCRIPT_DIR/clawpi.service" /etc/systemd/system/clawpi.service
systemctl daemon-reload
systemctl enable clawpi.service

echo "安装完成。编辑 /etc/clawpi/clawpi.env 后运行：systemctl start clawpi"
