# Deploying to a cloud VM

**Owner:** P2 (Week 8). Covers course criterion 11.

> **Status: written, NOT yet executed.** Nothing here has been run on a cloud machine, because it needs a cloud account. The steps
> are what the stack's own files imply (`docker-compose.yml`, `docs/running_the_stack.md`); treat them as a checklist to confirm,
> and fix this page when a step turns out different. Do not describe the project as "deployed" until the checks at the end pass
> on the VM.

## What is deployed

The whole stack is one `docker compose` project (16 services: API, UI, Jenkins, the three agents, the Coordinator, OPA, Prometheus,
Grafana, PostgreSQL, RabbitMQ, two MCP servers, a mail catcher and the demo target app), so the simplest honest deployment is **one virtual
machine running the same compose file**, not a managed-services rewrite. This is deployment of the real system, not a demo build.

## Pick a machine

- Any of AWS EC2, Google Compute Engine, Azure VM (a free-tier or student credit is enough for a demo).
- Ubuntu 22.04 or 24.04, **4 vCPU and 8 GB RAM minimum, 16 GB recommended** (Jenkins, Grafana, PostgreSQL, RabbitMQ and several
  Python services at once; an under-sized machine shows up as services restarting), 40 GB disk.
- Reserve a static IP (or use a domain name).

## Network: expose only what has its own sign-in

`docker-compose.yml` already binds every internal and developer service to `127.0.0.1`. Only three ports listen on all interfaces:

| Port | Service | Has its own sign-in |
|---|---|---|
| 3001 | Web UI | yes (the admin from `.env`) |
| 8000 | API | yes |
| 8080 | Jenkins | yes |

Open **only** those in the cloud firewall / security group, and restrict the source to your own IP or your demo audience where you
can. Do not open 5672, 15672, 5432, 9090, 3000, 8181 or the agent ports. Put the UI (and ideally the API and Jenkins) behind HTTPS:
a reverse proxy such as Caddy or nginx with a Let's Encrypt certificate, or the provider's load balancer. Without TLS, passwords and
session cookies cross the internet in clear text.

## Steps

```bash
# 1. Docker
curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER      # log out and in again

# 2. The code
git clone <your repository URL> jenkins_guardian && cd jenkins_guardian

# 3. Secrets: copy and fill in. Never commit .env.
cp .env.example .env && nano .env
#    needs: UI_SESSION_SECRET (32+ chars), UI_ADMIN_EMAIL (name@example.com form), UI_ADMIN_PASSWORD (10+ chars),
#    GEMINI_API_KEY, GITHUB_TOKEN, GITHUB_REPO, JENKINS_ADMIN_PASSWORD, JENKINS_USER, JENKINS_API_TOKEN,
#    POSTGRES_PASSWORD, RABBITMQ_PASSWORD, GRAFANA_ADMIN_PASSWORD, WEBHOOK_SHARED_SECRET, APPROVAL_API_TOKEN, A2A_SHARED_TOKEN
#    (use long random values for every secret; the full list is .env.example)
#    behind HTTPS also set UI_COOKIE_SECURE=true and GRAFANA_PUBLIC_URL to the public address

# 4. Build and start (the first build takes several minutes)
docker compose up -d --build
docker compose ps        # everything "healthy" except target-app and mailhog (they have no probe)
```

If Docker Hub is slow from the cloud region, nothing special is needed (the override file in `docs/running_the_stack.md` is for
networks that cannot reach it). The UI builds its static files inside the image on the VM, so `ui/Dockerfile.prebuilt` is **not**
used there.

## Jenkins and the Docker socket

Jenkins builds and runs the demo app through the host's Docker socket (the pipeline's Deploy stage). The container needs the socket's
group: on the VM set `DOCKER_GID` in `.env` to `getent group docker | cut -d: -f3` (on Docker Desktop it is `0`). Without it the Deploy
stage fails with `permission denied` on `/var/run/docker.sock`. Note what this means: anyone who can push a branch that Jenkins builds can
control the host's Docker. Restrict who can push, and keep secrets out of the build environment (the compose file already passes Jenkins
only the variables its own config reads).

## Connect GitHub and Jenkins

A real deployment needs Jenkins to reach the repository and the repository's pipeline to reach Jenkins and the API:

1. `JENKINS_URL` in `.env` is what the helper scripts use from outside; the containers talk to Jenkins at `http://jenkins:8080`
   inside the compose network, so nothing else needs the public address except the webhook below.
2. Jenkins finds new branches by itself (a scan every 2 minutes) and the build then reports to the API. If you also use a GitHub
   webhook, it must reach the API (`/webhooks/...`) over HTTPS, and the `WEBHOOK_SHARED_SECRET` must match on both sides.
3. Re-check the credential type in `jenkins/jenkins.yaml` (Username/Password) and rebuild Jenkins after any change:
   `docker compose up -d --build jenkins`.

## Checks that make "deployed" true

First, on the VM, in the project folder:

```bash
python3 scripts/preflight.py --build      # standard library only; exit code 1 if anything FAILs
```

It checks that every container is healthy **and** that the stack can do its job: the Gemini model answers, the MCP tools accept the agents,
a build of `main` passes its tests and Deploy, Jenkins cannot see other services' secrets, Prometheus sees the app, remediation is enabled
and the GitHub token can push. Keep its output as evidence. All of the failures it catches happened silently on the laptop.

Run these **on the VM** and keep the output (a screenshot or a pasted terminal) as the evidence:

```bash
docker compose ps                                          # all healthy
curl -s http://127.0.0.1:8000/health/live                  # {"status": ...}
python3 scripts/check_dashboards.py                        # needs the Python venv; or open Grafana through the UI
```

Then from **another machine**: open `https://<your host>:3001`, sign in, and see a project and its incidents; create a real incident
by pushing a fault branch (`scripts/inject_all_faults.ps1` is Windows PowerShell; on Linux use `scripts/inject_fault.py` from a clean
non-main branch) and watch it appear with three agents' findings and a report. That end-to-end run on the VM is the proof.

## Costs and safety

- Stop the VM when you are not demoing; `docker compose down` keeps the data, `down -v` deletes it.
- The agents call Gemini and GitHub with the keys in `.env`: use a key with a quota you can afford, never share the VM's `.env`.
- Keep the policy gate and the human approval on. Remediation opens pull requests on the real repository.
- Back up `data/` and the PostgreSQL volume if the audit trail matters beyond the demo.

## Not covered

High availability, autoscaling, managed databases, secrets managers and CI/CD for the deployment itself. For a single-VM demo they
are out of scope; for real use they would be the next steps (managed PostgreSQL and RabbitMQ first).
