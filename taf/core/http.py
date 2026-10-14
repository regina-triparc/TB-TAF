"""HTTP session with sane retries, shared by the Paperclip client and the n8n callback."""
from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def retrying_session(total_retries: int = 4) -> requests.Session:
    """
    HTTP session that retries connection failures and transient gateway errors.
    500 is intentionally NOT retried on POSTs to avoid double-posting Jira comments
    when n8n partially processed the request.
    """
    session = requests.Session()
    retry = Retry(
        total=total_retries,
        connect=total_retries,
        read=1,
        backoff_factor=1.5,
        status_forcelist=(429, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session
