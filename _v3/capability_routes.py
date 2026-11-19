"""能力型 API：只转写 / 润色 / 翻译 / OCR+翻译 / 原位 OCR。产品流不得用这些接口入队。"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile

from _contracts import (
    ErrorCode,
    LogicError,
    OcrInplaceResponse,
    OcrTranslateResponse,
    PolishRequest,
    PolishResponse,
    SttResponse,
    TranslateRequest,
    TranslateResponse,
)
from _v3.limits import AUDIO_UPLOAD_NOTE, IMAGE_UPLOAD_NOTE, read_single_form_file
from _v3.runtime import Runtime, decode_uploaded_audio


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

    @router.post("/api/v1/ocr-translate", response_model=OcrTranslateResponse, description=IMAGE_UPLOAD_NOTE)
    async def ocr_translate(
        request: Request,
        target_lang: str = Form(...),
        lang: str | None = Form(default=None),
        image: UploadFile | None = File(default=None),
    ) -> OcrTranslateResponse:
        del image
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
        ocr_text = await asyncio.to_thread(runtime.ocr, payload, **ocr_kwargs)
        if not (ocr_text or "").strip():
            raise LogicError(ErrorCode.ocr_failed, "OCR 未识别到文字")
        if target_lang.strip().lower() == "raw":
            return OcrTranslateResponse(text=ocr_text, ocr_text=ocr_text, target_lang="raw")
        if runtime.translate is None:
            raise LogicError(ErrorCode.translate_failed, "翻译能力不可用")
        translated = await asyncio.to_thread(
            runtime.translate,
            TranslateRequest(text=ocr_text, target_lang=target_lang),
        )
        return OcrTranslateResponse(text=translated.text, ocr_text=ocr_text, target_lang=target_lang)

    @router.post("/api/v1/ocr-inplace", response_model=OcrInplaceResponse, description=IMAGE_UPLOAD_NOTE)
    async def ocr_inplace(
        request: Request,
        target_lang: str = Form(...),
        lang: str | None = Form(default=None),
        image: UploadFile | None = File(default=None),
    ) -> OcrInplaceResponse:
        del image
        if runtime.ocr_layout is None:
            raise LogicError(ErrorCode.ocr_failed, "OCR 原位排版能力不可用")
        norm_target = (target_lang or "").strip()
        if not norm_target:
            raise LogicError(ErrorCode.invalid_request, "target_lang 不能为空")
        payload = await read_single_form_file(
            request,
            field="image",
            limit=runtime.max_upload_bytes,
            missing="必须提供图片",
        )
        ocr_kwargs: dict[str, Any] = {}
        if lang:
            ocr_kwargs["lang"] = lang
        width, height, ocr_text, blocks = await asyncio.to_thread(
            runtime.ocr_layout,
            payload,
            **ocr_kwargs,
        )
        clean_ocr = (ocr_text or "").strip()
        if not clean_ocr and not blocks:
            raise LogicError(ErrorCode.ocr_failed, "OCR 未识别到文字")
        if not clean_ocr and blocks:
            clean_ocr = "\n".join(b.ocr_text for b in blocks if b.ocr_text.strip()).strip()
            if not clean_ocr:
                raise LogicError(ErrorCode.ocr_failed, "OCR 未识别到文字")
        if norm_target.lower() == "raw":
            raw_blocks = [
                block.model_copy(update={"translated_text": block.ocr_text})
                for block in blocks
            ]
            return OcrInplaceResponse(
                text=clean_ocr,
                ocr_text=clean_ocr,
                target_lang="raw",
                image_width=width,
                image_height=height,
                blocks=raw_blocks,
            )
        if blocks:
            if runtime.translate_blocks is None:
                raise LogicError(ErrorCode.translate_failed, "分块翻译能力不可用")
            translated_blocks = await asyncio.to_thread(
                runtime.translate_blocks,
                blocks,
                target_lang=norm_target,
            )
            full_text = "\n".join(
                block.translated_text for block in translated_blocks if block.translated_text.strip()
            ).strip()
            if not full_text:
                raise LogicError(ErrorCode.translate_failed, "译文为空，不得把原文当作译文")
            return OcrInplaceResponse(
                text=full_text,
                ocr_text=clean_ocr,
                target_lang=norm_target,
                image_width=width,
                image_height=height,
                blocks=translated_blocks,
            )
        if runtime.translate is None:
            raise LogicError(ErrorCode.translate_failed, "翻译能力不可用")
        translated = await asyncio.to_thread(
            runtime.translate,
            TranslateRequest(text=clean_ocr, target_lang=norm_target),
        )
        return OcrInplaceResponse(
            text=translated.text,
            ocr_text=clean_ocr,
            target_lang=norm_target,
            image_width=width,
            image_height=height,
            blocks=[],
        )

    return router
