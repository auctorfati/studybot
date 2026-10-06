"""Распознавание речи: только превращает голосовое в текст.

Сервис — Deepgram ([stt] provider = "deepgram"); без него
работает заглушка: голосовой режим не включается, бот живёт в текстовом режиме
(текстовый режим работает целиком без сервисов распознавания и
озвучки). Озвучка — deepgram.py и voicing.py.
"""

from __future__ import annotations

from typing import Protocol


class STTUnavailable(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason     # 'not_configured', 'network', 'provider'


class STT(Protocol):
    name: str
    configured: bool

    async def transcribe(self, data: bytes, lang: str) -> str:
        """ogg/opus из Telegram → текст. lang: 'en' или 'ru'."""


class StubSTT:
    name = "stub"
    configured = False

    async def transcribe(self, data: bytes, lang: str) -> str:
        raise STTUnavailable("not_configured", "распознавание не подключено")


class FakeSTT:
    """Для тестов: отдаёт заготовленные расшифровки по очереди."""

    name = "fake"
    configured = True

    def __init__(self, *texts: str | Exception) -> None:
        self.texts = list(texts)
        self.calls: list[tuple[int, str]] = []

    async def transcribe(self, data: bytes, lang: str) -> str:
        self.calls.append((len(data), lang))
        if not self.texts:
            raise STTUnavailable("provider", "подставному распознаванию нечего ответить")
        t = self.texts.pop(0)
        if isinstance(t, Exception):
            raise t
        return t


class STTConfigError(ValueError):
    pass


class DeepgramSTT:
    """Deepgram Nova-3: английский для английского, русский для ответов по психологии."""

    name = "deepgram"
    configured = True

    def __init__(self, dg) -> None:
        self.dg = dg

    async def transcribe(self, data: bytes, lang: str) -> str:
        from .deepgram import DeepgramError
        try:
            return await self.dg.listen(data, lang)
        except DeepgramError as exc:
            raise STTUnavailable(exc.reason, str(exc)) from exc


def make_stt(provider: str, cfg=None) -> STT:
    if provider == "stub":
        return StubSTT()
    if provider == "deepgram" and cfg is not None:
        from .deepgram import Deepgram
        return DeepgramSTT(Deepgram(cfg.deepgram))
    raise STTConfigError(f"неизвестный сервис распознавания: {provider}")


def make_tts(cfg):
    """Озвучка во время работы (новое после импорта, живые реплики критерия выхода): Deepgram или None."""
    if cfg.tts_provider == "deepgram":
        from .deepgram import Deepgram
        return Deepgram(cfg.deepgram)
    return None
