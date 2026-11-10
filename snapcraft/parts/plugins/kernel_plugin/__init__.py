import logging
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

from craft_parts import errors, infos, plugins
from craft_parts.parts import PartSpec
from craft_parts.sources import SourceModel, get_source_handler
from pydantic import StringConstraints
from typing_extensions import override

from craft_archives import repo

from .build import (
    escape_shell_word,
    generate_build_commands,
    generate_pull_commands,
    UBUNTU_RELEASE_FROM_SNAP_BASE,
)
from .config import (
    KernelBuildConfig,
    KernelConfigBuildTypeGeneric,
    KernelConfigBuildTypeUbuntu,
)

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from craft_parts.parts import Part


KernelTools = Literal[
    "bpf",
    "cpupower",
    "perf",
    "perf_python",
    "perf_jvmti",
    "rtla",
    "x86",
    "acpidbg",
    "usbip",
    "hyperv",
]
UbuntuReleaseName = StringConstraints(min_length=1, to_lower=True, strict=True)
NonEmptyString = Annotated[str, StringConstraints(min_length=1, strict=True)]

VALID_KERNEL_TOOLS = set(KernelTools.__args__)
KERNEL_TOOLS_BUILD_DEPS = {
    "bpf": {
        "libelf-dev:target",
        "zlib1g-dev:target",
        "libcap-dev:target",
        "libiberty-dev:target",
    },
    "cpupower": {"libpci-dev:target", "gettext"},
    "perf": {
        "libdw-dev:target",
        "libunwind8-dev:target",
        "libtraceevent-dev:target",
        "libiberty-dev:target",
        "libnuma-dev:target",
        "liblzma-dev:target",
        "zlib1g-dev:target",
        "libelf-dev",
        "libelf-dev:target",
        "libbpf-dev:target",
        "libstd-rust-dev:target",
        "libzstd-dev",
        "libzstd-dev:target",
        "pahole",
    },
    "perf_python": {"python3-dev", "python3-setuptools", "libpython3-dev:target"},
    "perf_jvmti": {"java-common:target", "default-jdk-headless"},
    "rtla": {
        "libelf-dev:target",
        "libssl-dev:target",
        "libtraceevent-dev:target",
        "libtracefs-dev:target",
        "clang",
        "python3-docutils",
    },
    "x86": {"libnl-3-dev:target", "libnl-genl-3-dev:target"},
    "acpidbg": set(),
    "usbip": {
        "libtool:target",
        "libudev-dev:target",
        "pkg-config:target",
    },
    "hyperv": set(),
}
KERNEL_ARCH_FROM_SNAP_ARCH = {
    "i386": "x86",
    "amd64": "x86",
    "armhf": "arm",
    "arm64": "arm64",
    "ppc64el": "powerpc",
    "riscv64": "riscv",
    "s390x": "s390",
}


class SubPart:
    def __init__(self, name: str, source_dir: Path, source: dict[str, Any]) -> None:
        self.name = name
        self.source_dir = source_dir
        self.part_src_dir = source_dir
        self.spec = PartSpec.unmarshal(source)


class KernelPluginProperties(plugins.PluginProperties, frozen=True):
    """The part properties used by the Kernel plugin."""

    plugin: Literal["kernel"] = "kernel"
    kernel_build_config: KernelBuildConfig


class KernelPlugin(plugins.Plugin):
    """Plugin class implementing kernel build functionality."""

    properties_class = KernelPluginProperties

    def __init__(
        self, *, properties: plugins.PluginProperties, part_info: infos.PartInfo
    ) -> None:
        super().__init__(properties=properties, part_info=part_info)
        self.options = cast(KernelPluginProperties, self._options)

    def _setup_cross_build_environment(self) -> None:
        """Set up the cross-build repository for the target architecture."""
        target_arch = self._part_info.target_arch
        if target_arch not in KERNEL_ARCH_FROM_SNAP_ARCH:
            raise errors.PartsError(
                "unsupported architecture",
                f"Kernel build is not supported for architecture: {target_arch}",
            )
        series = UBUNTU_RELEASE_FROM_SNAP_BASE.get(self._part_info.base)
        if not series:
            raise errors.PartsError(
                "unsupported base",
                f"Kernel build is not supported for base: {self._part_info.base}",
            )
        if os.environ.get("SNAP_NAME") != "snapcraft":
            # If not running inside the build container, we need to skip this part
            return

        subprocess.check_call(["dpkg", "--add-architecture", target_arch])

        repo.install(
            [
                {
                    "url": "https://ports.ubuntu.com/ubuntu-ports/"
                    if target_arch != "amd64"
                    else "https://archive.ubuntu.com/ubuntu/",
                    "suites": [f"{series}", f"{series}-security", f"{series}-updates"],
                    "components": ["main", "universe"],
                    "formats": ["deb"],
                    "architectures": [target_arch],
                    "type": "apt",
                    "key-id": "F6ECB3762474EDA9D21B7022871920D1991BC93C",
                }
            ],
            key_assets=Path("/non-existent-path/keys"),
        )

        subprocess.check_call(["apt-get", "update", "-qq"])

    @override
    def get_build_snaps(self) -> set[str]:
        return set()

    @override
    def get_build_packages(self) -> set[str]:
        base: str = self._part_info.base
        base_number = int(base.removeprefix("core"))
        target_arch = self._part_info.target_arch
        target_arch_triplet = self._part_info.arch_triplet_build_for
        is_cross_build = self._part_info.project_info.is_cross_compiling
        is_ubuntu_tree_build = (
            self.options.kernel_build_config.build_type == "ubuntu-tree"
        )
        is_binary_repack = (
            self.options.kernel_build_config.build_type == "binary-repack"
        )

        gcc_arch = target_arch_triplet.replace("_", "-")
        gcc_package = "gcc" if not is_cross_build else f"gcc-{gcc_arch}"

        if is_cross_build:
            self._setup_cross_build_environment()

        base_packages = {
            "bc",
            "binutils",
            "bison",
            "cmake",
            "cpio",
            "cryptsetup",
            "dkms",
            "fakeroot",
            "flex",
            "gawk",
            gcc_package,
            "kmod",
            "kpartx",
            "openssl",
            "libssl-dev",
            f"libssl-dev:{target_arch}",
            "lz4",
            "rsync",
            "systemd",
            "xz-utils",
            "zstd",
        }

        if is_binary_repack and not self.options.kernel_build_config.extra_modules:
            # if we are just re-packing a binary package, we only need the tools to extract and repack the deb
            return {"dpkg-dev", "fakeroot", "rsync", "kmod"}

        if base_number >= 23:
            base_packages |= {
                "bindgen",
                "clang",
                "rust-src",
                "rustc",
                "rustfmt",
                "llvm",
            }

        if is_ubuntu_tree_build:
            base_packages |= {"debhelper", "dpkg-dev"}

        tools_to_build = set()
        build_config = self.options.kernel_build_config
        if (
            isinstance(build_config, KernelConfigBuildTypeUbuntu)
            and build_config.overrides
        ):
            tools_to_build = build_config.overrides.tools_to_build
        elif (
            isinstance(build_config, (KernelConfigBuildTypeGeneric))
            and build_config.config
        ):
            tools_to_build = build_config.config.tools_to_build

        for tool in tools_to_build:
            deps = KERNEL_TOOLS_BUILD_DEPS[tool]
            for dep in deps:
                base_packages.add(dep.replace(":target", f":{target_arch}"))

        return base_packages

    @override
    def get_pull_commands(self) -> list[str]:
        base_commands = super().get_pull_commands()
        for (
            module_name,
            module_source,
        ) in self.options.kernel_build_config.extra_modules.items():
            if not module_source:
                # this means we are re-packing a binary package
                base_commands.append(
                    f"apt-get download {escape_shell_word(module_name)}"
                )
                continue
            # create a new fake part for the source handler to pull into the correct location
            handler = get_source_handler(
                cache_dir=self._part_info.part_cache_dir,
                part=cast(
                    "Part",
                    SubPart(
                        name=module_name,
                        source_dir=(
                            self._part_info.part_src_dir / "dkms" / module_name
                        ),
                        source=module_source,
                    ),
                ),
                project_dirs=self._part_info._project_info.dirs,
            )
            if handler:
                logger.info("Pulling kernel module '%s' ...", module_name)
                handler.pull()
            else:
                raise errors.PartsError(
                    "download error",
                    f"failed to find source handler for kernel module: {module_name}",
                )

        base_commands.extend(
            generate_pull_commands(self.options.kernel_build_config, self._part_info)
        )

        return base_commands

    @override
    def get_build_commands(self) -> list[str]:
        return generate_build_commands(
            self.options.kernel_build_config, self._part_info
        )

    @override
    def get_build_environment(self) -> dict[str, str]:
        env = super().get_build_environment() or dict()
        if self.options.kernel_build_config.build_type == "generic-tree":
            env |= {
                "CROSS": f"{self._part_info.project_info.arch_triplet_build_for}-",
                "CROSS_COMPILE": f"{self._part_info.project_info.arch_triplet_build_for}-",
                "ARCH": KERNEL_ARCH_FROM_SNAP_ARCH[self._part_info.target_arch],
            }
        return env
