import shlex

from craft_parts import PartInfo
from jinja2 import Environment, PackageLoader

from .config import ConfigOptions, KernelBuildConfig

DEFAULT_KERNEL_IMAGE_TARGETS = {
    "amd64": ["bzImage", "modules"],
    "i386": ["bzImage", "modules"],
    "armhf": ["zImage", "modules", "dtbs"],
    "arm64": ["Image.gz", "modules", "dtbs"],
    "powerpc": ["uImage", "modules"],
    "ppc64el": ["vmlinux.strip", "modules"],
    "s390x": ["bzImage", "modules"],
    "riscv64": ["Image", "modules", "dtbs"],
}
UBUNTU_RELEASE_FROM_SNAP_BASE = {
    "core20": "focal",
    "core22": "jammy",
    "core24": "noble",
    "core26": "resolute",
}


def escape_shell_word(word: str) -> str:
    """Escape a word for safe use in shell commands."""
    return shlex.quote(word)


def to_bash_array(arr: list[str] | set[str] | dict[str, str]) -> str:
    """Escape a list of strings into a bash array literal."""
    # Jinja2 does not support type checking, so we need to validate the input at runtime
    if isinstance(arr, (list, set)):
        escaped_elements = [escape_shell_word(elem) for elem in arr]
        return f"({' '.join(escaped_elements)})"
    if isinstance(arr, dict):
        escaped_items = [
            f"[{escape_shell_word(k)}]={escape_shell_word(v)}" for k, v in arr.items()
        ]
        return f"({' '.join(escaped_items)})"
    raise ValueError(f"Input must be a list of strings or a dictionary, not {type(arr)}")


def normalize_kernel_config(config: ConfigOptions) -> list[str]:
    normalized_config: list[str] = []
    for key, value in config.items():
        normalized_value: str
        if isinstance(value, bool):
            normalized_value = "y" if value else "n"
        elif isinstance(value, int):
            normalized_value = str(value)
        elif isinstance(value, str):
            if value in ("m", "y", "n"):
                normalized_value = value
            else:
                normalized_value = f'"{value}"'  # wrap string values in quotes
        else:
            raise ValueError(
                f"Unsupported config value type: {type(value)} for key: {key}"
            )
        normalized_config.append(f"{key}={normalized_value}")
    return normalized_config


def serialize_kernel_config(config: ConfigOptions) -> str:
    normalized_config = normalize_kernel_config(config)
    return "\n".join(normalized_config)


def get_default_image_targets_for_arch(arch: str) -> list[str]:
    return DEFAULT_KERNEL_IMAGE_TARGETS.get(arch, ["bzImage", "modules"])


def _get_current_base_release(config: KernelBuildConfig, base_name: str) -> str:
    auto_detected_release = UBUNTU_RELEASE_FROM_SNAP_BASE.get(base_name)
    if config.build_type in {"generic-tree", "binary-repack"}:
        result = config.ubuntu_release_name or auto_detected_release
        if not result:
            raise ValueError(
                f"Unable to determine Ubuntu release name for base {base_name!r}. Please specify ubuntu_release_name in the kernel build config."
            )
        return result
    if config.build_type == "ubuntu-tree":
        if not auto_detected_release:
            raise ValueError(
                f"Unable to determine Ubuntu release name for base {base_name!r}."
            )
        return auto_detected_release
    raise ValueError(
        f"Unsupported build type: {config.build_type!r} for base {base_name!r}"
    )


def _build_jinja2_environment(
    config: KernelBuildConfig, part_info: PartInfo
) -> Environment:
    # create a Jinja2 environment with the directory of this file as the template search path
    loader = PackageLoader("snapcraft", "templates/kernel")
    # Jinja2's autoescape only works for HTML/XML by default, so we disable it since we are generating shell scripts
    env = Environment(loader=loader, autoescape=False)  # noqa: S701
    # add custom filters for escaping shell words and arrays
    env.filters["escape_shell_word"] = escape_shell_word
    env.filters["to_bash_array"] = to_bash_array
    env.filters["serialize_kernel_config"] = serialize_kernel_config
    # export variables for use in the templates
    env.globals["config"] = config
    # add convenience variables for use in the templates
    project_info = part_info.project_info
    env.globals |= {
        "target_arch": project_info.arch_build_for,
        "is_cross_compiling": project_info.is_cross_compiling,
        "kernel_image_targets": config.image_targets
        or get_default_image_targets_for_arch(project_info.arch_build_for)
        if config.build_type != "binary-repack"
        else None,
        "ubuntu_kernel_use_binary_package": config.build_type == "binary-repack",
        "ubuntu_kernel_release_name": _get_current_base_release(
            config, project_info.base
        ),
        "ubuntu_kernel_flavor": config.ubuntu_kernel_flavour
        if config.build_type in {"ubuntu-tree", "binary-repack"}
        else None,
        "craft_part_build_dir": str(part_info.part_build_dir),
        "craft_part_src_dir": str(part_info.part_src_dir),
        "craft_part_install_dir": str(part_info.part_install_dir),
        "craft_part_cache_dir": str(part_info.part_cache_dir),
        "craft_arch_build_on": project_info.arch_build_on,
        "craft_arch_build_for": project_info.arch_build_for,
    }
    return env


def generate_build_commands(
    config: KernelBuildConfig, part_info: PartInfo
) -> list[str]:
    env = _build_jinja2_environment(config, part_info)
    if config.build_type in {"ubuntu-tree", "binary-repack"}:
        return [env.get_template("kernel_ubuntu_build.sh.j2").render()]
    if config.build_type == "generic-tree":
        return [env.get_template("kernel_generic_build.sh.j2").render()]
    raise ValueError(f"Unsupported build type: {config.build_type}")


def generate_pull_commands(config: KernelBuildConfig, part_info: PartInfo) -> list[str]:
    env = _build_jinja2_environment(config, part_info)
    pull_commands = env.get_template("kernel_pull.sh.j2").render()
    if not pull_commands.strip():
        return []
    return [pull_commands]
