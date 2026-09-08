class SentinelError(Exception):
    """Помилка application з фіксованим безпечним повідомленням."""


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
