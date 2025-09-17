from amira_pyutils.errors import AmiraError


class FEError(AmiraError):
    """Root class for all distinguished errors raised by this library."""

    def __init__(self, *, msg: str, retryable: bool = True) -> None:
        """Initialize FEError with message and retry behavior.

        Args:
            msg: Error message describing the issue
            retryable: Whether the operation can be retried
        """
        super().__init__(msg, retryable=retryable)


class UnknownFeatureError(FEError):
    """Raised when a required feature is not found in the feature registry."""

    def __init__(self, *, feature_name: str) -> None:
        """Initialize with the unknown feature name.

        Args:
            feature_name: Name of the feature that could not be found
        """
        super().__init__(
            msg=f"Unknown feature required by model: {feature_name}",
            retryable=False,
        )


class BadManifestError(FEError):
    """Raised when a model manifest is malformed or invalid."""

    def __init__(self, *, reason: str) -> None:
        """Initialize with the reason for manifest invalidity.

        Args:
            reason: Specific reason why the manifest is invalid
        """
        super().__init__(msg=f"Bad model manifest: {reason}", retryable=False)


class UninitializedError(FEError):
    """Raised when attempting to use uninitialized components or services."""

    def __init__(self) -> None:
        """Initialize with standard uninitialized message."""
        super().__init__(msg="process_init() has not been called in this process")


class MissingFirstPassScoringError(FEError):
    """Raised when first pass scoring data is unavailable for a model/activity."""

    def __init__(self, *, model_id: str, activity_id: str, phrase_index: int = -1) -> None:
        """Initialize with model, activity, and optional phrase information.

        Args:
            model_id: Identifier of the model requiring scores
            activity_id: Identifier of the activity being scored
            phrase_index: Optional phrase index, -1 indicates all phrases missing
        """
        if phrase_index < 0:
            message = f"Unable to find any first pass scores from model {model_id} for activity {activity_id}"
        else:
            message = f"Unable to find first pass scores from model {model_id} for activity {activity_id}, phrase {phrase_index}"

        super().__init__(msg=message, retryable=False)


class MissingBaseFeaturesError(FEError):
    """Raised when required base features are not available for extraction."""

    def __init__(self, *, message: str) -> None:
        """Initialize with details about missing base features.

        Args:
            message: Specific details about which base features are missing
        """
        super().__init__(msg=f"Missing base features: {message}")


class MissingASRDataError(FEError):
    """Raised when required ASR data is not available for processing."""

    def __init__(self, *, asr: str) -> None:
        """Initialize with the ASR system identifier.

        Args:
            asr: Identifier of the ASR system with missing data
        """
        super().__init__(msg=f"Missing required ASR info for ASR: {asr}", retryable=False)


class BadRequestDataError(FEError):
    """Raised when request data contains inconsistencies or invalid values."""

    def __init__(self, *, details: str) -> None:
        """Initialize with specific details about the data inconsistency.

        Args:
            details: Specific description of what makes the request data invalid
        """
        super().__init__(msg=f"Request data is inconsistent: {details}", retryable=False)


class ExperimentalFeatureError(FEError):
    """Raised when attempting to use experimental features without proper enablement."""

    def __init__(self, *, feature_name: str) -> None:
        """Initialize with the experimental feature name.

        Args:
            feature_name: Name of the experimental feature being accessed
        """
        super().__init__(
            msg=f"Feature {feature_name} requires experimental support, which is not enabled",
            retryable=False,
        )


class MissingAudioError(FEError):
    """Raised when audio data is required for extraction but not provided."""

    def __init__(self) -> None:
        """Initialize with standard missing audio message."""
        super().__init__(msg="Extraction required audio to be provided, but it was not")
