"""
Streamlit UI for the Journey Health Advisor.
Talks to the FastAPI backend running at http://localhost:8000.

Run with:
    streamlit run app.py
"""

import json
import requests
import streamlit as st
from datetime import datetime, timezone

API_URL = "http://localhost:8000"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _health_emoji(status: str) -> str:
    return {"UNHEALTHY": "🔴", "AT_RISK": "🟠", "HEALTHY": "🟢"}.get(status, "⚪")


def _burn_emoji(rate: float) -> str:
    if rate == 0:
        return "⚪"
    if rate < 1:
        return "🟢"
    if rate < 5:
        return "🟡"
    if rate <= 10:
        return "🟠"
    return "🔴"


def _fmt_ts(ms) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _fmt_num(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


# ---------------------------------------------------------------------------
# Technical details expander
# ---------------------------------------------------------------------------

def render_technical(technical: dict) -> None:
    """Render the collapsible technical details panel for a single response."""
    with st.expander("📊 Technical details"):
        tr      = technical.get("time_resolution", {})
        data    = technical.get("data", {})
        filters = data.get("filters", {})
        records = data.get("records", [])

        # ── Time resolution ──────────────────────────────────────────────────
        st.markdown("##### Time Window")
        col1, col2 = st.columns(2)
        with col1:
            eff = tr.get("effective_time_range", "—")
            st.markdown(f"**Window:** {eff}")
            source = tr.get("source", "—")
            src_label = {
                "deterministic": "extracted from query",
                "llm":           "LLM-resolved",
                "fallback":      "2-hour fallback",
            }.get(source, source)
            st.markdown(f"**Resolved via:** {src_label}")
        with col2:
            start_ms = tr.get("start_time")
            end_ms   = tr.get("end_time")
            if start_ms:
                st.markdown(f"**From:** {_fmt_ts(start_ms)}")
            if end_ms:
                st.markdown(f"**To:**   {_fmt_ts(end_ms)}")

        st.caption(
            f"Application ID: `{filters.get('application_id', '—')}` · "
            f"Project ID: `{filters.get('project_id', '—')}` · "
            f"Range: `{filters.get('range', '—')}`"
        )

        st.divider()

        # ── Journey Health summary ───────────────────────────────────────────
        st.markdown("##### Journey Health Summary")

        if not records:
            st.info("No journey records returned.")
        else:
            for record in records:
                if not isinstance(record, dict):
                    continue

                journey_name  = record.get("journeyName") or record.get("journeyId", "Unknown Journey")
                eb_health     = record.get("ebHealth", record.get("health", "—"))
                resp_health   = record.get("responseHealth", "—")
                success_rate  = record.get("successRate", 0)
                total_req     = record.get("totalRequests", 0)
                error_count   = record.get("errorCount", 0)
                burn_rate     = record.get("burnRate")

                st.markdown(f"**{journey_name}**")
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.metric("EB Health",       f"{_health_emoji(eb_health)} {eb_health}")
                    st.metric("Response Health", f"{_health_emoji(resp_health)} {resp_health}")
                with col2:
                    st.metric("Success Rate",   f"{float(success_rate):.2f}%" if success_rate else "—")
                    st.metric("Total Requests", _fmt_num(total_req))
                with col3:
                    st.metric("Error Count", _fmt_num(error_count))
                    if burn_rate is not None:
                        st.metric("Burn Rate", f"{_burn_emoji(float(burn_rate))} {float(burn_rate):.2f}×")

                # ── Transactions ─────────────────────────────────────────────
                summaries = record.get("summaries", [])
                if summaries:
                    st.markdown("**Transactions:**")
                    rows = []
                    for s in summaries:
                        name          = s.get("alias") or s.get("transactionName", "—")
                        eb_consumed   = s.get("eBConsumedPercent", "—")
                        resp_left     = s.get("responseLeftPercent", "—")
                        br            = s.get("burnRate", 0)
                        eb_breached   = "✅" if s.get("ebBreached") else "—"
                        resp_breached = "✅" if s.get("responseBreached") else "—"
                        rows.append({
                            "Transaction":   name,
                            "Burn Rate":     f"{_burn_emoji(float(br))} {float(br):.2f}×" if br is not None else "—",
                            "EB Consumed %": eb_consumed,
                            "Resp Left %":   resp_left,
                            "EB Breached":   eb_breached,
                            "Resp Breached": resp_breached,
                        })
                    st.dataframe(rows, use_container_width=True)

                st.divider()

        # ── Fetch metadata ───────────────────────────────────────────────────
        fetched_at = data.get("fetched_at", "")
        if fetched_at:
            st.caption(f"Data fetched at: {fetched_at}")


# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="Journey Health Advisor",
    page_icon="🗺️",
    layout="wide",
)

st.title("🗺️ Journey Health Advisor")
st.caption("Ask anything about your application's user journey health in plain English.")

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("⚙️ Settings")
    api_base   = st.text_input("API URL", value=API_URL)
    app_id     = st.number_input("Application ID", value=0, step=1)
    project_id = st.number_input("Project ID",     value=0, step=1)
    range_type = st.text_input("Range", value="CUSTOM", placeholder="e.g. CUSTOM")

    st.divider()
    st.subheader("🕐 Time Override")
    st.caption("Leave blank to auto-extract from query.")
    start_input = st.text_input("Start Time (Unix ms)", value="", placeholder="e.g. 1778005800000")
    end_input   = st.text_input("End Time (Unix ms)",   value="", placeholder="e.g. 1779388200000")

    st.divider()
    if st.button("🗑️ Clear chat", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

# ---------------------------------------------------------------------------
# Chat history
# ---------------------------------------------------------------------------

if "messages" not in st.session_state:
    st.session_state.messages = []

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("technical"):
            render_technical(msg["technical"])

# ---------------------------------------------------------------------------
# Chat input
# ---------------------------------------------------------------------------

if not app_id or not project_id:
    st.warning("⚠️ Set a valid **Application ID** and **Project ID** in the sidebar before querying.", icon="⚠️")

query = st.chat_input(
    "e.g. How are my journeys performing in the last 2 hours?",
    disabled=(not app_id or not project_id),
)

if query:
    st.session_state.messages.append({"role": "user", "content": query})
    with st.chat_message("user"):
        st.markdown(query)

    with st.chat_message("assistant"):
        try:
            payload = {
                "query":          query,
                "application_id": int(app_id),
                "project_id":     int(project_id),
                "range":          range_type.strip() or "CUSTOM",
            }
            if start_input.strip():
                payload["start_time"] = int(start_input.strip())
            if end_input.strip():
                payload["end_time"] = int(end_input.strip())

            with requests.post(
                f"{api_base}/query/journey/stream",
                json=payload,
                stream=True,
                timeout=180,
            ) as resp:
                resp.raise_for_status()

                line_iter = resp.iter_lines(decode_unicode=True)
                technical = None

                # Phase 1 — wait for metadata while data is being fetched
                with st.spinner("Fetching Journey Health data..."):
                    for line in line_iter:
                        if not line or not line.startswith("data: "):
                            continue
                        event = json.loads(line[6:])
                        if event["type"] == "metadata":
                            technical = event["data"]
                            break
                        elif event["type"] == "error":
                            raise Exception(event.get("detail", "Unknown error from server"))

                # Phase 2 — stream LLM tokens into the chat bubble
                def token_gen():
                    for line in line_iter:
                        if not line or not line.startswith("data: "):
                            continue
                        event = json.loads(line[6:])
                        if event["type"] == "token":
                            yield event["text"]
                        elif event["type"] in ("done", "error"):
                            break

                full_answer = st.write_stream(token_gen())

                if technical:
                    render_technical(technical)

            st.session_state.messages.append({
                "role":      "assistant",
                "content":   full_answer or "",
                "technical": technical,
            })

        except requests.exceptions.ConnectionError:
            msg = (
                "Cannot connect to the backend. Make sure the FastAPI server is running:\n"
                "```\nuvicorn main:app --host 0.0.0.0 --port 8000 --workers 1\n```"
            )
            st.error(msg)
            st.session_state.messages.append({"role": "assistant", "content": msg})

        except requests.exceptions.Timeout:
            msg = "Request timed out (>180 s). The backend may still be processing."
            st.error(msg)
            st.session_state.messages.append({"role": "assistant", "content": msg})

        except Exception as e:
            st.error(f"Unexpected error: {e}")
            st.session_state.messages.append({"role": "assistant", "content": str(e)})
