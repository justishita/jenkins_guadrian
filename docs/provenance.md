# Provenance & Audit Traceability

This document describes how to trace any claim or conclusion in the final investigation report back to the exact Jenkins console log lines, raw metrics, or git commit diffs.

## 1. Trace ID & Context Propagation
Every incident lifecycle begins at the webhook entrypoint:
1. `POST /webhooks/jenkins` receives the failure event, assigns/records the `incident_id`, and initiates the OpenTelemetry root trace (`trace_id`).
2. The initial incident context is posted to the Blackboard:
   `section='context', key='incident_event', author='backend'` with the `trace_id`.
3. The Coordinator initiates investigation calls across agents over the **A2A protocol** by injecting the W3C `traceparent` header.
4. Each agent (`jenkins_agent`, `metrics_agent`, `code_agent`) extracts the `traceparent` header in `common/a2a_server.py`, ensuring all child spans (tool executions, LLM calls, guardrail checks) attach to the incident root trace.

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
5. **Trace Distributed Execution in Jaeger:**  
   Search Jaeger for `trace_id` recorded on the Blackboard entry or audit event to see exact tool call timing, parameters, and LLM token usage.
