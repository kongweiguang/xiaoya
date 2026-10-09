"""部署的删除边界和验收清理也必须离线验证，不能只检查脚本文本。"""

import asyncio
import importlib.util
import io
import json
import os
import shutil
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["xiaoya-speech", "xiaoya-agent"])
def test_units_allow_parent_lifecycle_before_group_cleanup(name):
    """父进程先收尾 GPU 或 Job 子进程，仍用有限停止预算和整组强杀兜底，不能放任残留。"""
    from configparser import ConfigParser

    # Environment 可重复，按 unit 语法读取停止策略而不把合法配置误判为重复键错误。
    unit = ConfigParser(interpolation=None, strict=False)
    unit.read(ROOT / "deployment" / "wsl" / f"{name}.service", encoding="utf-8")
    service = unit["Service"]
    assert service["KillMode"] == "mixed"
    assert service["TimeoutStopSec"] == "120"
    assert service.get("KillSignal", "SIGTERM") == "SIGTERM"
    assert service.get("SendSIGKILL", "yes") == "yes"


def load_deployment(name: str):
    """直接加载独立运维模块，使测试无需将部署脚本加入 Agent 运行包。"""
    specification = importlib.util.spec_from_file_location(name, ROOT / "deployment" / f"{name}.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_sync_prunes_only_managed_source(tmp_path):
    """源码删除应传播，但同级密钥、运行环境和模型不属于该生命周期。"""
    sync = load_deployment("sync_runtime")
    source = tmp_path / "source"
    target = tmp_path / "runtime" / "agent" / "src" / "xiaoya"
    source.mkdir()
    target.mkdir(parents=True)
    (source / "current.py").write_text("current", encoding="utf-8")
    (target / "retired.py").write_text("old", encoding="utf-8")
    secret = tmp_path / "runtime" / "agent" / ".env.local"
    secret.write_text("keep", encoding="utf-8")
    result = sync.sync_directory(source, target)
    assert set(result) == {"current.py"}
    assert (target / "current.py").read_text(encoding="utf-8") == "current"
    assert not (target / "retired.py").exists()
    assert secret.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("relative", ["../outside", "agent/../../../outside", "."])
def test_sync_rejects_target_escape(tmp_path, relative):
    """删除目标在构造时就必须留在指定子目录，不能依靠操作后的检查。"""
    with pytest.raises(ValueError):
        load_deployment("sync_runtime").checked_target(tmp_path, relative)


@pytest.mark.parametrize("change", ["file-to-directory", "directory-to-file", "root-file"])
def test_sync_accepts_managed_node_type_changes(tmp_path, change):
    """同一源码名称的类型变化须一次同步完成，删除仍仅限这个已验证的受管目录。"""
    sync = load_deployment("sync_runtime")
    source, target = tmp_path / "source", tmp_path / "runtime" / "src"
    source.mkdir()
    target.parent.mkdir()
    if change == "root-file":
        target.write_text("retired root", encoding="utf-8")
        (source / "main.py").write_text("new", encoding="utf-8")
    elif change == "file-to-directory":
        target.mkdir()
        (target / "module").write_text("old", encoding="utf-8")
        (source / "module").mkdir()
        (source / "module" / "main.py").write_text("new", encoding="utf-8")
    else:
        (target / "module").mkdir(parents=True)
        (target / "module" / "old.py").write_text("old", encoding="utf-8")
        (source / "module").write_text("new", encoding="utf-8")
    result = sync.sync_directory(source, target)
    assert len(result) == 1
    current = target / next(iter(result))
    assert current.read_text(encoding="utf-8") == "new"


@pytest.fixture
def managed_runtime(tmp_path, monkeypatch):
    """小型清单保留生产预检流程，使失败测试不依赖仓库模型、私有配置或现有运行目录。"""
    sync = load_deployment("sync_runtime")
    monkeypatch.setattr(sync, "DIRECTORIES", (("first", "agent/src"), ("second", "speech/src")))
    monkeypatch.setattr(sync, "FILES", (("manifest.toml", "agent/manifest.toml"),))
    repository, runtime = tmp_path / "repository", tmp_path / "runtime"
    for name in ("first", "second"):
        (repository / name).mkdir(parents=True)
        (repository / name / "new.py").write_text("new", encoding="utf-8")
    (repository / "manifest.toml").write_text("manifest", encoding="utf-8")
    (runtime / "agent/src").mkdir(parents=True)
    (runtime / "agent/src/old.py").write_text("old", encoding="utf-8")
    return sync, repository, runtime


@pytest.mark.parametrize(
    "failure",
    ["missing-directory", "source-file-is-directory", "target-file-is-directory", "parent-file"],
)
def test_sync_global_preflight_prevents_partial_writes(managed_runtime, failure):
    """后一个输入或目标失败时，前一个受管目录也不能先复制或删除。"""
    sync, repository, runtime = managed_runtime
    if failure == "missing-directory":
        shutil.rmtree(repository / "second")
    elif failure == "source-file-is-directory":
        (repository / "manifest.toml").unlink()
        (repository / "manifest.toml").mkdir()
    elif failure == "target-file-is-directory":
        (runtime / "agent/manifest.toml").mkdir()
    else:
        (runtime / "speech").write_text("must remain", encoding="utf-8")
    with pytest.raises(ValueError):
        sync.sync_runtime(repository, runtime)
    assert (runtime / "agent/src/old.py").read_text(encoding="utf-8") == "old"
    assert not (runtime / "agent/src/new.py").exists()


@pytest.mark.parametrize("location", ["source-parent", "later-target", "venv", "vendor"])
@pytest.mark.parametrize("link_kind", ["is_symlink", "is_junction"])
def test_sync_rejects_links_before_any_mutation(managed_runtime, monkeypatch, location, link_kind):
    """链接检测涵盖输入父目录、后续目标与 uv 运行环境，Windows junction 同样拒绝。"""
    sync, repository, runtime = managed_runtime
    blocked = {
        "source-parent": repository,
        "later-target": runtime / "speech/src",
        "venv": runtime / "agent/.venv",
        "vendor": runtime / "vendor/CosyVoice",
    }[location]
    original = getattr(Path, link_kind)

    def linked(path):
        """模拟链接元数据而不要求开发机启用 Windows 符号链接权限，其他路径保持真实判断。"""
        return path == blocked or original(path)

    monkeypatch.setattr(Path, link_kind, linked)
    with pytest.raises(ValueError):
        sync.sync_runtime(repository, runtime)
    assert (runtime / "agent/src/old.py").exists()
    assert not (runtime / "agent/src/new.py").exists()


def test_check_only_does_not_create_or_change_runtime(managed_runtime):
    """完整部署在 vendor 和环境写入前复用同一清单预检，检查不能顺带清理旧文件。"""
    sync, repository, runtime = managed_runtime
    assert sync.sync_runtime(repository, runtime, check_only=True) == {}
    assert (runtime / "agent/src/old.py").exists()
    assert not (runtime / "speech").exists()
    assert not (runtime / "agent/manifest.toml").exists()


@pytest.mark.asyncio
async def test_evidence_failure_still_attempts_every_cleanup():
    """报告与任一资源关闭失败时，后续媒体和控制客户端仍必须释放。"""
    module = load_deployment("verification_room")
    calls = []

    async def close(name, failure=False):
        """用失败资源暴露实际退出顺序，避免只验证上下文管理器被调用。"""
        calls.append(name)
        if failure:
            raise RuntimeError(name)

    async def delete(_request):
        """只记录本次创建房间的删除，测试不连接任何外部服务。"""
        await close("delete")

    room = SimpleNamespace(disconnect=lambda: close("room", True))
    source = SimpleNamespace(aclose=lambda: close("source"))
    control = SimpleNamespace(
        aclose=lambda: close("control"), room=SimpleNamespace(delete_room=delete)
    )
    with pytest.raises(RuntimeError, match="room"):
        async with module.verification_cleanup(
            room=room,
            source=source,
            control=control,
            room_name="owned-test",
            created=True,
            tasks=(),
        ):
            raise OSError("report")
    assert calls == ["room", "source", "delete", "control"]


@pytest.mark.parametrize("created", [False, True])
@pytest.mark.parametrize("failure", ["remote", "room", "source", "delete", "control"])
async def test_every_resource_is_attempted_when_one_close_fails(created, failure):
    """远程会话和任一关闭失败都不跳过其他资源，非本次创建房间绝不删除。"""
    module = load_deployment("verification_room")
    calls = []

    async def close(name):
        """单一故障隔离验证每个独立清理者，不能仅靠最后一个 callback 被调用判定成功。"""
        calls.append(name)
        if name == failure:
            raise RuntimeError(name)

    async def delete(request):
        """实际协议请求必须只指定此次生成的房间，不允许宽泛房间列表删除。"""
        assert request.room == "owned-test"
        await close("delete")

    room = SimpleNamespace(disconnect=lambda: close("room"))
    source = SimpleNamespace(aclose=lambda: close("source"))
    control = SimpleNamespace(
        aclose=lambda: close("control"), room=SimpleNamespace(delete_room=delete)
    )
    remote = SimpleNamespace(aclose=lambda: close("remote"))
    expected = ["remote", "room", "source", *(["delete"] if created else []), "control"]
    if failure == "delete" and not created:
        async with module.verification_cleanup(
            room=room,
            source=source,
            control=control,
            room_name="owned-test",
            created=created,
            tasks=(),
            remote=remote,
        ):
            pass
    else:
        with pytest.raises(RuntimeError, match=failure):
            async with module.verification_cleanup(
                room=room,
                source=source,
                control=control,
                room_name="owned-test",
                created=created,
                tasks=(),
                remote=remote,
            ):
                pass
    assert calls == expected


async def test_repeated_cancellation_cannot_interrupt_verification_cleanup():
    """用户打断验收或服务器再次取消时，当前资源关闭必须完成并继续释放后续资源。"""
    module = load_deployment("verification_room")
    closing, finish = asyncio.Event(), asyncio.Event()
    calls = []

    async def close(name):
        """第一项关闭暂停以模拟实际网络退出，重复取消不得传进独立清理任务。"""
        calls.append(name)
        if name == "remote":
            closing.set()
            await finish.wait()

    async def verify():
        """上下文按生产用法注册所有退出动作，不复制 AsyncExitStack 的实现。"""
        async with module.verification_cleanup(
            room=SimpleNamespace(disconnect=lambda: close("room")),
            source=SimpleNamespace(aclose=lambda: close("source")),
            control=SimpleNamespace(aclose=lambda: close("control")),
            room_name="owned-test",
            created=False,
            tasks=(),
            remote=SimpleNamespace(aclose=lambda: close("remote")),
        ):
            pass

    request = asyncio.create_task(verify())
    await asyncio.wait_for(closing.wait(), 2)
    request.cancel()
    await asyncio.sleep(0)
    request.cancel()
    assert not request.done() and calls == ["remote"]
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(request, 2)
    assert calls == ["remote", "room", "source", "control"]


@pytest.mark.parametrize("failure", ["source", "control"])
async def test_resource_construction_failure_closes_prior_objects(monkeypatch, failure):
    """验收尚未连房时也可能分配 FFI 资源，后一项构造失败不能留下前项。"""
    module = load_deployment("verification_room")
    room = SimpleNamespace(disconnect=AsyncMock())
    source = SimpleNamespace(aclose=AsyncMock())
    room_factory = Mock(return_value=room)
    source_factory = Mock(return_value=source)
    control_factory = Mock()
    if failure == "source":
        source_factory.side_effect = RuntimeError("source")
    else:
        control_factory.side_effect = RuntimeError("control")
    monkeypatch.setattr(module.rtc, "Room", room_factory)
    monkeypatch.setattr(module.rtc, "AudioSource", source_factory)
    monkeypatch.setattr(module.api, "LiveKitAPI", control_factory)
    with pytest.raises(RuntimeError, match=failure):
        await module.create_verification_resources("ws://private", "test-key", "test-secret")
    room.disconnect.assert_awaited_once()
    if failure == "source":
        control_factory.assert_not_called()
        source.aclose.assert_not_called()
    else:
        source.aclose.assert_awaited_once()


def verification_wav(*, rate=24000, channels=1, width=2, samples=480):
    """内存构造固定合成容器，协议回归不读取真人录音，也不依赖实际 TTS 或 RTC 客户端。"""
    content = io.BytesIO()
    with wave.open(content, "wb") as wav:
        wav.setframerate(rate)
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.writeframes(b"\x01" * samples * channels * width)
    return content.getvalue()


@pytest.mark.parametrize("rate", [24000, 48000])
async def test_verification_publishes_complete_frames_with_vad_silence(monkeypatch, rate):
    """两个实际房间脚本共用完整 20 毫秒帧，尾部一秒静音与补齐保持本地 VAD 收尾约束。"""
    module = load_deployment("verification_room")
    frame_factory = Mock(side_effect=lambda **values: SimpleNamespace(**values))
    monkeypatch.setattr(module.rtc, "AudioFrame", frame_factory)
    source = SimpleNamespace(capture_frame=AsyncMock(), wait_for_playout=AsyncMock())
    count = await module.publish_wav(source, verification_wav(rate=rate))
    frames = [call.args[0] for call in source.capture_frame.await_args_list]
    assert count == round(480 * 48000 / rate)
    assert len(frames) == (count + 48000 + 959) // 960
    assert all(
        frame.sample_rate == 48000
        and frame.num_channels == 1
        and frame.samples_per_channel == 960
        and len(frame.data) == 1920
        for frame in frames
    )
    assert frames[0].data[: count * 2] == b"\x01\x01" * min(count, 960)
    assert frames[-1].data == bytes(1920)
    source.wait_for_playout.assert_awaited_once()


@pytest.mark.parametrize("invalid", [{"channels": 2}, {"width": 1}, {"samples": 0}])
async def test_verification_rejects_invalid_wav_before_publishing(invalid):
    """格式与空内容在传输前显式失败，即使 Python 优化模式关闭 assert 也不会发出假成功。"""
    module = load_deployment("verification_room")
    source = SimpleNamespace(capture_frame=AsyncMock(), wait_for_playout=AsyncMock())
    with pytest.raises(ValueError, match="验收合成音频"):
        await module.publish_wav(source, verification_wav(**invalid))
    source.capture_frame.assert_not_called()
    source.wait_for_playout.assert_not_called()


async def test_verification_upstream_cancel_does_not_send_remaining_audio(monkeypatch):
    """关闭房间时传输取消必须原样传播，不发送剩余合成音频或等待虚假的播放完成。"""
    module = load_deployment("verification_room")
    monkeypatch.setattr(module.rtc, "AudioFrame", lambda **values: SimpleNamespace(**values))
    source = SimpleNamespace(
        capture_frame=AsyncMock(side_effect=asyncio.CancelledError), wait_for_playout=AsyncMock()
    )
    with pytest.raises(asyncio.CancelledError):
        await module.publish_wav(source, verification_wav())
    source.capture_frame.assert_awaited_once()
    source.wait_for_playout.assert_not_called()


@pytest.mark.parametrize("outcome", ["ready", "later", "timeout", "cancelled"])
async def test_verification_wait_uses_phase_budget_and_preserves_cancel(monkeypatch, outcome):
    """阶段检查共享单调时钟与 60 秒默认预算，立即成功不等待，超时和取消不能假装通过。"""
    module = load_deployment("verification_room")
    predicate = Mock(side_effect=[True] if outcome == "ready" else [False, True])
    clock = Mock(side_effect=[0, 0])
    sleep = AsyncMock()
    if outcome == "timeout":
        predicate = Mock(return_value=False)
        clock = Mock(side_effect=[0, 0, 60])
    if outcome == "cancelled":
        sleep.side_effect = asyncio.CancelledError
    monkeypatch.setattr(module, "monotonic", clock)
    monkeypatch.setattr(module, "asyncio", SimpleNamespace(sleep=sleep))
    if outcome == "timeout":
        with pytest.raises(TimeoutError, match="房间验收阶段超时"):
            await module.wait_until(predicate)
    elif outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await module.wait_until(predicate)
    else:
        await module.wait_until(predicate)
    if outcome == "ready":
        sleep.assert_not_called()
    else:
        sleep.assert_awaited_once_with(0.05)


@pytest.mark.parametrize("failure", ["none", "type", "start", "active"])
def test_wsl_launcher_uses_systemd_registration_gate(tmp_path, failure):
    """运行实际启动器，旧 simple 单元及启动失败均不能宣称 Agent 已注册。"""
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("未安装 PowerShell 7")
    script = tmp_path / "start-wsl.ps1"
    shutil.copy2(ROOT / "deployment/start-wsl.ps1", script)
    command = r"""
$global:calls = [Collections.Generic.List[string]]::new()
function wsl {
    # 只模拟 systemctl 的退出码和单元类型，不接触实际 WSL 或服务。
    $operation = if ($args -contains 'show') { 'show' }
        elseif ($args -contains 'start') { 'start' }
        elseif ($args -contains 'is-active') { 'active' } else { 'logs' }
    $global:calls.Add($operation)
    $global:LASTEXITCODE = if ($env:XIAOYA_TEST_FAILURE -eq $operation) { 1 } else { 0 }
    if ($operation -eq 'show') {
        if ($env:XIAOYA_TEST_FAILURE -eq 'type') { 'simple' } else { 'notify' }
    }
}
$succeeded = $false
try { & $env:XIAOYA_TEST_SCRIPT; $succeeded = $true } catch { }
[PSCustomObject]@{ succeeded = $succeeded; calls = @($global:calls) } | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", command],
        env=os.environ | {"XIAOYA_TEST_SCRIPT": str(script), "XIAOYA_TEST_FAILURE": failure},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    expected = {
        "none": ["show", "start", "active", "logs"],
        "type": ["show"],
        "start": ["show", "start"],
        "active": ["show", "start", "active"],
    }
    assert report["calls"] == expected[failure]
    assert report["succeeded"] is (failure == "none")


POWERSHELL_WEB_STUBS = r"""
$global:deploymentCalls = [Collections.Generic.List[string]]::new()
$global:stoppedIds = [Collections.Generic.List[int]]::new()
$global:listenerReads = 0
$global:startedAt = [DateTime]::UtcNow
$global:scenario = $env:XIAOYA_TEST_SCENARIO
$env:LIVEKIT_URL = 'before-test'
$env:NEXT_DIST_DIR = 'before-dist-test'
function wsl {
    # 不接触实际 WSL，只返回启动器所需的 eth0 协议输出。
    $global:LASTEXITCODE = 0
    '2: eth0 inet 172.16.0.2/20 scope global eth0'
}
function pnpm {
    # 隔离实际构建，检查启动器仍调用唯一 SDK 构建入口。
    $global:deploymentCalls.Add('sdk')
    $global:LASTEXITCODE = 0
}
function Get-NetTCPConnection {
    # 首次端口为空，随后由指定 PID 占用，模拟预检之后的端口竞争。
    param($LocalPort, $State, $LocalAddress, $ErrorAction)
    $global:listenerReads++
    if ($global:listenerReads -eq 1) { return }
    $owner = if ($global:scenario -in @('foreign', 'reused-child', 'reused-root') -or
        ($global:scenario -eq 'ownership-changed' -and $global:listenerReads -ge 3)) {
        9000
    } else { 4101 }
    [PSCustomObject]@{ OwningProcess = $owner }
}
function Start-Process {
    # 真正执行脚本的流程，仅替换副作用；Next CLI 与监听子进程具有不同 PID。
    param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle, [switch]$PassThru,
        $RedirectStandardOutput, $RedirectStandardError)
    if ($WindowStyle -ne 'Hidden') { throw '启动窗口不是隐藏模式' }
    if (-not $ArgumentList[0].StartsWith('"') -or -not $ArgumentList[0].EndsWith('"')) {
        throw '带空格路径未单独引用'
    }
    $global:deploymentCalls.Add('start')
    $global:childUrl = $env:LIVEKIT_URL
    $global:childDistDir = $env:NEXT_DIST_DIR
    $global:stdoutPath = $RedirectStandardOutput
    $global:stderrPath = $RedirectStandardError
    $global:webMock = [PSCustomObject]@{
        Id = 4100; StartTime = $global:startedAt; HasExited = $false
    }
    $global:webMock | Add-Member ScriptMethod Refresh {
        if ($global:scenario -eq 'exited') { $this.HasExited = $true }
    }
    $global:webMock
}
function Get-CimInstance {
    # 此刻仍存在的进程树包含一个不相关进程，失败路径绝不能关闭它。
    param($ClassName, $Property)
    if (-not $global:webMock.HasExited) {
        $rootCreation = if ($global:scenario -eq 'reused-root') {
            $global:startedAt.AddSeconds(20)
        } else { $global:startedAt }
        [PSCustomObject]@{
            ProcessId = 4100; ParentProcessId = 12; CreationDate = $rootCreation
        }
    }
    [PSCustomObject]@{
        ProcessId = 4101; ParentProcessId = 4100; CreationDate = $global:startedAt.AddSeconds(1)
    }
    [PSCustomObject]@{
        ProcessId = 9000; ParentProcessId = 13; CreationDate = $global:startedAt.AddSeconds(-10)
    }
}
function Invoke-WebRequest {
    # HTTP 成功本身不证明归属，真实脚本还必须复核监听进程。
    param($Uri, $TimeoutSec, [switch]$SkipHttpErrorCheck)
    $global:deploymentCalls.Add('http')
    [PSCustomObject]@{ StatusCode = 200 }
}
function Get-Process {
    # 用创建时间复核关闭对象，防止仅凭一个 PID 误关别的进程。
    param($Id, $ErrorAction)
    $creation = if ($Id -eq 4100) { $global:startedAt } else { $global:startedAt.AddSeconds(1) }
    if ($global:scenario -eq 'reused-child' -and $Id -eq 4101) {
        $creation = $global:startedAt.AddSeconds(20)
    }
    [PSCustomObject]@{ Id = $Id; StartTime = $creation }
}
function Stop-Process {
    # 只记录被关闭的对象，测试不会终止任何真实进程。
    param($InputObject, $ErrorAction)
    $global:stoppedIds.Add([int]$InputObject.Id)
}
function Start-Sleep {
    # 本组情景必须第一轮决出结果，意外进入等待即说明归属或就绪条件出错。
    param($Milliseconds)
    throw '意外等待'
}
$succeeded = $false
$failure = ''
try {
    & $env:XIAOYA_TEST_SCRIPT -Port ([int]$env:XIAOYA_TEST_PORT)
    $succeeded = $true
} catch { $failure = $_.Exception.Message }
[PSCustomObject]@{
    succeeded = $succeeded; failure = $failure
    calls = @($global:deploymentCalls); stopped = @($global:stoppedIds)
    parentUrl = $env:LIVEKIT_URL; childUrl = $global:childUrl
    parentDistDir = $env:NEXT_DIST_DIR; childDistDir = $global:childDistDir
    stdoutPath = $global:stdoutPath; stderrPath = $global:stderrPath
} | ConvertTo-Json -Compress
"""


@pytest.mark.parametrize(
    "scenario", ["normal", "foreign", "ownership-changed", "exited", "reused-child", "reused-root"]
)
@pytest.mark.parametrize("port", [3000, 3002])
def test_web_launcher_verifies_listener_and_cleans_only_owned_tree(tmp_path, scenario, port):
    """执行真实脚本验证端口缓存与记录隔离、父环境恢复及 PID 归属，失败不能污染原网页。"""
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("未安装 PowerShell 7")
    scripts = tmp_path / "workspace with spaces" / "deployment"
    scripts.mkdir(parents=True)
    script = scripts / "start-web.ps1"
    shutil.copy2(ROOT / "deployment/start-web.ps1", script)
    (scripts.parent / "web").mkdir()
    environment = os.environ | {
        "XIAOYA_TEST_SCRIPT": str(script),
        "XIAOYA_TEST_SCENARIO": scenario,
        "XIAOYA_TEST_PORT": str(port),
    }
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", POWERSHELL_WEB_STUBS],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["parentUrl"] == "before-test"
    assert report["childUrl"] == "ws://172.16.0.2:7880"
    assert report["parentDistDir"] == "before-dist-test"
    assert report["childDistDir"] == f".next-dev-{port}"
    assert Path(report["stdoutPath"]).name == f"web-{port}.stdout.log"
    assert Path(report["stderrPath"]).name == f"web-{port}.stderr.log"
    assert report["calls"][:2] == ["sdk", "start"]
    if scenario == "normal":
        assert report["succeeded"] and report["stopped"] == []
        assert (scripts.parent / f".tools/logs/web-{port}.pid").read_text() == "4100"
    else:
        assert not report["succeeded"]
        expected = {
            "exited": [4101],
            "reused-child": [4100],
            "reused-root": [],
        }.get(scenario, [4101, 4100])
        assert report["stopped"] == expected
        assert 9000 not in report["stopped"]
        assert not (scripts.parent / f".tools/logs/web-{port}.pid").exists()


def bash_executable() -> str:
    """Windows 默认测试只用 Git Bash 做语法检查，不调用可能启动真实服务的 WSL。"""
    if os.name != "nt":
        executable = shutil.which("bash")
    else:
        git = shutil.which("git")
        candidate = Path(git).parent.parent / "bin/bash.exe" if git else None
        executable = str(candidate) if candidate is not None and candidate.is_file() else None
    if executable is None:
        pytest.skip("未安装用于离线脚本验证的 Bash")
    return executable


@pytest.mark.parametrize(
    "script", ["deploy.sh", "sync-runtime.sh", "wait-speech.sh", "start-livekit.sh"]
)
def test_current_bash_scripts_parse_without_execution(script):
    """每个文件分别执行 bash -n，避免额外参数被当作首个脚本的参数而漏检其他脚本。"""
    result = subprocess.run(
        [bash_executable(), "--noprofile", "--norc", "-n", str(ROOT / "deployment/wsl" / script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_deploy_manages_only_the_current_three_services():
    """执行真实部署控制流但替换全部副作用，退役服务不能继续成为隐式迁移对象。"""
    command = r"""
function test { # 输入存在性由替身满足，离线验收不读取 /opt 私有环境。
    return 0
}
function mkdir { # 不创建运行目录，只验证服务命令的实际控制流。
    return 0
}
function git { # 不克隆或修改 vendor，但让真实脚本继续走完整部署路径。
    return 0
}
function uv { # 不安装依赖、下载模型或构建真实环境。
    return 0
}
function bash { # 源码同步由其他行为测试覆盖，此处不写入运行目录。
    return 0
}
function cp { # 不安装 systemd 单元，后续调用仅供记录。
    return 0
}
function systemctl { # 只记录命令，不允许诊断启动或停止任何真实服务。
    printf '%s\n' "$*"
}
source "$1" "$2"
"""
    result = subprocess.run(
        [
            bash_executable(),
            "--noprofile",
            "--norc",
            "-c",
            command,
            "deployment-test",
            str(ROOT / "deployment/wsl/deploy.sh"),
            ROOT.as_posix(),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "daemon-reload",
        "enable xiaoya-livekit xiaoya-speech xiaoya-agent",
        "start xiaoya-livekit",
        "restart xiaoya-speech xiaoya-agent",
        "is-active xiaoya-livekit xiaoya-speech xiaoya-agent",
    ]


@pytest.mark.parametrize("key", ["not-required", "independent-test-tts-key"])
async def test_synthetic_audio_generation_uses_explicit_private_credentials(
    private_environment, key
):
    """真实请求用内存传输核对密钥原样传递，显式无鉴权值也不能借用其他模型的密钥。"""
    module = load_deployment("generate-avatar-utterances")
    settings = module.Settings.from_environment(
        private_environment | {"VOICE_AGENT_TTS_API_KEY": key}
    )
    pcm = b"\xff\x7f" * 240

    def respond(request):
        """仅检查私有 PCM 契约，不连接实际服务、保存音频或触发付费模型。"""
        assert request.url == "http://tts.internal:8003/v1/audio/speech"
        assert request.headers["Authorization"] == "Bearer " + key
        assert json.loads(request.content)["response_format"] == "pcm"
        return module.httpx.Response(200, content=pcm, headers={"content-type": "audio/pcm"})

    async with module.httpx.AsyncClient(transport=module.httpx.MockTransport(respond)) as client:
        received, chunks, elapsed = await module.synthesize(client, settings, "合成测试。")
    assert received == pcm and chunks == 1 and elapsed >= 0


@pytest.mark.parametrize("available", [False, True])
def test_sync_script_checks_uv_before_running_source_sync(tmp_path, available):
    """缺少 uv 时源码也保持原状；完整路径用实际脚本与无副作用 CLI 替身验证调用顺序。"""
    bash = bash_executable()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    log = tmp_path / "commands.log"
    if available:
        executable = binaries / "uv"
        executable.write_text(
            '#!/bin/bash\nprintf "%s\\n" "$*" >> "$XIAOYA_TEST_LOG"\n', encoding="utf-8"
        )
        executable.chmod(0o755)
    environment = os.environ.copy()
    # Windows 原生 PATH 使用分号，Git Bash 会在进入子进程时转换为 POSIX 路径。
    for name in tuple(environment):
        if name.casefold() == "path":
            del environment[name]
    environment["PATH"] = str(binaries)
    environment["XIAOYA_TEST_LOG"] = log.as_posix()
    result = subprocess.run(
        [
            bash,
            "--noprofile",
            "--norc",
            str(ROOT / "deployment/wsl/sync-runtime.sh"),
            ROOT.as_posix(),
            "--configuration",
        ],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    if available:
        assert result.returncode == 0, result.stderr
        commands = log.read_text(encoding="utf-8").splitlines()
        assert len(commands) == 3
        assert commands[0].startswith("run --no-project --python 3.12 ")
        assert "sync_runtime.py" in commands[0] and commands[0].endswith("--configuration")
        assert commands[1] == "sync --directory /opt/xiaoya/agent --locked --no-dev"
        assert (
            commands[2]
            == "sync --directory /opt/xiaoya/speech --locked --no-dev --no-install-project"
        )
    else:
        assert result.returncode == 1 and not log.exists()
