from typing import Final

import grpc

from amira_fe.blank_detect_grpc import BlankDetectStub
from amira_fe.errors import UninitializedError
from amira_pyutils.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_HOST: Final[str] = "0.0.0.0"
_DEFAULT_PORT: Final[int] = 2343

_blank_detector: BlankDetectStub | None = None


def init_blank_detector(*, host: str = _DEFAULT_HOST, port: int = _DEFAULT_PORT) -> None:
    """Initialize the blank detection gRPC client.

    Establishes connection to the Blank Detection gRPC server and creates
    a global BlankDetectStub instance for subsequent use.

    Args:
        host: Server hostname or IP address
        port: Server port number

    Side Effects:
        Sets the global _blank_detector variable to a configured BlankDetectStub instance.
        Logs connection information.
    """
    global _blank_detector

    blank_detection_channel: str = f"{host}:{port}"
    logger.info(f"Connection to Blank Detection gRPC server at {blank_detection_channel}...")

    channel: grpc.Channel = grpc.insecure_channel(blank_detection_channel)
    _blank_detector = BlankDetectStub(channel)


def get_blank_detector() -> BlankDetectStub:
    """Retrieve the initialized blank detector instance.

    Returns:
        The global BlankDetectStub instance

    Raises:
        UninitializedError: If init_blank_detector has not been called
    """
    global _blank_detector

    if _blank_detector is None:
        raise UninitializedError

    return _blank_detector
