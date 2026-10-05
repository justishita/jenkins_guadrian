# Backend

**Owner:** P1. Owns the FastAPI application, webhook routes, incident coordinator entry points, and RabbitMQ event helpers.

## Jenkins failure catch-up

When `CATCHUP_ENABLED` is enabled (default `true`), the API scans Jenkins
every 60 seconds for `FAILURE` builds from the preceding 30 minutes and
records/publishes any missing incidents. `ABORTED` and `UNSTABLE` builds are
ignored. Configure `JENKINS_URL`, `JENKINS_USER`, and `JENKINS_API_TOKEN` for
Jenkins API access; set `CATCHUP_ENABLED=false` to disable polling.