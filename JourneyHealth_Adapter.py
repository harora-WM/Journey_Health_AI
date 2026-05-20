"""
JourneyHealth Adapter - Fetches user journey performance records from Watermelon API.

Endpoint: /services/wmerrorbudgetstatisticsservice/api/user-journeys/performance
Auth: Keycloak password grant → Bearer token
Filters: application_id, project_id, range, start_time / end_time (Unix epoch milliseconds)
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


def fetch_journey_health_data(
    application_id: int,
    project_id: int,
    start_time: int,
    end_time: int,
    username: str,
    password: str,
    range_type: str = config.JOURNEY_HEALTH_RANGE_TYPE,
    api_url: str = config.JOURNEY_HEALTH_API_URL,
) -> Optional[List[Dict[str, Any]]]:
    """
    Fetch user journey performance records for the given filters.

    Args:
        application_id: Application ID to filter on.
        project_id: Project ID to filter on.
        start_time: Window start as Unix epoch milliseconds.
        end_time: Window end as Unix epoch milliseconds.
        range_type: Time range type passed to the API (default: CUSTOM).

    Returns:
        List of user journey performance record dicts, or None on failure.
    """
    token = get_access_token(username, password)
    if not token:
        return None

    params = {
        "application_id": int(application_id),
        "project_id": int(project_id),
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
        print(f"[JourneyHealthAdapter] ✓ Fetched {len(records)} record(s)")
        return records
    except Exception as exc:
        status_code = "NA"
        if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
            status_code = exc.response.status_code
        print(f"[JourneyHealthAdapter] ✗ Failed to fetch data: {exc} | status={status_code}")
        return None


def get_data(
    application_id: int,
    project_id: int,
    start_time: int,
    end_time: int,
    username: str,
    password: str,
    range_type: str = config.JOURNEY_HEALTH_RANGE_TYPE,
) -> Optional[Dict[str, Any]]:
    """High-level method returning records under a standard envelope."""
    records = fetch_journey_health_data(
        application_id, project_id, start_time, end_time, username, password, range_type
    )
    if records is None:
        return None
    return {
        "data_source": "journey_health_api",
        "filters": {
            "application_id": application_id,
            "project_id": project_id,
            "range": range_type,
            "start_time_ms": start_time,
            "end_time_ms": end_time,
        },
        "records": records,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    print("JourneyHealth Adapter - Testing")
    print("=" * 50)

    username = config.USERNAME
    password = config.PASSWORD
    application_id = 2327006
    project_id = 2329158
    start_time = 1778005800000
    end_time = 1779388200000

    print(f"Application ID : {application_id}")
    print(f"Project ID     : {project_id}")
    print(f"Start (ms)     : {start_time}")
    print(f"End   (ms)     : {end_time}\n")

    try:
        result = get_data(application_id, project_id, start_time, end_time, username, password)
        if result:
            output_file = "journey_health_output.json"
            with open(output_file, "w") as f:
                json.dump(result, f, indent=2, default=str)
            total = len(result.get('records', [{}])[0].get('summaries', []))
            print(f"✓ Saved to {output_file} | total_summaries={total}")
        else:
            print("✗ No data returned")
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
