#!/usr/bin/env bash
# Prepare a fresh Ubuntu 22.04 VM to run the clinic monitoring system.
#
#   ./setup_vm.sh
#
# Installs KVM, the Android SDK (adb + emulator + a system image), creates an
# AVD, and builds the Python environment. It does NOT install Hik-Connect or
# sign in - those need a human, see DEPLOY_GCP.md step 5.
#
# Safe to re-run: every step checks before acting.
set -euo pipefail

SDK_ROOT="${ANDROID_SDK_ROOT:-$HOME/android-sdk}"
AVD_NAME="${CM_EMULATOR_AVD:-clinic}"
API="android-31"
IMAGE="system-images;${API};google_apis;x86_64"
CMDLINE_TOOLS_URL="https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="$(dirname "$HERE")"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

say "1. Checking hardware virtualisation"
if ! grep -Eqc '(vmx|svm)' /proc/cpuinfo; then
    cat >&2 <<'EOF'
This machine exposes no VMX/SVM support, so the Android emulator cannot be
accelerated and will be unusably slow.

On Google Compute Engine the instance must be created with nested
virtualisation enabled, on a machine type that supports it (N2, N2D, C2, C3 -
NOT E2 and not the ARM T2A):

  gcloud compute instances create clinic-monitor \
      --machine-type=n2-standard-4 \
      --enable-nested-virtualization \
      --image-family=ubuntu-2204-lts --image-project=ubuntu-os-cloud \
      --boot-disk-size=100GB --zone=asia-south1-a
EOF
    exit 1
fi
echo "virtualisation extensions present"

say "2. System packages"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    qemu-kvm libvirt-daemon-system bridge-utils cpu-checker \
    openjdk-17-jdk-headless unzip curl \
    python3-venv python3-pip \
    libgl1 libglib2.0-0 libpulse0 libnss3 libxcursor1 libxdamage1 libxrandr2
# libgl1/libglib2 are for OpenCV; the rest are the emulator's own dependencies.

if ! kvm-ok >/dev/null 2>&1; then
    echo "kvm-ok reports KVM is unavailable - check nested virtualisation" >&2
    exit 1
fi
sudo adduser "$USER" kvm >/dev/null 2>&1 || true
echo "KVM available (you may need to log out and back in for group membership)"

say "3. Android SDK"
if [[ ! -x "$SDK_ROOT/platform-tools/adb" ]]; then
    mkdir -p "$SDK_ROOT/cmdline-tools"
    tmp="$(mktemp -d)"
    curl -fsSL "$CMDLINE_TOOLS_URL" -o "$tmp/tools.zip"
    unzip -q "$tmp/tools.zip" -d "$tmp"
    rm -rf "$SDK_ROOT/cmdline-tools/latest"
    mv "$tmp/cmdline-tools" "$SDK_ROOT/cmdline-tools/latest"
    rm -rf "$tmp"
fi

export ANDROID_SDK_ROOT="$SDK_ROOT" ANDROID_HOME="$SDK_ROOT"
SDKMANAGER="$SDK_ROOT/cmdline-tools/latest/bin/sdkmanager"
yes | "$SDKMANAGER" --licenses >/dev/null 2>&1 || true
"$SDKMANAGER" --install "platform-tools" "emulator" "$IMAGE" >/dev/null
echo "SDK installed at $SDK_ROOT"

say "4. Virtual device"
AVDMANAGER="$SDK_ROOT/cmdline-tools/latest/bin/avdmanager"
if "$SDK_ROOT/emulator/emulator" -list-avds 2>/dev/null | grep -qx "$AVD_NAME"; then
    echo "AVD '$AVD_NAME' already exists"
else
    echo no | "$AVDMANAGER" create avd -n "$AVD_NAME" -k "$IMAGE" -d pixel_5
    echo "created AVD '$AVD_NAME'"
fi

# Memory settings that matter: the stock 2 GB is too thin for Android decoding
# several camera streams, which shows up as stalled frames and ANR dialogs.
CONFIG="$HOME/.android/avd/${AVD_NAME}.avd/config.ini"
if [[ -f "$CONFIG" ]]; then
    cp -n "$CONFIG" "$CONFIG.backup"
    sed -i 's/^hw\.ramSize=.*/hw.ramSize=4096/'   "$CONFIG" || true
    sed -i 's/^vm\.heapSize=.*/vm.heapSize=512/'  "$CONFIG" || true
    grep -q '^hw.ramSize='  "$CONFIG" || echo 'hw.ramSize=4096'  >>"$CONFIG"
    grep -q '^vm.heapSize=' "$CONFIG" || echo 'vm.heapSize=512'  >>"$CONFIG"
    echo "tuned $CONFIG (4096 MB RAM, 512 MB heap)"
fi

say "5. Python environment"
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
    python3 -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/pip" install -q --upgrade pip
# PyPI's Linux torch wheels bundle CUDA (~2.5 GB) and nothing here uses a GPU.
"$ROOT/.venv/bin/pip" install -q torch==2.4.1 torchvision==0.19.1 \
    --index-url https://download.pytorch.org/whl/cpu
"$ROOT/.venv/bin/pip" install -q -r "$HERE/requirements.txt"
echo "environment ready"

say "6. Environment variables"
PROFILE="$HOME/.profile"
add_line() { grep -qxF "$1" "$PROFILE" || echo "$1" >>"$PROFILE"; }
add_line "export ANDROID_SDK_ROOT=$SDK_ROOT"
add_line "export ANDROID_HOME=$SDK_ROOT"
add_line "export PATH=\$PATH:$SDK_ROOT/platform-tools:$SDK_ROOT/emulator"
add_line "export CM_ADB_PATH=$SDK_ROOT/platform-tools/adb"
add_line "export CM_EMULATOR_AVD=$AVD_NAME"
echo "written to $PROFILE"

cat <<EOF

Done. Still to do by hand (see DEPLOY_GCP.md):

  * put GEMINI_API_KEY in $HERE/.env
  * install the Hik-Connect APK on the AVD and sign in  (step 5)
  * verify:  $HERE/deploy/clinic.sh selftest
             $HERE/deploy/clinic.sh clinics
  * install the services: sudo cp deploy/*.service /etc/systemd/system/

Open a new shell first, so the environment variables above take effect.
EOF
