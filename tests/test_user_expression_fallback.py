"""用户表情仅填补本轮回复语义空窗，不能借此改变声音、手势或撤销边界。"""

import asyncio

import pytest

from xiaoya.application.presentation import PresentationController
from xiaoya.domain.delivery import DeliveryIntent
from xiaoya.domain.presentation import PresentationDecision, UserPresentationDecision


class HeldDecider:
    """每次调用独立Future，固定语义返回时序，避免真实网络和墙钟偶然性。"""

    def __init__(self) -> None:
        """用户和回复分队列，测试只能释放自己正在核对的身份。"""
        self.users = asyncio.Queue()
        self.replies = asyncio.Queue()
        self.ignore_user_cancel = False
        self.closed = False
        self.futures = []

    async def decide_user(self, context):
        """可模拟外部取消无效的迟到结果，验证应用仍按轮次和修订号拒绝。"""
        result = asyncio.get_running_loop().create_future()
        self.futures.append(result)
        self.users.put_nowait((context, result))
        try:
            return await asyncio.shield(result)
        except asyncio.CancelledError:
            if not self.ignore_user_cancel:
                raise
            return await result

    async def decide(self, context):
        """回复判断在测试显式释放前永不完成，确保说话无需等待它。"""
        result = asyncio.get_running_loop().create_future()
        self.futures.append(result)
        self.replies.put_nowait((context, result))
        return await result

    async def aclose(self):
        """仅记录会话端口关闭，不建立连接或留下跨测试状态。"""
        self.closed = True


@pytest.fixture
async def rig():
    """失败也统一关闭真实控制器，防止竞争测试的监督任务泄漏。"""
    port = HeldDecider()
    controller = PresentationController(port)
    cues = []
    controller.attach(cues.append)
    try:
        yield port, controller, cues
    finally:
        for future in port.futures:
            if not future.done():
                future.cancel()
        await controller.aclose()
        assert port.closed
        assert not any(not task.done() for task in controller._owned_tasks)


def expression(style="concerned", confidence=0.99, gesture="none", energy=0.2):
    """用合法固定概率测试原门槛，不能在应用层补写模型标签或提高分数。"""
    return PresentationDecision(DeliveryIntent(style, gesture), confidence, 0.99, 0.99, energy)


async def next_user(port):
    """有界等待真实端口进入，不把任意sleep当成异步任务已运行的证据。"""
    return await asyncio.wait_for(port.users.get(), 1.0)


async def next_reply(port):
    """仅消费有界当前或下一槽，防止测试意外释放错句段。"""
    return await asyncio.wait_for(port.replies.get(), 0.5)


async def resolve_user(controller, future, decision):
    """等待应用实际处理结果后再断言，不依赖事件循环调度次数。"""
    task = controller._user_task
    future.set_result(UserPresentationDecision(decision))
    await asyncio.wait_for(asyncio.shield(task), 0.5)


async def resolve_reply(controller, future, decision):
    """等待当前真实决策任务清理，确保断言检查采纳而非仅Future已完成。"""
    task = controller._decision_task
    future.set_result(decision)
    for _ in range(50):
        await asyncio.sleep(0)
        if controller._decision_task is not task:
            return
    raise AssertionError("ReplyResultNotConsumed")


@pytest.mark.parametrize("style", ["concerned", "gentle"])
@pytest.mark.parametrize("boundary", ["preview", "speaking"])
async def test_late_user_expression_fills_reply_semantic_gap(rig, style, boundary):
    """首个预判已启动仍可利用同轮用户表情；起声同步返回且不等待任一语义服务。"""
    port, controller, cues = rig
    controller.complete_turn("当前用户")
    _, user = await next_user(port)
    controller.prepare_reply("当前回复。")
    await next_reply(port)
    if boundary == "speaking":
        cue = controller.speak("r", "s", DeliveryIntent(), "当前回复。")
        assert cue.intent.style == "neutral"
    await resolve_user(controller, user, expression(style, gesture="wave"))
    assert cues[-1].intent.style == style
    assert controller._last_expression == style
    assert cues[-1].intent.gesture == "none"
    assert cues[-1].gesture_id == "" and cues[-1].gesture_source == "none"
    assert controller.voice_for("当前回复。") == DeliveryIntent()
    if boundary == "preview":
        cue = controller.speak("r", "s", DeliveryIntent(), "当前回复。")
        assert cue.intent.style == style


@pytest.mark.parametrize("style", ["neutral", "happy"])
async def test_current_high_confidence_reply_owns_expression_priority(rig, style):
    """高置信中性同样是有效回复语义，用户迟到不得把它覆盖成关切。"""
    port, controller, cues = rig
    controller.complete_turn("当前用户")
    _, user = await next_user(port)
    controller.prepare_reply("当前回复。")
    _, reply = await next_reply(port)
    await resolve_reply(controller, reply, expression(style))
    await resolve_user(controller, user, expression("concerned"))
    assert cues[-1].intent.style == style


async def test_previous_sentence_reply_priority_survives_current_unjudged_sentence(rig):
    """同轮首句已确定神态，后一句尚无结果也不能让迟到用户语义降级覆盖。"""
    port, controller, cues = rig
    controller.complete_turn("当前用户")
    _, user = await next_user(port)
    controller.prepare_reply("第一句。")
    _, first = await next_reply(port)
    await resolve_reply(controller, first, expression("happy"))
    controller.speak("r", "s1", DeliveryIntent(), "第一句。")
    controller.finish("r", "s1")
    controller.speak("r", "s2", DeliveryIntent(), "第二句。")
    await resolve_user(controller, user, expression("concerned"))
    assert cues[-1].intent.style == "happy"


async def test_future_cached_reply_cannot_claim_current_expression_priority(rig):
    """下一句缓存只属于未来身份，当前只有低置信结果时用户仍可填补表情空窗。"""
    port, controller, cues = rig
    controller.complete_turn("当前用户")
    _, user = await next_user(port)
    controller.prepare_reply("第一句。")
    _, first = await next_reply(port)
    controller.speak("r", "s1", DeliveryIntent(), "第一句。")
    controller.prepare_reply("第二句。")
    await resolve_reply(controller, first, expression("neutral", 0.64))
    _, second = await next_reply(port)
    await resolve_reply(controller, second, expression("happy"))
    assert controller._accepted[0].text == "第二句。"
    await resolve_user(controller, user, expression("concerned"))
    assert cues[-1].intent.style == "concerned"
    assert controller.voice_for("第一句。") == DeliveryIntent()
    controller.finish("r", "s1")
    assert controller.speak("r", "s2", DeliveryIntent(), "第二句。").intent.style == "happy"


@pytest.mark.parametrize("confidence", [0.0, 0.64])
async def test_low_confidence_reply_does_not_lock_user_expression_out(rig, confidence):
    """低置信回复不是有效表情事实，仍保留原0.65门而不阻止用户高置信兜底。"""
    port, controller, cues = rig
    controller.complete_turn("当前用户")
    _, user = await next_user(port)
    controller.prepare_reply("当前回复。")
    _, reply = await next_reply(port)
    await resolve_reply(controller, reply, expression("happy", confidence))
    await resolve_user(controller, user, expression("concerned"))
    assert cues[-1].intent.style == "concerned"


@pytest.mark.parametrize("confidence,expected", [(0.64, "neutral"), (0.65, "concerned")])
async def test_user_expression_preserves_exact_original_threshold(rig, confidence, expected):
    """临界值不加epsilon、不改标签，0.65以上仅允许视觉表情补空窗。"""
    port, controller, cues = rig
    controller.complete_turn("当前用户")
    _, user = await next_user(port)
    controller.prepare_reply("当前回复。")
    await resolve_user(controller, user, expression("concerned", confidence))
    assert cues[-1].intent.style == expected


async def test_partial_user_result_uses_same_expression_fallback_without_command(rig):
    """稳定增量的同轮表情仍可用于思考；完整提交许可和600ms倾听门均不绕过。"""
    port, controller, cues = rig
    controller.user_speaking(True)
    controller.transcribe("稳定增量", final=False)
    context, user = await next_user(port)
    assert context.phase == "listening"
    controller.phase("thinking")
    controller.prepare_reply("当前回复。")
    await resolve_user(controller, user, expression("gentle", gesture="nod"))
    assert cues[-1].intent.style == "gentle"
    assert cues[-1].gesture_source == "none" and controller._command is None


async def test_user_fallback_preserves_existing_command_identity_and_energy(rig):
    """用户表情兜底只改style，不能替换已授权独立指令身份、能量或回执事实。"""
    port, controller, cues = rig
    controller.complete_turn("当前用户")
    _, user = await next_user(port)
    controller.prepare_reply("当前回复。")
    command = UserPresentationDecision(expression("neutral", 0.1, energy=0.8), "wave", 1.0, 1.0)
    controller._adopt_user(command)
    before = controller._cue
    assert before.gesture_source == "command"
    await resolve_user(controller, user, expression("gentle", gesture="shake", energy=0.1))
    after = cues[-1]
    assert after.intent.style == "gentle" and after.intent.gesture == "wave"
    assert (after.gesture_id, after.gesture_source, after.energy) == (
        before.gesture_id,
        before.gesture_source,
        before.energy,
    )
    assert controller._execution_state == "pendingCommand"


async def test_user_fallback_preserves_existing_dialogue_motion_and_energy(rig):
    """完整用户返回前未知禁止只遮显示，用户表情兜底不能清掉已判定的原动作身份。"""
    port, controller, _ = rig
    controller.complete_turn("当前用户")
    _, user = await next_user(port)
    controller.prepare_reply("当前回复。")
    _, reply = await next_reply(port)
    controller.speak("r", "s", DeliveryIntent(), "当前回复。")
    await resolve_reply(controller, reply, expression("neutral", 0.64, "wave", 0.8))
    before = controller._base
    assert before.intent.gesture == "wave" and before.gesture_source == "dialogue"
    assert controller._cue.intent.gesture == "none" and controller._prohibited == "all"
    await resolve_user(controller, user, expression("gentle", gesture="shake", energy=0.1))
    after = controller._cue
    assert after.intent.style == "gentle" and after.intent.gesture == "wave"
    assert (after.gesture_id, after.gesture_source, after.energy) == (
        before.gesture_id,
        before.gesture_source,
        before.energy,
    )
    assert controller.voice_for("当前回复。") == DeliveryIntent()


async def test_changed_transcript_releases_reply_priority_for_revised_input(rig):
    """原实现已撤销不同user_text的回复采纳，新优先级也须同步失效，不能锁住旧语义。"""
    port, controller, _ = rig
    controller.complete_turn("原用户")
    _, old_user = await next_user(port)
    await resolve_user(controller, old_user, expression("neutral", 0.1))
    controller.prepare_reply("当前回复。")
    _, reply = await next_reply(port)
    await resolve_reply(controller, reply, expression("happy"))
    controller.transcribe("修订后用户", final=True)
    _, user = await next_user(port)
    assert controller._accepted is None
    await resolve_user(controller, user, expression("concerned"))
    assert controller._cue.intent.style == "concerned"


async def test_actual_reply_overrides_fallback_and_rejects_later_user_style(rig):
    """用户神态只是空窗兜底，真正当前回复到达后始终拥有更高优先级。"""
    port, controller, _ = rig
    controller.complete_turn("当前用户")
    _, user = await next_user(port)
    controller.prepare_reply("当前回复。")
    _, reply = await next_reply(port)
    await resolve_user(controller, user, expression("concerned"))
    assert controller._cue.intent.style == "concerned"
    await resolve_reply(controller, reply, expression("happy"))
    controller._adopt_user(UserPresentationDecision(expression("gentle")))
    assert controller._cue.intent.style == "happy"


async def test_idle_finish_does_not_revive_late_user_expression(rig):
    """单句播放已关闭的idle不应被晚用户判断重新点亮，下一轮再恢复正常许可。"""
    port, controller, cues = rig
    controller.complete_turn("当前用户")
    _, user = await next_user(port)
    controller.speak("r", "s", DeliveryIntent(), "当前回复。")
    controller.finish("r", "s")
    await resolve_user(controller, user, expression("concerned"))
    assert controller._cue.phase == "idle" and controller._cue.intent.style == "neutral"


async def test_reply_completed_terminal_does_not_revive_late_user_expression(rig):
    """正常ReplyDone封存终态，即便同turn旧用户仍返回也不能恢复已完成的神态。"""
    port, controller, cues = rig
    controller.complete_turn("当前用户")
    _, user = await next_user(port)
    controller.speak("r", "s", DeliveryIntent(), "当前回复。")
    controller.finish("r", "s")
    controller.complete_reply("r")
    assert controller._expression_finished
    await resolve_user(controller, user, expression("concerned"))
    assert cues[-1].intent.style == "neutral" and controller._last_expression is None


async def test_new_turn_resets_reply_priority_for_new_user_expression(rig):
    """回复优先级只属于原轮次，不能让上轮开心锁住新轮用户的关切神态。"""
    port, controller, cues = rig
    controller.complete_turn("上一轮")
    _, old_user = await next_user(port)
    await resolve_user(controller, old_user, expression("neutral", 0.1))
    controller.prepare_reply("上一轮回复。")
    _, reply = await next_reply(port)
    await resolve_reply(controller, reply, expression("happy"))
    controller.complete_turn("新用户")
    _, user = await next_user(port)
    controller.prepare_reply("新回复。")
    await resolve_user(controller, user, expression("concerned"))
    assert cues[-1].intent.style == "concerned"


async def test_canceled_old_turn_user_result_cannot_change_new_expression(rig):
    """供应商忽略取消也必须经过原revision/turn门，不能把旧用户表情借给新输入。"""
    port, controller, cues = rig
    port.ignore_user_cancel = True
    controller.complete_turn("旧用户")
    _, old = await next_user(port)
    old_task = controller._user_task
    controller.complete_turn("新用户")
    _, current = await next_user(port)
    controller.prepare_reply("新回复。")
    old.set_result(UserPresentationDecision(expression("happy")))
    await asyncio.wait_for(old_task, 0.5)
    assert cues[-1].intent.style == "neutral"
    await resolve_user(controller, current, expression("concerned"))
    assert cues[-1].intent.style == "concerned"
