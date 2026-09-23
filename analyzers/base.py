from __future__ import annotations

from typing import Protocol, runtime_checkable

from apps.orchestrator.domain import AdapterResult, AnalysisRequest


@runtime_checkable
class ModelAdapter(Protocol):
    """Transport-independent contract for any model runner or gateway."""

    provider: str
    model: str

    def analyze(self, request: AnalysisRequest) -> AdapterResult:
        ...
