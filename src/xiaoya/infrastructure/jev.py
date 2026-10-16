"""OpenRouter Decisions 只判断人物语义，用户授权与回复表现分开且各资源按会话持有。"""

import asyncio
import contextlib
import json
import logging
import math
import time
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

from xiaoya.domain.delivery import GESTURE_LABELS, STYLE_LABELS, DeliveryIntent
from xiaoya.domain.presentation import (
    PresentationContext,
    PresentationDecision,
    UserPresentationDecision,
)
from xiaoya.domain.presentation_timing import PRESENTATION_TIMING
from xiaoya.infrastructure.settings import JEV_PROXY_URL

_LOGGER = logging.getLogger(__name__)
JEV_RESPONSE_MODEL = "typesafe/jev-1.13-20260917"
COMMAND_LABELS = {**GESTURE_LABELS, "repeat": "再做一次最近确认完成的指令动作"}
PROHIBITION_LABELS = {**GESTURE_LABELS, "all": "取消当前并停止本轮所有额外动作"}
_PROBABILITY_ROUNDING_ERROR = 0.005
_NEARBY_CHOICE_GAP = 0.01


class JevPresentationDecider:
    """直接异步 HTTP 避免隐藏重试；Jev 提供证据，应用层拥有执行许可和取消规则。"""

    def __init__(
        self, *, base_url: str, model: str, api_key: str, proxy_url: str = JEV_PROXY_URL
    ) -> None:
        """软故障按回复/用户分别保护；供应商明确限流与鉴权仍全会话共享，不跨优先级熔断。"""
        self._url = base_url.rstrip("/") + "/api/alpha/decisions"
        self._model = model
        self._key = api_key
        self._proxy_url = proxy_url
        self._client: aiohttp.ClientSession | None = None
        self._warm_task: asyncio.Task[bool] | None = None
        self._disabled = False
        self._failures = {"reply": 0, "user": 0}
        self._soft_cooldown = {"reply": 0.0, "user": 0.0}
        self._cooldown = 0.0
        self._rate_backoff = 1.0
        self._closed = False
        self._user_lock = asyncio.Lock()
        self._user_identity: tuple[str, str] | None = None
        self._user_result: UserPresentationDecision | None = None
        self._user_task: asyncio.Task[UserPresentationDecision | None] | None = None
        self._user_complete = False
        self._user_timeout: asyncio.Timeout | None = None
        self._queries: set[asyncio.Task[Any]] = set()

    async def warmup(self) -> None:
        """舞台就绪即免费建立代理连接；独立监督任务不占用语音或完整输入的等待时间。"""
        await self._ready()

    async def decide(self, context: PresentationContext) -> PresentationDecision | None:
        """回复仅受自身软冷却与共享供应商许可约束；原总预算、单次POST和解析规则均不改。"""
        started = time.monotonic()
        stage = "连接"
        try:
            budget = (
                PRESENTATION_TIMING.listening_total
                if context.phase == "listening"
                else PRESENTATION_TIMING.reply_total
            )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + budget
            async with asyncio.timeout_at(deadline):
                if not self._ensure_client(lane="reply"):
                    return None
                stage = "决策"
                body = await self._exchange(self._request(context), budget, deadline=deadline)
                if body is None or self._closed:
                    return None
                decision = self._parse(body)
        except Exception as error:
            if isinstance(error, TimeoutError) and context.phase == "listening":
                # 倾听短窗到期是过期增强，不等于服务故障，不能把稍后的正式指令一起熔断。
                _LOGGER.debug("Jev 倾听窗口结束（%s）", stage)
            else:
                self._query_failure(error, stage=stage, lane="reply")
            return None
        self._success("reply")
        _LOGGER.info(
            "Jev 决策完成：%.1fms，阶段 %s，预判 %s，正文 %d 字，样式 %s/%.2f，"
            "动作 %s/%.2f，肯定概率 %.2f",
            (time.monotonic() - started) * 1000,
            context.phase,
            context.reply_preview,
            len(context.text),
            decision.intent.style,
            decision.style_confidence,
            decision.intent.gesture,
            decision.gesture_confidence,
            decision.perform_probability,
        )
        return decision

    async def decide_user(self, context: PresentationContext) -> UserPresentationDecision | None:
        """完整提交授权一次新判断，软冷却仅抑制倾听；同身份终态和供应商限制仍有效。"""
        if (
            self._closed
            or self._disabled
            or time.monotonic() < self._cooldown
            or (context.phase == "listening" and time.monotonic() < self._soft_cooldown["user"])
        ):
            return None
        identity = (context.turn_id, context.user_text)
        async with self._user_lock:
            if self._closed:
                return None
            if self._user_identity != identity:
                if self._user_task is not None:
                    self._user_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._user_task
                if self._closed:
                    return None
                self._user_identity = identity
                self._user_result = None
                self._user_complete = context.phase != "listening"
                self._user_task = asyncio.create_task(
                    self._load_user(context, identity), name="xiaoya-jev-user"
                )
            elif context.phase != "listening" and not self._user_complete:
                if self._user_task is not None and (
                    self._user_task.done()
                    or (self._user_timeout is not None and self._user_timeout.expired())
                ):
                    # 截止回调已触发但 HTTP 仍在退出时，先回收再升级，避免 reschedule 复活过期时钟。
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._user_task
                    if self._closed:
                        return None
                    self._user_complete = True
                    if self._user_result is None:
                        self._user_task = asyncio.create_task(
                            self._load_user(context, identity), name="xiaoya-jev-user"
                        )
                else:
                    self._user_complete = True
                    if self._user_timeout is not None:
                        self._user_timeout.reschedule(
                            asyncio.get_running_loop().time() + PRESENTATION_TIMING.user_total
                        )
            if self._user_result is not None:
                return self._user_result
            assert self._user_task is not None
            task = self._user_task
        return await asyncio.shield(task)

    async def _load_user(
        self, context: PresentationContext, identity: tuple[str, str]
    ) -> UserPresentationDecision | None:
        """按所有者升级状态准入，完整提交保留原8秒时钟；不重放已失败的完整身份。"""
        started = time.monotonic()
        stage = "连接"
        timer = asyncio.timeout(
            PRESENTATION_TIMING.user_total
            if self._user_complete
            else PRESENTATION_TIMING.listening_total
        )
        self._user_timeout = timer
        try:
            async with timer:
                if not self._ensure_client(lane=None if self._user_complete else "user"):
                    return None
                stage = "决策"
                body = await self._exchange(
                    self._user_request(context), PRESENTATION_TIMING.user_total, deadline=timer
                )
                if body is None or self._closed:
                    return None
                decision = self._parse_user(body)
                if self._user_identity == identity:
                    self._user_result = decision
                self._success("user")
                _LOGGER.info(
                    "Jev 用户判断：%.1fms，完整 %s，指令 %s/%.2f，禁止 %s/%.2f",
                    (time.monotonic() - started) * 1000,
                    self._user_complete,
                    decision.command,
                    decision.command_confidence,
                    decision.prohibited,
                    decision.prohibition_confidence,
                )
                return decision
        except Exception as error:
            if isinstance(error, TimeoutError) and not self._user_complete:
                # 最终转写有自己的较长时窗；短暂轻回应过期不能导致真实指令被冷却跳过。
                _LOGGER.debug("Jev 倾听窗口结束（%s）", stage)
            else:
                self._query_failure(error, stage=stage, lane="user")
            return None
        finally:
            if self._user_timeout is timer:
                self._user_timeout = None

    def _ensure_client(self, *, lane: str | None = None) -> bool:
        """分配仍不发网络；可选预热不受软故障控制，业务同时检查本通道与供应商共享许可。"""
        if (
            self._closed
            or self._disabled
            or time.monotonic() < self._cooldown
            or (lane is not None and time.monotonic() < self._soft_cooldown[lane])
        ):
            return False
        if self._client is None:
            self._client = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=PRESENTATION_TIMING.user_total),
                headers={"Authorization": f"Bearer {self._key}"},
                raise_for_status=False,
                trust_env=False,
            )
        return True

    async def _ready(self) -> bool:
        """免费握手仅供显式预热等待，失败不影响同连接池内独立发起的正式查询。"""
        if not self._ensure_client():
            return False
        if self._warm_task is None or (self._warm_task.done() and not self._warm_task.result()):
            self._warm_task = asyncio.create_task(
                self._warm_connection(), name="xiaoya-jev-connect"
            )
        return bool(await asyncio.shield(self._warm_task)) and not self._closed

    async def _exchange(
        self, payload: dict[str, Any], budget: float, *, deadline: float | asyncio.Timeout
    ) -> Any:
        """回复固定截止，用户沿用可升级的外层时钟；两者都监督取消和响应上下文回收。"""
        if self._closed:
            return None
        task = asyncio.create_task(
            self._post(payload, budget, deadline=deadline), name="xiaoya-jev-query"
        )
        self._queries.add(task)
        try:
            async with asyncio.timeout_at(deadline if isinstance(deadline, float) else None):
                return await task
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._queries.discard(task)

    async def _post(
        self, payload: dict[str, Any], budget: float, *, deadline: float | asyncio.Timeout
    ) -> Any:
        """真正POST前再核共享供应商许可，不能因另通道先收到限流而在下一调度拍继续发送。"""
        if self._closed or self._disabled or time.monotonic() < self._cooldown:
            return None
        expires = deadline if isinstance(deadline, float) else deadline.when()
        remaining = 0.0 if expires is None else expires - asyncio.get_running_loop().time()
        if remaining <= 0 or (isinstance(deadline, asyncio.Timeout) and deadline.expired()):
            raise TimeoutError
        if isinstance(deadline, float):
            budget = min(budget, remaining)
        assert self._client is not None
        async with self._client.post(
            self._url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=budget),
            allow_redirects=False,
            proxy=self._proxy_url,
        ) as response:
            if response.status in {401, 403}:
                self._disabled = True
                _LOGGER.warning("Jev 鉴权失败，本次会话停用增强；请检查配置密钥")
                return None
            if response.status in {429, 529}:
                delay = max(
                    self._rate_backoff, self._retry_after(response.headers.get("Retry-After"))
                )
                self._cooldown = max(self._cooldown, time.monotonic() + delay)
                self._rate_backoff = min(delay * 2, 30.0)
                return None
            if response.status != 200:
                raise ValueError("Jev 状态异常")
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(8192):
                size += len(chunk)
                if size > 65536:
                    raise ValueError("Jev 响应过大")
                chunks.append(chunk)
            return json.loads(b"".join(chunks))

    async def _warm_connection(self) -> bool:
        """官网 HEAD 只作可选预热；它的可用性不能代表 Decisions 或累积业务熔断。"""
        assert self._client is not None
        try:
            async with self._client.head(
                "https://openrouter.ai/",
                proxy=self._proxy_url,
                timeout=aiohttp.ClientTimeout(total=5),
                allow_redirects=False,
            ) as response:
                if response.status >= 500:
                    raise ValueError("Jev 连接预热失败")
            return True
        except Exception as error:
            _LOGGER.debug("Jev 免费预热未完成（%s）", type(error).__name__)
            return False

    @staticmethod
    def _presentation_questions(*, listening: bool) -> dict[str, Any]:
        """神态遵循当下含义；倾听成果以眉眼回应，把祝贺点头留给回复，避免抢占同类冷却。"""
        gesture_instructions = (
            "你是小芽，正在倾听 user，判断合适的轻微回应，不是在表演用户本人。"
            "只有理解明确安全的事实或愿望可轻点头；好奇、困惑可轻歪头。"
            "用户分享积极成果或喜悦时，以 happy 眉眼倾听，gesture 选 none；"
            "祝贺或认可的点头留给小芽自己的回复。"
            "当前主要是在问候、告别或仅致谢时选 none，保持贴合的眉眼和自然微动，"
            "把挥手或认可留给小芽自己的回复；即使同时提到聊天开心，也不要抢先点头。"
            "危险判断、用户自我否定、争议立场、悲伤、引语、动作请求或命令选 none。"
            "不要把用户说自己点头等过去动作当作小芽动作。"
            if listening
            else "判定范围严格限定 `assistant` 当前回复句段与 `user` 当前输入，判断"
            "小芽适合的自然手势，不要求正文提及动作，不要求我点头等自述。"
            "只有 `assistant` 含指代且 `user` 无法解析时才参考 `previous_user_message` 与"
            "`previous_assistant_message`，否则忽略这两个过去字段；"
            "上一轮的纠正、情绪或手势不得延续到当前新话题。"
            "问候告别可挥手；明确肯定鼓励可点头；好奇追问可歪头；受夸腼腆可害羞；"
            "当前句段在问候或告别时优先 wave，即使同时说谢谢或肯定也不选 nod。"
            "只有纠正 `user` 当前确实表达的错误观点或否定其不安全观点才可摇头。"
            "当前 user 只问知识原理，没有断言错误观点时，普通解说中的不是因为、"
            "其实、但是等对比不是纠正，选 none，不能承接上一轮纠正。"
            "普通说明、数字、复杂含混内容无需明显手势。"
            "不依据某个否定词或问号判断；不认可危险判断、争议立场或用户自我否定；"
            "不能把引语、假设、过去事件或他人动作当作小芽当前动作。"
            "用户已直接要求动作时由指令路径处理，本问题选 none，避免重复。"
        )
        criteria = (
            {
                "none": "无需额外手势；当前主要是问候、告别、仅致谢、分享积极成果或喜悦，"
                "或请求动作、"
                "危险、自我否定、争议、悲伤或不明确；眉眼仍可自然回应",
                "nod": "小芽理解明确安全的事实或愿望，适合一次轻点头回应；"
                "不用于分享积极成果或喜悦的祝贺认可；当前主要是在问候、告别或仅致谢时不选此项",
                "tilt": "小芽对用户表达好奇或轻微困惑，适合一次轻歪头回应",
                "wave": "倾听时不挥手，不选此项",
                "shy": "倾听时不表演害羞，不选此项",
                "shake": "倾听时不摇头，不选此项",
            }
            if listening
            else {
                "none": "普通说明或含混表达，不需要明显动作；或用户有直接动作命令",
                "nod": "小芽当前明确认可安全事实、赞同、鼓励、肯定对方努力，"
                "或温暖肯定地回应对陪伴的感谢；仅收到信息或知识说明不算认可",
                "tilt": "小芽正在表达好奇、追问或轻微思考困惑",
                "wave": "小芽正在友好问候、欢迎见面或告别",
                "shy": "小芽受到用户夸奖而表达腼腆、谦逊或不好意思",
                "shake": "当前 user 已表达确实错误或不安全的观点，当前 assistant 正在明确"
                "纠正那个观点；不包括普通知识解释中的对比，也不包括上一轮错误或安慰否定句",
            }
        )
        return {
            "style": {
                "type": "choice",
                "instructions": (
                    "判断小芽当前合适的自然面部神态，倾听只根据 `user`，"
                    "说话只判断 `assistant` 当前句段的真实沟通含义结合 `user` 当前输入。"
                    "只有当前句段含指代且当前输入无法解析时才参考 `previous_user_message` 与"
                    "`previous_assistant_message`；其他情况忽略过去字段，不延续过去情绪。"
                    "当前 user 有真实难过、失落、困难或风险时，assistant 的"
                    "安慰支持、陪伴或温柔追问都选 concerned；"
                    "上述关切目的优先于温柔语气或问句形式，不改选 gentle、neutral 或 curious。"
                    "gentle 仅用于当前没有难过、困难或风险的温暖支持、日常陪伴或感谢。"
                    "难过、失败、危险时不庆祝；安慰的不用难过不是开心。"
                    "受到夸奖可害羞，积极成果可开心；"
                    + ("倾听用户分享积极成果或喜悦时选 happy，眉眼回应即可。" if listening else "")
                    + "独立探索兴趣或细节可好奇，关切式追问仍 concerned。"
                    "没有问候告别时，回应感谢陪伴、感到轻松是温暖 gentle，不是害羞或惊讶。"
                    "当前亲切问候或告别选 happy，即使同时感谢陪伴；普通说明与数字选 neutral。"
                    "引语与假设不等于正在发生的事实。保持温暖克制，不按标点数量判断。"
                ),
                "criteria": {
                    "neutral": "当前普通事实、知识原理或数字说明，无明显情感姿态",
                    "happy": "当前亲切问候或告别，即使同时感谢；或庆祝成果与积极喜悦，非难过风险",
                    "gentle": "当前没有真实难过、失落、困难或风险时的温暖支持、日常陪伴或回应感谢，"
                    "没有问候告别或可爱夸奖；不用于安慰当下困境",
                    "concerned": "当前 user 的真实难过、失落、困难或风险需要关切，"
                    "包括 assistant 对该困境的安慰支持、陪伴或温柔追问；"
                    "关切目的优先于温柔语气或问句形式",
                    "curious": "当前真正好奇、追问兴趣或细节，普通事实解说本身不是好奇",
                    "shy": "当前受到可爱、形象或性格的夸奖而腼腆，不只是用户谢谢陪伴",
                    "surprised": "当前真实的意外事实带来惊讶，不是单纯问句或引用惊讶",
                },
            },
            "gesture": {
                "type": "choice",
                "instructions": gesture_instructions,
                "criteria": criteria,
            },
            "energy": {
                "type": "score",
                "instructions": (
                    "根据当前真实含义判断克制的情绪强度，不依据感叹号数量；"
                    "倾听根据 user，说话根据 assistant。"
                ),
                "criteria": ["安静安慰", "自然交流", "明确兴奋"],
            },
        }

    def _request(self, context: PresentationContext) -> dict[str, Any]:
        """历史字段不重复当前user/assistant路径，保持指代参考且避免分类目标歧义。"""
        listening = context.phase not in {"speaking"} and not context.reply_preview
        return {
            "model": self._model,
            "state": {
                "phase": "listening" if listening else "speaking",
                "user": context.user_text,
                "assistant": "" if listening else context.text,
                "previous_user_message": context.recent_user_text,
                "previous_assistant_message": context.recent_assistant_text,
            },
            "questions": self._presentation_questions(listening=listening),
        }

    def _user_request(self, context: PresentationContext) -> dict[str, Any]:
        """原文只交Jev判断；省略祈使须唯一且面向小芽，不让补词或助手回复替用户授权。"""
        questions = self._presentation_questions(listening=True)
        questions.update(
            requested_gesture={
                "type": "choice",
                "instructions": (
                    "仅依据 user 判断用户现在直接要求小芽执行的一个目录动作或姿态。"
                    "点点头、歪歪头、挥挥手、害羞一下、摇摇头是动作命令。"
                    "面向小芽、语义清楚的省略宾语祈使句，只要在五种目录动作中仍唯一可确定，"
                    "也按该动作判断，例如挥挥是 wave；省略造成歧义则选 none，不猜测补全。"
                    "歪着头听、问、好奇地看都是 tilt；听、问、看是伴随交流，不是第二个身体动作。"
                    "动作姿态可以与交流同时执行，要求用歪头的样子问或看仍是直接身体姿态请求。"
                    "面向小芽的祈使姿态语句不用加请或做：歪着头看向我仍是要求歪头。"
                    "明确的目录身体姿态优先于伴随的好奇情绪、视线或交流要求，不选 none。"
                    "看向用户只指舞台视线姿态，不要求相机、看见真人或识别用户，"
                    "不能因此把明确歪头姿态当成不支持的视觉任务。"
                    "害羞姿态本来包含低头和收手臂，合在一起仍是一个 shy，不是复合命令。"
                    "可以、请、用某姿态等礼貌请求与直接命令同样有效。"
                    "再做一次、再来一下是 repeat，是否有已完成历史由程序判断，不在此猜测。"
                    "否定或停止不是请求；引用、假设、解释词义、过去事件、他人动作、"
                    "不支持的动作、同时正向请求多个不同目录动作的复合命令均选 none。"
                    "普通情绪、问候、同意不是用户动作命令。"
                ),
                "criteria": {
                    "none": "没有当前单个目录姿态请求，或省略有歧义、否定、引用、假设、解释、"
                    "同时请求点头和挥手等两个不同目录动作、不支持",
                    "nod": "直接请求小芽点头、点点头或上下点头",
                    "tilt": "小芽被要求呈现侧倾头部、歪着头的姿态；"
                    "包含歪头听或问，歪着头好奇地看向用户；不用额外说请或做",
                    "wave": "直接请求小芽挥手、挥挥手或向用户招手；"
                    "包括明确面向小芽且唯一指向挥手的省略祈使挥挥",
                    "shy": "请求小芽做害羞、腼腆姿态，包括害羞地低头并收一下手臂",
                    "shake": "直接请求小芽摇头、摇摇头或左右摇头",
                    "repeat": "明确请求再做一次、再来一下最近的指令动作",
                },
            },
            prohibited_gesture={
                "type": "choice",
                "instructions": (
                    "仅依据 user 判断用户当前禁止小芽的身体动作。"
                    "全局禁止优先：别动、停下或取消动作、不要演示、不要额外或其他动作均选 all。"
                    "不用某动作并且不要其他动作，也是 all，不再选其中某个动作。"
                    "只有未禁止全部动作时，不要、不用、别做某一个目录动作选择那个动作。"
                    "不想看某动作、别做某动作并附带保持自然、正常说话或只想听回答，"
                    "仍只禁止所指的动作，不能扩大为 all。"
                    "判断被禁止的对象：禁止小芽身体动作才是禁令；"
                    "请求用摇头表达拒绝、不同意某件事不是禁止摇头，选 none。"
                    "这里害羞指目录里的害羞低头收臂姿态，不要害羞或不要害羞一下是禁止 shy。"
                    "他人的过去动作本身不是禁止，但接着说你不用跟着做则禁止小芽模仿那个动作。"
                    "普通否定句、不想聊天、过去事件、引语、假设不是当前身体动作禁止。"
                    "请做某动作不是禁止；收到或回复内容不能解除用户禁止。"
                ),
                "criteria": {
                    "none": "用户没有当前禁止小芽动作；仅普通否定、引用、假设或他人事件",
                    "nod": "当前不想看、不要、别或不用小芽点头，仅限制点头；"
                    "也可延后点头到以后；保持自然或只听回答不扩展为全局禁止",
                    "tilt": "当前不想看、不要、别或不用小芽歪头，仅限制歪头；"
                    "也可延后歪头到以后；保持自然或只听回答不扩展为全局禁止",
                    "wave": "当前不想看、不要、别或不用小芽挥手，仅限制挥手；"
                    "包括不用模仿他人挥手或等说完再挥手；保持自然不扩展为全局禁止",
                    "shy": "当前不想看、不要、别或不用小芽害羞低头收臂姿态，仅限制 shy；"
                    "保持自然或只听回答不扩展为全局禁止",
                    "shake": "当前不想看、不要、别或不用小芽摇头，仅限制摇头；"
                    "请求用摇头表示拒绝不属于此项",
                    "all": "明确全局停止：别动、停止取消当前动作、不要演示、"
                    "不要任何或所有额外动作，或禁止某动作同时明确禁止所有其他动作；"
                    "只有保持自然或正常说话不属于本项",
                },
            },
        )
        return {
            "model": self._model,
            "state": {"user": context.user_text},
            "questions": questions,
        }

    @staticmethod
    def _validate_choice(answer: Any, options: Any) -> float:
        """供应商近邻标签与分布不一致只废弃该答案置信度，更大冲突或非法数值仍整体拒绝。"""
        if answer["type"] != "choice":
            raise ValueError("Jev 分类原语不匹配")
        JevPresentationDecider._validate_distribution(answer["probabilities"], options)
        probabilities, confidence = answer["probabilities"], answer["confidence"]
        if (
            answer["choice"] not in probabilities
            or probabilities[answer["choice"]] + _NEARBY_CHOICE_GAP + 1e-9
            < max(probabilities.values())
            or type(confidence) not in {float, int}
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise ValueError("Jev 分类分布非法")
        if probabilities[answer["choice"]] < max(probabilities.values()):
            return 0.0
        return float(confidence)

    @staticmethod
    def _validate_distribution(probabilities: Any, options: Any) -> None:
        """低分选项同样必须合法，不能只校验被选中的类别而缓存污染分布。"""
        if (
            set(probabilities) != set(options)
            or any(
                type(value) not in {float, int} or not math.isfinite(value) or not 0 <= value <= 1
                for value in probabilities.values()
            )
            or not math.isclose(
                sum(probabilities.values()),
                1,
                abs_tol=len(probabilities) * _PROBABILITY_ROUNDING_ERROR + 1e-9,
            )
        ):
            raise ValueError("Jev 分类分布非法")

    @staticmethod
    def _parse(body: Any) -> PresentationDecision:
        """自然表现使用当前沟通分类，不把请求或回复中的动作词合并成执行授权。"""
        if body["model"] != JEV_RESPONSE_MODEL:
            raise ValueError("Jev 模型版本不匹配")
        answers = body["answers"]
        style, gesture, energy = (answers[name] for name in ("style", "gesture", "energy"))
        style_confidence = JevPresentationDecider._validate_choice(style, STYLE_LABELS)
        gesture_confidence = JevPresentationDecider._validate_choice(gesture, GESTURE_LABELS)
        if energy["type"] != "score":
            raise ValueError("Jev 强度原语不匹配")
        JevPresentationDecider._validate_distribution(energy["probabilities"], {"0", "1", "2"})
        score, confidence = energy["score"], energy["confidence"]
        if type(score) not in {float, int} or not math.isfinite(score) or not 0 <= score <= 2:
            raise ValueError("Jev 强度等级非法")
        if not math.isclose(
            score,
            sum(int(level) * probability for level, probability in energy["probabilities"].items()),
            abs_tol=0.02 + 1e-9,
        ):
            raise ValueError("Jev 强度与分布不一致")
        if set(energy["legend"]) != {"0", "1", "2"} or any(
            not isinstance(value, str) or not value.strip() for value in energy["legend"].values()
        ):
            raise ValueError("Jev 强度等级说明非法")
        if (
            type(confidence) not in {float, int}
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise ValueError("Jev 强度置信度非法")
        return PresentationDecision(
            DeliveryIntent(style["choice"], gesture["choice"]),
            style_confidence,
            gesture_confidence,
            1 - gesture["probabilities"]["none"],
            score / 2 if confidence >= 0.65 else 0.5,
            "dialogue",
        )

    @staticmethod
    def _parse_user(body: Any) -> UserPresentationDecision:
        """用户命令、禁止及轻回应分别携带证据，最终优先级、概率门和历史由应用层决定。"""
        presentation = JevPresentationDecider._parse(body)
        requested, prohibited = (
            body["answers"][name] for name in ("requested_gesture", "prohibited_gesture")
        )
        command_confidence = JevPresentationDecider._validate_choice(requested, COMMAND_LABELS)
        prohibition_confidence = JevPresentationDecider._validate_choice(
            prohibited, PROHIBITION_LABELS
        )
        return UserPresentationDecision(
            presentation,
            requested["choice"],
            command_confidence,
            1 - requested["probabilities"]["none"],
            prohibited["choice"],
            prohibition_confidence,
            1 - prohibited["probabilities"]["none"],
        )

    def _success(self, lane: str) -> None:
        """成功只清本通道软故障序列；另一通道成功不能掩盖本通道失败或提前解除供应商退避。"""
        self._failures[lane] = 0
        self._rate_backoff = 1.0

    def _query_failure(self, error: Exception, *, stage: str = "决策", lane: str = "reply") -> None:
        """只留连接errno和底层类型，不展开异常身份；is_proxy仅表示配置代理，故障策略原样保留。"""
        if isinstance(error, aiohttp.ClientConnectorError):
            cause = error.os_error
            _LOGGER.warning(
                "Jev 连接诊断",
                extra={
                    "connector_errno": cause.errno if type(cause.errno) is int else None,
                    "cause_type": type(cause).__name__,
                    "is_proxy": bool(self._proxy_url),
                    "stage": stage,
                    "lane": lane,
                },
            )
        if isinstance(error, (TimeoutError, aiohttp.ClientConnectionError)):
            if (
                self._warm_task is not None
                and self._warm_task.done()
                and (
                    isinstance(error, aiohttp.ClientConnectionError) or not self._warm_task.result()
                )
            ):
                self._warm_task = None
        self._failure(f"{stage}/{type(error).__name__}", lane=lane)

    def _failure(self, category: str, *, lane: str) -> None:
        """三次软故障暂停本通道可选增强30秒；完整新提交和供应商共享限制分别准入。"""
        self._failures[lane] += 1
        if self._failures[lane] >= 3:
            self._soft_cooldown[lane] = time.monotonic() + 30
            self._failures[lane] = 0
        _LOGGER.warning("Jev 增强已降级（%s/%s）", lane, category)

    @staticmethod
    def _retry_after(value: str | None) -> float:
        """同时支持 HTTP 秒数和日期，损坏头采用短退避而不立即重试过期请求。"""
        try:
            delay = float(value) if value else 1.0
        except ValueError:
            try:
                delay = parsedate_to_datetime(value).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                delay = 1.0
        return max(1.0, delay) if math.isfinite(delay) else 1.0

    async def aclose(self) -> None:
        """先标记关闭并回收用户、握手和 HTTP 任务再关连接池，迟到结果不能复活会话。"""
        self._closed = True
        self._user_result = None
        self._user_identity = None
        tasks = set(self._queries)
        tasks.update(task for task in (self._user_task, self._warm_task) if task is not None)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        client, self._client = self._client, None
        if client is not None:
            await client.close()
