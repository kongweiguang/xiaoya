"""只校正已支持的 WSL 宿主 CPU 容量，不做通用容器配额解析或修改过载规则。"""

import math
import os
import subprocess
import sys
from pathlib import Path, PurePosixPath


def _validated_cpu_budget(value: str) -> float:
    """部署者的显式预算优先，但非正数或无穷值会使 SDK 容量保护失效。"""
    try:
        budget = float(value)
    except ValueError:
        raise ValueError("NUM_CPUS 必须是正有限数") from None
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError("NUM_CPUS 必须是正有限数")
    return budget


def _cpu_quota_paths(cgroups: str) -> tuple[str, ...]:
    """SDK 计量根组时逐级核验 CPU 限额，systemd 的无限额子组不应误降为两核。"""
    paths: dict[str, str] = {}
    for line in cgroups.splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3:
            return ()
        hierarchy, controllers, path = fields
        if not hierarchy.isdecimal():
            return False
        for controller in controllers.split(","):
            if controller not in {"cpu", "cpuacct"}:
                continue
            if hierarchy == "0" or controller in paths:
                return ()
            paths[controller] = path
    if paths.get("cpuacct") != "/" or "cpu" not in paths:
        return ()
    path = PurePosixPath(paths["cpu"])
    if not path.is_absolute() or ".." in path.parts:
        return ()
    ancestors = [PurePosixPath("/"), *reversed(path.parents[:-1]), path]
    return tuple(
        dict.fromkeys(
            "/sys/fs/cgroup/cpu" + ("" if str(parent) == "/" else str(parent))
            for parent in ancestors
        )
    )


def _is_supported_wsl_host() -> bool:
    """只信任已支持的 WSL 识别结果，容器命名空间的根组可能隐藏宿主的上级配额。"""
    try:
        result = subprocess.run(
            ["systemd-detect-virt"], capture_output=True, text=True, timeout=2, check=False
        )
        return result.returncode == 0 and result.stdout.strip() == "wsl"
    except Exception:
        # 探测不是启动硬依赖；任何未知或执行失败都保留 SDK 的原有保守容量。
        return False


def configure_worker_cpu_budget() -> float | None:
    """仅为 WSL 宿主的无限额 v1 层级纠正两核回退，不推断通用容器容量。

    使用 SDK 公开的 NUM_CPUS 配置，不访问私有实现。预热池必须由装配根单独
    限定，避免真实核数扩大后同时加载等量模型；本函数不改变线程数或负载阈值。
    """
    if "NUM_CPUS" in os.environ:
        return _validated_cpu_budget(os.environ["NUM_CPUS"])
    if sys.platform != "linux" or not _is_supported_wsl_host():
        return None
    try:
        try:
            Path("/sys/fs/cgroup/cpu.stat").stat()
        except FileNotFoundError:
            pass
        else:
            return None
        cgroups = Path("/proc/self/cgroup").read_text(encoding="utf-8")
        quota_paths = _cpu_quota_paths(cgroups)
        if not quota_paths:
            return None
        for quota_path in quota_paths:
            quota = int(Path(f"{quota_path}/cpu.cfs_quota_us").read_text(encoding="ascii"))
            period = int(Path(f"{quota_path}/cpu.cfs_period_us").read_text(encoding="ascii"))
            if quota != -1 or period <= 0:
                return None
        logical_cpus = os.cpu_count()
        affinity_cpus = len(os.sched_getaffinity(0))
        if logical_cpus is None or logical_cpus <= 0 or affinity_cpus <= 0:
            return None
        budget = min(logical_cpus, affinity_cpus)
    except (OSError, ValueError, AttributeError):
        return None
    # 启动探测期间若已有调用方设置预算，也保持该显式值而不覆盖。
    return _validated_cpu_budget(os.environ.setdefault("NUM_CPUS", str(budget)))
