"""Custom exception classes for the pipeline."""


class PipelineException(Exception):
    """Base exception for all pipeline errors."""


class ConfigurationError(PipelineException):
    """Raised when configuration is invalid or missing."""


class InferenceError(PipelineException):
    """Base exception for inference-related errors."""


class TritonConnectionError(InferenceError):
    """Raised when unable to connect to Triton server."""


class ModelNotReadyError(InferenceError):
    """Raised when model is not ready for inference."""


class DecodingError(InferenceError):
    """Raised when decoding fails."""


class AudioProcessingError(PipelineException):
    """Raised when audio processing fails."""


class S3OperationError(PipelineException):
    """Raised when S3 operations fail."""


class AthenaQueryError(PipelineException):
    """Raised when Athena query fails."""


class AlignmentError(PipelineException):
    """Raised when alignment fails."""


class MetricsError(PipelineException):
    """Non-fatal error when metrics emission fails."""
