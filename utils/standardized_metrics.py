"""Standardized metrics emitter with consistent dimensions for better dashboarding.

This module ensures consistent EMF metric dimensions across all metrics for
improved CloudWatch dashboard grouping and filtering capabilities.
"""

import os
import platform

from loguru import logger

from amira_pyutils.logging import emit_emf_metric
from src.letter_scoring_pipeline.inference.metrics_constants import (
    DIM_ACTIVITY_ID,
    DIM_CORRELATION_ID,
    DIM_DEVICE,
)


class StandardizedMetricsEmitter:
    """EMF metrics emitter with standardized dimensions for consistent dashboarding."""

    def __init__(
        self,
        *,
        service: str,
        stage: str | None = None,
        correlation_id: str | None = None,
        device: str | None = None,
    ) -> None:
        """Initialize standardized metrics emitter.

        Args:
            service: Service name (e.g., 'letter-scoring', 'triton-inference')
            stage: Deployment stage (dev, staging, prod)
            correlation_id: Request/activity correlation ID
            device: Device type (cpu, gpu, mps)
        """
        self._service: str = service
        self._stage: str = stage or os.getenv("STAGE") or "unknown"
        self._correlation_id: str | None = correlation_id
        self._device: str = device or self._detect_device()
        self._instance_id: str = self._get_instance_id()

    def _detect_device(self) -> str:
        """Auto-detect device type based on environment."""
        # Check environment variables first
        device_env = os.getenv("DEVICE_TYPE")
        if device_env:
            return device_env

        # Auto-detect based on available hardware
        try:
            import torch

            if torch.cuda.is_available():
                return "gpu"
            elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return "mps"
        except ImportError:
            pass

        return "cpu"

    def _get_instance_id(self) -> str:
        """Get instance identifier for tracking."""
        # Try Lambda-specific identifier
        lambda_request_id = os.getenv("_X_AMZN_TRACE_ID")
        if lambda_request_id:
            return lambda_request_id[:8]  # Truncate for readability

        # Try container instance ID
        container_id = os.getenv("HOSTNAME", "")
        if container_id:
            return container_id[:8]

        # Fallback to platform node
        return platform.node()[:8]

    def get_standard_dimensions(
        self, *, activity_id: str | None = None, additional_dims: dict[str, str] | None = None
    ) -> dict[str, str]:
        """Get standard dimensions with optional overrides.

        Args:
            activity_id: Optional activity ID to include
            additional_dims: Additional dimensions to merge

        Returns:
            Dictionary of standardized dimensions
        """
        dimensions: dict[str, str] = {
            "Service": self._service,
            "Stage": self._stage,
            DIM_DEVICE: self._device,
            "InstanceId": self._instance_id,
        }

        # Add optional standard dimensions
        if self._correlation_id:
            dimensions[DIM_CORRELATION_ID] = self._correlation_id

        if activity_id:
            dimensions[DIM_ACTIVITY_ID] = activity_id

        # Merge additional dimensions
        if additional_dims:
            dimensions.update(additional_dims)

        return dimensions

    def emit_metric(
        self,
        *,
        namespace: str,
        metrics: dict[str, float],
        activity_id: str | None = None,
        additional_dimensions: dict[str, str] | None = None,
        timestamp_ms: int | None = None,
    ) -> None:
        """Emit metric with standardized dimensions.

        Args:
            namespace: CloudWatch metrics namespace
            metrics: Map of metric name to float value
            activity_id: Optional activity ID
            additional_dimensions: Additional custom dimensions
            timestamp_ms: Optional epoch ms; default now
        """
        try:
            dimensions = self.get_standard_dimensions(
                activity_id=activity_id, additional_dims=additional_dimensions
            )

            emit_emf_metric(
                namespace=namespace,
                metrics=metrics,
                dimensions=dimensions,
                timestamp_ms=timestamp_ms,
            )
        except Exception as e:
            logger.debug(
                f"Standardized metrics emission failed (non-fatal): {type(e).__name__}: {e}"
            )

    def update_correlation_id(self, *, correlation_id: str) -> None:
        """Update correlation ID for subsequent metrics.

        Args:
            correlation_id: New correlation ID
        """
        self._correlation_id = correlation_id

    def create_child_emitter(
        self,
        *,
        correlation_id: str | None = None,
        additional_service_suffix: str | None = None,
    ) -> "StandardizedMetricsEmitter":
        """Create child emitter with updated context.

        Args:
            correlation_id: Optional new correlation ID
            additional_service_suffix: Optional suffix to append to service name

        Returns:
            New StandardizedMetricsEmitter instance
        """
        service_name = self._service
        if additional_service_suffix:
            service_name = f"{self._service}-{additional_service_suffix}"

        return StandardizedMetricsEmitter(
            service=service_name,
            stage=self._stage,
            correlation_id=correlation_id or self._correlation_id,
            device=self._device,
        )


# Global emitter instance for convenience
_global_emitter: StandardizedMetricsEmitter | None = None


def get_global_emitter() -> StandardizedMetricsEmitter:
    """Get or create global standardized metrics emitter.

    Returns:
        Global StandardizedMetricsEmitter instance
    """
    global _global_emitter
    if _global_emitter is None:
        service_name = os.getenv("SERVICE_NAME", "amira-letter-scoring")
        _global_emitter = StandardizedMetricsEmitter(service=service_name)
    return _global_emitter


def set_global_emitter(emitter: StandardizedMetricsEmitter) -> None:
    """Set global standardized metrics emitter.

    Args:
        emitter: StandardizedMetricsEmitter instance to use globally
    """
    global _global_emitter
    _global_emitter = emitter


def emit_standardized_metric(
    *,
    namespace: str,
    metrics: dict[str, float],
    activity_id: str | None = None,
    additional_dimensions: dict[str, str] | None = None,
    timestamp_ms: int | None = None,
) -> None:
    """Convenience function to emit metric using global emitter.

    Args:
        namespace: CloudWatch metrics namespace
        metrics: Map of metric name to float value
        activity_id: Optional activity ID
        additional_dimensions: Additional custom dimensions
        timestamp_ms: Optional epoch ms; default now
    """
    emitter = get_global_emitter()
    emitter.emit_metric(
        namespace=namespace,
        metrics=metrics,
        activity_id=activity_id,
        additional_dimensions=additional_dimensions,
        timestamp_ms=timestamp_ms,
    )
