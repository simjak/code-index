from __future__ import annotations

import logging
import os
import sys
from typing import NoReturn, Optional, Union

from dotenv import load_dotenv

load_dotenv()

try:
    import colorlog  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    colorlog = None


class AppLogger(logging.Logger):
    def error_raise(
        self,
        message: str,
        *,
        exc: Optional[Union[BaseException, type[BaseException]]] = None,
    ) -> NoReturn:
        """
        Emit an error message and raise an exception afterwards.
        If ``exc`` is provided it will be raised, otherwise a RuntimeError.
        """
        self.error(message)
        if exc is None:
            raise RuntimeError(message)
        if isinstance(exc, type):
            raise exc(message)
        raise exc


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # pragma: no cover - trivial
        record.classname = record.module
        record.funcname = record.funcName
        return True


def _determine_level() -> int:
    raw_level = (os.getenv("LOG_LEVEL") or "").upper()
    return {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
        "TRACE": 5,
    }.get(raw_level, logging.INFO)


def _ensure_trace_level():
    if not hasattr(logging, "TRACE"):
        logging.TRACE = 5  # type: ignore[attr-defined]
        logging.addLevelName(logging.TRACE, "TRACE")  # type: ignore[attr-defined]

        def trace(self: logging.Logger, message: str, *args, **kwargs):
            if self.isEnabledFor(logging.TRACE):  # type: ignore[attr-defined]
                self._log(logging.TRACE, message, args, **kwargs)  # type: ignore[attr-defined]

        logging.Logger.trace = trace  # type: ignore[attr-defined]


def _build_formatter() -> logging.Formatter:
    base_format = "[%(levelname)s] %(asctime)s - %(classname)s:%(lineno)d %(funcname)s(): %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    if colorlog is not None:
        return colorlog.ColoredFormatter(
            fmt="%(log_color)s" + base_format,
            datefmt=date_format,
            log_colors={
                "TRACE": "white",
                "DEBUG": "cyan",
                "INFO": "green",
                "WARNING": "yellow",
                "ERROR": "red",
                "CRITICAL": "bold_red",
            },
        )
    return logging.Formatter(fmt=base_format, datefmt=date_format)


def setup_logger(name: str) -> AppLogger:
    _ensure_trace_level()
    logging.setLoggerClass(AppLogger)
    logger = logging.getLogger(name)
    if getattr(logger, "_logger_initialized", False):  # type: ignore[attr-defined]
        return logger  # type: ignore[return-value]

    level = _determine_level()
    logger.setLevel(level)
    logger.propagate = False

    formatter = _build_formatter()

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.NOTSET)
    stdout_handler.addFilter(lambda record: record.levelno < logging.ERROR)
    stdout_handler.setFormatter(formatter)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.ERROR)
    stderr_handler.setFormatter(formatter)

    logger.addHandler(stdout_handler)
    logger.addHandler(stderr_handler)
    logger.addFilter(ContextFilter())
    logger._logger_initialized = True  # type: ignore[attr-defined]
    return logger  # type: ignore[return-value]


logger: AppLogger = setup_logger("codeindex")

__all__ = ["logger", "setup_logger", "AppLogger"]
