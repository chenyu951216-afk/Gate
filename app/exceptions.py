class ScannerError(Exception):
    """Base application exception."""


class GateAPIError(ScannerError):
    def __init__(self, message: str, status_code: int | None = None, endpoint: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.endpoint = endpoint


class SchemaValidationError(GateAPIError):
    """Gate returned a response that cannot be safely interpreted."""


class DataUnavailable(ScannerError):
    """A public data source does not expose a requested historical metric."""


class TimeAlignmentError(ScannerError):
    """A requested replay timestamp cannot be aligned safely."""


class JobCancelled(ScannerError):
    """A replay or backtest job was cancelled by the caller."""

