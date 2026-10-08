"""The typed errors the source feature raises. Each carries a stable code the routes map to a status and the
clients act on; messages never contain Git output or credentials."""


class SourceError(ValueError):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code.replace("_", " "))


class SourceConflict(SourceError):
    pass


class StaleMutation(SourceConflict):
    pass


class SourceNotFound(SourceError):
    pass


class SourceForbidden(SourceError):
    pass


class CheckThrottled(SourceError):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__("check_throttled")
