"""v1 契约的唯一来源：请求/响应、枚举、结构化错误、两类 SSE 事件 schema。

本模块只描述数据形状，不含业务规则、鉴权或路由。
OpenAPI 的 components.schemas 必须由这些模型生成，禁止手写第二份。
"""
from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator

from _ports import DEFAULT_ASR_PORT, DEFAULT_OLLAMA_PORT


class ErrorCode(StrEnum):
    invalid_request = "invalid_request"
    not_found = "not_found"
    conflict = "conflict"
    unauthorized = "unauthorized"
    forbidden = "forbidden"
    timeout = "timeout"
    dependency_failed = "dependency_failed"
    asr_unavailable = "asr_unavailable"
    polish_failed = "polish_failed"
    translate_failed = "translate_failed"
    ocr_failed = "ocr_failed"
    ocr_language_missing = "ocr_language_missing"
    history_io = "history_io"
    jobs_io = "jobs_io"
    delivery_failed = "delivery_failed"
    config_io = "config_io"
    internal = "internal"


class ErrorBody(BaseModel):
    """所有能力接口失败时的结构化错误体。"""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[False] = False
    error: ErrorCode
    message: str
    details: dict[str, Any] | None = None


class LogicError(Exception):
    """逻辑层可见失败。调用方应把它转成 ErrorBody，不得吞掉后返回原文充成功。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_body(self) -> ErrorBody:
        return ErrorBody(error=self.code, message=self.message, details=self.details)


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class JobState(StrEnum):
    """jobs.json 持久化状态。转移规则属于业务层，本层只约束取值。

    failed 仅表示已确定本次毫无输出。
    delivery_unknown 覆盖部分输出、超时、ack 丢失等未确定投递结果。
    查询不存在的 job 时用 JobQueryUnknown.state="unknown"，不得把 unknown 写入 JobRecord。
    """

    queued = "queued"
    claimed = "claimed"
    done = "done"
    failed = "failed"
    delivery_unknown = "delivery_unknown"


class AckResult(StrEnum):
    """投递 ack 的结果取值（业务层解释；存储层原样保存）。"""

    complete = "complete"
    none = "none"
    partial_or_unknown = "partial_or_unknown"


class UtteranceSource(StrEnum):
    desktop = "desktop"
    web = "web"
    resend = "resend"


class SessionClosedReason(StrEnum):
    finished = "finished"
    cancelled = "cancelled"
    error = "error"
    timeout = "timeout"
    disconnected = "disconnected"


class SessionEventType(StrEnum):
    partial = "partial"
    final = "final"
    error = "error"
    closed = "closed"


class ProductEventType(StrEnum):
    """产品事件流（状态栏等）的类型判别，与识别会话 SSE 分开。不用自由文本 style 冒充事件。"""

    idle = "idle"
    recording = "recording"
    partial = "partial"
    polishing = "polishing"
    typed = "typed"
    error = "error"
    busy = "busy"
    history_write_failed = "history_write_failed"
    delivery_unknown = "delivery_unknown"
    component_command = "component_command"  # 服务端 → 应用：看护层命令（只给应用主进程，网页事件流忽略）
    config_changed = "config_changed"  # 配置已保存：应用重读配置（开机自启开关即时生效）


class ComponentName(StrEnum):
    """应用看护层拉起并保持运行的三个后台组件（拉起顺序即此顺序）。"""

    ollama = "ollama"
    asr = "asr"
    server = "server"


class ComponentState(StrEnum):
    not_installed = "not_installed"
    starting = "starting"
    running = "running"
    backing_off = "backing_off"
    stopped = "stopped"


class HistoryPolish(StrEnum):
    """历史记录中的润色结果。与旧字段 mode 分离，不互相当别名。"""

    on = "on"
    off = "off"
    failed = "failed"
    skipped = "skipped"


# ---------------------------------------------------------------------------
# 识别会话（产品 /api/v1/recognize/* 与 sidecar 共用字段形状）
# ---------------------------------------------------------------------------


class RecognizeSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str | None = None
    context: str | None = None


class RecognizeSessionCreated(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str


class ChunkAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted: Literal[True] = True
    index: int = Field(ge=0)


class SessionPartialEvent(BaseModel):
    """会话 partial 的唯一字段是 stable_text / tail_text，不并行维护 stable/tail 或独立 text。"""

    model_config = ConfigDict(extra="forbid")
    event: Literal["partial"] = "partial"
    session_id: str
    stable_text: str = ""
    tail_text: str = ""
    language: str | None = None
    audio_end_s: float | None = None


class SessionFinalEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal["final"] = "final"
    session_id: str
    text: str
    language: str | None = None


class SessionErrorEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal["error"] = "error"
    session_id: str
    error: ErrorCode
    message: str
    details: dict[str, Any] | None = None


class SessionClosedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal["closed"] = "closed"
    session_id: str
    reason: SessionClosedReason


SessionSSEEvent = Annotated[
    Union[SessionPartialEvent, SessionFinalEvent, SessionErrorEvent, SessionClosedEvent],
    Field(discriminator="event"),
]


class SessionSSEEventEnvelope(RootModel[SessionSSEEvent]):
    """识别会话 SSE 一条事件的显式信封，供 OpenAPI components.schemas 引用。"""


# ---------------------------------------------------------------------------
# 产品事件 SSE（只定义表示形状，不含业务状态转移）
# ---------------------------------------------------------------------------


class ProductIdleEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal[ProductEventType.idle] = ProductEventType.idle


class ProductRecordingEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal[ProductEventType.recording] = ProductEventType.recording
    session_id: str | None = None


class ProductPartialEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal[ProductEventType.partial] = ProductEventType.partial
    session_id: str | None = None
    job_id: str | None = None
    stable_text: str = ""
    tail_text: str = ""


class ProductPolishingEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal[ProductEventType.polishing] = ProductEventType.polishing
    job_id: str | None = None


class ProductTypedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal[ProductEventType.typed] = ProductEventType.typed
    job_id: str
    text: str = ""


class ProductErrorEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal[ProductEventType.error] = ProductEventType.error
    error: ErrorCode
    message: str
    details: dict[str, Any] | None = None


class ProductBusyEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal[ProductEventType.busy] = ProductEventType.busy


class ProductHistoryWriteFailedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal[ProductEventType.history_write_failed] = ProductEventType.history_write_failed
    job_id: str | None = None
    message: str
    error: ErrorCode | None = None


class ProductDeliveryUnknownEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal[ProductEventType.delivery_unknown] = ProductEventType.delivery_unknown
    job_id: str
    text: str | None = None


class ProductComponentCommandEvent(BaseModel):
    """设置页/托盘「重试」经服务端转给应用看护层；组件是否已安装由看护层按文件自行发现，不经事件。"""

    model_config = ConfigDict(extra="forbid")
    event: Literal[ProductEventType.component_command] = ProductEventType.component_command
    action: Literal["retry"]
    component: ComponentName


class ProductConfigChangedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event: Literal[ProductEventType.config_changed] = ProductEventType.config_changed


ProductSSEEvent = Annotated[
    Union[
        ProductIdleEvent,
        ProductRecordingEvent,
        ProductPartialEvent,
        ProductPolishingEvent,
        ProductTypedEvent,
        ProductErrorEvent,
        ProductBusyEvent,
        ProductHistoryWriteFailedEvent,
        ProductDeliveryUnknownEvent,
        ProductComponentCommandEvent,
        ProductConfigChangedEvent,
    ],
    Field(discriminator="event"),
]


class ProductSSEEventEnvelope(RootModel[ProductSSEEvent]):
    """产品事件 SSE 一条事件的显式信封。"""


PlatformEvent = Annotated[
    Union[ProductIdleEvent, ProductBusyEvent, ProductErrorEvent],
    Field(discriminator="event"),
]


class PlatformEventCreate(RootModel[PlatformEvent]):
    """本机桌面平台壳上报的状态（开麦失败、截图中、截图失败等），原样进入产品事件流。

    只允许 idle / busy / error；识别、润色、投递状态由服务端业务自己发布，平台不得冒充。
    """


class PlatformEventAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True


# ---------------------------------------------------------------------------
# Utterance / Job（契约形状；业务编排不在本层）
# ---------------------------------------------------------------------------


class UtteranceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    session_id: str
    source: UtteranceSource
    focus_id: str = Field(description="按 job 绑定的焦点，禁止使用全局唯一焦点变量")


class UtteranceFromAudio(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    source: Literal[UtteranceSource.web] = UtteranceSource.web
    language: str | None = None
    context: str | None = None


class UtteranceAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    state: Literal[JobState.queued] = JobState.queued
    text: str
    job_id: str


class UtteranceResendRequest(BaseModel):
    """original_job_id 必填：不得靠省略原 id 绕过 delivery_unknown 确认。"""

    model_config = ConfigDict(extra="forbid")
    job_id: str
    text: str
    original_job_id: str = Field(min_length=1)
    confirm: bool = False
    focus_id: str | None = Field(
        default=None,
        description="覆盖继承自原 job 的焦点。桌面重复键传按键时的焦点；省略则沿用原 job 焦点",
    )


class UtteranceResendQueued(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    state: Literal[JobState.queued] = JobState.queued
    job_id: str
    text: str


class UtteranceResendNeedsConfirmation(BaseModel):
    """未确认的 resend 响应。ok 必须为 false；是否入队由业务层执行，本模型不得表示已成功入队。"""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[False] = False
    requires_confirmation: Literal[True] = True
    original_job_id: str
    new_job_id: str


class JobClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeout_s: float | None = Field(default=None, ge=0)


class JobClaimed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    claim_id: str
    text: str
    source: UtteranceSource
    focus_id: str
    state: Literal[JobState.claimed] = JobState.claimed


class JobAckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    claim_id: str
    result: AckResult
    detail: str | None = None


class JobAckAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    job_id: str
    state: JobState


class JobRecord(BaseModel):
    """jobs.json 中一条记录的 schema。focus_id 跟 job，不是全局单值。state 不含 unknown。"""

    model_config = ConfigDict(extra="allow")
    job_id: str
    state: JobState
    text: str = ""
    source: UtteranceSource = UtteranceSource.desktop
    focus_id: str = ""
    session_id: str | None = None
    claim_id: str | None = None
    created_at: str
    updated_at: str
    detail: str | None = None
    original_job_id: str | None = None


class JobStoreDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    jobs: dict[str, JobRecord] = Field(default_factory=dict)


class JobQueryFound(BaseModel):
    """GET job 在记录存在时的查询响应。state 来自持久化 JobRecord，不可能是 unknown。"""

    model_config = ConfigDict(extra="forbid")
    found: Literal[True] = True
    job: JobRecord


class JobQueryUnknown(BaseModel):
    """GET job 在记录不存在时的查询响应。unknown 只出现在此查询形状，不可写入 JobRecord。"""

    model_config = ConfigDict(extra="forbid")
    found: Literal[False] = False
    job_id: str
    state: Literal["unknown"] = "unknown"


JobQueryResponse = Annotated[
    Union[JobQueryFound, JobQueryUnknown],
    Field(discriminator="found"),
]


class JobQueryResponseEnvelope(RootModel[JobQueryResponse]):
    """job 查询响应信封，供 OpenAPI 引用。"""


# ---------------------------------------------------------------------------
# 能力接口
# ---------------------------------------------------------------------------


class SttRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str | None = None
    context: str | None = None


class SttResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    text: str
    language: str | None = None


class PolishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    keywords: list[str] | None = None
    language: str | None = None


class PolishResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    text: str


DEFAULT_TRANSLATE_LANG_PAIR = "zh-en"
TRANSLATE_LANG_PAIRS: dict[str, tuple[str, str]] = {
    "zh-en": ("zh", "en"),
}


class TranslateDirectionMode(StrEnum):
    """翻译方向判定模式：auto 由后端自动判定，manual 为显式成对指定。"""

    auto = "auto"
    manual = "manual"


class OcrTranslateMode(StrEnum):
    """OCR 能力接口的处理模式：translate 识别并翻译，raw 仅识别原文。"""

    translate = "translate"
    raw = "raw"


def validate_lang_pair(lang_pair: str) -> str:
    """单源语言对校验与规范化（供契约模型、配置更新、翻译逻辑与路由共用）。非法值抛 ValueError。"""
    if not isinstance(lang_pair, str):
        raise ValueError(f"翻译语言对必须是字符串，收到 {type(lang_pair).__name__}")
    norm = lang_pair.strip().lower()
    if norm not in TRANSLATE_LANG_PAIRS:
        raise ValueError(f"不支持的翻译语言对: {lang_pair!r}，可选值: {sorted(TRANSLATE_LANG_PAIRS)}")
    return norm


def validate_direction_override(
    lang_pair: str,
    source_lang: str | None,
    target_lang: str | None,
) -> tuple[str, tuple[str, str] | None]:
    """单源语言对 + 可选手动方向校验。

    - (None, None) → (norm_pair, None)（自动判定模式）
    - 仅传单边或含空白串 → 抛 ValueError
    - (src, tgt) 必须同属该语言对且互不相同 → (norm_pair, (norm_src, norm_tgt))
    """
    norm_pair = validate_lang_pair(lang_pair)
    if source_lang is None and target_lang is None:
        return norm_pair, None
    if source_lang is None or target_lang is None:
        raise ValueError("手动指定翻译方向时必须同时提供非空的 source_lang 和 target_lang")
    norm_src = source_lang.strip().lower()
    norm_tgt = target_lang.strip().lower()
    if not norm_src or not norm_tgt:
        raise ValueError("手动指定翻译方向时必须同时提供非空的 source_lang 和 target_lang")
    allowed = TRANSLATE_LANG_PAIRS[norm_pair]
    if norm_src == norm_tgt:
        raise ValueError(f"源语言与目标语言不能相同: {norm_src!r}")
    if {norm_src, norm_tgt} != set(allowed):
        raise ValueError(
            f"方向 {norm_src!r} -> {norm_tgt!r} 不属于语言对 {norm_pair!r}（可选语言: {list(allowed)}）"
        )
    return norm_pair, (norm_src, norm_tgt)


class TranslateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    lang_pair: str = DEFAULT_TRANSLATE_LANG_PAIR
    source_lang: str | None = None
    target_lang: str | None = None

    @model_validator(mode="after")
    def _validate_request(self) -> "TranslateRequest":
        if not self.text.strip():
            raise ValueError("翻译文本不能为空")
        norm_pair, override = validate_direction_override(self.lang_pair, self.source_lang, self.target_lang)
        self.lang_pair = norm_pair
        if override is not None:
            self.source_lang, self.target_lang = override
        return self


class TranslateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    text: str
    lang_pair: str
    source_lang: str
    target_lang: str
    direction_mode: TranslateDirectionMode
    chunks: int = Field(default=1, ge=1)


class OcrTranslateResponse(BaseModel):
    """OCR+翻译成功体：text 是译文，ocr_text 是识别原文；不得把 ocr 当 text。"""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    text: str
    ocr_text: str
    mode: OcrTranslateMode
    lang_pair: str | None = None
    source_lang: str | None = None
    target_lang: str | None = None
    direction_mode: TranslateDirectionMode | None = None
    chunks: int | None = Field(default=None, ge=1)


class OcrTextLine(BaseModel):
    """一条可见文字行；坐标已还原放大倍数与留白。"""

    model_config = ConfigDict(extra="forbid")
    bbox: tuple[int, int, int, int] = Field(description="(x, y, width, height) 像素坐标，相对于所提交图片左上角")
    text: str = Field(min_length=1, description="行内文字，不含缩进空格")
    soft_wrap: bool = Field(default=False, description="True = 本行是上一行的自动折行续行；块内首行恒为 False")


class OcrTextBlock(BaseModel):
    """同列紧邻的若干行组成的文字块及其局部采样背景/前景色，供原位浮窗逐行绘制与排版。"""

    model_config = ConfigDict(extra="forbid")
    bbox: tuple[int, int, int, int] = Field(
        description="(x, y, width, height) 像素坐标，相对于所提交图片左上角；= 全部行框的并集"
    )
    lines: list[OcrTextLine] = Field(min_length=1, description="逐行绘制与全部文本派生的唯一来源")
    ocr_text: str = Field(description="逐行原文，保留可见换行与相对块左缘的缩进（_ocr_text.block_display_text）")
    translated_text: str = Field(
        default="",
        description="各硬行段译文按原缩进逐行拼回（_ocr_text.block_translated_text）；raw 模式 = ocr_text",
    )
    bg_color: str = Field(default="#ffffff", description="半透明贴片底色 #RRGGBB，采样自文字框外围")
    fg_color: str = Field(default="#0f172a", description="高对比度前景文字色 #RRGGBB")


class OcrInplaceResponse(BaseModel):
    """POST /api/v1/ocr-inplace 响应：同时携带整段文本与分块原位排版坐标。"""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    text: str
    ocr_text: str
    mode: OcrTranslateMode
    lang_pair: str | None = None
    source_lang: str | None = None
    target_lang: str | None = None
    direction_mode: TranslateDirectionMode | None = None
    image_width: int = Field(ge=1)
    image_height: int = Field(ge=1)
    blocks: list[OcrTextBlock] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 历史（处理记录，不是投递记录）
# ---------------------------------------------------------------------------


class HistoryRecord(BaseModel):
    """新记录显式使用 source / polish / job_id。旧 mode 仅兼容读取，不是这些字段的别名。"""

    model_config = ConfigDict(extra="allow")
    timestamp: str
    raw_text: str
    final_text: str
    language: str
    mode: str | None = None
    source: UtteranceSource | None = None
    polish: HistoryPolish | None = None
    job_id: str | None = None


class HistoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    records: list[HistoryRecord]
    total: int
    skipped_malformed: int = 0


# ---------------------------------------------------------------------------
# 配置（已知字段；未知字段在存取层保留）
# ---------------------------------------------------------------------------


class AppConfig(BaseModel):
    """持久化配置的已知字段。未在此声明的键由存取层原样保留。

    默认值只用于缺省键，不会在加载时改写磁盘。不把未选定项写成用户已选事实。
    """

    model_config = ConfigDict(extra="allow")
    ollama_model: str = "qwen3.5:4b"
    whisper_model: str = "small"
    whisper_language: str = "zh"
    custom_keywords: list[str] = Field(default_factory=list)
    translate_lang_pair: str = DEFAULT_TRANSLATE_LANG_PAIR
    translate_auto_enabled: bool = True
    baidu_appid: str = ""
    baidu_appkey: str = ""
    hotkey_voice: str = "ctrl+alt+z"
    hotkey_screenshot: str = "ctrl+alt+x"
    hotkey_ocr: str = "ctrl+alt+o"
    hotkey_repeat: str = "ctrl+alt+r"
    hotkey_annotate: str = "ctrl+alt+a"
    autostart_enabled: bool = True
    ui_theme: Literal["light", "dark"] = "light"
    audio_input_device: str = ""
    vision_model: str | None = None
    voice_mode: str | None = None
    asr_sidecar_url: str | None = None
    ollama_base_url: str | None = None
    polish_enabled: bool | None = None

    @field_validator("translate_lang_pair")
    @classmethod
    def _validate_translate_lang_pair(cls, value: str) -> str:
        return validate_lang_pair(value)



class ApiStatusResponse(BaseModel):
    """当前运行时状态。各能力是否就绪看 injected 与设置检查，不看 ok。"""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = Field(True, description="本接口响应成功；不代表各能力都已就绪。")
    service: Literal["ollama-voice-input"] = "ollama-voice-input"
    injected: dict[str, bool] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 产品固定模型与配置视图（API 只暴露这些字段，不回传旧密钥）
# ---------------------------------------------------------------------------

PRODUCT_OLLAMA_MODEL = "qwen3.5:4b"
PRODUCT_ASR_MODEL = "Qwen/Qwen3-ASR-1.7B"
OLLAMA_VERSION = "0.40.16"  # 设置页安装的官方 Ollama 发行版本（固定，安装目录与下载地址都由它派生）
DEFAULT_OLLAMA_BASE_URL = f"http://127.0.0.1:{DEFAULT_OLLAMA_PORT}"


class AppConfigView(BaseModel):
    """GET/POST /api/v1/config 的响应。只含产品可见字段；百度密钥、Whisper 等旧键留在磁盘但不回传。"""

    model_config = ConfigDict(extra="forbid")
    polish_enabled: bool
    custom_keywords: list[str]
    translate_lang_pair: str
    translate_auto_enabled: bool
    hotkey_voice: str
    hotkey_screenshot: str
    hotkey_ocr: str = "ctrl+alt+o"
    hotkey_repeat: str
    hotkey_annotate: str
    autostart_enabled: bool
    ui_theme: Literal["light", "dark"] = "light"
    audio_input_device: str = ""
    ollama_model: str = Field(description="产品固定纠错/翻译模型，只读")


class AppConfigUpdate(BaseModel):
    """POST /api/v1/config 请求体：唯一可写字段集合，未知字段拒绝。"""

    model_config = ConfigDict(extra="forbid")
    polish_enabled: bool | None = None
    custom_keywords: list[str] | None = None
    translate_lang_pair: str | None = Field(default=None, min_length=1)
    translate_auto_enabled: bool | None = None
    hotkey_voice: str | None = Field(default=None, min_length=1)
    hotkey_screenshot: str | None = Field(default=None, min_length=1)
    hotkey_ocr: str | None = Field(default=None, min_length=1)
    hotkey_repeat: str | None = Field(default=None, min_length=1)
    hotkey_annotate: str | None = Field(default=None, min_length=1)
    autostart_enabled: bool | None = None
    ui_theme: Literal["light", "dark"] | None = None
    audio_input_device: str | None = None


class AudioInputDevice(BaseModel):
    """单个可用麦克风输入设备（不暴露易随热插拔漂移的 PortAudio 整型 index）。"""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    hostapi: str = ""
    is_default: bool = False


class AudioInputListResponse(BaseModel):
    """GET /api/v1/audio-inputs 成功响应；0 麦返空列表，枚举/音频库异常单轨抛 dependency_failed。"""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    default_device: str | None = None
    devices: list[AudioInputDevice] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 看护层组件状态（应用主进程唯一来源，上报服务端缓存，设置页只读）
# ---------------------------------------------------------------------------


class ComponentStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: ComponentName
    state: ComponentState
    reason: str | None = Field(default=None, description="未安装缺什么 / 退避或停止的最后原因")
    failures: int = Field(default=0, ge=0, description="当前连续失败次数（运行满 60s 清零）")
    retry_at: str | None = Field(default=None, description="退避中时的下次拉起时间（ISO 8601）")
    pid: int | None = Field(default=None, ge=1)
    since: str = Field(description="进入当前状态的时间（ISO 8601）")


class ComponentReport(BaseModel):
    """POST /api/v1/components：整份快照，三个组件恰好各一项。"""

    model_config = ConfigDict(extra="forbid")
    components: list[ComponentStatus]

    @model_validator(mode="after")
    def _exactly_each_component_once(self) -> "ComponentReport":
        names = [item.name for item in self.components]
        if sorted(names) != sorted(ComponentName):
            raise ValueError(f"组件快照必须恰好包含 {[n.value for n in ComponentName]} 各一项，收到 {[n.value for n in names]}")
        return self


class ComponentReportAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True


class ComponentRetryAccepted(BaseModel):
    """POST /api/v1/components/{name}/retry：重试命令已经产品事件流转给应用看护层（是否拉起成功看后续组件状态）。"""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True


# ---------------------------------------------------------------------------
# 设置：真实状态检查与异步安装/下载任务
# ---------------------------------------------------------------------------


class SetupComponentState(StrEnum):
    missing = "missing"
    downloading = "downloading"
    ready = "ready"
    failed = "failed"


class SetupTaskKind(StrEnum):
    """安装任务种类（组件「启动」由看护层负责，不是任务）。"""

    asr_download = "asr_download"
    asr_runtime_install = "asr_runtime_install"
    ollama_install = "ollama_install"
    ollama_pull = "ollama_pull"


class SetupTaskState(StrEnum):
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    interrupted = "interrupted"  # worker 不在了（服务被结束、应用退出）：可清理后重试


class SetupProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    completed_bytes: int | None = Field(default=None, ge=0)
    total_bytes: int | None = Field(default=None, ge=0)
    percent: float | None = Field(default=None, ge=0, le=100)
    stage: str | None = None


class SetupTask(BaseModel):
    """一次真实安装/下载作业（在独立 worker 进程里执行）。succeeded 只在动作真实完成并复查通过后出现。"""

    model_config = ConfigDict(extra="forbid")
    task_id: str
    kind: SetupTaskKind
    state: SetupTaskState
    progress: SetupProgress | None = None
    message: str = ""
    error: ErrorCode | None = None
    started_at: str
    finished_at: str | None = None


class AsrSetupStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_name: str = PRODUCT_ASR_MODEL
    status: SetupComponentState
    weights_present: bool
    sidecar_running: bool
    size_label: str | None = None
    device_label: str | None = None
    progress: SetupProgress | None = None
    message: str = ""


class OllamaSetupStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_name: str = PRODUCT_OLLAMA_MODEL
    installed: bool
    version: str | None = None
    running: bool
    model_status: SetupComponentState
    size_label: str | None = None
    progress: SetupProgress | None = None
    message: str = ""


class InstallAllState(StrEnum):
    idle = "idle"
    running = "running"
    failed = "failed"
    done = "done"


class InstallStepKind(StrEnum):
    """一键装齐的五步，固定顺序。wait_ollama 不是任务：等看护层把 Ollama 报为 running。"""

    ollama_install = "ollama_install"
    wait_ollama = "wait_ollama"
    ollama_pull = "ollama_pull"
    asr_runtime_install = "asr_runtime_install"
    asr_download = "asr_download"


class InstallStepState(StrEnum):
    pending = "pending"
    running = "running"
    skipped = "skipped"  # 按事实已具备，不执行
    done = "done"
    failed = "failed"


class InstallStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: InstallStepKind
    state: InstallStepState
    message: str = ""
    progress: SetupProgress | None = None
    task_id: str | None = None


class InstallAllStatus(BaseModel):
    """POST /api/setup/install-all 的响应与 SetupCheckResponse.install_all：恒为五步、固定顺序。

    再次 POST = 从第一步重新判定（已具备的步 skipped），失败那步重新执行；运行中再次 POST 原样返回当前状态。
    """

    model_config = ConfigDict(extra="forbid")
    state: InstallAllState
    current: InstallStepKind | None = None
    steps: list[InstallStep]

    @model_validator(mode="after")
    def _five_steps_in_order(self) -> "InstallAllStatus":
        kinds = [step.kind for step in self.steps]
        if kinds != list(InstallStepKind):
            raise ValueError(f"一键装齐必须恰好按序包含 {[k.value for k in InstallStepKind]}，收到 {[k.value for k in kinds]}")
        return self


class SetupCapability(BaseModel):
    """系统能力项（剪贴板、Wayland portal 截图/快捷键、X11 maim、OCR 语言等）。"""

    model_config = ConfigDict(extra="forbid")
    id: str
    label: str
    available: bool
    required: bool = True
    detail: str | None = None


class SetupCheckResponse(BaseModel):
    """ok 只表示检查本身成功；组件是否就绪看 components / asr / ollama / capabilities。"""

    model_config = ConfigDict(extra="forbid")
    ok: Literal[True] = True
    session_type: str
    asr: AsrSetupStatus
    ollama: OllamaSetupStatus
    capabilities: list[SetupCapability]
    components: list[ComponentStatus] = Field(default_factory=list, description="看护层最新上报；尚未收到时为空")
    install_all: InstallAllStatus


# ---------------------------------------------------------------------------
# 手机局域网入口（仅本机签发）
# ---------------------------------------------------------------------------


class LanAccessEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ip: str
    url: str = Field(description="含 token 的手机录音页地址；只在本机响应中出现")
    url_masked: str
    qr_svg: str = Field(description="url 的二维码 SVG 文本")


class LanAccessResponse(BaseModel):
    """GET /api/v1/lan-access：仅本机。token 只供用户点击复制，不得写入日志或示例。"""

    model_config = ConfigDict(extra="forbid")
    port: int
    token: str
    token_masked: str
    entries: list[LanAccessEntry]
    message: str = ""


# ---------------------------------------------------------------------------
# Sidecar HTTP 路径（应用核心只通过这些路径访问 ASR，不 import torch/vLLM）
# ---------------------------------------------------------------------------

SIDECAR_DEFAULT_BASE_URL = f"http://127.0.0.1:{DEFAULT_ASR_PORT}"
SIDECAR_TRANSCRIBE_PATH = "/v1/transcribe"
SIDECAR_SESSIONS_PATH = "/v1/sessions"
SIDECAR_CHUNK_HEADER = "X-Chunk-Index"

# 流式长录音刷新（原 P2）已按用户口径验收（2026-11-11 03:03 America/New_York，证据
# v3-asr-r9-candidate-acceptance.md / v3-asr-r9-default-recheck.md：默认 0.4/5/5，真实 TLS uvicorn→provider→1.7B GPU，
# 六条近 120s 连续说话最大间隔 0.749–1.226s）。旧 p95=1.515s/1.966s 阻塞文案已由该证据关闭。
# 以下为仍然成立的限制（health.unmet_blockers 原样发布），不得据此宣称 ASR 全部能力或产品已可用。
ASR_STREAMING_BLOCKERS = (
    "有声硬帽字符串拼接会漏字/重复；不得把跨窗 suffix/prefix 合并当成正确。",
    "刷新仅按用户口径验收（通常 0.5–1s，词句交界偶发约 1.5s；r9 默认 0.4/5/5 六条近 120s 连续说话最大 0.749–1.226s），"
    "不等于严格 p95≤1s；超过 120s 单会话不支持；与 qwen3.5:4b 同机并存、真实设备/WebKit 未验收。",
    "长串同句重复计数可能多或少一次（模型识别错误；r10：九点×21 在 0.4/5/5 稳定多一句，属切分参数相关），"
    "不承诺零错，也不据此推出一般准确率退化。",
)


def contract_models() -> tuple[type[BaseModel], ...]:
    """所有需要进入 OpenAPI components.schemas 的模型（含两类 SSE）。"""
    return (
        ErrorBody,
        RecognizeSessionCreate,
        RecognizeSessionCreated,
        ChunkAccepted,
        SessionPartialEvent,
        SessionFinalEvent,
        SessionErrorEvent,
        SessionClosedEvent,
        SessionSSEEventEnvelope,
        ProductIdleEvent,
        ProductRecordingEvent,
        ProductPartialEvent,
        ProductPolishingEvent,
        ProductTypedEvent,
        ProductErrorEvent,
        ProductBusyEvent,
        ProductHistoryWriteFailedEvent,
        ProductDeliveryUnknownEvent,
        ProductComponentCommandEvent,
        ProductConfigChangedEvent,
        ProductSSEEventEnvelope,
        PlatformEventCreate,
        PlatformEventAccepted,
        UtteranceCreate,
        UtteranceFromAudio,
        UtteranceAccepted,
        UtteranceResendRequest,
        UtteranceResendQueued,
        UtteranceResendNeedsConfirmation,
        JobClaimRequest,
        JobClaimed,
        JobAckRequest,
        JobAckAccepted,
        JobRecord,
        JobStoreDocument,
        JobQueryFound,
        JobQueryUnknown,
        JobQueryResponseEnvelope,
        SttRequest,
        SttResponse,
        PolishRequest,
        PolishResponse,
        TranslateRequest,
        TranslateResponse,
        OcrTranslateResponse,
        OcrTextLine,
        OcrTextBlock,
        OcrInplaceResponse,
        HistoryRecord,
        HistoryListResponse,
        AppConfig,
        AppConfigView,
        AppConfigUpdate,
        AudioInputDevice,
        AudioInputListResponse,
        ApiStatusResponse,
        ComponentStatus,
        ComponentReport,
        ComponentReportAccepted,
        ComponentRetryAccepted,
        SetupProgress,
        SetupTask,
        InstallStep,
        InstallAllStatus,
        AsrSetupStatus,
        OllamaSetupStatus,
        SetupCapability,
        SetupCheckResponse,
        LanAccessEntry,
        LanAccessResponse,
    )


def build_openapi_skeleton() -> dict[str, Any]:
    """由模型生成 OpenAPI 3 骨架：只有 components.schemas，没有虚构已实现的 paths。

    集成阶段必须再与 FastAPI 导出的 /openapi.json 交叉核对；本函数不宣称 API 全通过。
    """
    from pydantic.json_schema import models_json_schema

    _, tree = models_json_schema(
        [(model, "validation") for model in contract_models()],
        ref_template="#/components/schemas/{model}",
    )
    defs = tree.get("$defs") or tree.get("definitions") or {}
    schemas = {name: defs[name] for name in sorted(defs)}
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "ollama-voice-input v1 contracts",
            "version": "0.0.0-l1",
            "description": (
                "Generated from _contracts.py. Paths are intentionally empty until "
                "routes exist. SSE payloads are in components.schemas."
            ),
        },
        "paths": {},
        "components": {"schemas": schemas},
    }


__all__ = [
    "ComponentName",
    "ComponentReport",
    "ComponentReportAccepted",
    "ComponentRetryAccepted",
    "ComponentState",
    "ComponentStatus",
    "ProductComponentCommandEvent",
    "ProductConfigChangedEvent",
    "AppConfigView",
    "AppConfigUpdate",
    "AudioInputDevice",
    "AudioInputListResponse",
    "AsrSetupStatus",
    "LanAccessEntry",
    "LanAccessResponse",
    "OllamaSetupStatus",
    "PRODUCT_ASR_MODEL",
    "PRODUCT_OLLAMA_MODEL",
    "SetupCapability",
    "SetupCheckResponse",
    "SetupComponentState",
    "InstallAllState",
    "InstallAllStatus",
    "InstallStep",
    "InstallStepKind",
    "InstallStepState",
    "SetupProgress",
    "SetupTask",
    "SetupTaskKind",
    "SetupTaskState",
    "ASR_STREAMING_BLOCKERS",
    "AckResult",
    "ApiStatusResponse",
    "AppConfig",
    "ChunkAccepted",
    "DEFAULT_OLLAMA_BASE_URL",
    "DEFAULT_TRANSLATE_LANG_PAIR",
    "OLLAMA_VERSION",
    "OcrTranslateMode",
    "TRANSLATE_LANG_PAIRS",
    "TranslateDirectionMode",
    "validate_direction_override",
    "validate_lang_pair",
    "ErrorBody",
    "ErrorCode",
    "HistoryListResponse",
    "HistoryPolish",
    "HistoryRecord",
    "JobAckAccepted",
    "JobAckRequest",
    "JobClaimRequest",
    "JobClaimed",
    "JobQueryFound",
    "JobQueryResponse",
    "JobQueryResponseEnvelope",
    "JobQueryUnknown",
    "JobRecord",
    "JobState",
    "JobStoreDocument",
    "LogicError",
    "OcrInplaceResponse",
    "OcrTextBlock",
    "OcrTextLine",
    "OcrTranslateResponse",
    "PolishRequest",
    "PolishResponse",
    "ProductBusyEvent",
    "ProductDeliveryUnknownEvent",
    "ProductErrorEvent",
    "ProductEventType",
    "ProductHistoryWriteFailedEvent",
    "ProductIdleEvent",
    "ProductPartialEvent",
    "ProductPolishingEvent",
    "ProductRecordingEvent",
    "ProductSSEEvent",
    "ProductSSEEventEnvelope",
    "ProductTypedEvent",
    "RecognizeSessionCreate",
    "RecognizeSessionCreated",
    "SIDECAR_CHUNK_HEADER",
    "SIDECAR_DEFAULT_BASE_URL",
    "SIDECAR_SESSIONS_PATH",
    "SIDECAR_TRANSCRIBE_PATH",
    "SessionClosedEvent",
    "SessionClosedReason",
    "SessionErrorEvent",
    "SessionEventType",
    "SessionFinalEvent",
    "SessionPartialEvent",
    "SessionSSEEvent",
    "SessionSSEEventEnvelope",
    "SttRequest",
    "SttResponse",
    "TranslateRequest",
    "TranslateResponse",
    "UtteranceAccepted",
    "UtteranceCreate",
    "UtteranceFromAudio",
    "UtteranceResendNeedsConfirmation",
    "UtteranceResendQueued",
    "UtteranceResendRequest",
    "UtteranceSource",
    "build_openapi_skeleton",
    "contract_models",
]
