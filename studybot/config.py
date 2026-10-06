"""Конфиг: статичное и секретное (токены, пути, модели, цены).

Всё, что ученик меняет из бота (время уведомления, лимиты, режимы),
живёт не здесь, а в таблице settings — модуль settings.py.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover — Python 3.10 на сервере
    import tomli as tomllib  # type: ignore[no-redef]

from .clock import StudyCalendar, parse_hhmm


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Paths:
    root: Path
    data_dir: Path
    content_dir: Path
    sources_dir: Path
    prompts_dir: Path

    @property
    def db_file(self) -> Path:
        return self.data_dir / "studybot.sqlite3"

    @property
    def audio_dir(self) -> Path:
        return self.data_dir / "audio"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def lock_file(self) -> Path:
        return self.data_dir / "studybot.lock"

    def ensure(self) -> None:
        for p in (self.data_dir, self.content_dir, self.sources_dir,
                  self.prompts_dir, self.audio_dir, self.backup_dir):
            p.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class LLMPrices:
    cheap_input: float = 0.0
    cheap_output: float = 0.0
    flagship_input: float = 0.0
    flagship_output: float = 0.0


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    base_url: str
    api_key: str
    cheap_model: str
    flagship_model: str
    daily_budget_usd: float
    prices: LLMPrices
    timeout_sec: float = 60.0
    json_mode: bool = True
    temperature: float | None = None
    max_tokens: int = 1500
    # по уровню модели: поля, добавляемые в запрос как есть ([llm.cheap_extra], [llm.flagship_extra]),
    # и предел ответа и ожидание флагмана — у моделей с рассуждением оно идёт из того же предела
    cheap_extra: dict = field(default_factory=dict)
    flagship_extra: dict = field(default_factory=dict)
    flagship_max_tokens: int | None = None
    flagship_timeout_sec: float | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.cheap_model and self.flagship_model)


@dataclass(frozen=True)
class DeepgramConfig:
    """Распознавание и озвучка Deepgram ([deepgram]): ключ, модели, голоса."""
    api_key: str = ""
    base_url: str = "https://api.deepgram.com/v1"
    stt_model: str = "nova-3"
    voice: str = "aura-2-thalia-en"            # фразы, реплики сценариев, живые реплики критерия выхода
    record_voices: tuple[str, ...] = ("aura-2-thalia-en", "aura-2-helena-en", "aura-2-luna-en",
                                      "aura-2-apollo-en", "aura-2-arcas-en", "aura-2-odysseus-en")
    slow_speed: float = 0.8                    # медленная версия, модель держит 0,7–1,5
    timeout_sec: float = 60.0


@dataclass(frozen=True)
class BackupConfig:
    time: str = "03:30"
    keep: int = 14
    weekly_to_telegram: bool = True


@dataclass(frozen=True)
class Config:
    token: str
    owner_id: int
    paths: Paths
    timezone: str
    day_boundary: str
    llm: LLMConfig
    stt_provider: str
    tts_provider: str
    backup: BackupConfig
    deepgram: DeepgramConfig = field(default_factory=DeepgramConfig)
    source_file: Path | None = field(default=None, compare=False)

    def calendar(self) -> StudyCalendar:
        return StudyCalendar.from_strings(self.timezone, self.day_boundary)

    def require_telegram(self) -> None:
        """Проверка перед запуском бота; тесты и импорт без Telegram её не вызывают."""
        if not self.token:
            raise ConfigError("telegram.token пуст")
        if self.owner_id <= 0:
            raise ConfigError("telegram.owner_id не задан")


def _section(raw: dict, name: str) -> dict:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] должен быть разделом")
    return value


def load_config(path: str | os.PathLike) -> Config:
    path = Path(path).resolve()
    if not path.exists():
        raise ConfigError(f"нет файла конфига: {path}")
    with path.open("rb") as fh:
        try:
            raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"ошибка разбора {path.name}: {exc}") from exc

    root = path.parent
    tg = _section(raw, "telegram")
    pth = _section(raw, "paths")
    tm = _section(raw, "time")
    llm = _section(raw, "llm")
    prices = llm.get("prices", {})
    bk = _section(raw, "backup")

    def p(key: str, default: str) -> Path:
        value = Path(pth.get(key, default))
        return value if value.is_absolute() else root / value

    paths = Paths(root, p("data_dir", "data"), p("content_dir", "content"),
                  p("sources_dir", "sources"), p("prompts_dir", "prompts"))

    tz = str(tm.get("timezone", "Europe/Moscow"))
    boundary = str(tm.get("day_boundary", "03:00"))
    try:
        StudyCalendar.from_strings(tz, boundary)
    except Exception as exc:  # неизвестный пояс или кривое время
        raise ConfigError(f"[time]: {exc}") from exc

    try:
        owner_id = int(tg.get("owner_id", 0))
    except (TypeError, ValueError) as exc:
        raise ConfigError("telegram.owner_id должен быть числом") from exc

    try:
        budget = float(llm.get("daily_budget_usd", 0.5))
        price_obj = LLMPrices(**{k: float(v) for k, v in prices.items()})
    except TypeError as exc:
        raise ConfigError(f"[llm.prices]: неизвестный ключ ({exc})") from exc
    except ValueError as exc:
        raise ConfigError(f"[llm]: число задано неверно ({exc})") from exc
    if budget < 0:
        raise ConfigError("llm.daily_budget_usd не может быть отрицательным")
    extras = {}
    for tier in ("cheap", "flagship"):
        extra = llm.get(f"{tier}_extra", {})
        if not isinstance(extra, dict):
            raise ConfigError(f"llm.{tier}_extra должен быть таблицей: [llm.{tier}_extra]")
        reserved = sorted(set(extra) & {"model", "messages", "max_tokens", "response_format", "temperature"})
        if reserved:
            raise ConfigError(f"[llm.{tier}_extra]: эти поля задаются в [llm], а не здесь: {', '.join(reserved)}")
        extras[tier] = dict(extra)
    try:
        fl_max = int(llm["flagship_max_tokens"]) if "flagship_max_tokens" in llm else None
        fl_timeout = float(llm["flagship_timeout_sec"]) if "flagship_timeout_sec" in llm else None
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"[llm]: число задано неверно ({exc})") from exc

    dg = _section(raw, "deepgram")
    try:
        voices = dg.get("record_voices", list(DeepgramConfig.record_voices))
        dg_cfg = DeepgramConfig(
            api_key=str(dg.get("api_key", "")), base_url=str(dg.get("base_url", DeepgramConfig.base_url)),
            stt_model=str(dg.get("stt_model", DeepgramConfig.stt_model)),
            voice=str(dg.get("voice", DeepgramConfig.voice)),
            record_voices=tuple(str(v) for v in voices) or (DeepgramConfig.voice,),
            slow_speed=float(dg.get("slow_speed", DeepgramConfig.slow_speed)),
            timeout_sec=float(dg.get("timeout_sec", DeepgramConfig.timeout_sec)))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"[deepgram]: значение задано неверно ({exc})") from exc
    if not 0.7 <= dg_cfg.slow_speed <= 1.5:
        raise ConfigError("deepgram.slow_speed: от 0.7 до 1.5")
    stt_provider = str(_section(raw, "stt").get("provider", "stub"))
    tts_provider = str(_section(raw, "tts").get("provider", "stub"))
    for name, prov in (("stt", stt_provider), ("tts", tts_provider)):
        if prov not in ("stub", "deepgram"):
            raise ConfigError(f"[{name}] provider: stub или deepgram, не {prov!r}")
        if prov == "deepgram" and not dg_cfg.api_key:
            raise ConfigError(f"[{name}] provider = deepgram, но в [deepgram] нет api_key")

    backup_time = str(bk.get("time", "03:30"))
    try:
        parse_hhmm(backup_time)
    except ValueError as exc:
        raise ConfigError(f"backup.time: {exc}") from exc
    keep = int(bk.get("keep", 14))
    if keep < 1:
        raise ConfigError("backup.keep должен быть не меньше 1")

    return Config(
        token=str(tg.get("token", "")),
        owner_id=owner_id,
        paths=paths,
        timezone=tz,
        day_boundary=boundary,
        llm=LLMConfig(
            provider=str(llm.get("provider", "moonshot")),
            base_url=str(llm.get("base_url", "")),
            api_key=str(llm.get("api_key", "")),
            cheap_model=str(llm.get("cheap_model", "")),
            flagship_model=str(llm.get("flagship_model", "")),
            daily_budget_usd=budget,
            prices=price_obj,
            timeout_sec=float(llm.get("timeout_sec", 60)),
            json_mode=bool(llm.get("json_mode", True)),
            temperature=(float(llm["temperature"]) if "temperature" in llm else None),
            max_tokens=int(llm.get("max_tokens", 1500)),
            cheap_extra=extras["cheap"],
            flagship_extra=extras["flagship"],
            flagship_max_tokens=fl_max,
            flagship_timeout_sec=fl_timeout,
        ),
        stt_provider=stt_provider,
        tts_provider=tts_provider,
        backup=BackupConfig(backup_time, keep, bool(bk.get("weekly_to_telegram", True))),
        deepgram=dg_cfg,
        source_file=path,
    )


def insecure_permissions(path: Path) -> bool:
    """True, если конфиг с секретами читается не только владельцем."""
    mode = path.stat().st_mode
    return bool(mode & (stat.S_IRWXG | stat.S_IRWXO))
