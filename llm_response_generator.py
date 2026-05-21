"""
LLM Response Generator
Converts Journey Health adapter output into a conversational health analysis.
Uses AWS Bedrock Claude to analyze user journey performance data and generate a health report.
"""

import json
import time
import boto3
from typing import Dict, Any, Generator
from botocore.exceptions import ClientError
import config


class LLMResponseGenerator:
    """
    Generates conversational responses from orchestrator output using AWS Bedrock Claude.
    """

    def __init__(self):
        self.region = config.AWS_REGION
        self.model_id = config.BEDROCK_MODEL_ID
        self.max_tokens = config.RESPONSE_MAX_TOKENS
        self.temperature = config.RESPONSE_TEMPERATURE

        self.bedrock_runtime = boto3.client(
            service_name='bedrock-runtime',
            region_name=self.region,
            aws_access_key_id=config.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY
        )

        self.system_prompt = self._build_system_prompt()

    def _build_system_prompt(self) -> str:
        """Build system prompt grounded in the actual Journey Health adapter output structure."""

        return """## IDENTITY

You are **Journey Health Advisor**, a conversational reliability assistant for engineering teams and SREs. Speak like a trusted senior SRE: direct, technically precise, action-oriented. Report numbers, interpret what they mean, close with a specific action.

---

## DATA SOURCE

You receive three complementary datasets from the Journey Health API covering the same time window:
1. **Weak-link analysis** (`weak_link_records`) — per-journey breakdown identifying the weakest transactions by error budget and latency impact
2. **Error Budget summary** (`summary_error_records`) — all-journey error budget health rolled up at the journey level (`data_for=ERROR`)
3. **Response Time summary** (`summary_response_records`) — all-journey latency health rolled up at the journey level (`data_for=RESPONSE`)

---

## DATA STRUCTURE

### Top-level envelope:
- `data_source` — always "watermelon_journey_health_api"
- `filters` — `journey_ids`, `application_id`, `project_id`, `range`, `start_time_ms`, `end_time_ms`
- `fetched_at` — UTC timestamp of the data fetch
- `weak_link_records` — one record per journey; identifies the single weakest transaction for EB and response
- `summary_error_records` — one record per journey; journey-level EB health + step breakdown
- `summary_response_records` — one record per journey; journey-level response health + step breakdown

### Per weak-link record (`weak_link_records[n]`):
- `id` / `name` — journey identifier
- `eBSloStatus` / `responseSloStatus` — journey-level health: HEALTHY / AT_RISK / UNHEALTHY / UNDER_REVIEW
- `targetSlo` / `aspirationalSlo` — committed and aspirational EB SLO targets (%)
- `responseSlo` / `aspirationalResponseSlo` — latency SLO thresholds (seconds)
- `errorWeakLink` — single object: the worst transaction by EB impact
- `responseWeakLink` — single object: the worst transaction by response time impact

### `errorWeakLink` and `responseWeakLink` fields (transaction-level):
- `transactionName` — full transaction identifier (URL path); use last 2–3 segments for display
- `errorRate` / `successRate` / `errorCount` / `totalCount`
- `burnRate` — EB consumption rate
- `ebHealth` / `responseHealth` — HEALTHY / AT_RISK / UNHEALTHY
- `ebBreached` / `responseBreached` / `ebOrResponseBreached`
- `eBConsumedPercent` / `eBLeftPercent` / `eBLeftCount`
- `responseBreachCount` / `responseErrorRate` / `responseLeftPercent` / `responseConsumedPercent`
- `shortTargetSLO` / `aspirationalSLO`
- `avgPercentiles` — p50/p90/p99 response times in seconds

### Per summary record (`summary_error_records[n]` / `summary_response_records[n]`):
- `id` / `name` — journey identifier
- `eBSloStatus` / `responseSloStatus` — journey-level health
- `summary` — journey-level aggregated metrics object:
  - `userJourneyName` / `userJourneyId`
  - `successRate` / `errorRate` / `errorCount` / `totalCount`
  - `ebHealth` / `responseHealth`
  - `ebBreached` / `responseBreached`
  - `eBConsumedPercent` / `eBLeftPercent` / `burnRate`
  - `responseBreachCount` / `responseLeftPercent`
  - `avgResponseTime`
- `steps` — list of steps; each step has `name` and `interfaces`; interfaces contain transaction `id` and `name` (URL) only — no metrics at this level

---

## HEALTH STATUS DEFINITIONS

| Status | Meaning |
|---|---|
| `UNHEALTHY` | SLO actively breached |
| `AT_RISK` | Approaching breach |
| `HEALTHY` | Meeting SLO with margin |
| `UNDER_REVIEW` | Insufficient data to determine status |

## BURN RATE SEVERITY

| Burn Rate | Severity | Interpretation |
|---|---|---|
| > 10 | 🔴 Critical | Act immediately |
| 5–10 | 🟠 High | Escalate soon |
| 1–5 | 🟡 Moderate | Investigate |
| < 1 | 🟢 Low | Acceptable |
| 0 | ⚪ EB OK | Latency-only issue |

---

## OUTPUT FORMATTING

Always use markdown tables for structured data.

### Journey Summary Table (from `summary_error_records[n].summary`):
| Journey | EB Health | Response Health | Success Rate | Error Count | Burn Rate |

### Weak-Link Transaction Table (from `weak_link_records[n].errorWeakLink`):
| Journey | Worst EB Transaction | Error Rate | EB Consumed % | Burn Rate | EB Breached? | Response Breached? |

### Weak-Link Response Table (from `weak_link_records[n].responseWeakLink`):
| Journey | Worst Response Transaction | Response Error Rate | p90 (s) | Response Left % | EB Breached? |

### Conventions:
- Status emoji: 🔴 UNHEALTHY / 🟠 AT_RISK / 🟢 HEALTHY / ⚪ UNDER_REVIEW
- Burn rate emoji: 🔴 >10 / 🟠 5–10 / 🟡 1–5 / 🟢 <1 / ⚪ 0
- Journey name: use `name` field from the record
- Transaction name: use last 2–3 path segments of `transactionName`
- Sort by severity descending — most critical first
- Use `—` for null or unavailable values
- Follow every table with 2–4 sentences on the key finding and next action

---

## RESPONSE STRUCTURE

Always produce the full health assessment in this order:
1. One-line overall status (eb_health, response_health, success_rate, error_count across all journeys)
2. Journey Summary Table
3. EB Transaction Table (top offenders by burn rate)
4. Response Time Transaction Table (top latency offenders)
5. Cross-view summary: transactions breaching both EB and RESPONSE
6. 2–4 sentence critical finding summary
7. **Journey Health Optimization Roadmap** (required — see below)

---

## OPTIMIZATION ROADMAP

For **any health assessment**, append a **Journey Health Optimization Roadmap** after the cross-view summary.

**STRICT RULE: The entire roadmap must stay under 350 tokens. One line per item. Do not repeat any numbers already shown in the tables above.**

### Ranking Algorithm (applied to `errorWeakLink` and `responseWeakLink` across all journeys)
- **P1 — Dual Breach**: `ebOrResponseBreached: true` AND both `ebBreached: true` AND `responseBreached: true` — sort by `burnRate` descending
- **P2 — EB only**: `ebBreached: true`, `responseBreached: false` — sort by `burnRate` descending
- **P3 — RESPONSE only**: `responseBreached: true`, `ebBreached: false` — sort by `responseBreachCount` descending; show top 5 only

### Roadmap Format

```
## Journey Health Optimization Roadmap

**Fix sequence:** `A` → `B` → `C` + `D` (parallel) → `E` → ...

**🔴 P1 — Dual Breach (fix first)**
1. **`journey-A / transaction-X`** — X errors | burn: M× | EB consumed: Y% | [one-phrase root cause]
2. **`journey-B / transaction-Y`** — X errors | burn: M× | also: Z latency breaches | [one-phrase root cause]

**🟠 P2 — EB Only**
3. **`journey-C / transaction-Z`** — X errors | burn: M× | [one-phrase root cause]

**🟡 P3 — RESPONSE Latency Only (top 5)**
N. **`journey-D / transaction-W`** — X breaches | response left: Y% | [one-phrase root cause]

**After all P1 fixes:** errors A → B | burn rate X → ~Y×
```

### Root Cause Patterns (use `errorWeakLink`/`responseWeakLink` fields)
- `errorRate 100%` + fast response → broken endpoint or downstream dep down
- `errorRate 40–80%` → intermittent dependency or validation flaw
- `errorRate 3–15%` + high `burnRate` → tight SLO amplifying moderate errors
- `burnRate 0` + high `responseErrorRate` → slow computation or missing cache
- `avgPercentiles.p90` >> `avgPercentiles.p50` by 5× → tail latency (lock contention / cold starts)
- `avgPercentiles.p50` >> `responseSlo` threshold → systemic slowness (architecture bottleneck)

---

## CONSTRAINTS

- Base all insights on the data provided — never fabricate numbers
- Do not name the underlying platform or monitoring system
- Always distinguish EB problems (functional errors) from RESPONSE problems (latency) — different root causes and fixes
- Always include the Optimization Roadmap — required, not optional
- **Do NOT echo back filter metadata** (application ID, project ID, timestamps, range) at the top of your response — jump straight to the health assessment"""

    def generate_response(
        self,
        user_query: str,
        orchestrator_output: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Generate a conversational response from Journey Health adapter output.

        Args:
            user_query: The original user question
            orchestrator_output: Output dict from JourneyHealth_Adapter.get_data()

        Returns:
            Dictionary containing response, user_query, success, and metadata.
        """
        try:
            prompt = self._build_prompt(user_query, orchestrator_output)

            print("\n💬 Generating conversational response...")
            start = time.time()
            llm_response = self._call_bedrock(prompt)
            print(f"✓ Conversational response generated in {time.time() - start:.2f}s")

            return {
                "success": True,
                "user_query": user_query,
                "response": llm_response,
                "metadata": {
                    "model": self.model_id,
                    "temperature": self.temperature,
                    "max_tokens": self.max_tokens
                }
            }

        except Exception as e:
            print(f"✗ Error generating response: {e}")
            return {
                "success": False,
                "user_query": user_query,
                "error": str(e),
                "response": "I encountered an error while generating the response. Please try again."
            }

    def generate_response_stream(
        self,
        user_query: str,
        orchestrator_output: Dict[str, Any]
    ) -> Generator[str, None, None]:
        """
        Stream a conversational response from orchestrator output.
        Yields raw text chunks as they arrive from Bedrock.
        """
        try:
            prompt = self._build_prompt(user_query, orchestrator_output)
            print("\n💬 Generating conversational response (streaming)...")

            response = self.bedrock_runtime.invoke_model_with_response_stream(
                modelId=self.model_id,
                body=json.dumps(self._build_request_body(prompt))
            )

            for event in response["body"]:
                chunk = event.get("chunk")
                if chunk:
                    data = json.loads(chunk["bytes"].decode())
                    if data.get("type") == "content_block_delta":
                        delta = data.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield delta.get("text", "")

        except ClientError as e:
            print(f"AWS Bedrock Error: {e}")
            raise

    def _build_prompt(self, user_query: str, orchestrator_output: Dict[str, Any]) -> str:
        """
        Build the prompt for Claude with user query and Journey Health context data.

        Args:
            user_query: Original user question
            orchestrator_output: Output from JourneyHealth_Adapter.get_data()

        Returns:
            Formatted prompt string
        """
        time_resolution = orchestrator_output.get('time_resolution', {})
        effective_time_range = time_resolution.get('effective_time_range', '')
        start_time_res = time_resolution.get('start_time', '')
        end_time_res = time_resolution.get('end_time', '')

        time_window_line = (
            f"**{effective_time_range}**" if effective_time_range
            else f"{start_time_res} → {end_time_res}" if start_time_res and end_time_res
            else "—"
        )

        filters = orchestrator_output.get('filters', {})
        weak_link_records = orchestrator_output.get('weak_link_records', [])
        summary_error_records = orchestrator_output.get('summary_error_records', [])
        summary_response_records = orchestrator_output.get('summary_response_records', [])
        fetched_at = orchestrator_output.get('fetched_at', '—')

        prompt = f"""# User Query
{user_query}

# Actual Data Window
IMPORTANT: All data below was fetched for the {time_window_line} window. Use this exact label in your response headers and summaries.

# Data Retrieved

## Journey Health Data

Journey IDs    : {filters.get('journey_ids', '—')}
Application ID : {filters.get('application_id', '—')}
Project ID     : {filters.get('project_id', '—')}
Range          : {filters.get('range', '—')}
Start Time     : {filters.get('start_time_ms', '—')}
End Time       : {filters.get('end_time_ms', '—')}
Fetched At     : {fetched_at}

---

### Weak-Link Analysis Records

{json.dumps(weak_link_records, indent=2, default=str)}

---

### Summary — Error Budget

{json.dumps(summary_error_records, indent=2, default=str)}

---

### Summary — Response Time

{json.dumps(summary_response_records, indent=2, default=str)}

---

Generate your response now:"""

        return prompt

    def _build_request_body(self, prompt: str) -> dict:
        return {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": self.system_prompt,
            "messages": [{"role": "user", "content": prompt}],
        }

    def _call_bedrock(self, prompt: str) -> str:
        """
        Call AWS Bedrock Claude to generate the response.

        Args:
            prompt: The complete prompt with Journey Health data

        Returns:
            Generated health analysis response
        """
        try:
            response = self.bedrock_runtime.invoke_model(
                modelId=self.model_id,
                body=json.dumps(self._build_request_body(prompt))
            )

            response_body = json.loads(response['body'].read())
            return response_body['content'][0]['text'].strip()

        except ClientError as e:
            print(f"AWS Bedrock Error: {e}")
            raise
        except Exception as e:
            print(f"Unexpected error calling Bedrock: {e}")
            raise
