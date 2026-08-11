# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
# pylint: disable=line-too-long,too-many-lines,attribute-defined-outside-init
#
# Copyright 2025 Canonical Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3 as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""The initrd plugin for building kernel snaps.

- initrd-addons
  (list of strings; default: none)
  A list of files to include in the initrd, provided as relative paths to
  $CRAFT_STAGE/addons. For example,

      initrd-addons:
          - usr/bin/foo

  will result in "${CRAFT_STAGE}/addons/usr/bin/foo" being placed in the
  initrd as /usr/bin/foo.

- initrd-firmware:
  (list of strings; default: none)
  A list of firmware to include in the initrd, provided as relative paths to
  $CRAFT_STAGE/firmware. For example,

      initrd-firmware:
          - foo/bar.bin

  will result in "${CRAFT_STAGE}/firmware/foo/bar.bin" being placed in the
  initrd as /usr/lib/firmware/foo/bar.bin.

- initrd-modules:
  (list of strings; default: none)
  A list of modules to include in the initrd, provided as a list of module
  names. If the specified module(s) have dependencies, they are also installed.

- initrd-build-efi-image
  (string; default: false)
  Set to true if an EFI or UKI image is preferred over discrete kernel and
  initrd files. Only valid on systems where an EFI stub file is available.

- initrd-efi-image-key
  (string; default: snake oil key (/usr/lib/ubuntu-core-initramfs/snakeoil/PkKek-1-snakeoil.key))
  Requires initrd-build-efi-image to be true.
  Key to be used when creating the EFI image, provided as a relative path to
  $CRAFT_STAGE/signing. For example,

      initrd-efi-image-key: signing.key

  will result in "${CRAFT_STAGE}/signing/signing.key" being placed in the
  initrd chroot as /root/signing.key.

- initrd-efi-image-cert
  (string; default: snake oil certificate (/usr/lib/ubuntu-core-initramfs/snakeoil/PkKek-1-snakeoil.pem))
  Requires initrd-build-efi-image to be true.
  Certificate to be used when creating the EFI image, provided as a relative
  path to $CRAFT_STAGE/signing. For example,

      initrd-efi-image-cert: cert.pem

  will result in "${CRAFT_STAGE}/signing/cert.pem" being placed in the
  initrd chroot as /root/cert.pem.
"""

import os
from typing import Annotated, Literal, cast

from craft_application.util import humanize_list
from craft_parts import errors, infos, plugins
from craft_parts.packages.snaps import SnapPackage
from pydantic import Discriminator, Field, StringConstraints
from pydantic.dataclasses import dataclass
from typing_extensions import override

NonEmptyString = Annotated[str, StringConstraints(min_length=1, strict=True)]
INITRD_RELEASE_FROM_SNAP_BASE = {
    "core20": "focal",
    "core22": "jammy",
    "core24": "noble",
    "core26": "resolute",
}


@dataclass(slots=True, config=plugins.PluginProperties.model_config)
class InitrdConfigBase:
    kernel_modules: list[str] = Field(
        min_length=1, default_factory=list, examples=["modules/6.8.0-134-generic"]
    )
    build_backend: Literal["dracut", "u-c-i"] = "dracut"
    firmware: list[str] = Field(default_factory=list)
    extra_files: list[str] = Field(default_factory=list)


@dataclass(slots=True, config=plugins.PluginProperties.model_config)
class InitrdConfigEFISigning:
    key: NonEmptyString = "/snap/ubuntu-core-initramfs/current/usr/lib/ubuntu-core-initramfs/snakeoil/PkKek-1-snakeoil.key"
    cert: NonEmptyString = "/snap/ubuntu-core-initramfs/current/usr/lib/ubuntu-core-initramfs/snakeoil/PkKek-1-snakeoil.pem"


@dataclass(slots=True, config=plugins.PluginProperties.model_config)
class InitrdConfigEFI(InitrdConfigBase):
    image_type: Literal["efi"] = "efi"
    signing: InitrdConfigEFISigning = InitrdConfigEFISigning()


@dataclass(slots=True, config=plugins.PluginProperties.model_config)
class InitrdConfigRawImage(InitrdConfigBase):
    image_type: Literal["raw"] = "raw"


class InitrdPluginProperties(plugins.PluginProperties, frozen=True):
    """The part properties used by the Initrd plugin."""

    plugin: Literal["initrd"] = "initrd"

    initrd_config: Annotated[
        InitrdConfigRawImage | InitrdConfigEFI, Discriminator("image_type")
    ] = InitrdConfigRawImage()


class InitrdPlugin(plugins.Plugin):
    """Plugin for the initrd snap build."""

    properties_class = InitrdPluginProperties

    def __init__(
        self, *, properties: plugins.PluginProperties, part_info: infos.PartInfo
    ) -> None:
        super().__init__(properties=properties, part_info=part_info)
        self.options = cast(InitrdPluginProperties, self._options)

        if not self.options.initrd_config.kernel_modules:
            raise ValueError(
                "initrd_config.kernel_modules must be specified and contain at least one module path."
            )
        self.guessed_kernel_version = os.path.basename(
            self.options.initrd_config.kernel_modules[0]
        )
        if not self.guessed_kernel_version:
            raise ValueError(
                "Could not determine kernel version from kernel-modules path name. Please make sure the path is correct and points to a valid kernel module directory."
            )


    @override
    def get_pull_commands(self) -> list[str]:
        commands = ["snap install --edge ubuntu-core-initramfs", "snap install --classic --edge dracut"]
        base = self._part_info.base
        target_arch = self._part_info._project_info.arch_build_for
        target_triple = self._part_info._project_info.arch_triplet_build_for
        if base not in INITRD_RELEASE_FROM_SNAP_BASE:
            raise errors.PartsError(
                f"base {base!r} is not supported for the initrd plugin. Supported bases are {humanize_list(INITRD_RELEASE_FROM_SNAP_BASE.keys(), 'and')}"
            )

        initrd_root = "uc-initramfs-build"

        if self._part_info._project_info.arch_build_on != target_arch:
            commands.extend(
                [
                    f"UBUNTU_STORE_ARCH={target_arch} snap download ubuntu-core-initramfs --edge --basename=u-c-i-snap",
                    f"unsquashfs -d u-c-i-snap u-c-i-snap.snap /opt/{target_triple}-sysroot.tar",
                ]
            )
            sysroot_tar_path = f"u-c-i-snap/opt/{target_triple}-sysroot.tar"
        else:
            sysroot_tar_path = (
                f"/snap/ubuntu-core-initramfs/current/opt/{target_triple}-sysroot.tar"
            )

        commands.extend(
            [
                f"mkdir -p {initrd_root}",
                f"tar -xf {sysroot_tar_path} -C {initrd_root}",
                f"mknod {initrd_root}/dev/null c 1 3 || touch {initrd_root}/dev/null",
            ]
        )

        return commands

    @override
    def get_build_snaps(self) -> set[str]:
        # return {
        #     "ubuntu-core-initramfs",
        # }
        return set()

    @override
    def get_build_packages(self) -> set[str]:
        host_arch = self._part_info.host_arch
        target_arch = self._part_info.target_arch

        build_packages = {
            "tar",
            "binutils-multiarch",
            "squashfs-tools",
            "fakeroot",
        }

        # if running as non-root and cross-building we need libfake{ch}root for
        # the target arch
        if host_arch != target_arch and (os.getuid() != 0):
            build_packages |= {
                f"libfakeroot:{target_arch}",
            }

        return build_packages

    @override
    def get_build_environment(self) -> dict[str, str]:
        return {}

    def __generate_copy_files_commands(self) -> list[str]:
        guessed_kernel_version = self.guessed_kernel_version
        commands = [
            f"mkdir -p $CRAFT_PART_BUILD/uc-initramfs-build/usr/lib/modules/{guessed_kernel_version}/",
            "mkdir -p $CRAFT_PART_BUILD/uc-initramfs-build/usr/lib/firmware/",
            f"cp --reflink=auto --dereference $CRAFT_STAGE/kernel.img $CRAFT_PART_BUILD/uc-initramfs-build/boot/vmlinuz-{guessed_kernel_version}",
        ]
        for module in self.options.initrd_config.kernel_modules:
            commands.append(
                f"cp --reflink=auto -arT {module} $CRAFT_PART_BUILD/uc-initramfs-build/usr/lib/modules/{guessed_kernel_version}/"
            )
        for firmware in self.options.initrd_config.firmware:
            commands.append(
                f"cp --reflink=auto -arT {firmware} $CRAFT_PART_BUILD/uc-initramfs-build/usr/lib/firmware/"
            )
        for file in self.options.initrd_config.extra_files:
            commands.append(
                f"cp --reflink=auto -arT {file} $CRAFT_PART_BUILD/uc-initramfs-build/"
            )
        return commands

    def __get_systemd_efi_stub_name(self) -> str:
        """Return the name of the systemd EFI stub file for the target architecture."""
        arch = self._part_info._project_info.arch_build_for
        stub_name = {
            "amd64": "linuxx64.efi.stub",
            "i386": "linuxia32.efi.stub",
            "arm64": "linuxaa64.efi.stub",
            "armhf": "linuxarm.efi.stub",
            "riscv64": "linuxriscv64.efi.stub",
        }.get(arch)
        if not stub_name:
            raise ValueError(f"Unsupported architecture for EFI stub: {arch}")
        return stub_name

    def __generate_build_commands_uci(self) -> list[str]:
        guessed_kernel_version = self.guessed_kernel_version
        commands = [
            *self.__generate_copy_files_commands(),
            f"ln -sv vmlinuz-{guessed_kernel_version} $CRAFT_PART_BUILD/uc-initramfs-build/boot/kernel.img",
            f"ubuntu-core-initramfs create-initrd --kernelver={guessed_kernel_version} --root $CRAFT_PART_BUILD/uc-initramfs-build",
        ]
        if self.options.initrd_config.image_type == "efi":
            signing = self.options.initrd_config.signing
            stub_name = self.__get_systemd_efi_stub_name()
            commands.extend(
                [
                    "umount $CRAFT_PART_BUILD/uc-initramfs-build/etc/uci-signing.key || touch $CRAFT_PART_BUILD/uc-initramfs-build/etc/uci-signing.key",
                    "umount $CRAFT_PART_BUILD/uc-initramfs-build/etc/uci-signing.crt || touch $CRAFT_PART_BUILD/uc-initramfs-build/etc/uci-signing.crt",
                    f"mount --bind -r {signing.key} $CRAFT_PART_BUILD/uc-initramfs-build/etc/uci-signing.key",
                    f"mount --bind -r {signing.cert} $CRAFT_PART_BUILD/uc-initramfs-build/etc/uci-signing.crt",
                    f"ubuntu-core-initramfs create-efi --kernelver={guessed_kernel_version} --root $CRAFT_PART_BUILD/uc-initramfs-build --stub /usr/lib/systemd/boot/efi/{stub_name} --key /etc/uci-signing.key --cert /etc/uci-signing.crt",
                    f"install -Dvm755 $CRAFT_PART_BUILD/uc-initramfs-build/boot/kernel.efi-{guessed_kernel_version} $CRAFT_PART_INSTALL/kernel.efi-{guessed_kernel_version}",
                    f"ln -sv kernel.efi-{guessed_kernel_version} $CRAFT_PART_INSTALL/kernel.efi",
                ]
            )
        else:
            commands.append(
                f"cp -v $CRAFT_PART_BUILD/uc-initramfs-build/boot/initrd.img-{guessed_kernel_version} $CRAFT_PART_INSTALL/boot/"
            )
            commands.append(
                f"ln -sv initrd.img-{guessed_kernel_version} $CRAFT_PART_INSTALL/boot/initrd.img"
            )
        return commands

    def __generate_build_commands_dracut(self) -> list[str]:
        commands = [
            *self.__generate_copy_files_commands(),
            "echo Unimplemented",
            "exit 1",
        ]
        return commands

    @override
    def get_build_commands(self) -> list[str]:
        base = self._part_info.base
        arch = self._part_info.target_arch
        build_efi_image = self.options.initrd_config.image_type == "efi"

        if build_efi_image:
            # There are no EFI stubs for s390x or ppc64el
            if arch in {"s390x", "ppc64el"}:
                raise ValueError("initrd-build-efi-image not allowed for " + arch)

            # There are no EFI stubs for riscv until 24.04
            if arch == "riscv64" and base == "core22":
                raise ValueError(
                    "initrd-build-efi-image not allowed for riscv64 on core22"
                )

        if self.options.initrd_config.build_backend == "u-c-i":
            return self.__generate_build_commands_uci()
        if self.options.initrd_config.build_backend == "dracut":
            return self.__generate_build_commands_dracut()
        raise ValueError(
            f"Unsupported initrd-build-backend: {self.options.initrd_config.build_backend!r}"
        )
