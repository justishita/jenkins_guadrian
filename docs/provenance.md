# Provenance & Audit Traceability

This document describes how to trace any claim or conclusion in the final investigation report back to the exact Jenkins console log lines, raw metrics, or git commit diffs.

## 1. Trace ID & Context Propagation
Every incident gets one trace, built from these spans (all started with `common/tracing.py`):

| Span | Started by | Notes |
|---|---|---|
| `webhook.create_incident` | `POST /webhooks/jenkins` (`backend/routes/webhooks.py`) | Root span. Attributes: job, build number, `incident.id`, whether the incident was new |
| `coordinator.handle_message` | the Coordinator, on `incident.created` / `incident.reinvestigate` | Continues the root trace from the message's `traceparent` header |
| `coordinator.run_agent` | the Coordinator, once per agent | Attributes: `agent`, `incident.id`, `outcome` |
| `coordinator.synthesize` | the Coordinator | The fusion step |
| `a2a_execute_task` | each agent's A2A task server (`common/a2a_server.py`) | Continues the trace from the `traceparent` header on the Coordinator's A2A call |
| `metrics_agent.handle_message` / `code_agent.handle_message` | the Metrics and Code agents, when they consume `incident.created` from RabbitMQ themselves (`TRIGGER_MODE=rabbitmq`) | Continues the root trace from the message's `traceparent` header |

How the trace travels:
1. The backend publishes `incident.created` with the active trace as a `traceparent` message header
   (`backend/events/publisher.py`), so the trace follows the event over RabbitMQ.
2. The Coordinator reads that header, continues the trace, and adds the same header to every A2A call it makes
   (`coordinator/app.py`).
3. Each agent's task server extracts it and runs the task inside `a2a_execute_task`. In `TRIGGER_MODE=rabbitmq` the
   Metrics and Code agents read the header from the RabbitMQ message instead (`span_from_headers` in
   `common/tracing.py`); the Jenkins agent is always started over A2A by the Coordinator.
4. Every Blackboard entry written inside a span records its `trace_id` and `span_id` (`common/blackboard.py`), including
   the incident's `context/incident_event` entry, so an incident can be matched to its trace from the Blackboard.

Each service installs its tracer provider at start-up (`setup_tracing`). To see the spans, set
`OTEL_EXPORTER_OTLP_ENDPOINT` to any OTLP/HTTP collector (for example a Jaeger all-in-one container), or
`OTEL_TRACES_CONSOLE=1` to print them. With neither set, spans still exist and trace ids still propagate and appear on
Blackboard entries, but nothing is exported.

## 2. Blackboard Append-Only History
The Blackboard (`common/blackboard.py`) records an immutable, versioned, append-only history of every intermediate finding:
- **`context/incident_event`**: Original failure notification payload.
- **`hints/jenkins_agent`**: Early failure signals (`failure_window`, `failed_stage`, `error_signature`, `suspect_files`).
- **`evidence/jenkins_agent`**: Complete evidence object containing `evidence_items` and `tool_calls`.
- **`synthesis/unified_report`**: Synthesized narrative citing evidence IDs.

## 3. Navigating from Final Report to Raw Log Lines

Follow these steps to trace a claim back to the source:
1. **Locate Cited Evidence ID:**  
   In the synthesis report, each hypothesis references supporting evidence IDs (e.g. `evidence_items[id='error_blocks[0]']` or `id='failing_tests[0]'`).
2. **Inspect Evidence Item in Blackboard:**  
   Retrieve the evidence entry from Blackboard:
   ```python
   bb = get_blackboard()
   entry = bb.read(incident_id, section='evidence', key='jenkins_agent')
   # Inspect entry.payload['evidence_items'] for the matching ID
   ```
3. **Retrieve Source Reference & Line Range:**  
   The `EvidenceItem` contains:
   - `id`: e.g. `compiler_errors[0]` or `failing_tests[0]`
   - `location.file`: e.g. `target_app/app.py`
   - `location.line`: e.g. line `42`
   - `source`: e.g. `jenkins:orders/main#12`
4. **Fetch Exact Console Range via Jenkins MCP:**  
   Call `get_log_range` tool on `mcp_servers/jenkins_mcp` for `job_name='orders/main'`, `build_number=12`, with `start_line=30`, `end_line=50` to inspect raw context around the error.
5. **Trace Distributed Execution:**  
   Take the `trace_id` recorded on the Blackboard entry (the `context/incident_event` entry has the incident's root trace
   id) and search your trace backend for it, if spans are being exported (see section 1). The trace shows the webhook, the
   Coordinator's handling, each agent's dispatch and outcome, and each agent's `a2a_execute_task` span with its timing.
