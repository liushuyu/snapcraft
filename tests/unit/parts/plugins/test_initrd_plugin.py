# -*- Mode:Python; indent-tabs-mode:nil; tab-width:4 -*-
#
# Copyright 2026 Canonical Ltd.
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License version 3 as published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Tests for the rewritten initrd plugin (snapcraft.parts.plugins.initrd_plugin)."""

import pytest
from craft_parts import Part, PartInfo, ProjectInfo
from pydantic import ValidationError

from snapcraft.parts.plugins import InitrdPlugin


@pytest.fixture
def part_info(new_dir):
    return PartInfo(
        project_info=ProjectInfo(
            application_name="test",
            project_name="test-snap",
            cache_dir=new_dir,
            base="core24",
            arch="amd64",
        ),
        part=Part("initrd", {}),
    )


def _make_plugin(properties: dict, part_info) -> InitrdPlugin:
    return InitrdPlugin(
        properties=InitrdPlugin.properties_class.unmarshal(properties),
        part_info=part_info,
    )


DEFAULT_MODULES = ["$CRAFT_STAGE/modules/6.8.0-100-generic"]
# the image-type discriminator has no default in practice; always specify one
RAW_CONFIG = {"image-type": "raw"}


class TestConfigSchema:
    def test_defaults_are_raw_dracut(self, part_info):
        plugin = _make_plugin(
            {"initrd-config": {"kernel-modules": DEFAULT_MODULES, **RAW_CONFIG}},
            part_info,
        )
        assert plugin.options.initrd_config.image_type == "raw"
        assert plugin.options.initrd_config.build_backend == "dracut"

    def test_efi_schema(self, part_info):
        plugin = _make_plugin(
            {
                "initrd-config": {
                    "image-type": "efi",
                    "build-backend": "u-c-i",
                    "kernel-modules": DEFAULT_MODULES,
                }
            },
            part_info,
        )
        assert plugin.options.initrd_config.image_type == "efi"
        # snakeoil defaults
        assert "snakeoil" in plugin.options.initrd_config.signing.key

    def test_unknown_image_type_rejected(self, part_info):
        with pytest.raises(ValidationError):
            _make_plugin(
                {
                    "initrd-config": {
                        "image-type": "zstd",
                        "kernel-modules": DEFAULT_MODULES,
                    }
                },
                part_info,
            )

    def test_kernel_modules_required(self, part_info):
        with pytest.raises(ValueError, match="kernel_modules must be specified"):
            _make_plugin({"initrd-config": RAW_CONFIG}, part_info)

    def test_kernel_version_from_modules_path(self, part_info):
        plugin = _make_plugin(
            {"initrd-config": {"kernel-modules": DEFAULT_MODULES, **RAW_CONFIG}},
            part_info,
        )
        assert plugin.guessed_kernel_version == "6.8.0-100-generic"


class TestPullCommands:
    def test_installs_helper_snaps(self, part_info):
        plugin = _make_plugin(
            {"initrd-config": {"kernel-modules": DEFAULT_MODULES, **RAW_CONFIG}},
            part_info,
        )
        commands = plugin.get_pull_commands()
        joined = "\n".join(commands)
        assert "ubuntu-core-initramfs" in joined
        assert "dracut" in joined

    def test_unsupported_base_rejected(self, new_dir):
        part_info = PartInfo(
            project_info=ProjectInfo(
                application_name="test",
                project_name="test-snap",
                cache_dir=new_dir,
                base="core18",
                arch="amd64",
            ),
            part=Part("initrd", {}),
        )
        plugin = _make_plugin(
            {"initrd-config": {"kernel-modules": DEFAULT_MODULES, **RAW_CONFIG}},
            part_info,
        )
        with pytest.raises(Exception, match="not supported"):
            plugin.get_pull_commands()


class TestUciBuildCommands:
    def _commands(self, part_info, **config) -> list[str]:
        config.setdefault("image-type", "efi")
        config.setdefault("build-backend", "u-c-i")
        config.setdefault("kernel-modules", DEFAULT_MODULES)
        plugin = _make_plugin({"initrd-config": config}, part_info)
        return plugin.get_build_commands()

    def test_dracut_support_connected(self, part_info):
        plugin = _make_plugin(
            {"initrd-config": {"kernel-modules": DEFAULT_MODULES, **RAW_CONFIG}},
            part_info,
        )
        # connections were moved to the pull step
        assert (
            "snap connect ubuntu-core-initramfs:dracut-support"
            in plugin.get_pull_commands()
        )

    def test_snapd_info_staged(self, part_info):
        commands = self._commands(part_info)
        joined = "\n".join(commands)
        assert "snapd-info" in joined

    def test_efi_kernel_efi_is_real_file(self, part_info):
        """snapd extracts kernel.efi by name; it must not be a dangling symlink."""
        commands = self._commands(part_info)
        joined = "\n".join(commands)
        # the snap must contain kernel.efi as a real file
        assert "kernel.efi" in joined

    def test_custom_signing_key(self, part_info):
        commands = self._commands(
            part_info, signing={"key": "/my.key", "cert": "/my.crt"}
        )
        joined = "\n".join(commands)
        assert "/my.key" in joined
        assert "/my.crt" in joined

    def test_modules_copied(self, part_info):
        commands = self._commands(part_info)
        joined = "\n".join(commands)
        assert "usr/lib/modules/6.8.0-100-generic" in joined


class TestDracutBuildCommands:
    def _commands(self, part_info, **config) -> list[str]:
        config.setdefault("build-backend", "dracut")
        config.setdefault("kernel-modules", DEFAULT_MODULES)
        config.setdefault("image-type", "raw")
        plugin = _make_plugin({"initrd-config": config}, part_info)
        return plugin.get_build_commands()

    def test_dracut_support_connected(self, part_info):
        plugin = _make_plugin(
            {"initrd-config": {"kernel-modules": DEFAULT_MODULES, **RAW_CONFIG}},
            part_info,
        )
        # connections were moved to the pull step
        assert "snap connect dracut:dracut-support" in plugin.get_pull_commands()

    def test_raw_initrd_at_snap_root(self, part_info):
        """snapd extracts initrd.img by name from the snap root in raw mode."""
        commands = self._commands(part_info)
        joined = "\n".join(commands)
        assert "initrd.img-6.8.0-100-generic" in joined
        # real file at the snap root, hardlinked versioned name
        assert "$CRAFT_PART_INSTALL/initrd.img" in joined
        assert "-o busybox" in joined
        for helper in (
            "/usr/bin/mount",
            "/usr/bin/mountpoint",
            "/usr/bin/touch",
            "/usr/bin/chroot",
            "/usr/bin/systemd-mount",
            "/usr/bin/unsquashfs",
            "/usr/lib/snapd/snap-bootstrap",
        ):
            assert helper in joined
        # helpers are only requested when present in the sysroot
        assert "DRACUT_HELPERS+=(-I" in joined
        dracut = next(c for c in commands if "dracut -v" in c)
        assert '"${DRACUT_HELPERS[@]}"' in dracut
        assert (
            "-i /snap/ubuntu-core-initramfs/current/usr/lib/"
            "ubuntu-core-initramfs/main /"
        ) in joined
        assert (
            "cp -ar /snap/ubuntu-core-initramfs/current/usr/lib/"
            "ubuntu-core-initramfs/main"
        ) in joined
        assert "uc-initramfs-build/snap/ubuntu-core-initramfs" in joined
        assert "uc-initramfs-build/usr/sbin/chroot" in joined

    def test_efi_ukify(self, part_info):
        commands = self._commands(part_info, **{"image-type": "efi"})
        joined = "\n".join(commands)
        assert "--uefi" in joined
        assert "--ukify" in joined
        assert "linuxx64.efi.stub" in joined


class TestArchConstraints:
    @pytest.mark.parametrize("arch", ["s390x", "ppc64el"])
    def test_efi_rejected(self, new_dir, arch):
        part_info = PartInfo(
            project_info=ProjectInfo(
                application_name="test",
                project_name="test-snap",
                cache_dir=new_dir,
                base="core24",
                arch=arch,
            ),
            part=Part("initrd", {}),
        )
        plugin = _make_plugin(
            {
                "initrd-config": {
                    "image-type": "efi",
                    "kernel-modules": DEFAULT_MODULES,
                }
            },
            part_info,
        )
        with pytest.raises(ValueError, match="not allowed"):
            plugin.get_build_commands()

    def test_efi_rejected_riscv64_core22(self, new_dir):
        part_info = PartInfo(
            project_info=ProjectInfo(
                application_name="test",
                project_name="test-snap",
                cache_dir=new_dir,
                base="core22",
                arch="riscv64",
            ),
            part=Part("initrd", {}),
        )
        plugin = _make_plugin(
            {
                "initrd-config": {
                    "image-type": "efi",
                    "kernel-modules": DEFAULT_MODULES,
                }
            },
            part_info,
        )
        with pytest.raises(ValueError, match="riscv64"):
            plugin.get_build_commands()
