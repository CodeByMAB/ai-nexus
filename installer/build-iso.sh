#!/usr/bin/env bash
# ============================================================
# AI Nexus — ISO Builder
# Remaster Ubuntu 24.04.2 LTS Server with AI Nexus autoinstall
#
# Requirements: xorriso p7zip-full wget
#   sudo apt install xorriso p7zip-full wget
#
# Usage: bash installer/build-iso.sh [--version 1.0.0]
# Output: ai-nexus-installer-<version>-amd64.iso
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
VERSION="${1:-1.0.0}"
[[ "$1" == "--version" ]] && VERSION="${2:-1.0.0}"

UBUNTU_VERSION="24.04.2"
UBUNTU_ISO="ubuntu-${UBUNTU_VERSION}-live-server-amd64.iso"
UBUNTU_URL="https://releases.ubuntu.com/24.04/${UBUNTU_ISO}"
OUTPUT_ISO="$SCRIPT_DIR/ai-nexus-installer-${VERSION}-amd64.iso"
WORK_DIR="$(mktemp -d /tmp/ai-nexus-iso-XXXXXX)"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
log()  { echo -e "${CYAN}[ISO-BUILD]${NC} $*"; }
ok()   { echo -e "${GREEN}[  OK  ]${NC} $*"; }
fail() { echo -e "${RED}[ FAIL ]${NC} $*"; rm -rf "$WORK_DIR"; exit 1; }

cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT

log "AI Nexus ISO Builder v${VERSION}"
log "Output: $OUTPUT_ISO"
echo ""

# ---- Dependency check ----
for cmd in xorriso 7z wget sha256sum tar; do
  command -v "$cmd" &>/dev/null || fail "Missing: $cmd — install with: sudo apt install xorriso p7zip-full wget"
done
ok "Dependencies satisfied"

# ---- Download Ubuntu ISO ----
cd "$SCRIPT_DIR"
if [[ ! -f "$UBUNTU_ISO" ]]; then
  log "Downloading Ubuntu ${UBUNTU_VERSION} Server..."
  wget -c --progress=bar:force "$UBUNTU_URL" -O "$UBUNTU_ISO" || fail "Download failed"
fi

# Verify SHA256
log "Verifying Ubuntu ISO checksum..."
SUMS_FILE="/tmp/ubuntu-noble-sha256sums"
wget -q "https://releases.ubuntu.com/24.04/SHA256SUMS" -O "$SUMS_FILE"
if grep -q "$UBUNTU_ISO" "$SUMS_FILE"; then
  grep "$UBUNTU_ISO" "$SUMS_FILE" | sha256sum --check --status || fail "Checksum verification failed! ISO may be corrupt."
  ok "Checksum verified"
else
  log "WARNING: Could not find checksum entry — proceeding anyway"
fi

# ---- Extract ISO ----
log "Extracting Ubuntu ISO..."
mkdir -p "$WORK_DIR/iso"
7z x "$UBUNTU_ISO" -o"$WORK_DIR/iso" -y > /dev/null
ok "ISO extracted"

# ---- Bundle AI Nexus source ----
log "Bundling AI Nexus source (excluding secrets/data/models)..."
BUNDLE="$WORK_DIR/iso/ai-nexus.tar.gz"
tar -czf "$BUNDLE" \
  --exclude='.git' \
  --exclude='*.sqlite' \
  --exclude='*.sqlite3' \
  --exclude='.env' \
  --exclude='.env.local' \
  --exclude='data/' \
  --exclude='*.log' \
  --exclude='*.iso' \
  --exclude='.venv/' \
  --exclude='venv/' \
  --exclude='__pycache__/' \
  --exclude='node_modules/' \
  --exclude='*.pyc' \
  --exclude='backups/' \
  --exclude='huggingface/' \
  --exclude='*.safetensors' \
  --exclude='*.gguf' \
  --exclude='*.bin' \
  -C "$REPO_ROOT" .

BUNDLE_SIZE=$(du -sh "$BUNDLE" | cut -f1)
ok "Source bundle: $BUNDLE_SIZE"

# ---- Inject autoinstall ----
log "Injecting autoinstall configuration..."
mkdir -p "$WORK_DIR/iso/autoinstall"
cp "$SCRIPT_DIR/autoinstall/user-data"  "$WORK_DIR/iso/autoinstall/"
cp "$SCRIPT_DIR/autoinstall/meta-data"  "$WORK_DIR/iso/autoinstall/"
cp "$SCRIPT_DIR/install.sh"             "$WORK_DIR/iso/"
ok "Autoinstall injected"

# ---- Patch GRUB ----
log "Patching GRUB boot menu..."
GRUB_CFG="$WORK_DIR/iso/boot/grub/grub.cfg"

cat > "$GRUB_CFG" << 'GRUBEOF'
set default="0"
set timeout=10
set timeout_style=menu

loadfont unicode

menuentry "Install AI Nexus (Automated)" {
    set gfxpayload=keep
    linux   /casper/vmlinuz quiet autoinstall ds=nocloud\;s=/cdrom/autoinstall/ ---
    initrd  /casper/initrd
}

menuentry "Install AI Nexus (Interactive)" {
    set gfxpayload=keep
    linux   /casper/vmlinuz ---
    initrd  /casper/initrd
}

menuentry "Boot from first hard disk" {
    set root=(hd0)
    chainloader +1
}
GRUBEOF

# Also patch EFI grub if present
EFI_GRUB="$WORK_DIR/iso/EFI/boot/grub.cfg"
if [[ -f "$EFI_GRUB" ]]; then
  cp "$GRUB_CFG" "$EFI_GRUB"
fi
ok "GRUB patched"

# ---- Update MD5 checksums ----
log "Updating ISO checksums..."
cd "$WORK_DIR/iso"
find . -type f -not -name "md5sum.txt" -not -path './.git/*' | sort | \
  xargs md5sum > md5sum.txt 2>/dev/null || true
cd "$SCRIPT_DIR"
ok "Checksums updated"

# ---- Repack ISO ----
log "Repacking as bootable ISO (this may take a few minutes)..."

# Detect MBR boot image
MBR_IMG=""
for p in \
  "$WORK_DIR/iso/boot/grub/i386-pc/boot_hybrid.img" \
  "$WORK_DIR/iso/isolinux/isohdpfx.bin" \
  "$WORK_DIR/iso/[BOOT]/1-Boot-NoEmul.img"; do
  [[ -f "$p" ]] && { MBR_IMG="$p"; break; }
done

# Detect EFI image
EFI_IMG=""
for p in \
  "$WORK_DIR/iso/boot/grub/efi.img" \
  "$WORK_DIR/iso/EFI/boot/bootx64.efi"; do
  [[ -f "$p" ]] && { EFI_IMG="$p"; break; }
done

XORRISO_ARGS=(
  xorriso -as mkisofs
  -r -V "AI_NEXUS_${VERSION//./_}"
  -J -joliet-long
)

if [[ -n "$MBR_IMG" ]]; then
  XORRISO_ARGS+=(--grub2-mbr "$MBR_IMG" -partition_offset 16 --mbr-force-bootable)
fi

XORRISO_ARGS+=(
  -c '/boot/grub/boot.cat'
  -b '/boot/grub/i386-pc/eltorito.img'
  -no-emul-boot -boot-load-size 4 -boot-info-table --grub2-boot-info
)

if [[ -n "$EFI_IMG" ]]; then
  XORRISO_ARGS+=(
    -append_partition 2 28732ac11ff8d211ba4b00a0c93ec93b "$EFI_IMG"
    -appended_part_as_gpt
    -eltorito-alt-boot
    -e '--interval:appended_partition_2:::'
    -no-emul-boot
  )
fi

XORRISO_ARGS+=(-o "$OUTPUT_ISO" "$WORK_DIR/iso")

"${XORRISO_ARGS[@]}" 2>&1 | grep -v "^xorriso" | tail -5 || fail "xorriso failed"

ok "ISO repacked"

# ---- Final output ----
ISO_SIZE=$(du -sh "$OUTPUT_ISO" | cut -f1)
ISO_SHA256=$(sha256sum "$OUTPUT_ISO" | cut -d' ' -f1)

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  AI Nexus Installer ISO Ready"
echo ""
echo "  File   : $OUTPUT_ISO"
echo "  Size   : $ISO_SIZE"
echo "  SHA256 : $ISO_SHA256"
echo ""
echo "  Flash to USB:"
echo "  sudo dd if=\"$OUTPUT_ISO\" of=/dev/sdX bs=4M status=progress oflag=sync"
echo ""
echo "  Or use Balena Etcher: https://etcher.balena.io"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
