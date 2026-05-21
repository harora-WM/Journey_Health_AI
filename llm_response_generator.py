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

You receive user journey performance records from the Journey Health API. Each record represents a user journey with its associated SLO health metrics, error budget status, and response time data.

---

## DATA STRUCTURE

### Top-level envelope:
- `data_source` — always "journey_health_api"
- `filters` — `application_id`, `project_id`, `range`, `start_time_ms`, `end_time_ms`
- `fetched_at` — UTC timestamp of the data fetch
- `records` — list of user journey performance records

### Per-record fields (user journey performance):
- `journeyName` / `journeyId` — journey identifier; use `journeyName` if present
- `health` / `ebHealth` / `responseHealth` — overall, error budget, and response time health status
- `successRate` / `totalRequests` / `errorCount` — top-level traffic and error metrics
- `burnRate` — error budget consumption rate; high values indicate rapid budget depletion
- `eBConsumedPercent` / `eBLeftPercent` / `eBLeftCount` — error budget position
- `responseBreachCount` / `responseLeftPercent` / `responseLeftCount` — response time SLO budget
- `shortTargetSLO` / `aspirationalSLO` — committed and internal SLO targets
- `responseSlo` — latency threshold in seconds
- `avgPercentiles` — p50/p75/p90/p99 response times in seconds
- `summaries` — list of transaction-level summaries within the journey

### Per-summary (transaction) fields inside `summaries`:
- `transactionName` / `alias` — use alias if present, otherwise last 2–3 path segments
- `errorRate` / `burnRate` — functional error rate and burn rate
- `eBConsumedPercent` / `eBLeftPercent` / `eBLeftCount` — EB budget position
- `responseErrorRate` / `responseConsumedPercent` / `responseLeftPercent` — response budget position
- `absoluteErrorRateAgainstApplication` — share of total app errors (primary ranking signal)
- `comparativeErrorRateAgainstApplication` — % of all app errors this transaction contributes
- `avgPercentiles` — p50/p90/p99 in seconds
- `ebBreached` / `responseBreached` — which SLO is violated

---

## HEALTH STATUS DEFINITIONS

| Status | Meaning |
|---|---|
| `UNHEALTHY` | SLO actively breached |
| `AT_RISK` | Approaching breach |
| `HEALTHY` | Meeting SLO with margin |

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

### Journey Summary Table:
| Journey | EB Health | Response Health | Success Rate | Total Requests | Error Count | Burn Rate |

### Transaction Table (EB issues):
| Transaction | Journey | Error Rate | EB Consumed % | Burn Rate | Severity |

### Transaction Table (Response issues):
| Transaction | Journey | Response Breach Rate | p50 (s) | p90 (s) | Budget Left % | EB Breached? |

### Conventions:
- Status emoji: 🔴 UNHEALTHY / 🟠 AT_RISK / 🟢 HEALTHY
- Burn rate emoji: 🔴 >10 / 🟠 5–10 / 🟡 1–5 / 🟢 <1 / ⚪ 0
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

### Ranking Algorithm
- **P1 — Dual Breach**: `ebBreached: true` AND `responseBreached: true` — sort by `absoluteErrorRateAgainstApplication` descending
- **P2 — EB only**: `ebBreached: true`, `responseBreached: false` — sort by `absoluteErrorRateAgainstApplication` descending
- **P3 — RESPONSE only**: `responseBreached: true`, `ebBreached: false` — sort by `responseBreachCount` descending; show top 5 only

### Roadmap Format

```
## Journey Health Optimization Roadmap

**Fix sequence:** `A` → `B` → `C` + `D` (parallel) → `E` → ...

**🔴 P1 — Dual Breach (fix first)**
1. **`journey-A / transaction-X`** — X errors (Y% of app) | burn: M× | [one-phrase root cause]
2. **`journey-B / transaction-Y`** — X errors (Y%) | also: Z latency breaches | [one-phrase root cause]

**🟠 P2 — EB Only**
3. **`journey-C / transaction-Z`** — X errors (Y%) | [one-phrase root cause]

**🟡 P3 — RESPONSE Latency Only (top 5)**
N. **`journey-D / transaction-W`** — X breaches | [one-phrase root cause]

**After all P1 fixes:** errors A → B | burn rate X → ~Y×
```

### Root Cause Patterns
- `errorRate 100%` + fast response → broken endpoint or downstream dep down
- `errorRate 40–80%` → intermittent dependency or validation flaw
- `errorRate 3–15%` + high burn → tight SLO amplifying moderate errors
- `burnRate 0` + high responseErrorRate → slow computation or missing cache
- `p90` >> `p50` by 5× → tail latency (lock contention / cold starts)
- `p50` >> SLO threshold → systemic slowness (architecture bottleneck)

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
        records = orchestrator_output.get('records', [])
        fetched_at = orchestrator_output.get('fetched_at', '—')

        prompt = f"""# User Query
{user_query}

# Actual Data Window
IMPORTANT: All data below was fetched for the {time_window_line} window. Use this exact label in your response headers and summaries.

# Data Retrieved

## Journey Health Performance Data

Application ID : {filters.get('application_id', '—')}
Project ID     : {filters.get('project_id', '—')}
Range          : {filters.get('range', '—')}
Start Time     : {filters.get('start_time_ms', '—')}
End Time       : {filters.get('end_time_ms', '—')}
Fetched At     : {fetched_at}

---

### User Journey Performance Records

{json.dumps(records, indent=2, default=str)}

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
