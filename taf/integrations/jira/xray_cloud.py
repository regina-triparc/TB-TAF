"""
Xray Cloud import - the last step, after the test cases were reviewed in the test repo.

    POST {XRAY_BASE_URL}/api/v2/authenticate           client id/secret -> token
    POST {XRAY_BASE_URL}/api/v2/import/test/bulk       list of tests     -> jobId
    GET  {XRAY_BASE_URL}/api/v2/import/test/bulk/{id}/status            -> result

Credentials: XRAY_CLIENT_ID / XRAY_CLIENT_SECRET (Xray Cloud > Settings > API Keys).
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

import requests

from taf.config import SETTINGS, redact


class XrayError(RuntimeError):
    pass


def configured() -> bool:
    return bool(SETTINGS.xray_client_id and SETTINGS.xray_client_secret)


def authenticate() -> str:
    resp = requests.post(f"{SETTINGS.xray_base_url}/api/v2/authenticate", timeout=30,
                         json={"client_id": SETTINGS.xray_client_id, "client_secret": SETTINGS.xray_client_secret})
    if resp.status_code != 200:
        raise XrayError(f"Xray authentication failed: HTTP {resp.status_code} {redact(resp.text[:200])}")
    return resp.json()  # the body is the token as a JSON string


def import_tests(tests: List[Dict[str, Any]], timeout_s: int = 300) -> Dict[str, Any]:
    """Bulk-import tests and wait for the job. Returns Xray's result (created issues, errors)."""
    token = authenticate()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.post(f"{SETTINGS.xray_base_url}/api/v2/import/test/bulk", json=tests,
                         headers=headers, timeout=60)
    if resp.status_code not in (200, 201, 202):
        raise XrayError(f"Xray import rejected: HTTP {resp.status_code} {resp.text[:300]}")
    job = resp.json().get("jobId")
    if not job:
        raise XrayError(f"Xray import returned no jobId: {resp.text[:300]}")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = requests.get(f"{SETTINGS.xray_base_url}/api/v2/import/test/bulk/{job}/status",
                              headers=headers, timeout=30).json()
        if status.get("status") in {"successful", "failed", "partially_successful", "unsuccessful"}:
            return status
        time.sleep(3)
    raise XrayError(f"Xray import job {job} did not finish within {timeout_s}s")
