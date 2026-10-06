"""Инструкции для моделей — отдельные файлы, меняются без правки кода."""

from __future__ import annotations

from pathlib import Path

NAMES = ("check_en", "check_psy", "check_vignette", "dialog_en", "dialog_corrections",
         "monologue_en", "questions_en", "explain_en", "scenario_en", "exit_dialog_en", "retell_psy", "check_term", "check_text", "exam_check", "exam_talk", "speech_partner", "speech_review", "exit_review", "live_review", "listen_review")


class PromptError(RuntimeError):
    pass


class Prompts:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._cache: dict[str, str] = {}
        missing = [n for n in NAMES if not (self.root / f"{n}.md").exists()]
        if missing:
            raise PromptError(f"нет инструкций в {self.root}: {', '.join(missing)}")
        self.reload()

    def reload(self) -> None:
        self._cache = {n: (self.root / f"{n}.md").read_text(encoding="utf-8").strip() for n in NAMES}

    def __getitem__(self, name: str) -> str:
        return self._cache[name]
