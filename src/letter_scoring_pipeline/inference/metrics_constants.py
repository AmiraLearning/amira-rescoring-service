from typing import Final

# Namespaces
NS_INFERENCE: Final[str] = "Amira/Inference"
NS_ACTIVITY: Final[str] = "Amira/Activity"
NS_ALIGNMENT: Final[str] = "Amira/Alignment"
NS_DECODER: Final[str] = "Amira/Decoder"

# Standard dimensions (consistent across all metrics for better dashboarding)
DIM_SERVICE: Final[str] = "Service"
DIM_STAGE: Final[str] = "Stage"
DIM_DEVICE: Final[str] = "Device"
DIM_INSTANCE_ID: Final[str] = "InstanceId"

# Context dimensions
DIM_CORRELATION_ID: Final[str] = "CorrelationId"
DIM_ACTIVITY_ID: Final[str] = "ActivityId"
DIM_MODEL: Final[str] = "Model"

# Inference metrics
MET_INFER_TOTAL_MS: Final[str] = "InferenceTotalMs"
MET_INFER_PRE_MS: Final[str] = "PreprocessMs"
MET_INFER_MODEL_MS: Final[str] = "ModelMs"
MET_INFER_DECODE_MS: Final[str] = "DecodeMs"
MET_INFER_INCLUDE_CONF: Final[str] = "IncludeConfidence"
MET_INFER_SUCCESS: Final[str] = "Success"

# Activity metrics
MET_ACTIVITY_TOTAL_MS: Final[str] = "ActivityTotalMs"
MET_ACTIVITY_PHRASES: Final[str] = "Phrases"
MET_ACTIVITY_SUCCESS: Final[str] = "ActivitySuccess"
MET_ACTIVITY_ALIGN_FAILURE: Final[str] = "AlignFailure"

# Alignment metrics
MET_ALIGN_ERROR_COUNT: Final[str] = "AlignErrorCount"
MET_ALIGN_TOTAL: Final[str] = "AlignTotal"
MET_ALIGN_ACCURACY: Final[str] = "AlignAccuracy"

# Decoder metrics
MET_DECODER_PARSE_FAILURE: Final[str] = "ParseFailure"
