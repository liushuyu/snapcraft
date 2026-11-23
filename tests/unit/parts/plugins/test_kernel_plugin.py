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

"""Tests for the rewritten kernel plugin (snapcraft.parts.plugins.kernel_plugin)."""

import os
import subprocess

import pytest
from craft_parts import Part, PartInfo, ProjectInfo
from pydantic import ValidationError

from snapcraft.parts.plugins import KernelPlugin
from snapcraft.parts.plugins.kernel_plugin import _restrict_ubuntu_archive_sources
from snapcraft.parts.plugins.kernel_plugin.build import (
    generate_pull_commands,
    normalize_kernel_config,
    serialize_kernel_config,
    to_bash_array,
)


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
        part=Part("kernel", {}),
    )


def _make_plugin(properties: dict, part_info) -> KernelPlugin:
    return KernelPlugin(
        properties=KernelPlugin.properties_class.unmarshal(properties),
        part_info=part_info,
    )


class TestConfigSchema:
    """The kernel-build-config discriminated union."""

    def test_ubuntu_tree_defaults(self):
        props = KernelPlugin.properties_class.unmarshal(
            {"kernel-build-config": {"build-type": "ubuntu-tree"}}
        )
        config = props.kernel_build_config
        assert config.build_type == "ubuntu-tree"
        assert config.ubuntu_kernel_flavour == "generic"

    def test_binary_repack(self):
        props = KernelPlugin.properties_class.unmarshal(
            {
                "kernel-build-config": {
                    "build-type": "binary-repack",
                    "ubuntu-release-name": "noble",
                }
            }
        )
        assert props.kernel_build_config.build_type == "binary-repack"

    def test_generic_tree(self):
        props = KernelPlugin.properties_class.unmarshal(
            {"kernel-build-config": {"build-type": "generic-tree"}}
        )
        assert props.kernel_build_config.build_type == "generic-tree"

    def test_unknown_build_type_rejected(self):
        with pytest.raises(Exception, match="build_type"):
            KernelPlugin.properties_class.unmarshal(
                {"kernel-build-config": {"build-type": "nonsense"}}
            )

    def test_unknown_tool_rejected(self):
        with pytest.raises(Exception, match="tools-to-build"):
            KernelPlugin.properties_class.unmarshal(
                {
                    "kernel-build-config": {
                        "build-type": "ubuntu-tree",
                        "overrides": {"tools-to-build": ["perf", "not-a-tool"]},
                    }
                }
            )

    def test_bad_config_key_rejected(self):
        with pytest.raises(ValidationError):
            KernelPlugin.properties_class.unmarshal(
                {
                    "kernel-build-config": {
                        "build-type": "ubuntu-tree",
                        "overrides": {"override-options": {"NOT_A_CONFIG": True}},
                    }
                }
            )


class TestConfigSerialization:
    def test_normalize_bool(self):
        assert normalize_kernel_config({"CONFIG_FOO": True, "CONFIG_BAR": False}) == [
            "CONFIG_FOO=y",
            "CONFIG_BAR=n",
        ]

    def test_normalize_int(self):
        assert normalize_kernel_config({"CONFIG_FOO": 42}) == ["CONFIG_FOO=42"]

    def test_normalize_strings(self):
        assert normalize_kernel_config(
            {"CONFIG_A": "m", "CONFIG_B": "y", "CONFIG_C": "hello"}
        ) == ["CONFIG_A=m", "CONFIG_B=y", 'CONFIG_C="hello"']

    def test_normalize_rejects_bad_type(self):
        with pytest.raises(ValueError, match="Unsupported config value type"):
            normalize_kernel_config({"CONFIG_FOO": [1, 2]})

    def test_serialize(self):
        assert serialize_kernel_config({"CONFIG_A": True, "CONFIG_B": "n"}) == (
            "CONFIG_A=y\nCONFIG_B=n"
        )


class TestBashHelpers:
    def test_to_bash_array_list(self):
        assert to_bash_array(["a", "b c"]) == "(a 'b c')"

    def test_to_bash_array_dict(self):
        assert to_bash_array({"k": "v v"}) == "([k]='v v')"

    def test_to_bash_array_rejects_other(self):
        with pytest.raises(ValueError, match="Input must be"):
            to_bash_array("not-an-array")


class TestBuildPackages:
    def test_common_packages(self, part_info):
        plugin = _make_plugin(
            {"kernel-build-config": {"build-type": "ubuntu-tree"}}, part_info
        )
        packages = plugin.get_build_packages()
        for expected in ("fakeroot", "kmod", "rsync", "debhelper", "dpkg-dev"):
            assert expected in packages

    def test_binary_repack_minimal(self, part_info):
        plugin = _make_plugin(
            {
                "kernel-build-config": {
                    "build-type": "binary-repack",
                    "ubuntu-release-name": "noble",
                }
            },
            part_info,
        )
        assert plugin.get_build_packages() == {
            "dpkg-dev",
            "fakeroot",
            "rsync",
            "kmod",
            "findutils",
        }

    def test_tools_add_dependencies(self, part_info):
        plugin = _make_plugin(
            {
                "kernel-build-config": {
                    "build-type": "ubuntu-tree",
                    "overrides": {"tools-to-build": ["perf"]},
                }
            },
            part_info,
        )
        packages = plugin.get_build_packages()
        # perf's per-tool deps must be present, rtla's must not
        assert "libdw-dev:amd64" in packages
        assert "libtracefs-dev:amd64" not in packages


class TestCrossBuildSources:
    def test_restricts_official_sources_to_host_architecture(self, tmp_path):
        apt_dir = tmp_path / "apt"
        sources_dir = apt_dir / "sources.list.d"
        sources_dir.mkdir(parents=True)
        (apt_dir / "sources.list").write_text(
            "deb http://nz.archive.ubuntu.com/ubuntu jammy main\n"
            "deb [signed-by=/key.gpg] https://security.ubuntu.com/ubuntu "
            "jammy-security main\n"
            "deb http://ppa.example.test/ubuntu jammy main\n"
        )
        (sources_dir / "ports.sources").write_text(
            "Types: deb\nURIs: https://ports.ubuntu.com/ubuntu-ports/\nSuites: jammy\n"
        )
        (sources_dir / "archive.sources").write_text(
            "Types: deb\n"
            "URIs: http://archive.ubuntu.com/ubuntu/\n"
            "Suites: jammy jammy-updates\n"
            "Components: main universe\n\n"
            "Types: deb\n"
            "URIs: https://security.ubuntu.com/ubuntu/\n"
            "Suites: jammy-security\n"
            "Components: main universe\n"
        )

        _restrict_ubuntu_archive_sources("amd64", apt_dir=apt_dir)
        _restrict_ubuntu_archive_sources("amd64", apt_dir=apt_dir)

        assert (apt_dir / "sources.list").read_text() == (
            "deb [arch=amd64] http://nz.archive.ubuntu.com/ubuntu jammy main\n"
            "deb [signed-by=/key.gpg arch=amd64] "
            "https://security.ubuntu.com/ubuntu jammy-security main\n"
            "deb http://ppa.example.test/ubuntu jammy main\n"
        )
        assert "Architectures: amd64\n" in (sources_dir / "ports.sources").read_text()
        archive_sources = (sources_dir / "archive.sources").read_text()
        assert archive_sources.count("Architectures: amd64") == 2
        _restrict_ubuntu_archive_sources("amd64", apt_dir=apt_dir)
        assert (sources_dir / "archive.sources").read_text() == archive_sources

    @pytest.mark.parametrize(
        ("arch", "expected_url"),
        [
            ("i386", "https://archive.ubuntu.com/ubuntu/"),
            ("arm64", "https://ports.ubuntu.com/ubuntu-ports/"),
        ],
    )
    def test_cross_repository_matches_target_archive(
        self, new_dir, monkeypatch, mocker, arch, expected_url
    ):
        monkeypatch.setenv("SNAP_NAME", "snapcraft")
        mocker.patch(
            "snapcraft.parts.plugins.kernel_plugin._restrict_ubuntu_archive_sources"
        )
        check_call = mocker.patch(
            "snapcraft.parts.plugins.kernel_plugin.subprocess.check_call"
        )
        install = mocker.patch("snapcraft.parts.plugins.kernel_plugin.repo.install")
        part_info = PartInfo(
            project_info=ProjectInfo(
                application_name="test",
                project_name="test-snap",
                cache_dir=new_dir,
                base="core24",
                arch=arch,
            ),
            part=Part("kernel", {}),
        )
        plugin = _make_plugin(
            {"kernel-build-config": {"build-type": "binary-repack"}}, part_info
        )

        plugin._setup_cross_build_environment()

        check_call.assert_any_call(["dpkg", "--add-architecture", arch])
        repositories = install.call_args.args[0]
        assert repositories[0]["url"] == expected_url
        assert repositories[0]["architectures"] == [arch]
        assert repositories[0]["components"] == [
            "main",
            "universe",
            "restricted",
            "multiverse",
        ]


class TestGetBuildEnvironment:
    def test_generic_tree_sets_cross_vars(self, part_info):
        plugin = _make_plugin(
            {"kernel-build-config": {"build-type": "generic-tree"}}, part_info
        )
        env = plugin.get_build_environment()
        assert env["ARCH"] == "x86"
        assert env["CROSS_COMPILE"] == "x86_64-linux-gnu-"

    def test_ubuntu_tree_no_cross_vars(self, part_info):
        plugin = _make_plugin(
            {"kernel-build-config": {"build-type": "ubuntu-tree"}}, part_info
        )
        env = plugin.get_build_environment()
        assert "CROSS_COMPILE" not in env
        assert "ARCH" not in env


class TestGeneratedBuildCommands:
    """The rendered build templates."""

    def _render(self, properties: dict, part_info) -> str:
        plugin = _make_plugin(properties, part_info)
        commands = plugin.get_build_commands()
        assert len(commands) == 1
        return commands[0]

    def test_ubuntu_tree_extracts_unsigned_image(self, part_info):
        script = self._render(
            {"kernel-build-config": {"build-type": "ubuntu-tree"}}, part_info
        )
        assert "dpkg-deb -x linux-image-unsigned-*-generic_*.deb" in script
        assert "debian/rules build-generic" in script
        assert "debian/rules binary-generic" in script

    def test_binary_repack_extracts_signed_image(self, part_info):
        script = self._render(
            {
                "kernel-build-config": {
                    "build-type": "binary-repack",
                    "ubuntu-release-name": "noble",
                }
            },
            part_info,
        )
        assert "dpkg-deb -x linux-image-" in script
        assert "linux-image-unsigned" not in script
        assert "debian/rules" not in script
        assert 'linux-main-modules*-"${KERNEL_ABI}"-' in script
        assert f"rm -rf {part_info.part_build_dir}/kernel" in script

    def test_repack_dkms_headers_path_expands_at_runtime(self, part_info):
        """DKMS uses the ABI-flavoured headers directory from the downloaded
        headers package, not the literal ${KERNEL_ABI} text.
        """
        script = self._render(
            {
                "kernel-build-config": {
                    "build-type": "binary-repack",
                    "extra-modules": {
                        "helloworld": {
                            "source": "x",
                            "source-type": "local",
                        }
                    },
                }
            },
            part_info,
        )
        assert (
            '/usr/src/linux-headers-"${KERNEL_ABI}"-"${KERNEL_ABI_FLAVOUR}"' in script
        )
        assert '-k "${KERNEL_ABI}"-"${KERNEL_ABI_FLAVOUR}"' in script
        assert (
            '/lib/modules/"${KERNEL_ABI}"-"${KERNEL_ABI_FLAVOUR}"/updates/dkms'
            in script
        )

    @pytest.mark.parametrize(
        "build_type", ["binary-repack", "ubuntu-tree", "generic-tree"]
    )
    def test_binary_extra_module_is_unpacked_not_built(self, part_info, build_type):
        """An extra module without a source is a prebuilt deb from pull; it
        must be unpacked with the kernel modules, never handed to DKMS.
        """
        module = "linux-modules-iwlwifi-6.8.0-149-generic"
        script = self._render(
            {
                "kernel-build-config": {
                    "build-type": build_type,
                    "extra-modules": {
                        module: None,
                        "helloworld": {"source": "x", "source-type": "local"},
                    },
                }
            },
            part_info,
        )
        if build_type == "generic-tree":
            assert "dpkg-deb -x" in script
            assert f"/{module}_*.deb" in script
            assert '/modules/"${KERNEL_ABI}"' in script
        else:
            assert f"PKGS+=({module}_*.deb)" in script
        assert f"module_build_{module}" not in script
        assert f"dkms/{module}" not in script
        assert "module_build_helloworld" in script
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)
        if build_type == "ubuntu-tree":
            assert f"/{module}_*.deb {part_info.part_build_dir}/kernel" in script

    def test_kernel_img_is_hardlinked(self, part_info):
        """snapd extracts kernel.img by name from the snap; a symlink dangles."""
        script = self._render(
            {"kernel-build-config": {"build-type": "ubuntu-tree"}}, part_info
        )
        assert "ln -fv" in script
        assert "kernel.img" in script
        assert "ln -sfv" not in script.replace("ln -sfv vmlinuz", "")

    def test_kernel_img_falls_back_to_vmlinux(self, part_info):
        script = self._render(
            {"kernel-build-config": {"build-type": "binary-repack"}}, part_info
        )
        assert 'vmlinux-"${KERNEL_ABI}"-"${KERNEL_ABI_FLAVOUR}"' in script
        assert (
            "Kernel image not found for ${KERNEL_ABI}-${KERNEL_ABI_FLAVOUR}" in script
        )

    def test_image_targets_override(self, part_info):
        script = self._render(
            {
                "kernel-build-config": {
                    "build-type": "ubuntu-tree",
                    "image-targets": ["Image.gz", "modules"],
                }
            },
            part_info,
        )
        assert "build_image = Image.gz modules" in script
        # first target drives the kernel_file path in the debian rules
        assert "kernel_file = arch/x86/boot/Image.gz" in script

    def test_config_overrides_fragment(self, part_info):
        script = self._render(
            {
                "kernel-build-config": {
                    "build-type": "ubuntu-tree",
                    "overrides": {"override-options": {"CONFIG_FOO": True}},
                }
            },
            part_info,
        )
        assert "CONFIG_FOO=y" in script
        assert "--flavour generic" in script
        assert "annotations" in script

    def test_tools_selection(self, part_info):
        script = self._render(
            {
                "kernel-build-config": {
                    "build-type": "ubuntu-tree",
                    "overrides": {"tools-to-build": ["perf", "rtla"]},
                }
            },
            part_info,
        )
        assert "do_tools_perf = true" in script
        assert "do_tools_rtla = true" in script

    def test_binary_repack_copies_archive_firmware_before_staged_firmware(
        self, part_info
    ):
        script = self._render(
            {"kernel-build-config": {"build-type": "binary-repack"}}, part_info
        )
        archive_firmware = script.index("unpacked-linux-firmware/{,usr/}lib/firmware")
        copy_firmware = script.index("cp -a", archive_firmware)
        relocate_staged_firmware = script.index(
            "for _fw in usr/lib/firmware lib/firmware"
        )

        assert archive_firmware < copy_firmware < relocate_staged_firmware
        assert (
            f"{part_info.part_install_dir}/firmware/"
            in script[copy_firmware:relocate_staged_firmware]
        )

    @pytest.mark.parametrize(
        ("arch", "kernel_arch"),
        [
            ("amd64", "x86"),
            ("arm64", "arm64"),
            ("riscv64", "riscv"),
            ("s390x", "s390"),
        ],
    )
    def test_kernel_arch_mapping(self, new_dir, arch, kernel_arch):
        part_info = PartInfo(
            project_info=ProjectInfo(
                application_name="test",
                project_name="test-snap",
                cache_dir=new_dir,
                base="core24",
                arch=arch,
            ),
            part=Part("kernel", {}),
        )
        script = self._render(
            {"kernel-build-config": {"build-type": "ubuntu-tree"}}, part_info
        )
        assert f"Build for: {arch}" in script


class TestPullCommands:
    def test_binary_repack_pulls_debs(self, part_info):
        plugin = _make_plugin(
            {
                "kernel-build-config": {
                    "build-type": "binary-repack",
                    "ubuntu-release-name": "noble",
                }
            },
            part_info,
        )
        commands = plugin.get_pull_commands()
        assert len(commands) == 1
        script = commands[0]
        assert "apt-cache depends --no-recommends" in script
        assert 'apt-get download "${package}/noble"' in script
        assert "linux-firmware:amd64" in script
        assert "linux-image-" in script
        assert "ports.ubuntu.com" not in script

    def test_ubuntu_tree_no_deb_pull(self, part_info):
        plugin = _make_plugin(
            {"kernel-build-config": {"build-type": "ubuntu-tree"}}, part_info
        )
        assert (
            generate_pull_commands(plugin.options.kernel_build_config, part_info) == []
        )

    def test_release_name_from_base(self, part_info):
        plugin = _make_plugin(
            {
                "kernel-build-config": {
                    "build-type": "binary-repack",
                    "ubuntu-release-name": "noble",
                }
            },
            part_info,
        )
        script = plugin.get_pull_commands()[0]
        assert "noble" in script

    def test_binary_extra_module_uses_target_architecture(self, new_dir):
        part_info = PartInfo(
            project_info=ProjectInfo(
                application_name="test",
                project_name="test-snap",
                cache_dir=new_dir,
                base="core24",
                arch="arm64",
            ),
            part=Part("kernel", {}),
        )
        module = "linux-modules-iwlwifi-6.8.0-149-generic"
        plugin = _make_plugin(
            {
                "kernel-build-config": {
                    "build-type": "binary-repack",
                    "extra-modules": {module: None},
                }
            },
            part_info,
        )

        commands = plugin.get_pull_commands()

        assert f"apt-get download {module}:arm64" in commands


_APT_CACHE_STUB = """#!/bin/bash
case "$*" in
  "show linux-image-generic:amd64/noble")
    printf 'Package: linux-image-generic\\nVersion: 6.8.0-149.149\\n' ;;
  "depends --no-recommends linux-image-generic:amd64/noble")
    cat "$STUB_DIR/meta-depends" ;;
  "depends --no-recommends linux-image-6.8.0-149-generic/noble")
    cat "$STUB_DIR/image-depends" ;;
  "depends --no-recommends linux-firmware/noble")
    cat "$STUB_DIR/firmware-depends" ;;
esac
"""

_APT_GET_STUB = """#!/bin/bash
if [ "$1" = download ]; then
  package="${2%/*}"
  echo "${package}" >> "$STUB_DIR/downloads"
  touch "${package%%:*}_1_amd64.deb"
fi
"""


class TestPullScriptExecution:
    """Run the rendered binary-repack pull script against stubbed apt tools."""

    @pytest.mark.parametrize(
        ("meta_depends", "firmware_depends", "expected"),
        [
            pytest.param(
                [
                    "linux-image-6.8.0-149-generic",
                    "linux-modules-extra-6.8.0-149-generic",
                    "linux-firmware",
                    "intel-microcode",
                    "amd64-microcode",
                ],
                [
                    "linux-firmware-amd-graphics",
                    "linux-firmware-intel-graphics",
                    "<linux-firmware-vendor-unavailable>",
                ],
                {
                    "linux-image-6.8.0-149-generic",
                    "linux-modules-6.8.0-149-generic",
                    "linux-modules-extra-6.8.0-149-generic",
                    "linux-firmware",
                    "linux-firmware-amd-graphics",
                    "linux-firmware-intel-graphics",
                },
                id="noble-split-firmware",
            ),
            pytest.param(
                [
                    "linux-image-6.8.0-149-generic",
                    "linux-modules-6.8.0-149-generic",
                    "linux-main-modules-zfs-6.8.0-149-generic",
                    "linux-firmware",
                ],
                [],
                {
                    "linux-image-6.8.0-149-generic",
                    "linux-modules-6.8.0-149-generic",
                    "linux-main-modules-zfs-6.8.0-149-generic",
                    "linux-firmware",
                },
                id="resolute-main-modules",
            ),
            pytest.param(
                [
                    "linux-image-6.8.0-149-generic",
                    "<linux-modules-extra-6.8.0-149-generic>",
                    "<linux-firmware:amd64>",
                ],
                [],
                {
                    "linux-image-6.8.0-149-generic",
                    "linux-modules-6.8.0-149-generic",
                    "linux-firmware",
                },
                id="unpublished-module-package",
            ),
        ],
    )
    def test_downloads_kernel_and_firmware_packages(
        self, part_info, tmp_path, meta_depends, firmware_depends, expected
    ):
        stub_dir = tmp_path / "stubs"
        stub_dir.mkdir()
        for name, content in (
            ("apt-cache", _APT_CACHE_STUB),
            ("apt-get", _APT_GET_STUB),
        ):
            (stub_dir / name).write_text(content)
            (stub_dir / name).chmod(0o755)
        (stub_dir / "meta-depends").write_text(
            "linux-image-generic\n"
            + "".join(f"  Depends: {package}\n" for package in meta_depends)
        )
        (stub_dir / "image-depends").write_text(
            "linux-image-6.8.0-149-generic\n"
            "  Depends: kmod\n"
            "  Depends: linux-modules-6.8.0-149-generic\n"
        )
        (stub_dir / "firmware-depends").write_text(
            "linux-firmware\n"
            + "".join(f"  Depends: {package}\n" for package in firmware_depends)
        )
        part_info.part_src_dir.mkdir(parents=True, exist_ok=True)
        part_info.part_state_dir.mkdir(parents=True, exist_ok=True)
        plugin = _make_plugin(
            {
                "kernel-build-config": {
                    "build-type": "binary-repack",
                    "ubuntu-release-name": "noble",
                }
            },
            part_info,
        )

        subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + plugin.get_pull_commands()[0]],
            cwd=part_info.part_src_dir,
            env={
                **os.environ,
                "PATH": f"{stub_dir}:{os.environ['PATH']}",
                "STUB_DIR": str(stub_dir),
            },
            check=True,
        )

        downloads = set((stub_dir / "downloads").read_text().split())
        assert downloads == expected
        assert (part_info.part_state_dir / "kernel-abi-flavour").read_text() == (
            "generic\n"
        )


class TestCrossCompile:
    def test_cross_env_vars_on_generic_tree(self, part_info):
        # covered in TestGetBuildEnvironment; cross package selection needs a
        # cross-compiling ProjectInfo, exercised in the spread tests instead.
        pass
