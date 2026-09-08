class SentinelError(Exception):
    """Помилка application з фіксованим безпечним повідомленням."""


class ConfigurationError(SentinelError):
    pass


class ScanError(SentinelError):
    pass


class ParseError(ScanError):
    pass


class NotificationError(SentinelError):
    pass
