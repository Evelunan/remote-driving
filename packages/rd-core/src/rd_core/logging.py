"""Explicit application logging; importing a library never creates handlers/files."""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

get_logger = logging.getLogger


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
