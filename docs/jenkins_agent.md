# Jenkins Investigation Agent

The **Jenkins Investigation Agent** (`jenkins_agent`) is an automated root-cause analysis worker in the JenkinsGuardian ecosystem. It inspects failing Jenkins CI builds, parses raw console logs and pipeline stage telemetry, checks historical test flakiness, matches deterministic classification rules, correlates prior incidents on the Blackboard, and synthesizes structured, validated diagnostic evidence using an LLM.

---

## Architecture Diagram

```mermaid
flowchart TD
    subgraph Trigger ["Trigger Mechanisms"]
        WH["Jenkins Webhook"] --> BE["Backend Service"]
        BE -->|"Publish incident.created"| RMQ[("RabbitMQ Topic Exchange\n'incidents'")]
        BE -->|"Post initial context"| BB[("Shared Blackboard\n(Redis / File)")]
        COORD["Coordinator Agent"] -->|"A2A POST /tasks\n(Bearer Auth)"| A2A["A2A Server (Port 9101)"]
        RMQ -->|"Consume queue\njenkins_agent.incident.created"| WORKER["Jenkins Worker"]
    end

    subgraph AgentPipeline ["Jenkins Investigation Pipeline"]
        A2A --> RUN["run_investigation()"]
        WORKER --> RUN
        RUN --> PRE["Deterministic Pre-Fetch\n(Console, Stages, Test Report)"]
        PRE --> NORM["Log Normalization &\nRegex Parsing"]
        NORM --> RULE["Rule-Based Classifier\n(RULES in classifier.py)"]
        NORM --> FLAKY["Flakiness Analyzer\n(20-build history)"]
        RULE --> HYP["Initial Hypotheses & Evidence Items"]
        FLAKY --> HYP

        HYP --> DECIDE{"LLM Available &\nNot Offline?"}
        DECIDE -->|Yes| CREW["CrewAI Investigation Flow\n(Tools & Prompts)"]
        DECIDE -->|No / Timeout / Error| FALLBACK["Rule-Based Fallback\n(_fallback_evidence)"]

        CREW --> EXTRACT["Output Extractor &\nPydantic Draft Validation"]
        EXTRACT --> CONF["Confidence Engine\n(Base Score - Penalties)"]
        FALLBACK --> CONF
    end

    subgraph ExternalSources ["External Services & MCP"]
        PRE -->|"HTTP Basic Auth (Read-Only)"| JREST["Jenkins REST API\n(/consoleText, /wfapi/describe)"]
        CREW -->|"Remote MCP"| MCPJ["MCP Jenkins Server\n(Log ranges, stage summaries)"]
        CREW -->|"Remote MCP"| MCPA["MCP Analytics Server\n(Signatures, duplicates)"]
        CREW -->|"Local Tool"| FLTOOL["CheckFlakinessTool\n(Historical build transitions)"]
    end

    subgraph OutputContract ["Evidence Output"]
        CONF --> STORE[("Evidence Store\n./data/evidence/<id>/jenkins_agent.json")]
        CONF --> BBPOST["Blackboard Post\nsection='evidence', key='jenkins_agent'"]
        BBPOST --> A2A_RESP["A2A Response\n(blackboard_ref)"]
    end
```

---

## Event Contract

The Jenkins agent supports dual invocation modes: asynchronous messaging via RabbitMQ and synchronous Agent-to-Agent (A2A) HTTP invocation.

### 1. RabbitMQ Messaging Contract

- **Exchange Name:** `incidents` (Type: `topic`, Durable: `true`)
- **Queue Name:** `jenkins_agent.incident.created` (Durable, quorum queue)
- **Routing Key:** `incident.created`

#### Inbound Message Payload (`incident.created` / `IncidentCreatedEvent`):

```json
{
  "incident_id": "1235ed88-acd3-4ef3-8c82-7f3b59fd8907",
  "job_name": "ecommerce-api/main",
  "build_number": 42,
  "build_url": "http://jenkins.local/job/ecommerce-api/job/main/42/",
  "branch": "main",
  "git_commit": "a1b2c3d4e5f67890123456789abcdef012345678",
  "failed_stage": "Test",
  "remediation_attempt": 0,
  "timestamp": "2026-11-08T14:30:00Z",
  "received_at": "2026-11-08T14:30:01Z",
  "preliminary_fingerprint": "ecommerce-api/main:Test"
}
```

### 2. A2A (Agent-to-Agent) Contract

The A2A interface runs on `http://0.0.0.0:9101` and adheres to the A2A communication specification.

- **Authentication:** All requests must include `Authorization: Bearer <A2A_SHARED_TOKEN>` header. Constant-time comparison prevents timing attacks.
- **Trace Propagation:** Distributed tracing headers (`traceparent`) are extracted and passed through child spans.

#### A2A Discovery Endpoints:
- `GET /.well-known/agent-card.json` (or `/.well-known/agent.json`)
- `GET /health` (`{"status": "ok"}`)

#### Agent Card Specification (`a2a/jenkins_agent.json`):

```json
{
  "name": "jenkins_agent",
  "description": "Analyzes Jenkins CI build logs, pipeline stages, test reports, and git commits to diagnose pipeline failures and validate remediation builds.",
  "version": "1.1.0",
  "url": "http://localhost:9101",
  "skills": [
    {
      "name": "investigate_incident",
      "description": "Performs root-cause analysis on a failed Jenkins build incident and writes findings to the Blackboard.",
      "input_schema": {
        "type": "object",
        "properties": {
          "incident_id": { "type": "string", "format": "uuid" },
          "attempt": { "type": "integer", "default": 0, "minimum": 0, "maximum": 0 }
        },
        "required": ["incident_id"]
      },
      "output_schema": {
        "type": "object",
        "properties": {
          "status": { "type": "string", "enum": ["completed", "failed"] },
          "blackboard_ref": { "type": "object" }
        }
      }
    },
    {
      "name": "reinvestigate_incident",
      "description": "Re-investigates a new validation build after a fix attempt (attempt > 0). Fetches new build logs, compares error signatures, checks test counts, and runs flakiness re-analysis. Writes versioned evidence to the Blackboard.",
      "input_schema": {
        "type": "object",
        "properties": {
          "incident_id": { "type": "string", "format": "uuid" },
          "attempt": { "type": "integer", "minimum": 1 },
          "previous_failure_summary": { "type": "string" },
          "new_build_url": { "type": "string" }
        },
        "required": ["incident_id", "attempt", "new_build_url"]
      },
      "output_schema": {
        "type": "object",
        "properties": {
          "status": { "type": "string", "enum": ["completed", "failed"] },
          "blackboard_ref": { "type": "object" }
        }
      }
    }
  ],
  "capabilities": {
    "push": false,
    "streaming": false
  }
}
```

#### Task Request (`POST /tasks` or `POST /messages`):

```json
{
  "incident_id": "1235ed88-acd3-4ef3-8c82-7f3b59fd8907",
  "attempt": 0,
  "previous_failure_summary": null,
  "new_build_url": null
}
```

#### Task Response:
To maintain Blackboard single-source-of-truth semantics, the A2A server **never returns evidence payloads in the HTTP response body**. It returns a direct reference to the persisted Blackboard entry:

```json
{
  "status": "completed",
  "incident_id": "1235ed88-acd3-4ef3-8c82-7f3b59fd8907",
  "attempt": 0,
  "blackboard_ref": {
    "incident_id": "1235ed88-acd3-4ef3-8c82-7f3b59fd8907",
    "section": "evidence",
    "key": "jenkins_agent",
    "version": 1
  }
}
```

---

## Tool List & Failure Behavior

### 1. Internal HTTP Client (`JenkinsClient`)

Used by the agent's deterministic pre-fetch pipeline. Configured via `JENKINS_URL`, `JENKINS_USER`, and `JENKINS_API_TOKEN`.

| Method | Parameters | HTTP Target | Description | Failure Behavior |
|---|---|---|---|---|
| `get_console_text` | `job: JobPath`, `n: int`, `max_bytes: int = 2_000_000` | `GET .../consoleText` | Streams raw console text. When log exceeds `max_bytes`, keeps 20% head and 80% tail, inserting `[... truncated N bytes ...]`. Strips ANSI escape sequences. | Retries up to 3 times on 5xx or transport errors with backoff (0.25s - 2.0s). Raises `JenkinsUnavailable` on persistent failure. |
| `get_stage_summary` | `job: JobPath`, `n: int` | `GET .../wfapi/describe` | Returns Jenkins Pipeline Graph stages, execution results (`SUCCESS`, `FAILED`), and durations. | Raises `JenkinsUnavailable` or `JenkinsNotFound`. |
| `get_test_report` | `job: JobPath`, `n: int` | `GET .../testReport/api/json` | Fetches test counts and failure suites. | Returns `None` if Jenkins returns 404 (e.g. build failed before test execution). |
| `get_build` | `job: JobPath`, `n: int` | `GET .../api/json` | Fetches build metadata, commit actions, and parameters. Waits if build is still executing. | Polls up to `build_wait_timeout` (30s) if `building: true`. |
| `get_build_history` | `job: JobPath`, `limit: int = 30` | `GET .../api/json?tree=builds[...]` | Fetches recent build numbers, timestamps, and results. | Raises `JenkinsUnavailable`. |
| `get_previous_successful_build` | `job: JobPath` | `GET .../lastSuccessfulBuild/api/json` | Retrieves metadata of last successful run for baseline comparison. | Returns `None` if 404. |

### 2. CrewAI Tools (Accessible to the LLM)

Wrapped in `MCPToolWrapper` with bounded execution caps and automatic secret redaction:
- **Usage Cap:** Maximum 3 invocations per tool per investigation run. Returns `{"error": "MCP tool usage cap reached", "error_type": "UsageCapExceeded"}` when exceeded.
- **Per-Call Timeout:** 10.0 seconds.

| Tool Name | Origin | Parameters | Description |
|---|---|---|---|
| `check_flakiness` | Local (`CheckFlakinessTool`) | `test_name: str` | Analyzes test outcome history across the last 20 builds. Computes pass/fail counts, state transitions, and same-commit inconsistency. Returns `flaky_score` in `[0, 1]`. |
| `get_build_summary` | Remote MCP (`MCP_JENKINS_URL`) | None (bound to current build) | Returns high-level summary of the build currently being investigated. |
| `get_stage_summary` | Remote MCP (`MCP_JENKINS_URL`) | None (bound to current build) | Returns stages, step status, and failing pipeline stages. |
| `get_error_blocks` | Remote MCP (`MCP_JENKINS_URL`) | None (bound to current build) | Returns extracted error snippets and tracebacks from build output. |
| `get_test_report` | Remote MCP (`MCP_JENKINS_URL`) | None (bound to current build) | Returns detailed JUnit/pytest failure summaries. |
| `compare_with_last_success`| Remote MCP (`MCP_JENKINS_URL`) | None (bound to current build) | Compares log output, stage timings, and diffs against the last successful build. |
| `get_log_range` | Remote MCP (`MCP_JENKINS_URL`) | `start_line: int`, `end_line: int` | Fetches up to 300 lines of console text around a specific log region. |
| `get_build_history` | Remote MCP (`MCP_JENKINS_URL`) | `job_name: str` | Returns up to 30 recent builds for historical pattern matching. |
| `compute_error_signature` | Remote MCP (`MCP_ANALYTICS_URL`)| `failed_stage: str`, `error_blocks: list` | Computes a SHA-256 normalized failure signature. |
| `find_related_incidents` | Remote MCP (`MCP_ANALYTICS_URL`)| `error_signature: str` | Queries previous incidents sharing the same failure signature. |

---

## Classification Rules Table

Deterministic rules are evaluated in `agents/jenkins_agent/classifier.py` against normalized `ParsedBuild` data. When multiple rules match, hypotheses are ranked by confidence.

| Failure Taxonomy (`FailureTaxonomy`) | Base Confidence | Description / Trigger Condition | Matched Evidence IDs |
|---|---|---|---|
| `CODE_TEST_FAILURE` | **0.90** | Pytest failure reporting an `AssertionError`. | `failing_tests[i]` where `ASSERTION_RE` matches `test.message`. |
| `BUILD_COMPILATION_FAILURE` | **0.90** | Build/compile stage failure with Python exception (`SyntaxError`, `ImportError`, `ModuleNotFoundError`, `SyntaxWarning`) or compiler file+line error. Bare pip errors excluded. | `stages[i]`, `compiler_errors[i]`, or `error_blocks[i]` matching collection errors. |
| `AUTH_FAILURE` | **0.90** | Build reported 401/403 HTTP status, credentials rejection, or permission denied. | `auth_signals[i]`. |
| `TIMEOUT` | **0.85** | Build or test stage timed out (`timeout` or `timed out` keyword in failed stage). | `stages[i]`, `timeouts[i]`. |
| `INFRA_NETWORK_FAILURE` | **0.85** | Network connection refused, DNS resolution failure (`Name or service not known`), HTTP 503, or git remote checkout failure. | `error_blocks[i]` matching `NETWORK_RE`. |
| `CONFIG_ERROR` | **0.85** | Failed `deploy` stage reporting invalid YAML, missing environment variable, or configuration file error. | `stages[deploy]`, `error_blocks[i]` matching `CONFIG_RE`. |
| `DEPLOYMENT_FAILURE` | **0.80** | Failed `deploy` stage without a specific configuration signal (e.g. Kubernetes `ImagePullBackOff`, container exit). | `stages[deploy]`, `error_blocks[i]`. |
| `DEPENDENCY_REGRESSION` | **0.65** | Pip dependency resolution conflict, incompatible package constraints, or missing imported dependency. | `compiler_errors[i]` (import), `error_blocks[i]` matching `DEPENDENCY_RE`. |
| `FLAKY_TEST` | **0.60** *(base)*<br>**0.70 – 0.95** *(adjusted)* | Failing test previously passed on the identical commit, or historical transition flakiness score $> 0.40$. | `error_blocks[i]` matching `FLAKY_HISTORY_RE`, `failing_tests[i]`. |
| `UNKNOWN` | **0.20** | Fallback when no deterministic rules match. | None. |

---

## Confidence Scoring Formula

Confidence calculation is deterministic and implemented in [`agents/jenkins_agent/confidence.py`](file:///c:/Users/ishit/OneDrive/Desktop/projects/jenkins_guadrian/agents/jenkins_agent/confidence.py).

### 1. Base Score Components (Weights Sum to 1.0)

$$\text{Base Score} = (w_{\text{rule}} \cdot \text{rule\_match}) + (w_{\text{llm}} \cdot \text{llm\_agreement}) + (w_{\text{signal}} \cdot \text{signal\_strength}) + (w_{\text{history}} \cdot \text{history\_support})$$

- **$w_{\text{rule}} = 0.35$ (`rule_match`):** Calibrated confidence of the primary matching rule from `classifier.py`.
- **$w_{\text{llm}} = 0.25$ (`llm_agreement`):** Concordance between rule classification and the LLM diagnosis, modulated by the LLM's reported confidence. When LLM is absent or disabled, mirrors `rule_match`.
- **$w_{\text{signal}} = 0.25$ (`signal_strength`):** Structural richness of parsed signals (detected failed stage, parsed failing tests, compiler errors, error blocks, exit codes).
- **$w_{\text{history}} = 0.15$ (`history_support`):** Corroboration from previous runs (flakiness analysis, recurring signature). Defaults to a neutral baseline of `0.70` when build history is empty.

### 2. Penalties Deducted from Base Score

| Penalty | Deduction | Condition |
|---|---|---|
| `truncation_penalty` | **-0.20** | Console text exceeded 2MB and was truncated. |
| `disagreement_penalty` | **-0.20** | Rule classification and LLM diagnosis disagree on failure type. |
| `unknown_penalty` | **-0.25** | Failure cannot be matched to a known taxonomy category. |
| `partial_penalty` | **-0.10** | Build log was incomplete or fetched mid-flight. |

### 3. Caps & Clamping

$$\text{Final Confidence} = \text{clamp}\left(\text{Base Score} - \sum \text{Penalties},\ 0.0,\ 0.98\right)$$

- **Empty Log Cap:** If the log contains no usable signals, confidence is strictly capped at $\le 0.20$.
- **Unknown Classification Cap:** If classified as `unknown`, confidence is strictly capped at $\le 0.30$.
- **Independent Beliefs:** Confidences represent independent belief strengths and are not normalized to sum to 1.0 across alternative hypotheses (multi-agent fusion is performed downstream by the Coordinator).

---

## Configuration & Environment Variables

Validated runtime settings are managed via Pydantic (`JenkinsAgentSettings` in `agents/jenkins_agent/config.py`):

| Variable | Type | Default | Description |
|---|---|---|---|
| `RABBITMQ_URL` | `SecretStr` | *(Required)* | AMQP connection string (e.g. `amqp://guest:guest@rabbitmq:5672/`). |
| `DATABASE_URL` | `SecretStr` | `None` | Optional PostgreSQL connection string. |
| `JENKINS_URL` | `str` | *(Required)* | Base URL of Jenkins master (e.g. `http://jenkins:8080`). |
| `JENKINS_USER` | `str` | *(Required)* | Jenkins username or API user. |
| `JENKINS_API_TOKEN` | `SecretStr` | *(Required)* | Jenkins API token for authenticated REST calls. |
| `A2A_SHARED_TOKEN` | `str` | *(Required for A2A)* | Shared secret token for A2A HTTP authentication (`Bearer <token>`). |
| `GEMINI_API_KEY` | `SecretStr` | `None` | API key for Gemini LLM calls. |
| `GEMINI_MODEL` | `str` | `gemini-2.5-flash` | Gemini model name for CrewAI analysis. |
| `LLM_PROVIDER` | `str` | `gemini` | LLM backend provider (`gemini`). |
| `LLM_OFFLINE` | `bool` | `False` | When `True`, skips all LLM calls and executes deterministic rule path. |
| `LLM_REQUESTS_PER_MINUTE`| `float` | `10.0` | Client rate-limiter budget to avoid provider 429 quota exhaustion. |
| `TOTAL_TIMEOUT_SECONDS` | `float` | `180.0` | Maximum investigation timeout before returning fallback evidence. |
| `PER_CALL_TIMEOUT_SECONDS` | `float` | `20.0` | Timeout per Jenkins REST API call. |
| `LLM_TIMEOUT_SECONDS` | `float` | `30.0` | Timeout for CrewAI LLM kickoff. |
| `MCP_JENKINS_URL` | `str` | `http://mcp-jenkins:8081/mcp` | Streamable-HTTP endpoint for Jenkins MCP server. |
| `MCP_ANALYTICS_URL` | `str` | `http://mcp-analytics:8082/mcp` | Streamable-HTTP endpoint for Analytics MCP server. |
| `EVIDENCE_DIR` | `str` | `./data/evidence` | Directory where evidence JSON files are persisted. |
| `PORT` | `int` | `9101` | Listening port for A2A HTTP server. |
| `LOG_LEVEL` | `str` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |

---

## How to Run

### Running Locally

1. **Activate the virtual environment:**
   ```powershell
   .venv\Scripts\activate
   ```
2. **Ensure required environment variables are set in `.env`:**
   ```env
   RABBITMQ_URL=amqp://guest:guest@localhost:5672/
   JENKINS_URL=http://localhost:8080
   JENKINS_USER=admin
   JENKINS_API_TOKEN=11a2b3c4d5e6f7a8b9c0...
   A2A_SHARED_TOKEN=super-secret-a2a-token
   GEMINI_API_KEY=AIzaSy...
   ```
3. **Start the A2A HTTP Service (Port 9101):**
   ```powershell
   python -m agents.jenkins_agent.a2a_app
   ```
4. **Health check verification:**
   ```powershell
   curl http://localhost:9101/health
   # Returns: {"status":"ok"}
   ```

### Running with Docker Compose

The service is defined as `jenkins-agent` in the root `docker-compose.yml`:

```bash
docker compose up -d jenkins-agent
```

To view live agent logs:
```bash
docker compose logs -f jenkins-agent
```

---

## How to Add a New Classification Rule

Deterministic rules are modular and contained entirely in [`agents/jenkins_agent/classifier.py`](file:///c:/Users/ishit/OneDrive/Desktop/projects/jenkins_guadrian/agents/jenkins_agent/classifier.py). Follow these steps:

### Step 1: Ensure Taxonomy Enum Value Exists
If adding a new failure category, define it in `common/models.py`:
```python
class FailureTaxonomy(str, Enum):
    # ... existing ...
    OUT_OF_MEMORY = "out_of_memory"
```

### Step 2: Implement an Evidence Matcher Function
Create an evidence matcher with signature `(ParsedBuild) -> list[str]`. It returns a list of evidence IDs (e.g. `["error_blocks[0]"]`) if the rule matches, or an empty list `[]` otherwise:

```python
OOM_RE = re.compile(r"java\.lang\.OutOfMemoryError|Killed process|OOMKilled", re.IGNORECASE)

def _oom_evidence(parsed: ParsedBuild) -> list[str]:
    return [
        f"error_blocks[{index}]"
        for index, block in enumerate(parsed.error_blocks)
        if OOM_RE.search(block)
    ]
```

### Step 3: Register the Rule in `RULES`
Add the `Rule` object to the `RULES` list in `classifier.py`:

```python
RULES = [
    # ... existing rules ...
    Rule(
        FailureTaxonomy.OUT_OF_MEMORY,
        "Build process was terminated due to memory exhaustion (OOM).",
        0.90,  # Base confidence
        _oom_evidence,
    ),
]
```

### Step 4: Add Regression Tests
Add unit tests in `tests/jenkins_agent/test_classifier.py`:
```python
def test_out_of_memory_classification():
    console = "Process died: java.lang.OutOfMemoryError: Java heap space\nBuild failed"
    parsed = parse_build(console, {"stages": [{"name": "build", "result": "FAILURE"}]})
    hyps = rule_based_classify(parsed)
    assert len(hyps) > 0
    assert hyps[0].failure_type == FailureTaxonomy.OUT_OF_MEMORY
    assert hyps[0].rule_confidence == 0.90
```

---

## Testing & Evaluation

### 1. Running Unit & Integration Tests

Run the dedicated test suite using pytest:

```powershell
# Run all jenkins_agent tests
.venv\Scripts\python.exe -m pytest tests/jenkins_agent/ -v

# Run only classifier tests
.venv\Scripts\python.exe -m pytest tests/jenkins_agent/test_classifier.py -v
```

### 2. Running Evaluation Harness

The evaluation harness in [`scripts/evaluate_jenkins_agent.py`](file:///c:/Users/ishit/OneDrive/Desktop/projects/jenkins_guadrian/scripts/evaluate_jenkins_agent.py) benchmarks classification accuracy, localization, latency, calibration (ECE), and fallback rates against ground-truth scenarios in `scenarios/`:

```powershell
# Fast rules-only evaluation (no API quota usage, runs in milliseconds)
.venv\Scripts\python.exe scripts\evaluate_jenkins_agent.py --no-llm -n 5

# LLM-only evaluation (skips rule-based classification)
.venv\Scripts\python.exe scripts\evaluate_jenkins_agent.py --no-rules -n 3

# Full comparative ablation (rules vs LLM vs hybrid)
.venv\Scripts\python.exe scripts\evaluate_jenkins_agent.py --compare -n 3
```

Results are automatically exported to [`results/jenkins_agent_eval.json`](file:///c:/Users/ishit/OneDrive/Desktop/projects/jenkins_guadrian/results/jenkins_agent_eval.json) and rendered as a table in [`results/jenkins_agent_eval.md`](file:///c:/Users/ishit/OneDrive/Desktop/projects/jenkins_guadrian/results/jenkins_agent_eval.md).

---

## Known Limitations

1. **Log Truncation Ceiling (2 MB):** Very verbose build logs are truncated at 2,000,000 bytes. The client preserves a 20% head / 80% tail split to retain compilation errors and stack traces, but middle sections are omitted. When truncated, a `-0.20` confidence penalty is applied.
2. **Flakiness History Requirement:** Historical flakiness analysis requires at least 5 previous builds (`MIN_BUILDS_FOR_HISTORY = 5`). In fresh repositories or new branches without baseline history, the analyzer returns status `insufficient_history`.
3. **Free-Tier LLM Rate Limits:** Free-tier Gemini models enforce strict daily request ceilings (e.g. 20 requests/day). The agent includes offline fallback and prompt-caching (`LLM_CACHE_PATH`), but sustained production use requires an enterprise or paid tier API key.
4. **Read-Only Scope:** The investigation agent cannot modify Jenkins job definitions or environment settings. Build triggering is strictly gated for post-fix remediation validation.

---

## Security Notes

1. **Automated Credential Redaction:**
   All logs, tool outputs, and LLM prompts pass through [`common/redaction.py`](file:///c:/Users/ishit/OneDrive/Desktop/projects/jenkins_guadrian/common/redaction.py) before storage or reasoning:
   - **Bearer Tokens:** Stripped to `Bearer [REDACTED]`.
   - **Provider Keys:** Regex patterns automatically scrub GitHub tokens (`ghp_*`, `github_pat_*`), AWS access keys (`AKIA*`), Slack tokens (`xox*`), and Google API keys (`AIza*`).
   - **URL Basic Auth:** URLs like `https://user:password@jenkins/` are sanitized to `https://[REDACTED]@jenkins/`.
   - **Key-Value Redaction:** Any structured JSON fields named `password`, `token`, `secret`, `api_key`, `credential`, or `cookie` are masked to `[REDACTED]`.
2. **Strictly Read-Only MCP Tools:**
   Investigation tools exposed to LLM agents are strictly read-only. Tool invocations cannot execute shell scripts or alter pipeline configurations.
3. **A2A Bearer Token Handling:**
   All HTTP endpoints under port 9101 mandate `A2A_SHARED_TOKEN` verification using `hmac.compare_digest` to prevent timing attacks.
4. **Execution Boundaries & Timeouts:**
   Every tool invocation has a hard timeout (10s) and a maximum invocation limit (3 per tool). Total execution is bounded by `TOTAL_TIMEOUT_SECONDS` (180s) to prevent thread exhaustion or infinite reasoning loops.
