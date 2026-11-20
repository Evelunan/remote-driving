"""Explicit application logging; importing a library never creates handlers/files."""

import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

get_logger = logging.getLogger


class SampledControlLog:
    """Log zero transitions immediately, sample activity, and trace packets at DEBUG."""

    def __init__(self, logger):
        self.logger = logger
        self.epoch = None
        self.next_info = 0.0
        self.all_zero = False

    def write(self, epoch, message, *args, all_zero=False):
        if not self.logger.isEnabledFor(logging.INFO):
            return
        now = time.monotonic()
        if (
            epoch != self.epoch
            or all_zero != self.all_zero
            or (not all_zero and now >= self.next_info)
        ):
            self.epoch = epoch
            self.all_zero = all_zero
            self.next_info = now + 1
            self.logger.info(message, *args)
        else:
            self.logger.debug(message, *args)


def runtime_directory() -> Path:
    configured = os.environ.get("RD_RUNTIME_DIR")
    path = (
        Path(configured)
        if configured
        else Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        / "remote-driving"
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def configure_logging(level: str = "INFO", *, directory: Path | None = None):
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                directory / "app.log",
                maxBytes=5_000_000,
                backupCount=3,
                encoding="utf-8",
            )
        )
    logging.basicConfig(
        level=level,
        handlers=handlers,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
