# A2A feasibility spike (Week 4, slice 0)

**Owner:** P2. **Script:** `scripts/a2a_spike.py` (run it to reproduce). **Versions:** a2a-sdk 1.2.3,
google-adk 2.13.0, crewai 1.15.28, Python 3.12.

## Question
Can the three investigation agents notify the ADK Coordinator over a real A2A transport, and what
would it take? This was time-boxed and is **not** a prerequisite for the fusion work.

## Result: feasible. Every check passed on the first run

| # | Question | Result |
|---|---|---|
| 1 | Does the SDK's server/client API work? | Yes (details below) |
| 2 | Can an agent send a minimal evidence-ready notification to a Coordinator endpoint? | Yes: HTTP/JSON-RPC round trip in about 30-40 ms on localhost |
| 3 | Do the incident id and the evidence reference survive transport? | Yes: P3's `parse_evidence_ready()` works unchanged on the received message; `(incident_id, agent)` retrieves the same document from the blackboard |
| 3b | Is a malformed notification rejected? | Yes, with a reason; it is not trusted or recorded |
| 4 | What happens when one agent never reports? | Implementable: an `asyncio.Condition` with `wait_for(timeout)` returns after the timeout with the missing agent named; it completes immediately once all three have reported |
| 5 | Concurrent and duplicate notifications? | Three simultaneous agents plus one duplicate are each recorded once |

## How it works with this SDK
- **Server:** an `AgentExecutor` subclass (`execute(context, event_queue)`) wrapped by
  `DefaultRequestHandler(agent_executor, task_store=InMemoryTaskStore(), agent_card)`, exposed with
  `create_agent_card_routes(card)` + `create_jsonrpc_routes(handler, rpc_url="/a2a")` in a Starlette app,
  served by uvicorn. The `AgentCard` needs a `supported_interfaces` entry
  (`url`, `protocol_binding="JSONRPC"`, `protocol_version="1.0"`).
- **Client:** `create_client("http://host:port", ClientConfig(streaming=False))` resolves the card from
  `/.well-known/agent-card.json`; then `async for r in client.send_message(pb.SendMessageRequest(message=m))`.
- **Types are protobuf** (as P3's `a2a_contract.py` already notes); messages are built field by field.
- **Bridge to P3's Coordinator:** `CoordinatorAgent.on_evidence_ready(message)` already accepts an A2A
  `Message`, so the executor can be a thin adapter that calls it and replies. `google-adk` also ships
  `google.adk.a2a` (`agent_to_a2a`, `A2aAgentExecutor`, `RemoteA2aAgent`); those route user text through the
  agent, which is a worse fit for metadata-only `evidence.ready` notifications, so the thin adapter is the
  recommended starting point.

## What this does NOT prove (do not claim live A2A integration from this)
- It runs in **one process, one event loop, on localhost**: no Docker network, no separate containers.
- The sender is a script, **not the real agents**: their `notifier` is still unwired in production.
- The Coordinator is **not a running service** and is not in `docker-compose.yml`.
- `InMemoryTaskStore` and the in-memory inbox lose state on restart; there is no auth on the endpoint.
- Hosted executor is a plain `AgentExecutor`, not the ADK `CoordinatorAgent` itself.

## Implications for the live A2A milestone (slice 5)
1. Add `coordinator/requirements.txt` declaring `google-adk` and `a2a-sdk` (currently undeclared).
2. Coordinator service: the executor adapter + inbox with timeout (fuse when all three reported, or on
   timeout and mark the report `incomplete`), run by uvicorn, plus a compose service.
3. Wire each agent's `notifier` to an A2A client pointing at `COORDINATOR_URL`; keep it best-effort, as it is.
4. Decide persistence for pending incidents across a Coordinator restart (the blackboard already holds the
   evidence, so a restart can re-collect rather than lose findings).
