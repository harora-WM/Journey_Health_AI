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
        records = data.get("weak_link_records", [])

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
            f"Journey IDs: `{filters.get('journey_ids', '—')}` · "
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

                journey_name = record.get("name") or str(record.get("id", "Unknown Journey"))
                eb_status    = record.get("eBSloStatus", "—")
                resp_status  = record.get("responseSloStatus", "—")

                st.markdown(f"**{journey_name}**")
                col1, col2 = st.columns(2)
                with col1:
                    st.metric("EB Status",       f"{_health_emoji(eb_status)} {eb_status}")
                with col2:
                    st.metric("Response Status", f"{_health_emoji(resp_status)} {resp_status}")

                # ── Weak-link transactions ────────────────────────────────────
                rows = []
                for label, wl in [("EB Weak Link", record.get("errorWeakLink")),
                                   ("Response Weak Link", record.get("responseWeakLink"))]:
                    if not isinstance(wl, dict):
                        continue
                    txn_name  = wl.get("transactionName", "—")
                    br        = wl.get("burnRate")
                    eb_pct    = wl.get("eBConsumedPercent", "—")
                    resp_left = wl.get("responseLeftPercent", "—")
                    eb_b      = "✅" if wl.get("ebBreached") else "—"
                    resp_b    = "✅" if wl.get("responseBreached") else "—"
                    rows.append({
                        "Type":          label,
                        "Transaction":   "/".join(txn_name.rstrip("/").split("/")[-2:]) if txn_name != "—" else "—",
                        "Burn Rate":     f"{_burn_emoji(float(br))} {float(br):.2f}×" if br is not None else "—",
                        "EB Consumed %": eb_pct,
                        "Resp Left %":   resp_left,
                        "EB Breached":   eb_b,
                        "Resp Breached": resp_b,
                    })
                if rows:
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
    api_base        = st.text_input("API URL", value=API_URL)
    journey_ids_raw = st.text_input("Journey IDs (comma-separated)", placeholder="e.g. 2338008,2331452,2338003")
    app_id          = st.number_input("Application ID", value=0, step=1)
    project_id      = st.number_input("Project ID",     value=0, step=1)
    range_type      = st.text_input("Range", value="CUSTOM", placeholder="e.g. CUSTOM")

    st.divider()
    st.subheader("🕐 Time Override")
    st.caption("Leave blank to auto-extract from query.")
    timezone_input = st.text_input("Timezone (IANA)", value="UTC", placeholder="e.g. America/New_York")
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

journey_ids = [int(j.strip()) for j in journey_ids_raw.split(",") if j.strip().isdigit()]

if not app_id or not project_id or not journey_ids:
    st.warning("⚠️ Set a valid **Application ID**, **Project ID**, and at least one **Journey ID** in the sidebar before querying.", icon="⚠️")

query = st.chat_input(
    "e.g. How are my journeys performing in the last 2 hours?",
    disabled=(not app_id or not project_id or not journey_ids),
)

if query:
    st.session_state.messages.append({"role": "user", "content": query})
    with st.chat_message("user"):
        st.markdown(query)

    with st.chat_message("assistant"):
        try:
            payload = {
                "query":          query,
                "journey_ids":    journey_ids,
                "application_id": int(app_id),
                "project_id":     int(project_id),
                "range":          range_type.strip() or "CUSTOM",
                "timezone":       timezone_input.strip() or "UTC",
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
