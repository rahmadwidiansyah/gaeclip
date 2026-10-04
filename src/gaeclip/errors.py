class GaeclipError(Exception):
    """Base error with a stable, user-facing code."""

    code = "GAECLIP_ERROR"
    retryable = False

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ConfigurationError(GaeclipError):
    code = "INVALID_CONFIGURATION"


class ValidationError(GaeclipError):
    code = "INVALID_INPUT"


class ConflictError(GaeclipError):
    code = "IDEMPOTENCY_CONFLICT"


class SourceError(GaeclipError):
    code = "SOURCE_ERROR"


class ExternalCommandError(GaeclipError):
    code = "EXTERNAL_COMMAND_FAILED"


class CancelledError(GaeclipError):
    code = "JOB_CANCELLED"
