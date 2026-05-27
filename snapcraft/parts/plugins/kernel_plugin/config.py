from typing import Annotated, Literal

from craft_parts.plugins import PluginProperties
from craft_parts.sources import SourceModel
from pydantic import Discriminator, Field, StringConstraints
from pydantic.dataclasses import dataclass

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
KernelConfigKey = StringConstraints(pattern=r"^CONFIG_[A-Z0-9_]+$", strict=True)
ConfigOptions = dict[Annotated[str, KernelConfigKey], str | int | bool]

VALID_KERNEL_TOOLS = set(KernelTools.__args__)


@dataclass(slots=True, config=PluginProperties.model_config)
class KernelConfigBuildKconfig:
    defconfig: NonEmptyString | None = None
    override_file: NonEmptyString | None = None
    override_options: ConfigOptions = Field(default_factory=dict)
    tools_to_build: set[KernelTools] = Field(default_factory=VALID_KERNEL_TOOLS.copy)


@dataclass(slots=True, config=PluginProperties.model_config)
class KernelConfigBase:
    extra_modules: dict[NonEmptyString, SourceModel | None] = Field(
        default_factory=dict
    )


@dataclass(slots=True, config=PluginProperties.model_config)
class KernelConfigBuildTypeRepack(KernelConfigBase):
    build_type: Literal["binary-repack"] = "binary-repack"


@dataclass(slots=True, config=PluginProperties.model_config)
class KernelConfigBuildTypeUbuntu(KernelConfigBase):
    ubuntu_kernel_flavour: NonEmptyString = "generic"
    build_type: Literal["ubuntu-tree"] = "ubuntu-tree"
    image_targets: list[NonEmptyString] | None = None
    overrides: KernelConfigBuildKconfig = Field(
        default_factory=KernelConfigBuildKconfig
    )


@dataclass(slots=True, config=PluginProperties.model_config)
class KernelConfigBuildTypeGeneric(KernelConfigBase):
    build_type: Literal["generic-tree"] = "generic-tree"
    ubuntu_release_name: Annotated[str, UbuntuReleaseName] | None = None
    image_targets: list[NonEmptyString] | None = None
    config: KernelConfigBuildKconfig = Field(default_factory=KernelConfigBuildKconfig)


KernelBuildConfig = Annotated[
    KernelConfigBuildTypeRepack
    | KernelConfigBuildTypeGeneric
    | KernelConfigBuildTypeUbuntu,
    Discriminator("build_type"),
]
