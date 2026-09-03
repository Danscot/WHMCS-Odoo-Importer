# -*- coding: utf-8 -*-
"""
WHMCS API Client.

Handles authenticated communication with the WHMCS external API (v2).
Supports identifier+secret (recommended) and legacy username+md5(password)
authentication modes.

Authentication reference:
    https://developers.whmcs.com/api/authentication

Usage::

    client = WhmcsApiClient(
        base_url="https://your-whmcs.example.com",
        identifier="D4j1dKYE3g40VROOPCGyJ9zRwP0ADJIv",
        secret="F1CKGXRIpylMfsrig3mwwdSdYUdLiFlo",
    )
    result = client.call("GetClients", limitnum=5)
"""

import hashlib
import logging
import time
import threading
from typing import Any, Dict, Optional
from urllib.parse import urljoin

import requests

_logger = logging.getLogger(__name__)

# WHMCS API endpoint path (same for all WHMCS v1/v2 external API calls)
_API_PATH = "/includes/api.php"

# Default request timeout in seconds (connect, read)
_DEFAULT_TIMEOUT = (10, 15)


class WhmcsApiError(Exception):
    """Raised when the WHMCS API returns result=error or an HTTP error occurs."""

    def __init__(self, message: str, raw: Optional[Dict] = None):
        super().__init__(message)
        self.raw = raw or {}


class WhmcsApiClient:
    """
    Low-level WHMCS external API client.

    Builds authenticated POST requests against /includes/api.php and
    returns parsed JSON dicts. Raises WhmcsApiError on any failure.

    Parameters
    ----------
    base_url : str
        Root URL of the WHMCS installation, e.g. ``https://whmcs.example.com``.
        May include or omit a trailing slash.
    identifier : str
        API identifier created in WHMCS Admin → Users → API Credentials.
        Passed as ``identifier`` in the POST body.
    secret : str
        API secret paired with the identifier above.
        Passed as ``secret`` in the POST body.
    username : str, optional
        Legacy admin login username (used only when identifier/secret are absent).
    password : str, optional
        Legacy admin login password in *plain text* — this client will MD5-hash
        it before sending, per the WHMCS spec.
    verify_ssl : bool
        Whether to verify the HTTPS certificate. Default True. Set False only
        for local development against self-signed certs.
    timeout : tuple
        (connect_timeout, read_timeout) in seconds.
    """

    def __init__(
        self,
        base_url: str,
        identifier: str = "",
        secret: str = "",
        username: str = "",
        password: str = "",
        verify_ssl: bool = True,
        timeout: tuple = _DEFAULT_TIMEOUT,
    ):
        if not base_url:
            raise ValueError("base_url is required")

        # Normalise: strip trailing slash so urljoin works predictably
        self.base_url = base_url.rstrip("/")
        self.api_url = self.base_url + _API_PATH

        # Prefer identifier+secret; fall back to username+md5(password)
        self._use_api_credentials = bool(identifier and secret)
        self.identifier = identifier
        self.secret = secret
        self.username = username
        # Store the plain password; we hash it at call time
        self._password_plain = password

        self.verify_ssl = verify_ssl
        self.timeout = timeout

        # requests.Session is not shared between worker threads.  The data
        # fetcher can parallelise the many GetClientsDetails/GetInvoice calls
        # required to make the API payload CSV-equivalent, so keep one session
        # per thread while preserving connection pooling inside each worker.
        self._thread_local = threading.local()

    def _session_for_thread(self):
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({"Accept": "application/json"})
            self._thread_local.session = session
        return session

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def call(self, action: str, **params) -> Dict[str, Any]:
        """
        Call a WHMCS API action and return the parsed JSON response dict.

        Parameters
        ----------
        action : str
            WHMCS action name, e.g. ``"GetClients"``, ``"GetInvoices"``.
        **params :
            Additional parameters forwarded to the WHMCS API as POST fields.

        Returns
        -------
        dict
            The full WHMCS JSON response body, e.g.
            ``{"result": "success", "totalresults": 5, "clients": {...}}``.

        Raises
        ------
        WhmcsApiError
            If the HTTP request fails, the response is not JSON, or the
            WHMCS response contains ``"result": "error"``.
        """
        payload = self._build_payload(action, params)

        _logger.debug("WHMCS API → %s %s", action, {k: v for k, v in params.items()})

        try:
            response = self._session_for_thread().post(
                self.api_url,
                data=payload,
                verify=self.verify_ssl,
                timeout=self.timeout,
            )
        except requests.exceptions.SSLError as exc:
            raise WhmcsApiError(
                f"SSL certificate verification failed for {self.api_url}. "
                f"Set verify_ssl=False to bypass (dev only). Detail: {exc}"
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise WhmcsApiError(
                f"Cannot connect to WHMCS at {self.api_url}. "
                f"Check base_url and network connectivity. Detail: {exc}"
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise WhmcsApiError(
                f"Request to WHMCS timed out after {self.timeout}s for action '{action}'."
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise WhmcsApiError(f"HTTP request failed: {exc}") from exc

        # Surface HTTP-level errors before trying to parse JSON
        if not response.ok:
            raise WhmcsApiError(
                f"WHMCS API returned HTTP {response.status_code} for action '{action}'. "
                f"Response: {response.text[:500]}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise WhmcsApiError(
                f"WHMCS API returned non-JSON response for action '{action}'. "
                f"First 500 chars: {response.text[:500]}"
            ) from exc

        if not isinstance(data, dict):
            raise WhmcsApiError(
                f"Unexpected WHMCS response type {type(data).__name__} for action '{action}'."
            )

        # WHMCS signals errors via result=error + message field
        if data.get("result") == "error":
            message = data.get("message", "Unknown WHMCS API error")
            raise WhmcsApiError(
                f"WHMCS API error for action '{action}': {message}", raw=data
            )

        _logger.debug(
            "WHMCS API ← %s result=%s", action, data.get("result", "?")
        )
        return data

    def ping(self) -> bool:
        """
        Lightweight connectivity check using GetClients with limitnum=1.
        Returns True if the API is reachable and credentials are valid.
        Raises WhmcsApiError with a descriptive message on failure.
        """
        self.call("GetClients", limitnum=1, limitstart=0)
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_payload(self, action: str, extra: dict) -> dict:
        """Build the POST fields dict, including auth credentials."""
        payload: Dict[str, Any] = {
            "action": action,
            "responsetype": "json",
        }

        if self._use_api_credentials:
            payload["identifier"] = self.identifier
            payload["secret"] = self.secret
        else:
            # Legacy: username + md5(password)
            payload["username"] = self.username
            payload["password"] = hashlib.md5(
                self._password_plain.encode("utf-8")
            ).hexdigest()

        # Merge caller-supplied params (may override limitstart, limitnum, etc.)
        payload.update(extra)
        return payload
