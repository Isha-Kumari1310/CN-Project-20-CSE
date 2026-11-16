#!/usr/bin/env bash
# One-time setup for Ubuntu 20.04 / 22.04 / 24.04 (follows the Mininet manual).
#   ./setup.sh
# - installs Mininet, Open vSwitch, matplotlib from apt
# - creates ./.venv (with --system-site-packages so Mininet is visible) and
#   installs os-ken there (maintained fork of Ryu that works on Python 3.10-3.12)
# Nothing is installed with "sudo pip", so Ubuntu 24.04's
# "externally-managed-environment" error cannot happen.
set -e
cd "$(dirname "$0")"

echo "==> apt packages"
sudo apt update
sudo apt install -y mininet openvswitch-switch python3-matplotlib python3-pip \
                    python3-venv python3-dev iproute2 iperf net-tools psmisc
sudo systemctl enable --now openvswitch-switch 2>/dev/null \
  || sudo service openvswitch-switch start || true

echo "==> virtualenv with the OpenFlow controller framework"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv --system-site-packages .venv
fi
PY=.venv/bin/python
$PY -m pip install --upgrade pip setuptools wheel >/dev/null 2>&1 || true

ok() { $PY controller/run_controller.py --check >/dev/null 2>&1; }

if ! ok; then
    echo "   trying os-ken ..."
    $PY -m pip install os-ken || true
fi
if ! ok; then
    echo "   os-ken failed, trying ryu + eventlet 0.30.2 ..."
    $PY -m pip install "setuptools<58" "eventlet==0.30.2" ryu || true
fi
if ! ok; then
    echo "   pip failed, trying apt python3-ryu ..."
    sudo apt install -y python3-ryu || true
fi
if ! ok; then
    echo
    echo "ERROR: could not get a working os-ken / ryu. Details:"
    $PY controller/run_controller.py --check || true
    exit 1
fi

echo
echo "==> versions"
command -v mn && sudo mn --version
$PY --version
$PY controller/run_controller.py --check
$PY -c "import mininet, matplotlib; print('mininet + matplotlib importable from the venv')"
echo
echo "Setup finished. Next:  sudo mn --test pingall   then   ./run_all.sh"
