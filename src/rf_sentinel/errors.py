class SentinelError(Exception):
    """Помилка application з фіксованим безпечним повідомленням."""


SAFE_SCAN_ERROR_CODES = frozenset({
    "timeout", "output_too_large", "stderr_too_large", "subprocess_exit",
    "tuner_pll", "executable_missing", "io_error", "incomplete_coverage",
    "parser_malformed", "frame_count", "bin_width", "device_busy", "stopped",
})


class ConfigurationError(SentinelError):
    pass


class ScanError(SentinelError):
    def __init__(self, message: str, *, reason: str = "unknown",
                 returncode: int | None = None):
        super().__init__(message)
        self.reason = reason
        self.returncode = returncode


class ParseError(ScanError):
    pass


class NotificationError(SentinelError):
    pass


class MeasurementSinkError(SentinelError):
    """The acquisition-to-persistence boundary could not accept a sweep."""


class MeasurementQueueFullError(MeasurementSinkError):
    pass


class MeasurementPersistenceError(MeasurementSinkError):
    pass
