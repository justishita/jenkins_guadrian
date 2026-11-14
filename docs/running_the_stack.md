# Running the stack

**Owner:** P2 (Week 8, Docker Compose cleanup). From a clean checkout to a working demo, and what to do when it is not.

## You need

- Docker Desktop (Compose v2). On Windows it runs on WSL2.
- Python 3.12 and a virtualenv in `.venv` (for the tests and the scripts); Node 22 only if you change the web UI.
- A `.env`. Copy `.env.example` and fill it in. **Never commit it**: it holds the GitHub token, the Gemini key and the
  passwords. The web UI needs three values: `UI_SESSION_SECRET` (32+ random characters), `UI_ADMIN_EMAIL` (a real-looking
  address: `name@example.com`; a domain with an underscore is refused) and `UI_ADMIN_PASSWORD` (10+ characters). The first
  admin is created from them **only while there are no users yet**; later changes to those two values do nothing.

## First run

```bash
docker compose up -d --build        # the first build takes several minutes
docker compose ps                   # everything should say "healthy" (mailhog and target-app have no probe)
```

Then open the web UI and sign in as that admin.

| What | Where | Reachable from |
|---|---|---|
| **Web UI** (projects, incidents, reviews, charts, embedded Grafana) | http://localhost:3001 | other machines too |
| API | http://localhost:8000 (`/health/live`) | other machines too |
| Jenkins | http://localhost:8080 | other machines too (it has its own sign-in) |
| Grafana, admin login | http://127.0.0.1:3000/grafana/ | this machine only |
| Prometheus, RabbitMQ (`:15672`), OPA (`:8181`), Mailhog (`:8025`), the agents (`:9100-9103`), the MCP servers (`:8081-8082`) | `http://127.0.0.1:<port>` | this machine only |

Use `127.0.0.1`, not `localhost`, for the admin Grafana: browsers share cookies between `localhost:3000` and `localhost:3001`,
and the admin session cookie confuses the embedded dashboards.

## If a port is taken, or Docker Hub is unreachable

`docker-compose.override.example.yml` shows both fixes. Copy it to `docker-compose.override.yml` (git-ignored, yours alone) and
edit. After a change to a file the images bake in (`jenkins/jenkins.yaml`, the nginx config) rebuild that service:
`docker compose up -d --build jenkins`.

## Day to day

```bash
docker compose logs -f agent-api            # follow one service
docker compose up -d --build coordinator    # rebuild and restart one service after changing its code
docker compose restart grafana              # pick up a dashboard or alert-rule file
docker compose down                         # stop, keep the data
docker compose down -v                      # stop AND DELETE the database, Jenkins home and Grafana data
docker compose up -d --remove-orphans       # also clear containers left from a renamed service
```

After Docker restarts, every service comes back by itself (`restart: unless-stopped`).

## Try it

```bash
.venv/Scripts/python scripts/inject_fault.py tc01_unit_test         # from a non-main branch with a clean tree; see below
.venv/Scripts/python scripts/check_audit.py --latest                # the audit trail of the newest incident
.venv/Scripts/python scripts/check_dashboards.py                    # every dashboard panel against the real stack
.venv/Scripts/python scripts/evaluate_coordinator.py                # Coordinator evaluation -> results/coordinator_eval.md
.venv/Scripts/python -m scripts.score_metrics_agent                 # Metrics agent evaluation (--heldout N for random cases)
```

`inject_fault.py` refuses to run on `main` and needs a completely clean tree (untracked files included). To inject all four
fault types for the end-to-end accuracy evaluation without disturbing your working tree:
`powershell -File scripts\inject_all_faults.ps1 -Rounds 2`, wait 10-15 minutes, then
`.venv\Scripts\python scripts\evaluate_end_to_end.py`. It pushes `fault/...` branches to GitHub; delete them afterwards with
`inject_fault.py revert <branch>`.

## When something is wrong

| Symptom | Cause | Fix |
|---|---|---|
| Docker Desktop: "distro installation timeout" | WSL did not answer | `wsl --shutdown`, reopen Docker Desktop; if the WSL service will not stop, reboot Windows. Data in volumes is safe |
| Build fails: `TLS handshake timeout` fetching `node` | Docker Hub unreachable | use the offline UI build in the example override (`ui/Dockerfile.prebuilt`) |
| Login page says "Sign-in is not configured" | `UI_SESSION_SECRET` missing or under 32 characters | set it in `.env`, `docker compose up -d agent-api` |
| Cannot sign in, no admin exists | seeding skipped: email invalid, password under 10 characters, or users already exist | fix `.env`, recreate `agent-api`; check `docker compose logs agent-api \| grep -i seed` |
| A new fault branch takes minutes to become an incident | Jenkins scans branches every 2 minutes, then the build runs | use "Scan Multibranch Pipeline Now" in Jenkins |
| Jenkins log: "no credentials, anonymous access"; scans crawl | the GitHub credential was Secret text, which the scanner cannot use | fixed in `jenkins/jenkins.yaml` (Username/Password); rebuild the Jenkins image |
| `agent-api` logs "Jenkins catch-up scan failed" every minute | `JENKINS_API_TOKEN` is not set | set `JENKINS_USER` and `JENKINS_API_TOKEN` in `.env`; the webhook path works without it |
| Embedded Grafana keeps reloading | stale Grafana cookie plus a failing token-rotation call | fixed in `ui/nginx.conf`; clear `localhost` cookies once and use `127.0.0.1` for the admin Grafana |
| A dashboard panel says "No data" | the event has not happened yet (no approval, no re-validation) | `check_dashboards.py` lists the panels that are empty and the ones that are broken |
| `inject_fault.py`: "refusing to run on main" / "working tree must be clean" | safety checks in the script | use `scripts\inject_all_faults.ps1`, or a throwaway branch with a clean tree |
