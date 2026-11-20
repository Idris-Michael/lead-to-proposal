from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from lead_to_proposal import telemetry


def test_trace_id_is_added_to_log_events_inside_a_span() -> None:
    telemetry.setup("test", None)
    tracer = TracerProvider().get_tracer("t")
    assert "trace_id" not in telemetry._add_trace_ids(None, "info", {})
    with tracer.start_as_current_span("s"):
        event = telemetry._add_trace_ids(None, "info", {})
    assert len(str(event["trace_id"])) == 32


def test_otlp_endpoint_installs_a_tracer_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: list[object] = []
    monkeypatch.setattr(trace, "set_tracer_provider", installed.append)  # keep the global untouched
    telemetry.setup("test", "http://localhost:4318")
    assert isinstance(installed[0], TracerProvider)
    installed[0].shutdown()  # type: ignore[attr-defined]
