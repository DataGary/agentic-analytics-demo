-- Append-only audit trail of every tool call: who asked, what ran, what came back.
CREATE SCHEMA IF NOT EXISTS audit;

CREATE TABLE audit.tool_calls (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    trace_id    text,
    user_sub    text NOT NULL,
    role        text NOT NULL,
    tool        text NOT NULL,
    args        jsonb,
    query_text  text,          -- generated SQL or Cube query JSON
    row_count   int,
    latency_ms  int,
    status      text NOT NULL, -- ok | denied | error
    error       text
);
CREATE INDEX tool_calls_ts_idx ON audit.tool_calls (ts DESC);

GRANT USAGE ON SCHEMA audit TO audit_writer;
GRANT INSERT ON audit.tool_calls TO audit_writer;
GRANT USAGE ON SEQUENCE audit.tool_calls_id_seq TO audit_writer;
