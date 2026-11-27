"""能力型 API：只转写 / 润色 / 翻译 / OCR 预处理预览 / OCR+翻译 / 原位 OCR。产品流不得用这些接口入队。"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError

from _contracts import (
    DEFAULT_TRANSLATE_LANG_PAIR,
    ErrorCode,
    LogicError,
    OcrInplaceResponse,
    OcrPreprocessParams,
    OcrPreprocessPreviewResponse,
    OcrTranslateMode,
    OcrTranslateResponse,
    PolishRequest,
    PolishResponse,
    SttResponse,
    TranslateRequest,
    TranslateResponse,
    validate_direction_override,
)
from _ocr_text import join_block_texts
from _translate import resolve_translation_direction
from _v3.http import validation_error_message
from _v3.limits import AUDIO_UPLOAD_NOTE, IMAGE_UPLOAD_NOTE, read_single_form_file
from _v3.runtime import Runtime, decode_uploaded_audio


def _validate_ocr_direction(
    lang_pair: str,
    source_lang: str | None,
    target_lang: str | None,
) -> tuple[str, tuple[str, str] | None]:
    try:
        return validate_direction_override(lang_pair, source_lang, target_lang)
    except ValueError as exc:
        raise LogicError(ErrorCode.invalid_request, str(exc)) from exc


def _reject_json_constant(token: str) -> Any:
    raise ValueError(f"preprocess_params 含非法数值常量: {token}")


def _parse_preprocess_params(raw: str | None) -> OcrPreprocessParams | None:
    """解析可选 JSON 表单字段 preprocess_params：None/空白视为未提供；拒 NaN/Infinity 与非法字段。"""
    if raw is None or not raw.strip():
        return None
    try:
        parsed = json.loads(raw, parse_constant=_reject_json_constant)
    except ValueError as exc:
        raise LogicError(ErrorCode.invalid_request, f"preprocess_params 必须是合法 JSON 对象: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LogicError(ErrorCode.invalid_request, "preprocess_params 必须是 JSON 对象")
    try:
        return OcrPreprocessParams.model_validate(parsed)
    except ValidationError as exc:
        errors = list(exc.errors())
        raise LogicError(
            ErrorCode.invalid_request,
            validation_error_message(errors),
            details={"errors": jsonable_encoder(errors, custom_encoder={Exception: str})},
        ) from exc


def _enabled_ocr_presets(runtime: Runtime):
    return tuple(preset for preset in runtime.config.load().ocr_preprocess_presets if preset.enabled)


def build_capability_router(runtime: Runtime) -> APIRouter:
    router = APIRouter(tags=["capabilities"])

    @router.post("/api/v1/stt", response_model=SttResponse, description=AUDIO_UPLOAD_NOTE)
    async def stt(
        request: Request,
        language: str | None = Form(default=None),
        context: str | None = Form(default=None),
        audio: UploadFile | None = File(default=None),
    ) -> SttResponse:
        upload_ct = audio.content_type if audio is not None else None
        upload_name = audio.filename if audio is not None else None
        payload = await read_single_form_file(
            request,
            field="audio",
            limit=runtime.max_upload_bytes,
            missing="必须提供音频文件",
        )
        pcm = await asyncio.to_thread(
            decode_uploaded_audio,
            runtime,
            payload,
            content_type=upload_ct,
            filename=upload_name,
        )
        return await asyncio.to_thread(
            runtime.utterance.transcribe_public,
            pcm,
            language=language,
            context=context,
        )

    @router.post("/api/v1/polish", response_model=PolishResponse)
    async def polish(body: PolishRequest) -> PolishResponse:
        if runtime.polish is None:
            raise LogicError(ErrorCode.polish_failed, "润色能力不可用")
        return await asyncio.to_thread(runtime.polish, body)

    @router.post("/api/v1/translate", response_model=TranslateResponse)
    async def translate(body: TranslateRequest) -> TranslateResponse:
        if runtime.translate is None:
            raise LogicError(ErrorCode.translate_failed, "翻译能力不可用")
        return await asyncio.to_thread(runtime.translate, body)

    @router.post(
        "/api/v1/ocr/preview",
        response_model=OcrPreprocessPreviewResponse,
        operation_id="ocr_preview_api_v1_ocr_preview_post",
        description=IMAGE_UPLOAD_NOTE,
    )
    async def ocr_preview(
        request: Request,
        preprocess_params: str | None = Form(default=None),
        image: UploadFile | None = File(default=None),
    ) -> OcrPreprocessPreviewResponse:
        del image
        parsed_params = _parse_preprocess_params(preprocess_params)
        if runtime.ocr_preview is None:
            raise LogicError(ErrorCode.ocr_failed, "OCR 预处理预览能力不可用")
        payload = await read_single_form_file(
            request,
            field="image",
            limit=runtime.max_upload_bytes,
            missing="必须提供图片",
        )
        return await asyncio.to_thread(runtime.ocr_preview, payload, parsed_params)

    @router.post("/api/v1/ocr-translate", response_model=OcrTranslateResponse, description=IMAGE_UPLOAD_NOTE)
    async def ocr_translate(
        request: Request,
        mode: OcrTranslateMode = Form(default=OcrTranslateMode.translate),
        lang_pair: str = Form(default=DEFAULT_TRANSLATE_LANG_PAIR),
        source_lang: str | None = Form(default=None),
        target_lang: str | None = Form(default=None),
        lang: str | None = Form(default=None),
        preprocess_params: str | None = Form(default=None),
        image: UploadFile | None = File(default=None),
    ) -> OcrTranslateResponse:
        del image
        norm_pair = DEFAULT_TRANSLATE_LANG_PAIR
        override: tuple[str, str] | None = None
        if mode == OcrTranslateMode.translate:
            norm_pair, override = _validate_ocr_direction(lang_pair, source_lang, target_lang)
        parsed_params = _parse_preprocess_params(preprocess_params)
        if runtime.ocr is None:
            raise LogicError(ErrorCode.ocr_failed, "OCR 能力不可用")
        payload = await read_single_form_file(
            request,
            field="image",
            limit=runtime.max_upload_bytes,
            missing="必须提供图片",
        )
        ocr_kwargs: dict[str, Any] = {}
        if lang:
            ocr_kwargs["lang"] = lang
        if parsed_params is not None:
            ocr_kwargs["preprocess_params"] = parsed_params
        else:
            ocr_kwargs["presets"] = _enabled_ocr_presets(runtime)
        ocr_result = await asyncio.to_thread(runtime.ocr, payload, **ocr_kwargs)
        ocr_text = ocr_result.text
        ocr_meta = ocr_result.meta
        if not (ocr_text or "").strip():
            raise LogicError(ErrorCode.ocr_failed, "OCR 未识别到文字")
        if mode == OcrTranslateMode.raw:
            return OcrTranslateResponse(
                text=ocr_text,
                ocr_text=ocr_text,
                mode=OcrTranslateMode.raw,
                ocr_meta=ocr_meta,
            )
        if runtime.translate is None:
            raise LogicError(ErrorCode.translate_failed, "翻译能力不可用")
        direction = resolve_translation_direction(
            ocr_text,
            lang_pair=norm_pair,
            source_lang=override[0] if override else None,
            target_lang=override[1] if override else None,
        )
        translated = await asyncio.to_thread(
            runtime.translate,
            TranslateRequest(
                text=ocr_text,
                lang_pair=direction.lang_pair,
                source_lang=direction.source_lang,
                target_lang=direction.target_lang,
            ),
        )
        return OcrTranslateResponse(
            text=translated.text,
            ocr_text=ocr_text,
            mode=OcrTranslateMode.translate,
            lang_pair=direction.lang_pair,
            source_lang=direction.source_lang,
            target_lang=direction.target_lang,
            direction_mode=direction.mode,
            chunks=translated.chunks,
            ocr_meta=ocr_meta,
        )

    @router.post("/api/v1/ocr-inplace", response_model=OcrInplaceResponse, description=IMAGE_UPLOAD_NOTE)
    async def ocr_inplace(
        request: Request,
        mode: OcrTranslateMode = Form(default=OcrTranslateMode.translate),
        lang_pair: str = Form(default=DEFAULT_TRANSLATE_LANG_PAIR),
        source_lang: str | None = Form(default=None),
        target_lang: str | None = Form(default=None),
        lang: str | None = Form(default=None),
        image: UploadFile | None = File(default=None),
    ) -> OcrInplaceResponse:
        del image
        norm_pair = DEFAULT_TRANSLATE_LANG_PAIR
        override: tuple[str, str] | None = None
        if mode == OcrTranslateMode.translate:
            norm_pair, override = _validate_ocr_direction(lang_pair, source_lang, target_lang)
        if runtime.ocr_layout is None:
            raise LogicError(ErrorCode.ocr_failed, "OCR 原位排版能力不可用")
        payload = await read_single_form_file(
            request,
            field="image",
            limit=runtime.max_upload_bytes,
            missing="必须提供图片",
        )
        ocr_kwargs: dict[str, Any] = {
            "presets": _enabled_ocr_presets(runtime),
        }
        if lang:
            ocr_kwargs["lang"] = lang
        # ocr_layout 的契约：识别不到文字即抛 ocr_failed，否则块非空、全文由块派生（块间空一行）
        layout_result = await asyncio.to_thread(
            runtime.ocr_layout,
            payload,
            **ocr_kwargs,
        )
        width = layout_result.image_width
        height = layout_result.image_height
        ocr_text = layout_result.ocr_text
        blocks = layout_result.blocks
        ocr_meta = layout_result.meta
        if mode == OcrTranslateMode.raw:
            blocks = [block.model_copy(update={"translated_text": block.ocr_text}) for block in blocks]
            return OcrInplaceResponse(
                text=join_block_texts([block.translated_text for block in blocks]),
                ocr_text=ocr_text,
                mode=OcrTranslateMode.raw,
                image_width=width,
                image_height=height,
                blocks=blocks,
                ocr_meta=ocr_meta,
            )
        if runtime.translate_blocks is None:
            raise LogicError(ErrorCode.translate_failed, "分块翻译能力不可用")
        direction = resolve_translation_direction(
            ocr_text,
            lang_pair=norm_pair,
            source_lang=override[0] if override else None,
            target_lang=override[1] if override else None,
        )
        blocks = await asyncio.to_thread(
            runtime.translate_blocks,
            blocks,
            source_lang=direction.source_lang,
            target_lang=direction.target_lang,
            lang_pair=direction.lang_pair,
        )
        return OcrInplaceResponse(
            text=join_block_texts([block.translated_text for block in blocks]),
            ocr_text=ocr_text,
            mode=OcrTranslateMode.translate,
            lang_pair=direction.lang_pair,
            source_lang=direction.source_lang,
            target_lang=direction.target_lang,
            direction_mode=direction.mode,
            image_width=width,
            image_height=height,
            blocks=blocks,
            ocr_meta=ocr_meta,
        )

    return router
