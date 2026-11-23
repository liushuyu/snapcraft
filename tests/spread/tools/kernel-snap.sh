#!/bin/bash -e

# Helpers for kernel plugin spread tests.

#shellcheck source=tests/spread/tools/package-utils.sh
. "$TOOLS_DIR/package-utils.sh"

# Host builds change the host's package configuration: the kernel plugin
# restricts Ubuntu archive sources to the host architecture and adds the
# target architecture to dpkg. Save that state so it can be restored.
save_kernel_host_state() {
  local -a sources=(apt/sources.list.d)
  if [ -e /etc/apt/sources.list ]; then
    sources+=(apt/sources.list)
  fi

  create_dpkg_restore_point
  dpkg --print-foreign-architectures >/tmp/kernel-foreign-architectures.list
  tar -C /etc -cf /tmp/kernel-apt-sources.tar "${sources[@]}"
}

restore_kernel_host_state() {
  local arch

  dpkg_restore_point
  for arch in $(dpkg --print-foreign-architectures); do
    if ! grep -qx "$arch" /tmp/kernel-foreign-architectures.list; then
      dpkg --remove-architecture "$arch"
    fi
  done
  rm -rf /etc/apt/sources.list.d
  tar -C /etc -xf /tmp/kernel-apt-sources.tar
  apt-get update
}

# Print the kernel image for a flavour. Some architectures ship an
# uncompressed vmlinux rather than vmlinuz.
kernel_image() {
  local flavour="$1"
  local image

  for image in squashfs-root/vmlinuz-*-"$flavour" squashfs-root/vmlinux-*-"$flavour"; do
    if [ -f "$image" ]; then
      echo "$image"
      return
    fi
  done
}

# Unpack pc-kernel_*.snap into squashfs-root and check for the files snapd
# needs from a kernel snap of the given flavour.
verify_kernel_snap() {
  local flavour="$1"
  local expect_firmware="${2:-true}"
  local -a modules

  rm -rf squashfs-root
  unsquashfs pc-kernel_*.snap

  if [ ! -f squashfs-root/kernel.img ]; then
    echo "kernel.img is missing"
    return 1
  fi
  if [ -z "$(kernel_image "$flavour")" ]; then
    echo "No vmlinuz or vmlinux image for flavour $flavour"
    return 1
  fi
  modules=(squashfs-root/modules/*-"$flavour"/kernel)
  if [ ! -d "${modules[0]}" ]; then
    echo "No kernel modules for flavour $flavour"
    return 1
  fi
  if [ "$expect_firmware" = true ]; then
    verify_kernel_snap_has_files firmware
  fi
}

# Check that the unpacked snap declares an architecture and that its kernel
# image was built for it.
verify_kernel_snap_arch() {
  local arch="$1"
  local flavour="$2"
  local image pattern

  if ! grep -A1 "^architectures:" squashfs-root/meta/snap.yaml | grep -q "^- $arch$"; then
    echo "snap.yaml does not declare architecture $arch"
    return 1
  fi

  case "$arch" in
  arm64) pattern="ARM64|aarch64" ;;
  armhf) pattern="ARM boot|ARM " ;;
  riscv64) pattern="RISC-V|riscv" ;;
  s390x) pattern="S390|s390" ;;
  ppc64el) pattern="PowerPC|ppc64|OpenPOWER" ;;
  *)
    echo "No kernel image check for architecture $arch"
    return 1
    ;;
  esac

  image="$(kernel_image "$flavour")"
  if ! describe_kernel "$image" | grep -Eq "$pattern"; then
    echo "$image is not a $arch kernel"
    return 1
  fi
}

describe_kernel() {
  if gzip -t "$1" 2>/dev/null; then
    gzip -cd "$1" | file -
  else
    file "$1"
  fi
}

# Check that a directory in an unpacked snap contains at least one file.
verify_kernel_snap_has_files() {
  local dir="$1"
  local root="${2:-squashfs-root}"

  if ! find "$root/$dir" -type f -print -quit | grep -q .; then
    echo "$root/$dir is missing or empty"
    return 1
  fi
}
