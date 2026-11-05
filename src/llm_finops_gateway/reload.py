"""Reload a configuration file when it changes on disk, without a restart.

A :class:`WatchedFile` wraps a loader such as ``PriceBook.from_yaml``. ``get()`` stats the
file; when its modification time or size differs from the last load, the loader runs again.
A file that fails to load keeps the previous value in service and logs the error, so a typo
in a pull request cannot take the gateway down; the next successful load replaces it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Generic, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


class WatchedFile(Generic[T]):
    def __init__(self, path: Path, loader: Callable[[Path], T], describe: Callable[[T], str]):
        self._path = path
        self._loader = loader
        self._describe = describe
        self._stamp = self._current_stamp()
        self._value: T = loader(path)
        log.info("loaded %s: %s", path, describe(self._value))

    @property
    def path(self) -> Path:
        return self._path

    @property
    def value(self) -> T:
        """The last successfully loaded value, without checking the file."""
        return self._value

    def _current_stamp(self) -> tuple[int, int] | None:
        try:
            st = os.stat(self._path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def get(self) -> T:
        """The current value, reloaded first if the file changed since the last load."""
        stamp = self._current_stamp()
        if stamp == self._stamp or stamp is None:
            return self._value
        try:
            value = self._loader(self._path)
        except Exception as exc:  # a broken edit must not take the service down
            log.error(
                "%s changed but failed to load, keeping %s: %s",
                self._path,
                self._describe(self._value),
                exc,
            )
            self._stamp = stamp  # do not retry on every request until it changes again
            return self._value
        self._value = value
        self._stamp = stamp
        log.info("reloaded %s: %s", self._path, self._describe(value))
        return value
