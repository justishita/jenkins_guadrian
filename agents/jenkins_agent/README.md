# Jenkins Agent

**Owner:** P1. Investigates Jenkins pipeline failures and contributes evidence through the shared contract.

## Worker

Run locally with `python -m agents.jenkins_agent.agent`. The worker consumes
`incident.created` from the durable `jenkins_agent.incident.created` quorum
queue, investigates with a read-only LangGraph tool set, and upserts validated
Evidence to `./data/evidence/<incident_id>/jenkins_agent.json` before
acknowledging the delivery. Repeated processing
failures are requeued up to the queue delivery limit and then dead-lettered.

Required environment variables are `RABBITMQ_URL`, `JENKINS_URL`,
`JENKINS_USER`, `JENKINS_API_TOKEN`, and `GEMINI_API_KEY`.
`GEMINI_MODEL` defaults to `gemini-2.5-flash`. The Evidence Pydantic model is
versioned as `0.1-stub` until the shared canonical schema is delivered.