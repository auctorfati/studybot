"""Deepgram: распознавание (Nova-3) и озвучка (Aura-2).

Ключ — в [deepgram] конфига. Распознавание: голосовое из Telegram (ogg/opus) → текст, язык задаёт
бот (en или ru). Озвучка: текст → ogg/opus для голосового сообщения; текст длиннее предела запроса
(2 000 знаков) режется по предложениям, куски склеиваются в mp3 — длинная запись идёт аудиофайлом
с перемоткой. Скорость медленной версии делает сама модель (0,7–1,5), речь остаётся естественной.
"""

from __future__ import annotations

import json
import re

import httpx

from .config import DeepgramConfig

CHUNK = 1900                       # предел запроса озвучки — 2 000 знаков, с запасом
TTS_PRICE_PER_1K = 0.030           # Aura-2, долларов за 1 000 знаков (прайс Deepgram, 05.10.2026)


class DeepgramError(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason       # 'not_configured', 'network', 'provider'


def split_text(text: str, limit: int = CHUNK) -> list[str]:
    """Куски не длиннее предела, по границам предложений (длинное предложение — по словам)."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return [text] if text else []
    out, cur = [], ""
    for sent in re.split(r"(?<=[.!?…])\s+", text):
        pieces = [sent]
        if len(sent) > limit:
            pieces, w = [], ""
            for word in sent.split(" "):
                if w and len(w) + 1 + len(word) > limit:
                    pieces.append(w)
                    w = word
                else:
                    w = f"{w} {word}" if w else word
            pieces.append(w)
        for piece in pieces:
            if cur and len(cur) + 1 + len(piece) > limit:
                out.append(cur)
                cur = piece
            else:
                cur = f"{cur} {piece}" if cur else piece
    if cur:
        out.append(cur)
    return out


class Deepgram:
    def __init__(self, cfg: DeepgramConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cfg = cfg
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.cfg.api_key)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.cfg.base_url.rstrip("/") + "/", timeout=self.cfg.timeout_sec,
                                 transport=self.transport,
                                 headers={"Authorization": f"Token {self.cfg.api_key}"})

    async def _post(self, path: str, params: dict, content: bytes, ctype: str) -> httpx.Response:
        if not self.configured:
            raise DeepgramError("not_configured", "Deepgram: нет ключа в [deepgram]")
        try:
            async with self._client() as c:
                r = await c.post(path, params=params, content=content, headers={"Content-Type": ctype})
        except httpx.HTTPError as exc:
            raise DeepgramError("network", f"Deepgram: сеть ({type(exc).__name__})") from exc
        if r.status_code != 200:
            raise DeepgramError("provider", f"Deepgram: HTTP {r.status_code} {r.text[:200]}")
        return r

    async def listen(self, data: bytes, lang: str, mime: str = "audio/ogg") -> str:
        """Голосовое → текст. Пустая расшифровка — пустая строка (тишина или шум)."""
        r = await self._post("listen", {"model": self.cfg.stt_model, "language": "ru" if lang == "ru" else "en",
                                        "punctuate": "true"}, data, mime)
        try:
            alt = r.json()["results"]["channels"][0]["alternatives"][0]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise DeepgramError("provider", "Deepgram: ответ распознавания без расшифровки") from exc
        return str(alt.get("transcript") or "").strip()

    async def speak(self, text: str, voice: str | None = None, speed: float = 1.0) -> tuple[bytes, str]:
        """Текст → (аудио, расширение): один кусок — ogg/opus, несколько — склейка mp3."""
        chunks = split_text(text)
        if not chunks:
            raise DeepgramError("provider", "Deepgram: пустой текст для озвучки")
        ogg = len(chunks) == 1
        params = {"model": voice or self.cfg.voice}
        params.update({"encoding": "opus", "container": "ogg"} if ogg else {"encoding": "mp3"})
        if abs(speed - 1.0) > 1e-6:
            params["speed"] = f"{speed:g}"
        out = b""
        for chunk in chunks:
            r = await self._post("speak", params, json.dumps({"text": chunk}).encode("utf-8"), "application/json")
            out += r.content
        return out, ("ogg" if ogg else "mp3")
