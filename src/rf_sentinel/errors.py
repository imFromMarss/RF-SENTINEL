class SentinelError(Exception):
    """Application failure with a fixed, safe public message."""


class ConfigurationError(SentinelError):
    pass


class ScanError(SentinelError):
    pass


class ParseError(ScanError):
    pass


class NotificationError(SentinelError):
    pass
