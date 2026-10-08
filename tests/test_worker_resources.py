"""容量校正只适用于已支持 WSL 宿主的 v1 根组，离线测试不探测真实宿主。"""

from collections.abc import Iterator
from subprocess import TimeoutExpired
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from xiaoya.infrastructure import worker_resources

QUOTA = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
PERIOD = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"
CGROUPS = "/proc/self/cgroup"
V2_STAT = "/sys/fs/cgroup/cpu.stat"


@pytest.fixture
def unlimited_v1(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, str | Exception]]:
    """模拟 WSL 与内核探测并主动还原预算，产品直接写环境的值不由 monkeypatch 跟踪。"""
    files: dict[str, str | Exception] = {
        QUOTA: "-1\n",
        PERIOD: "100000\n",
        CGROUPS: "3:cpuacct:/\n2:cpu:/\n1:cpuset:/\n0::/\n",
    }

    class VirtualPath:
        """仅实现容量探测所需的读取，缺失与权限失败保持真实文件语义。"""

        def __init__(self, path: str) -> None:
            """保存绝对路径键，测试可单独模拟各控制器的失败。"""
            self.path = path

        def stat(self) -> None:
            """不存在与无法访问必须区分，避免把未知的 v2 文件误认成 v1。"""
            if self.path not in files:
                raise FileNotFoundError(self.path)
            if isinstance(value := files[self.path], Exception):
                raise value

        def read_text(self, *, encoding: str) -> str:
            """让异常在与真实文件相同的读取边界产生，编码由实现显式指定。"""
            self.stat()
            value = files[self.path]
            assert isinstance(value, str)
            return value

    original_budget = worker_resources.os.environ.get("NUM_CPUS")
    try:
        monkeypatch.delenv("NUM_CPUS", raising=False)
        monkeypatch.setattr(worker_resources, "Path", VirtualPath)
        monkeypatch.setattr(worker_resources, "sys", SimpleNamespace(platform="linux"))
        monkeypatch.setattr(
            worker_resources,
            "subprocess",
            SimpleNamespace(run=Mock(return_value=SimpleNamespace(returncode=0, stdout="wsl\n"))),
        )
        monkeypatch.setattr(worker_resources.os, "cpu_count", lambda: 32)
        monkeypatch.setattr(
            worker_resources.os, "sched_getaffinity", lambda _: set(range(32)), raising=False
        )
        yield files
    finally:
        if original_budget is None:
            worker_resources.os.environ.pop("NUM_CPUS", None)
        else:
            worker_resources.os.environ["NUM_CPUS"] = original_budget


@pytest.mark.parametrize("original_budget", [None, "2.5"])
def test_fixture_restores_initial_budget(
    monkeypatch: pytest.MonkeyPatch, original_budget: str | None
) -> None:
    """显式完成 fixture 生命周期，验证自动写入不会污染后续测试且已有预算原样保留。"""
    if original_budget is None:
        monkeypatch.delenv("NUM_CPUS", raising=False)
    else:
        monkeypatch.setenv("NUM_CPUS", original_budget)
    with pytest.MonkeyPatch.context() as fixture_patch:
        fixture_lifecycle = unlimited_v1.__wrapped__(fixture_patch)
        next(fixture_lifecycle)
        try:
            assert worker_resources.configure_worker_cpu_budget() == 32.0
            assert worker_resources.os.environ["NUM_CPUS"] == "32"
        finally:
            fixture_lifecycle.close()
    assert worker_resources.os.environ.get("NUM_CPUS") == original_budget


def test_unlimited_root_v1_uses_proven_cpu_count(unlimited_v1: dict[str, str | Exception]) -> None:
    """已支持 WSL 的 32 核无限额根组应校正预算，宿主识别有界且只在未配置时执行。"""
    assert worker_resources.configure_worker_cpu_budget() == 32.0
    assert worker_resources.os.environ["NUM_CPUS"] == "32"
    assert worker_resources.configure_worker_cpu_budget() == 32.0
    worker_resources.subprocess.run.assert_called_once_with(
        ["systemd-detect-virt"], capture_output=True, text=True, timeout=2, check=False
    )


@pytest.mark.parametrize(
    "virtualization", ["docker", "podman", "lxc", "kvm", "vmware", "vm", "none", "", "wsl-other"]
)
def test_other_virtualization_keeps_sdk_behavior(
    unlimited_v1: dict[str, str | Exception], virtualization: str
) -> None:
    """容器根组可能隐藏上级限制，普通 Linux 和其他虚拟化环境也不扩展本轮修正。"""
    worker_resources.subprocess.run.return_value = SimpleNamespace(
        returncode=0, stdout=f"{virtualization}\n"
    )
    unlimited_v1[QUOTA] = AssertionError("未识别为支持的 WSL 时不应继续探测")
    assert worker_resources.configure_worker_cpu_budget() is None
    assert "NUM_CPUS" not in worker_resources.os.environ


@pytest.mark.parametrize("returncode", [1, 127, -9])
def test_failed_virtualization_probe_keeps_sdk_behavior(
    unlimited_v1: dict[str, str | Exception], returncode: int
) -> None:
    """即使标准输出残留 WSL 字样，失败退出也不能作为自动扩大预算的依据。"""
    worker_resources.subprocess.run.return_value = SimpleNamespace(
        returncode=returncode, stdout="wsl"
    )
    assert worker_resources.configure_worker_cpu_budget() is None
    assert "NUM_CPUS" not in worker_resources.os.environ


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("missing"),
        TimeoutExpired(["systemd-detect-virt"], 2),
        OSError("unavailable"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
        RuntimeError("unexpected"),
    ],
)
def test_virtualization_probe_exceptions_keep_sdk_behavior(
    unlimited_v1: dict[str, str | Exception], error: Exception
) -> None:
    """工具缺失、超时及异常均保持保守启动，不安装工具或把探测变成可用性依赖。"""
    worker_resources.subprocess.run.side_effect = error
    assert worker_resources.configure_worker_cpu_budget() is None
    assert "NUM_CPUS" not in worker_resources.os.environ


@pytest.mark.parametrize("logical,affinity,expected", [(32, 8, 8.0), (4, 32, 4.0), (1, 1, 1.0)])
def test_budget_respects_smaller_available_cpu_count(
    unlimited_v1: dict[str, str | Exception],
    monkeypatch: pytest.MonkeyPatch,
    logical: int,
    affinity: int,
    expected: float,
) -> None:
    """亲和性或系统核数的任一限制都不能被另一项较大的容量覆盖。"""
    monkeypatch.setattr(worker_resources.os, "cpu_count", lambda: logical)
    monkeypatch.setattr(worker_resources.os, "sched_getaffinity", lambda _: set(range(affinity)))
    assert worker_resources.configure_worker_cpu_budget() == expected


@pytest.mark.parametrize("quota", ["200000", "50000", "0", "-2", "invalid"])
def test_non_unlimited_quota_keeps_sdk_behavior(
    unlimited_v1: dict[str, str | Exception], quota: str
) -> None:
    """有限配额和无法解释的值不属于本轮修正，不能以宿主核数放大它们。"""
    unlimited_v1[QUOTA] = quota
    assert worker_resources.configure_worker_cpu_budget() is None
    assert "NUM_CPUS" not in worker_resources.os.environ


@pytest.mark.parametrize("period", ["0", "-1", "invalid"])
def test_invalid_period_keeps_sdk_behavior(
    unlimited_v1: dict[str, str | Exception], period: str
) -> None:
    """无限额标记也必须有合法周期作证，部分损坏的挂载应保守处理。"""
    unlimited_v1[PERIOD] = period
    assert worker_resources.configure_worker_cpu_budget() is None
    assert "NUM_CPUS" not in worker_resources.os.environ


def test_v2_is_not_overridden(unlimited_v1: dict[str, str | Exception]) -> None:
    """混合挂载下 SDK 优先选择 v2，本次校正不得抢占它的容量推断。"""
    unlimited_v1[V2_STAT] = "usage_usec 1\n"
    assert worker_resources.configure_worker_cpu_budget() is None
    assert "NUM_CPUS" not in worker_resources.os.environ


@pytest.mark.parametrize(
    "cgroups",
    [
        "3:cpuacct:/\n2:cpu:/agent\n",
        "3:cpuacct:/agent\n2:cpu:/\n",
        "3:cpuacct:/\n",
        "2:cpu:/\n",
        "0::/\n",
        "2:cpu:/\n3:cpuacct:/\n4:cpu:/\n",
        "0:cpu,cpuacct:/\n",
        "bad:cpu,cpuacct:/\n",
        "malformed\n",
    ],
)
def test_non_root_or_ambiguous_membership_keeps_sdk_behavior(
    unlimited_v1: dict[str, str | Exception], cgroups: str
) -> None:
    """只有两个控制器均明确属于根组才安全，容器子组与残缺证据都不扩容。"""
    unlimited_v1[CGROUPS] = cgroups
    assert worker_resources.configure_worker_cpu_budget() is None
    assert "NUM_CPUS" not in worker_resources.os.environ


def test_combined_root_controllers_are_supported(unlimited_v1: dict[str, str | Exception]) -> None:
    """内核允许合并控制器挂载，分行格式不应成为人为限制。"""
    unlimited_v1[CGROUPS] = "2:cpu,cpuacct:/\n0::/\n"
    assert worker_resources.configure_worker_cpu_budget() == 32.0


@pytest.mark.parametrize("path", [QUOTA, PERIOD, CGROUPS, V2_STAT])
@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("unavailable")])
def test_probe_errors_keep_sdk_behavior(
    unlimited_v1: dict[str, str | Exception], path: str, error: Exception
) -> None:
    """无法读取的权限和内核错误不应扩大预算，也不阻止保守模式启动。"""
    unlimited_v1[path] = error
    assert worker_resources.configure_worker_cpu_budget() is None
    assert "NUM_CPUS" not in worker_resources.os.environ


@pytest.mark.parametrize("path", [QUOTA, PERIOD, CGROUPS])
def test_missing_probe_files_keep_sdk_behavior(
    unlimited_v1: dict[str, str | Exception], path: str
) -> None:
    """v1 必需证据缺失时不能只凭操作系统核数补齐未知限制。"""
    del unlimited_v1[path]
    assert worker_resources.configure_worker_cpu_budget() is None
    assert "NUM_CPUS" not in worker_resources.os.environ


@pytest.mark.parametrize("logical,affinity", [(None, 32), (0, 32), (32, 0)])
def test_unknown_cpu_count_keeps_sdk_behavior(
    unlimited_v1: dict[str, str | Exception],
    monkeypatch: pytest.MonkeyPatch,
    logical: int | None,
    affinity: int,
) -> None:
    """容量与亲和性均应可探明，零或未知值不能作为可靠的自动配置。"""
    monkeypatch.setattr(worker_resources.os, "cpu_count", lambda: logical)
    monkeypatch.setattr(worker_resources.os, "sched_getaffinity", lambda _: set(range(affinity)))
    assert worker_resources.configure_worker_cpu_budget() is None
    assert "NUM_CPUS" not in worker_resources.os.environ


def test_missing_affinity_api_keeps_sdk_behavior(
    unlimited_v1: dict[str, str | Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    """运行环境没有亲和性接口时保持保守，不假设逻辑核全部可用。"""
    monkeypatch.delattr(worker_resources.os, "sched_getaffinity")
    assert worker_resources.configure_worker_cpu_budget() is None
    assert "NUM_CPUS" not in worker_resources.os.environ


def test_affinity_failure_keeps_sdk_behavior(
    unlimited_v1: dict[str, str | Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    """内核拒绝亲和性查询时也不扩大预算，避免主机逻辑核数掩盖未知限制。"""

    def unavailable_affinity(process_id: int) -> set[int]:
        """模拟系统调用失败，不启动真实进程或改变测试进程亲和性。"""
        raise OSError("affinity unavailable")

    monkeypatch.setattr(worker_resources.os, "sched_getaffinity", unavailable_affinity)
    assert worker_resources.configure_worker_cpu_budget() is None
    assert "NUM_CPUS" not in worker_resources.os.environ


def test_unreadable_membership_keeps_sdk_behavior(unlimited_v1: dict[str, str | Exception]) -> None:
    """异常编码不能提供可信的根组归属证据，继续使用 SDK 原有保守逻辑。"""
    unlimited_v1[CGROUPS] = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
    assert worker_resources.configure_worker_cpu_budget() is None
    assert "NUM_CPUS" not in worker_resources.os.environ


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_other_platforms_keep_sdk_behavior(
    unlimited_v1: dict[str, str | Exception], monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """其他平台不执行 Linux 宿主识别或访问控制器，也不改变原有配置。"""
    monkeypatch.setattr(worker_resources, "sys", SimpleNamespace(platform=platform))
    unlimited_v1[QUOTA] = AssertionError("不应读取 Linux 内核文件")
    assert worker_resources.configure_worker_cpu_budget() is None
    assert "NUM_CPUS" not in worker_resources.os.environ
    worker_resources.subprocess.run.assert_not_called()


@pytest.mark.parametrize("value,expected", [("32", 32.0), ("0.5", 0.5), (" 2.5 ", 2.5)])
def test_explicit_budget_is_validated_without_rewriting(
    unlimited_v1: dict[str, str | Exception],
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    expected: float,
) -> None:
    """显式预算可为分数且跳过宿主识别与内核探测，原始环境值保持不变。"""
    monkeypatch.setenv("NUM_CPUS", value)
    unlimited_v1[V2_STAT] = AssertionError("显式配置不应触发探测")
    assert worker_resources.configure_worker_cpu_budget() == expected
    assert worker_resources.os.environ["NUM_CPUS"] == value
    worker_resources.subprocess.run.assert_not_called()


def test_explicit_budget_applies_on_windows(
    unlimited_v1: dict[str, str | Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    """平台仅限制自动探测，部署者显式提供的合法 SDK 配置在 Windows 也保留。"""
    monkeypatch.setattr(worker_resources, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setenv("NUM_CPUS", "1.5")
    assert worker_resources.configure_worker_cpu_budget() == 1.5
    assert worker_resources.os.environ["NUM_CPUS"] == "1.5"


def test_explicit_budget_set_during_probe_is_not_overwritten(
    unlimited_v1: dict[str, str | Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    """探测期间其他启动配置写入预算时仍尊重显式值，不能用较大的自动值覆盖。"""

    def affinity_after_configuration(process_id: int) -> set[int]:
        """在最后的探测步骤注入配置竞争，无需实际线程也能验证提交边界。"""
        monkeypatch.setenv("NUM_CPUS", "3")
        return set(range(32))

    monkeypatch.setattr(worker_resources.os, "sched_getaffinity", affinity_after_configuration)
    assert worker_resources.configure_worker_cpu_budget() == 3.0
    assert worker_resources.os.environ["NUM_CPUS"] == "3"


@pytest.mark.parametrize("value", ["", " ", "0", "-1", "nan", "inf", "-inf", "1e999", "bad"])
def test_invalid_explicit_budget_fails_without_rewriting(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """显式错误直接拒绝且不探测宿主，避免零除、NaN 或无限容量破坏过载保护。"""
    monkeypatch.setenv("NUM_CPUS", value)
    virtualization_probe = Mock()
    monkeypatch.setattr(worker_resources, "subprocess", SimpleNamespace(run=virtualization_probe))
    with pytest.raises(ValueError, match="NUM_CPUS 必须是正有限数"):
        worker_resources.configure_worker_cpu_budget()
    assert worker_resources.os.environ["NUM_CPUS"] == value
    virtualization_probe.assert_not_called()
