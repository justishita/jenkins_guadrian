# Deploying JenkinsGuardian on Azure (one VM)

> **Status: written, not yet executed.** Nothing here has run against a real Azure subscription. Treat every command as something to read
> before you run it, and fix this page when a step turns out different. Do not call the project "deployed" until step 6 passes.

One Ubuntu VM runs the same `docker compose` stack as the laptop, with Caddy in front for HTTPS. Only ports 22 (your IP only), 80 and 443
are open in Azure; the API, Jenkins and the dev tools are not reachable from the internet. Jenkins finds new branches by scanning GitHub every
2 minutes, so no inbound webhook is needed.

**Cost.** `Standard_B4ms` (4 vCPU, 16 GB) is roughly 0.15-0.17 USD per hour (check the Azure pricing calculator). Running day and night would
use a $100 student credit in about 3-4 weeks. A **deallocated** VM costs no compute, only its disk (a few dollars a month): start it to work
or demo, deallocate it after.

**Time.** About 3 hours if nothing surprises you; the first `docker compose up --build` alone is 15-25 minutes.

## 1. Create the VM (on your laptop, about 20 minutes)

Install the CLI once (`winget install Microsoft.AzureCLI`), then:

```powershell
az login
```

Find a region your student subscription allows (it is usually limited to a few):

```powershell
az account list-locations --query "[].name" -o tsv
```

```powershell
.\deploy\azure\create_vm.ps1 -Location <allowed-region> -MyIp (Invoke-RestMethod https://api.ipify.org)
```

It prints the VM's address, `<label>.<region>.cloudapp.azure.com`. Use it everywhere below as `<fqdn>`. If the portal or CLI says the size is
unavailable in that region, try another allowed region, or `-Size Standard_D4s_v5`.

## 2. Set up the machine (on the VM, about 20 minutes)

```powershell
ssh azureuser@<fqdn>
```

On the VM (copy `setup_vm.sh` over first with `scp deploy/azure/setup_vm.sh azureuser@<fqdn>:` if the repository is private):

```bash
bash setup_vm.sh <git clone URL> <fqdn>
```

It installs Docker, clones the code to `~/jenkins_guardian`, starts Caddy, and prints the values you need in `.env`. **Log out and back in** so the
`docker` group applies.

## 3. Copy and adapt `.env` (about 10 minutes)

`.env` holds the secrets, so it is never committed: copy it from your laptop.

```powershell
scp .env azureuser@<fqdn>:jenkins_guardian/.env
```

On the VM, fix the line endings (a Windows file has CRLF, which can leave a stray `\r` in every value) and then change four values:

```bash
cd ~/jenkins_guardian && sed -i 's/\r$//' .env && nano .env
```

| Variable | Set to |
|---|---|
| `DOCKER_GID` | the number `setup_vm.sh` printed (`getent group docker \| cut -d: -f3`). On the laptop it was `0`; on Linux it is the real group |
| `JENKINS_URL` | `http://localhost:8080` (the laptop `.env` has the remapped port) |
| `UI_COOKIE_SECURE` | `true` |
| `GRAFANA_PUBLIC_URL` | `https://<fqdn>/grafana` |

Keep `GEMINI_MODEL` as it is (or unset: the compose file defaults to a model that is still served). Do not copy a `docker-compose.override.yml`: it
holds laptop-only port remaps and the offline UI build.

## 4. Start it (about 25 minutes the first time)

```bash
docker compose up -d --build
```

```bash
docker compose ps
```

Everything should reach `healthy` (`mailhog` and `target-app` have no probe).

## 5. Check that it can really do its job

```bash
python3 scripts/preflight.py --build
```

It checks the Gemini model, the MCP tools, a real build of `main`, Jenkins's environment, Prometheus, remediation and GitHub access, and prints the
fix for each failure. Run it until it ends with `0 failed`.

## 6. Prove it works from outside (the evidence)

1. From another machine or your phone: open `https://<fqdn>`, sign in as the admin from `.env`, open the project.
2. Create a real incident: from your laptop, in a clean non-main branch, `python scripts/inject_fault.py tc01_unit_test` (it pushes a branch).
   Within a few minutes it should appear in the UI with the three agents' findings and a report.
3. Keep the evidence: a screenshot of the UI at the `https://<fqdn>` address with the incident, the output of `docker compose ps`, and the output of
   `preflight.py`. Then delete the fault branch: `python scripts/inject_fault.py revert <branch>`.

## Stopping, restarting, deleting

```powershell
az vm deallocate --resource-group jguardian-rg --name jguardian
```

```powershell
az vm start --resource-group jguardian-rg --name jguardian
```

The containers come back by themselves (`restart: unless-stopped`); run `preflight.py` again after a start. The address (`<fqdn>`) stays the same.
To remove everything and stop all charges: `az group delete --name jguardian-rg --yes --no-wait`.

## If something fails

| Symptom | Likely cause |
|---|---|
| `SkuNotAvailable` / quota error creating the VM | the region or size is not allowed for the student subscription: try another allowed region or size |
| Caddy cannot get a certificate | port 80 or 443 is not open in Azure (`az vm open-port`), or the VM was started under a different name than `<fqdn>` |
| Jenkins build fails at Deploy with `permission denied ... docker.sock` | `DOCKER_GID` is wrong: `docker compose exec jenkins sh -c 'stat -c %g /var/run/docker.sock; id'` |
| Everything is up but the UI says "Request failed (502)" after a recreate | a stale upstream address: fixed in `ui/nginx.conf`; `docker compose restart ui` if you see it on an old image |
| Agents answer but with `Rule-based classification` summaries | the Gemini model or key: read the LLM line of `preflight.py` |
| Out of memory, containers restarting | the VM is too small: resize it (`az vm resize`) to 16 GB |

## Security notes

- The NSG is the only firewall: Docker publishes the API (8000) and Jenkins (8080) on all interfaces of the VM, so do not open those ports in Azure.
  Reach Jenkins with an SSH tunnel: `ssh -L 8080:localhost:8080 azureuser@<fqdn>`, then http://localhost:8080.
- The VM holds the Gemini key, the GitHub token and the Docker socket (Jenkins builds can control the host's Docker). Restrict who can push
  branches to the repository, never share the VM's `.env`, and delete the resource group when the project is over.
