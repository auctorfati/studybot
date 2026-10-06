"""Один экземпляр бота на папку данных.

Замок — flock на data/studybot.lock. Держит его запущенный бот; второй запуск
(например, ручной run при работающем сервисе — Telegram оборвал бы опрос
обоим) и восстановление базы при работающем боте отказываются сразу.
Замок снимает ядро при завершении процесса, даже аварийном.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class LockBusy(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None

    def holder(self) -> str:
        """pid процесса, который держит замок, — для сообщения об ошибке."""
        try:
            return self.path.read_text(encoding="utf-8").strip() or "?"
        except OSError:
            return "?"

    def __enter__(self) -> "InstanceLock":
        if not self.acquire():
            raise LockBusy(f"замок {self.path} держит процесс {self.holder()}")
        return self

    def __exit__(self, *exc) -> None:
        self.release()
