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

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
    echo "ClawPi 需要 Python >= 3.10" >&2
    exit 1
}

if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
        PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        apt-get update
        apt-get install -y "python${PYTHON_VERSION}-venv" || apt-get install -y python3-venv
    else
        echo "缺少 Python venv/ensurepip，请先安装当前 Python 版本的 venv 包" >&2
        exit 1
    fi
fi

node -e 'const [major, minor] = process.versions.node.split(".").map(Number); process.exit(major > 22 || (major === 22 && minor >= 19) ? 0 : 1)' || {
    echo "Pi 0.84.2 需要 Node.js >= 22.19.0" >&2
    exit 1
}

id clawpi >/dev/null 2>&1 || useradd --system --home-dir /var/lib/clawpi --shell /usr/sbin/nologin clawpi
install -d -m 0755 /opt/clawpi
install -d -o root -g clawpi -m 0770 /var/lib/clawpi
install -d -o clawpi -g clawpi -m 0750 /var/lib/clawpi/workspace /var/lib/clawpi/sessions /var/lib/clawpi/pi-config /var/lib/clawpi/pi-config/skills /var/lib/clawpi/pi-config/extensions
install -d -m 0750 /etc/clawpi
chown root:clawpi /var/lib/clawpi
chmod 0770 /var/lib/clawpi
chown -R clawpi:clawpi /var/lib/clawpi/workspace /var/lib/clawpi/sessions /var/lib/clawpi/pi-config
chmod 0750 /var/lib/clawpi/workspace /var/lib/clawpi/sessions /var/lib/clawpi/pi-config
if [ -f /var/lib/clawpi/credentials.json ]; then
    chown root:clawpi /var/lib/clawpi/credentials.json
    chmod 0600 /var/lib/clawpi/credentials.json
fi
if [ -f /var/lib/clawpi/agent.json ]; then
    chown clawpi:clawpi /var/lib/clawpi/agent.json
    chmod 0660 /var/lib/clawpi/agent.json
fi

install -m 0644 "$SCRIPT_DIR/daemon.py" /opt/clawpi/daemon.py
install -m 0644 "$SCRIPT_DIR/simulator.py" /opt/clawpi/simulator.py
install -m 0644 "$SCRIPT_DIR/requirements.txt" /opt/clawpi/requirements.txt
install -o clawpi -g clawpi -m 0644 "$SCRIPT_DIR/clawpi-interaction.ts" /var/lib/clawpi/pi-config/extensions/clawpi-interaction.ts
python3 -m venv --clear /opt/clawpi/venv
/opt/clawpi/venv/bin/pip install --disable-pip-version-check --no-cache-dir -r /opt/clawpi/requirements.txt
npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@$PI_VERSION"

if [ ! -f /etc/clawpi/clawpi.env ]; then
    install -m 0600 "$SCRIPT_DIR/clawpi.env.example" /etc/clawpi/clawpi.env
fi
install -m 0644 "$SCRIPT_DIR/clawpi.service" /etc/systemd/system/clawpi.service
systemctl daemon-reload
systemctl enable clawpi.service

echo "安装完成。编辑 /etc/clawpi/clawpi.env 后运行：systemctl start clawpi"
