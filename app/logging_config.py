"""Centralised logging setup for the whole application.

WHY THIS EXISTS
---------------
The learning script (main.py) used print() everywhere. That is fine for a
teaching log, but a production app needs real logging: levels (DEBUG/INFO/...),
timestamps, the module each message came from, and one switch to make it loud or
quiet. We initialise logging ONCE, at the very first line of main_prod.py, and
then EVERY other module just asks for its own logger with get_logger(__name__).

HOW TO USE IT
-------------
    # once, at program start (main_prod.py does this for you):
    from app.logging_config import setup_logging
    setup_logging(level="DEBUG")

    # anywhere else, at the top of a module:
    from app.logging_config import get_logger
    logger = get_logger(__name__)
    logger.debug("about to do the thing with %s", some_value)

Because setup_logging configures the ROOT logger, every get_logger(__name__)
child inherits its handler and level. You set the verbosity in exactly one place.
"""

import logging
import sys

# The format string. Each field is labelled so the output is self-explanatory:
#   time        -> when the message happened (down to milliseconds)
#   levelname   -> DEBUG / INFO / WARNING / ERROR
#   name        -> which module logged it (e.g. app.simulation.simulator)
#   message     -> the actual text
_LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s"
_DATE_FORMAT = "%H:%M:%S"

# A module-level flag so that if setup_logging is accidentally called twice we do
# not attach two handlers (which would print every line twice).
_already_configured = False


def setup_logging(level: str = "DEBUG", log_to_file: str | None = None) -> None:
    """Configure the root logger once for the whole process.

    Configures the root logger so that every ``get_logger(__name__)`` child
    inherits the same handler and level. Safe to call more than once: later
    calls only adjust the level and never attach duplicate handlers.

    Args:
        level: Minimum level to show. ``"DEBUG"`` shows everything (use this for
            the demo); ``"INFO"`` hides the fine-grained debug lines. Passed as a
            string so it reads naturally from a config file later.
        log_to_file: Optional path. When given, the same logs are also written to
            this file. ``None`` means console only.

    Returns:
        None.
    """
    global _already_configured

    # Translate the friendly string ("DEBUG") into the logging module's number.
    numeric_level = getattr(logging, level.upper(), logging.DEBUG)

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # If we were already configured, just adjust the level and stop. This makes
    # the function safe to call more than once (e.g. Streamlit re-runs scripts).
    if _already_configured:
        root_logger.setLevel(numeric_level)
        root_logger.debug(
            f"setup_logging called again; level set to {level.upper()}, no new handlers added"
        )
        return

    formatter = logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # Handler 1: the console (stdout). This is what you see in the terminal.
    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Handler 2 (optional): a file, with the SAME format, for later inspection.
    if log_to_file is not None:
        file_handler = logging.FileHandler(log_to_file, mode="w", encoding="utf-8")
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    _already_configured = True

    root_logger.debug(f"logging initialised at level {level.upper()}")
    if log_to_file is not None:
        root_logger.debug(f"also writing logs to file: {log_to_file}")


def get_logger(module_name: str) -> logging.Logger:
    """Return the logger a module should use.

    Args:
        module_name: Always pass ``__name__`` so the log line shows which module
            emitted the message.

    Returns:
        The named :class:`logging.Logger` instance.
    """
    return logging.getLogger(module_name)
