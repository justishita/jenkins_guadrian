#!/usr/bin/env python3
"""Preflight: can this running stack actually do its job? (Week 8, P2)

`docker compose ps` says "healthy" for a stack that, in practice, did nothing useful for days. Each check here exists because
something failed *silently* and was only found by reading logs:

  * the LLM model name was retired for the API project (HTTP 404) and the agents quietly fell back to rules;
  * the MCP servers refused every tool call (HTTP 421 "Invalid Host header"), so the LLM, with no build log, invented diagnoses;
  * Jenkins was handed the whole `.env`: the backend's DATABASE_URL broke every test run, and every secret sat in the build env;
  * Jenkins could not use the Docker socket (wrong DOCKER_GID), so the Deploy stage failed;
  * Prometheus scraped a host that did not exist, so the Metrics agent said "target down" for every incident;
  * remediation was switched off (an empty APPROVAL_API_TOKEN) so no fix was ever proposed.

    python scripts/preflight.py                # all checks; exit 1 if any FAIL
    python scripts/preflight.py --skip-llm     # no Gemini call (it costs one tiny request)
    python scripts/preflight.py --build        # also start a build of main and wait for it (tests + Deploy really run)
    python scripts/preflight.py --json         # machine-readable

Standard library only, so it runs on a fresh VM. It reads values from inside the containers and never prints a secret: only
whether a name is set, status codes, and model names. Jenkins credentials come from the environment or `.env`.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"

#: What the Jenkins container must have (its config reads these) and must NOT have (they belong to other services, and builds run
#: branch code that can read the environment).
JENKINS_REQUIRED = [
    "JENKINS_ADMIN_USER", "JENKINS_ADMIN_PASSWORD", "JENKINS_URL", "GITHUB_USERNAME", "GITHUB_TOKEN", "GITHUB_REPO",
    "GITHUB_REPO_OWNER", "GITHUB_REPO_NAME", "GITHUB_CREDENTIALS_ID", "WEBHOOK_SHARED_SECRET",
]
JENKINS_FORBIDDEN = ["DATABASE_URL", "GEMINI_API_KEY", "POSTGRES_PASSWORD", "RABBITMQ_PASSWORD", "APPROVAL_API_TOKEN", "A2A_SHARED_TOKEN", "UI_SESSION_SECRET"]
REMEDIATION_NEEDS = ["GITHUB_TOKEN", "GITHUB_REPO", "OPA_URL", "BACKEND_URL", "APPROVAL_API_TOKEN"]
LLM_AGENTS = ["jenkins-agent", "code-agent", "metrics-agent"]


@dataclass
class Result:
    name: str
    status: str
    detail: str
    hint: str = ""


# ---- probes that run INSIDE a container (they print one JSON line) ----

LLM_PROBE = r'''
import json, os, urllib.request, urllib.error
out = {"key_set": bool(os.getenv("GEMINI_API_KEY")), "offline": os.getenv("LLM_OFFLINE", "0") == "1", "model": os.getenv("GEMINI_MODEL") or ""}
try:
    from common.llm_client import LLMClient
    out["model"] = LLMClient().model_name
except Exception as error:
    out["client_error"] = type(error).__name__
if out["key_set"] and not out["offline"] and not os.getenv("PREFLIGHT_SKIP_LLM"):
    body = json.dumps({"contents": [{"parts": [{"text": "Reply with the single word: ok"}]}], "generationConfig": {"maxOutputTokens": 20}}).encode()
    url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % out["model"]
    request = urllib.request.Request(url, data=body, headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(request, timeout=40)
        out["status"] = 200
    except urllib.error.HTTPError as error:
        out["status"] = error.code
        try:
            out["message"] = json.loads(error.read() or b"{}").get("error", {}).get("message", "")[:110]
        except Exception:
            out["message"] = ""
    except Exception as error:
        out["status"] = 0
        out["message"] = type(error).__name__
print(json.dumps(out))
'''

MCP_PROBE = r'''
import json, os, urllib.request, urllib.error
body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "preflight", "version": "1"}}}).encode()
out = {}
for name, env, default in (("jenkins", "MCP_JENKINS_URL", "http://mcp-jenkins:8081/mcp"), ("analytics", "MCP_ANALYTICS_URL", "http://mcp-analytics:8082/mcp")):
    url = os.getenv(env) or default
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    try:
        out[name] = {"url": url, "status": urllib.request.urlopen(request, timeout=15).status}
    except urllib.error.HTTPError as error:
        out[name] = {"url": url, "status": error.code, "body": error.read()[:70].decode("utf-8", "replace")}
    except Exception as error:
        out[name] = {"url": url, "status": 0, "body": type(error).__name__}
print(json.dumps(out))
'''

PROMETHEUS_PROBE = r'''
import json, os, urllib.parse, urllib.request
base = (os.getenv("PROMETHEUS_URL") or "http://prometheus:9090").rstrip("/")
query = urllib.parse.quote('up{job="target_app"}')
out = {"url": base}
try:
    data = json.load(urllib.request.urlopen(base + "/api/v1/query?query=" + query, timeout=15))
    result = data.get("data", {}).get("result", [])
    out["status"] = 200
    out["value"] = result[0]["value"][1] if result else None
except Exception as error:
    out["status"] = 0
    out["error"] = type(error).__name__
print(json.dumps(out))
'''

GITHUB_PROBE = r'''
import json, os, urllib.request, urllib.error
repo, token = os.getenv("GITHUB_REPO", ""), os.getenv("GITHUB_TOKEN", "")
api = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")
out = {"repo_set": bool(repo), "token_set": bool(token)}
if repo and token:
    request = urllib.request.Request(api + "/repos/" + repo, headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "User-Agent": "preflight"})
    try:
        data = json.load(urllib.request.urlopen(request, timeout=20))
        out["status"] = 200
        out["push"] = bool((data.get("permissions") or {}).get("push"))
    except urllib.error.HTTPError as error:
        out["status"] = error.code
    except Exception as error:
        out["status"] = 0
        out["error"] = type(error).__name__
print(json.dumps(out))
'''


class Stack:
    """The running compose project, reached through `docker compose` (so it works wherever the stack runs)."""

    def __init__(self, root: Path = ROOT, skip_llm: bool = False) -> None:
        self.root = root
        self.skip_llm = skip_llm
        self._env_file: dict[str, str] | None = None

    def _run(self, argv: list[str], timeout: int = 120) -> tuple[int, str]:
        try:
            done = subprocess.run(argv, cwd=self.root, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        except (subprocess.TimeoutExpired, FileNotFoundError) as error:
            return 127, type(error).__name__
        return done.returncode, (done.stdout + done.stderr)

    def exec_sh(self, service: str, script: str, timeout: int = 120) -> tuple[int, str]:
        return self._run(["docker", "compose", "exec", "-T", service, "sh", "-c", script], timeout)

    def exec_py(self, service: str, source: str, timeout: int = 120) -> dict:
        env = ["-e", "PREFLIGHT_SKIP_LLM=1"] if self.skip_llm else []
        rc, out = self._run(["docker", "compose", "exec", "-T", *env, service, "python", "-c", source], timeout)
        for line in reversed(out.strip().splitlines()):
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    break
        return {"_error": f"no JSON from {service} (exit {rc}): {out.strip()[-120:]}"}

    def ps(self) -> list[dict]:
        rc, out = self._run(["docker", "compose", "ps", "--all", "--format", "json"])
        text = out.strip()
        if rc != 0 or not text:
            return []
        try:
            data = json.loads(text)
            return data if isinstance(data, list) else [data]
        except json.JSONDecodeError:
            return [json.loads(line) for line in text.splitlines() if line.startswith("{")]

    def jenkins_base(self) -> str | None:
        rc, out = self._run(["docker", "compose", "port", "jenkins", "8080"])
        match = re.search(r":(\d+)\s*$", out.strip())
        return f"http://127.0.0.1:{match.group(1)}" if rc == 0 and match else None

    def jenkins_credentials(self) -> tuple[str, str] | None:
        values = {k: os.environ[k] for k in ("JENKINS_USER", "JENKINS_API_TOKEN") if os.environ.get(k)}
        if len(values) < 2:
            env_path = self.root / ".env"
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8-sig").splitlines():
                    match = re.match(r"\s*(JENKINS_USER|JENKINS_API_TOKEN)\s*=\s*(.*?)\s*$", line)
                    if match and match.group(2).strip("\"'"):
                        values.setdefault(match.group(1), match.group(2).strip("\"'"))
        return (values["JENKINS_USER"], values["JENKINS_API_TOKEN"]) if len(values) == 2 else None

    def jenkins_request(self, method: str, path: str) -> tuple[int, dict | None]:
        base, creds = self.jenkins_base(), self.jenkins_credentials()
        if not base or not creds:
            raise RuntimeError("no Jenkins address or credentials")
        token = base64.b64encode(f"{creds[0]}:{creds[1]}".encode()).decode()
        request = urllib.request.Request(base + path, data=b"" if method == "POST" else None, method=method, headers={"Authorization": "Basic " + token})
        try:
            with urllib.request.urlopen(request, timeout=30) as reply:
                text = reply.read().decode("utf-8", "replace")
                return reply.status, (json.loads(text) if text.startswith("{") else None)
        except urllib.error.HTTPError as error:
            return error.code, None


# ---- checks ----


def check_services(stack: Stack) -> list[Result]:
    rows = stack.ps()
    if not rows:
        return [Result("services", FAIL, "no containers found", "Is the stack up? `docker compose up -d`, then run this from the project folder")]
    bad = [f"{r.get('Service', r.get('Name'))} ({r.get('State')}/{r.get('Health') or 'no probe'})" for r in rows if r.get("State") != "running" or r.get("Health") == "unhealthy"]
    starting = [r.get("Service", r.get("Name")) for r in rows if r.get("Health") == "starting"]
    if bad:
        return [Result("services", FAIL, "not running or unhealthy: " + ", ".join(bad), "docker compose logs <service> --tail 50")]
    if starting:
        return [Result("services", WARN, f"{len(rows)} running, still starting: {', '.join(starting)}", "wait a minute and run again")]
    return [Result("services", PASS, f"{len(rows)} containers running, none unhealthy")]


def check_shell_env(stack: Stack) -> list[Result]:
    if os.environ.get("DOCKER_GID"):
        return [Result("shell DOCKER_GID", WARN, "set in this shell: it overrides .env whenever compose runs from here", "Remove-Item Env:DOCKER_GID  (PowerShell)  /  unset DOCKER_GID")]
    return [Result("shell DOCKER_GID", PASS, "not set in the shell, so .env decides")]


def check_jenkins_env(stack: Stack) -> list[Result]:
    names = JENKINS_REQUIRED + JENKINS_FORBIDDEN
    rc, out = stack.exec_sh("jenkins", "for v in " + " ".join(names) + '; do if [ -n "$(printenv $v)" ]; then echo "SET $v"; else echo "UNSET $v"; fi; done')
    if rc != 0:
        return [Result("jenkins environment", FAIL, "could not read the Jenkins container", "docker compose ps jenkins")]
    present = {line.split()[1] for line in out.splitlines() if line.startswith("SET ")}
    leaked = [v for v in JENKINS_FORBIDDEN if v in present]
    missing = [v for v in JENKINS_REQUIRED if v not in present]
    results = []
    if leaked:
        results.append(Result("jenkins environment (secrets)", FAIL, "builds can see: " + ", ".join(leaked),
                              "docker-compose.yml: the jenkins service must list only the variables its config reads (no env_file). DATABASE_URL there also breaks every test run"))
    else:
        results.append(Result("jenkins environment (secrets)", PASS, "no other service's secrets are visible to builds"))
    if missing:
        results.append(Result("jenkins environment (config)", FAIL, "missing: " + ", ".join(missing),
                              "set them in .env, then `docker compose up -d --no-deps jenkins`. A missing GITHUB_REPO_OWNER/NAME gives 'Repository name must be in format owner/repo'"))
    else:
        results.append(Result("jenkins environment (config)", PASS, "everything Jenkins's own config reads is set"))
    return results


def check_docker_socket(stack: Stack) -> list[Result]:
    rc, out = stack.exec_sh("jenkins", 'docker version --format "{{.Server.Version}}" 2>&1 | head -3')
    text = out.strip()
    if rc == 0 and re.match(r"^\d+\.\d+", text):
        return [Result("jenkins -> docker socket", PASS, f"Docker server {text.splitlines()[0]} reachable (the Deploy stage can run)")]
    if "permission denied" in text.lower():
        return [Result("jenkins -> docker socket", FAIL, "permission denied on /var/run/docker.sock",
                       "Set DOCKER_GID in .env: 0 on Docker Desktop, `getent group docker | cut -d: -f3` on Linux; then `docker compose up -d --no-deps jenkins`")]
    return [Result("jenkins -> docker socket", FAIL, text[:120] or "docker not reachable from the Jenkins container", "check the docker.sock mount and DOCKER_GID")]


def check_jenkins_build(stack: Stack, trigger: bool = False) -> list[Result]:
    try:
        status, info = stack.jenkins_request("GET", "/job/target-app/job/main/api/json?tree=lastBuild[number,result,building]")
    except RuntimeError:
        return [Result("jenkins build of main", SKIP, "no Jenkins credentials (JENKINS_USER / JENKINS_API_TOKEN in the environment or .env)")]
    if status != 200 or info is None:
        return [Result("jenkins build of main", FAIL, f"job target-app/main not found (HTTP {status})", "scan the multibranch job; check GITHUB_REPO_OWNER/NAME and the GitHub credential")]
    last = info.get("lastBuild") or {}
    if trigger:
        before = last.get("number") or 0
        status, _ = stack.jenkins_request("POST", "/job/target-app/job/main/buildWithParameters")
        if status not in (200, 201):
            return [Result("jenkins build of main", FAIL, f"could not start a build (HTTP {status})")]
        for _ in range(60):
            time.sleep(10)
            status, info = stack.jenkins_request("GET", "/job/target-app/job/main/api/json?tree=lastBuild[number,result,building]")
            last = (info or {}).get("lastBuild") or {}
            if last.get("number", 0) > before and not last.get("building"):
                break
        else:
            return [Result("jenkins build of main", WARN, "the build did not finish in 10 minutes")]
    if not last:
        return [Result("jenkins build of main", WARN, "main has never been built", "run with --build, or start a build in Jenkins")]
    if last.get("building"):
        return [Result("jenkins build of main", WARN, f"build #{last['number']} is still running")]
    if last.get("result") == "SUCCESS":
        return [Result("jenkins build of main", PASS, f"build #{last['number']} SUCCESS (checkout, tests, compile and deploy all ran)")]
    return [Result("jenkins build of main", FAIL, f"build #{last['number']} is {last.get('result')}",
                   "open its console. Common causes: tests dying at collection (a leaked DATABASE_URL), Deploy denied on docker.sock (DOCKER_GID), a bad repo name")]


def check_llm(stack: Stack) -> list[Result]:
    probes = {agent: stack.exec_py(agent, LLM_PROBE, timeout=90) for agent in LLM_AGENTS}
    broken = {a: p["_error"] for a, p in probes.items() if "_error" in p}
    if broken:
        return [Result("LLM", FAIL, "; ".join(f"{a}: {e}" for a, e in broken.items()), "docker compose ps; docker compose logs <agent>")]
    models = {p["model"] for p in probes.values()}
    first = probes[LLM_AGENTS[0]]
    results = []
    if len(models) > 1:
        results.append(Result("LLM model (agents agree)", WARN, f"agents use different models: {sorted(models)}", "pass GEMINI_MODEL to every LLM-calling service (docker-compose.yml)"))
    if not first["key_set"]:
        return results + [Result("LLM", FAIL, "GEMINI_API_KEY is not set in the agents", "set it in .env and recreate the agents")]
    if first["offline"]:
        return results + [Result("LLM", WARN, "LLM_OFFLINE=1: the agents use rules only, by choice", "set LLM_OFFLINE=0 to use the model")]
    status, model = first.get("status"), first["model"]
    if status is None:
        return results + [Result("LLM", SKIP, f"model {model}: not called (--skip-llm)")]
    if status == 200:
        return results + [Result("LLM", PASS, f"model {model} answered")]
    if status == 404:
        return results + [Result("LLM", FAIL, f"model {model} is not served to this API project (404: {first.get('message', '')})",
                                 'List what your key can use: curl -H "x-goog-api-key: $GEMINI_API_KEY" https://generativelanguage.googleapis.com/v1beta/models ; then set GEMINI_MODEL in .env and `docker compose up -d jenkins-agent code-agent metrics-agent`. Without this the agents silently fall back to rules')]
    if status in (401, 403):
        return results + [Result("LLM", FAIL, f"the key was refused (HTTP {status})", "check GEMINI_API_KEY and that the Generative Language API is enabled for its project")]
    if status == 429:
        return results + [Result("LLM", WARN, f"model {model}: quota or rate limit (429)", "wait, lower LLM_REQUESTS_PER_MINUTE, or use another model")]
    return results + [Result("LLM", WARN, f"model {model}: HTTP {status} {first.get('message', '')} (often temporary)", "run again; if it persists pick another model")]


def check_mcp(stack: Stack) -> list[Result]:
    probe = stack.exec_py("jenkins-agent", MCP_PROBE)
    if "_error" in probe:
        return [Result("MCP servers", FAIL, probe["_error"])]
    results = []
    for name, info in probe.items():
        label = f"MCP {name}"
        if info["status"] == 200:
            results.append(Result(label, PASS, f"{info['url']} accepts the agent's requests"))
        elif info["status"] == 421:
            results.append(Result(label, FAIL, f"{info['url']} answers 421 Invalid Host header: every tool call fails and the LLM invents the diagnosis",
                                  "the MCP server must allow its own service name: see _transport_security in mcp_servers/*/server.py (or set MCP_ALLOWED_HOSTS), then rebuild it"))
        else:
            results.append(Result(label, FAIL, f"{info['url']} -> HTTP {info['status']} {info.get('body', '')}", "docker compose logs mcp-jenkins mcp-analytics"))
    return results


def check_prometheus(stack: Stack) -> list[Result]:
    probe = stack.exec_py("metrics-agent", PROMETHEUS_PROBE)
    if "_error" in probe or probe.get("status") != 200:
        return [Result("Prometheus target", FAIL, probe.get("_error") or f"cannot query {probe.get('url')} ({probe.get('error')})", "docker compose logs prometheus")]
    if probe.get("value") == "1":
        return [Result("Prometheus target", PASS, 'up{job="target_app"} = 1: the Metrics agent sees the app')]
    return [Result("Prometheus target", FAIL, f'up{{job="target_app"}} = {probe.get("value")}: the Metrics agent will report "target down" for every incident',
                   "Prometheus > Status > Targets; the address in monitoring/prometheus.yml must be a compose service name (target-app:8000)")]


def check_remediation(stack: Stack) -> list[Result]:
    rc, out = stack.exec_sh("coordinator", "for v in " + " ".join(REMEDIATION_NEEDS) + '; do if [ -n "$(printenv $v)" ]; then echo "SET $v"; else echo "UNSET $v"; fi; done; echo "REMEDIATION_ENABLED=${REMEDIATION_ENABLED:-1}"')
    if rc != 0:
        return [Result("remediation", FAIL, "could not read the Coordinator container", "docker compose ps coordinator")]
    present = {line.split()[1] for line in out.splitlines() if line.startswith("SET ")}
    missing = [v for v in REMEDIATION_NEEDS if v not in present]
    if "REMEDIATION_ENABLED=0" in out:
        return [Result("remediation", WARN, "switched off on purpose (REMEDIATION_ENABLED=0): no fix will be proposed")]
    if missing:
        return [Result("remediation", FAIL, "disabled: missing " + ", ".join(missing) + " (no fix is ever proposed, and nothing says so)",
                       "set them in .env (APPROVAL_API_TOKEN is any long random string), then `docker compose up -d agent-api coordinator`")]
    return [Result("remediation", PASS, "enabled: the Coordinator has everything it needs to propose fixes")]


def check_github(stack: Stack) -> list[Result]:
    probe = stack.exec_py("coordinator", GITHUB_PROBE)
    if "_error" in probe:
        return [Result("GitHub access", FAIL, probe["_error"])]
    if not (probe["repo_set"] and probe["token_set"]):
        return [Result("GitHub access", FAIL, "GITHUB_REPO or GITHUB_TOKEN is not set in the Coordinator", "set both in .env (GITHUB_REPO is owner/repo)")]
    status = probe.get("status")
    if status == 200 and probe.get("push"):
        return [Result("GitHub access", PASS, "the token can read and push to the repository (fix branches and draft PRs can be created)")]
    if status == 200:
        return [Result("GitHub access", WARN, "the token can read the repository but not push", "remediation needs contents and pull-request write access")]
    if status in (401, 403, 404):
        return [Result("GitHub access", FAIL, f"GitHub answered {status} for the repository", "check GITHUB_TOKEN (expired? wrong scopes?) and GITHUB_REPO=owner/repo")]
    return [Result("GitHub access", WARN, f"could not reach GitHub (status {status}, {probe.get('error', '')})")]


def run_all(stack: Stack, *, skip_llm: bool = False, build: bool = False) -> list[Result]:
    results: list[Result] = []
    for check in (check_services, check_shell_env, check_jenkins_env, check_docker_socket):
        results += check(stack)
    results += check_jenkins_build(stack, trigger=build)
    results += check_mcp(stack)
    results += check_prometheus(stack)
    results += check_remediation(stack)
    results += check_github(stack)
    results += check_llm(stack)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-llm", action="store_true", help="do not call Gemini")
    parser.add_argument("--build", action="store_true", help="start a build of main and wait for it")
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    args = parser.parse_args(argv)

    results = run_all(Stack(skip_llm=args.skip_llm), skip_llm=args.skip_llm, build=args.build)
    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        for r in results:
            print(f"[{r.status}] {r.name}: {r.detail}")
            if r.hint and r.status in (FAIL, WARN):
                print(f"       -> {r.hint}")
        counts = {s: sum(1 for r in results if r.status == s) for s in (PASS, WARN, FAIL, SKIP)}
        print(f"\n{counts[PASS]} passed, {counts[WARN]} warnings, {counts[FAIL]} failed, {counts[SKIP]} skipped")
    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
