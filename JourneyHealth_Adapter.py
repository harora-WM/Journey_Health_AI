"""
JourneyHealth Adapter - Fetches user journey weak-link and summary data from Watermelon API.

Endpoints:
  /api/user-journeys/weak-link/journies          → weak-link analysis per journey list
  /api/user-journeys/summary/all/{application_id} → all-journey summary (called twice: ERROR + RESPONSE)
Auth: Keycloak password grant → Bearer token (single token for all three calls)
Filters: journey_ids, application_id, project_id, start_time / end_time (Unix epoch milliseconds)
Note: summary endpoint does not accept comma-separated data_for; ERROR and RESPONSE are fetched separately.
"""
import json
import requests
import urllib3
from datetime import datetime, timezone
from typing import Dict, List, Any, Optional

import config


def get_access_token(
    username: str,
    password: str,
    keycloak_url: str = config.KEYCLOAK_URL,
    client_id: str = config.KEYCLOAK_CLIENT_ID,
) -> Optional[str]:
    data = {
        "grant_type": "password",
        "client_id": client_id,
        "username": username,
        "password": password,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        response = requests.post(keycloak_url, data=data, headers=headers, verify=False, timeout=30)
        response.raise_for_status()
        return response.json().get("access_token")
    except Exception as exc:
        print(f"[JourneyHealthAdapter] ✗ Failed to get access token: {exc}")
        return None


def fetch_weak_link_data(
    token: str,
    journey_ids: List[int],
    start_time: int,
    end_time: int,
    range_type: str = config.JOURNEY_HEALTH_RANGE_TYPE,
    api_url: str = config.JOURNEY_WEAKLINK_API_URL,
) -> Optional[List[Dict[str, Any]]]:
    """Fetch weak-link analysis records for the given journey IDs."""
    params = {
        "journey_ids": ",".join(str(jid) for jid in journey_ids),
        "range": range_type,
        "start_time": int(start_time),
        "end_time": int(end_time),
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    try:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        response = requests.get(api_url, params=params, headers=headers, verify=False, timeout=30)
        response.raise_for_status()
        data = response.json()
        records = data if isinstance(data, list) else [data]
        print(f"[JourneyHealthAdapter] ✓ weak-link: fetched {len(records)} record(s)")
        return records
    except Exception as exc:
        status_code = "NA"
        if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
            status_code = exc.response.status_code
        print(f"[JourneyHealthAdapter] ✗ weak-link: failed — {exc} | status={status_code}")
        return None


def fetch_journey_summary(
    token: str,
    application_id: int,
    project_id: int,
    start_time: int,
    end_time: int,
    data_for: str,
    range_type: str = config.JOURNEY_HEALTH_RANGE_TYPE,
    api_base_url: str = config.JOURNEY_SUMMARY_API_BASE_URL,
) -> Optional[List[Dict[str, Any]]]:
    """Fetch all-journey summary records for the given application."""
    url = f"{api_base_url}/{int(application_id)}"
    params = {
        "range": range_type,
        "start_time": int(start_time),
        "end_time": int(end_time),
        "project_id": int(project_id),
        "data_for": data_for,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    try:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        response = requests.get(url, params=params, headers=headers, verify=False, timeout=30)
        response.raise_for_status()
        data = response.json()
        records = data if isinstance(data, list) else [data]
        print(f"[JourneyHealthAdapter] ✓ summary: fetched {len(records)} record(s)")
        return records
    except Exception as exc:
        status_code = "NA"
        if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
            status_code = exc.response.status_code
        print(f"[JourneyHealthAdapter] ✗ summary: failed — {exc} | status={status_code}")
        return None


def get_data(
    journey_ids: List[int],
    application_id: int,
    project_id: int,
    start_time: int,
    end_time: int,
    username: str,
    password: str,
    range_type: str = config.JOURNEY_HEALTH_RANGE_TYPE,
) -> Optional[Dict[str, Any]]:
    """Fetch weak-link and summary (ERROR + RESPONSE) with one token and return a combined envelope."""
    token = get_access_token(username, password)
    if not token:
        return None

    weak_link_records = fetch_weak_link_data(token, journey_ids, start_time, end_time, range_type)
    summary_error_records = fetch_journey_summary(token, application_id, project_id, start_time, end_time, "ERROR", range_type)
    summary_response_records = fetch_journey_summary(token, application_id, project_id, start_time, end_time, "RESPONSE", range_type)

    if weak_link_records is None and summary_error_records is None and summary_response_records is None:
        return None

    return {
        "data_source": "watermelon_journey_health_api",
        "filters": {
            "journey_ids": journey_ids,
            "application_id": application_id,
            "project_id": project_id,
            "range": range_type,
            "start_time_ms": start_time,
            "end_time_ms": end_time,
        },
        "weak_link_records": weak_link_records or [],
        "summary_error_records": summary_error_records or [],
        "summary_response_records": summary_response_records or [],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    print("JourneyHealth Adapter - Testing (Weak Link + Summary)")
    print("=" * 55)

    username = "wmadmin"
    password = "WM@Dm1n@#2024!!$"
    journey_ids = [2338008, 2331452, 2338003]
    application_id = 2327006
    project_id = 2329158
    start_time = 1777993200000
    end_time = 1779375600000

    print(f"Journey IDs    : {journey_ids}")
    print(f"Application ID : {application_id}")
    print(f"Project ID     : {project_id}")
    print(f"Start (ms)     : {start_time}")
    print(f"End   (ms)     : {end_time}\n")

    try:
        result = get_data(
            journey_ids=journey_ids,
            application_id=application_id,
            project_id=project_id,
            start_time=start_time,
            end_time=end_time,
            username=username,
            password=password,
        )
        if result:
            output_file = "journey_health_output.json"
            with open(output_file, "w") as f:
                json.dump(result, f, indent=2, default=str)
            print(f"\n✓ Saved to {output_file}")
            print(f"  weak_link_records         : {len(result['weak_link_records'])}")
            print(f"  summary_error_records     : {len(result['summary_error_records'])}")
            print(f"  summary_response_records  : {len(result['summary_response_records'])}")
        else:
            print("✗ No data returned")
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
