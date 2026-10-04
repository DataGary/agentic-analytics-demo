"""OpenTelemetry setup shared by the agent and the MCP server. Exports to Phoenix over OTLP/HTTP."""

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from common.config import get_settings

_configured = False


def setup_tracing(service_name: str) -> None:
    global _configured
    if _configured:
        return
    _configured = True
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    endpoint = get_settings().otel_endpoint
    if endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)
    # Propagates W3C traceparent on outgoing httpx calls (agent -> MCP -> Cube), so one trace spans services.
    HTTPXClientInstrumentor().instrument()
