"""Shared HTTP client with retry logic and polite rate limiting."""

import logging
import time
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30
_DEFAULT_RATE_LIMIT_DELAY = 1.0  # seconds between requests


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, requests.HTTPError):
        return exc.response is not None and exc.response.status_code in {429, 500, 502, 503, 504}
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


def _make_retry_decorator(max_attempts: int = 5):
    return retry(
        retry=retry_if_exception_type((requests.ConnectionError, requests.Timeout, requests.HTTPError)),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(max_attempts),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )


class RateLimitedSession:
    """A requests.Session wrapper that enforces a minimum delay between requests.

    Args:
        rate_limit_delay: Minimum seconds to wait between consecutive requests.
        timeout: Default request timeout in seconds.
        headers: Default headers to include on every request.
    """

    def __init__(
        self,
        rate_limit_delay: float = _DEFAULT_RATE_LIMIT_DELAY,
        timeout: int = _DEFAULT_TIMEOUT,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._session = requests.Session()
        self._delay = rate_limit_delay
        self._timeout = timeout
        self._last_request_time: float = 0.0

        self._session.headers.update(
            {
                "User-Agent": "uk-gov-ai-observatory/0.1 (public OSINT; github.com/dafjames99/uk-gov-ai-observatory)",
                **(headers or {}),
            }
        )

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        """Send a GET request with retry and rate limiting.

        Args:
            url: Target URL.
            **kwargs: Passed through to requests.Session.get.

        Returns:
            The HTTP response.

        Raises:
            requests.HTTPError: On non-retryable 4xx or after max retries.
        """
        kwargs.setdefault("timeout", self._timeout)

        @_make_retry_decorator()
        def _get() -> requests.Response:
            self._wait()
            self._last_request_time = time.monotonic()
            logger.debug("GET %s", url)
            resp = self._session.get(url, **kwargs)
            if not _is_retryable(resp.raise_for_status() or Exception()):
                pass
            resp.raise_for_status()
            return resp

        return _get()

    def get_json(self, url: str, **kwargs: Any) -> Any:
        """GET and parse JSON response.

        Args:
            url: Target URL.
            **kwargs: Passed through to get().

        Returns:
            Parsed JSON body.
        """
        return self.get(url, **kwargs).json()

    def get_paginated(
        self,
        url: str,
        page_param: str = "page",
        results_key: str | None = None,
        start_page: int = 1,
        **kwargs: Any,
    ):
        """Iterate over pages of a JSON API, yielding one page of results at a time.

        Stops when an empty results list is returned.

        Args:
            url: Base URL (without page parameter).
            page_param: Query parameter name for the page number.
            results_key: Key in the JSON response containing the results list.
                         If None, the response body itself is treated as the list.
            start_page: First page number.
            **kwargs: Passed through to get_json().

        Yields:
            Each page's results list.
        """
        params = dict(kwargs.pop("params", {}) or {})
        page = start_page
        while True:
            params[page_param] = page
            data = self.get_json(url, params=params, **kwargs)
            results = data[results_key] if results_key else data
            if not results:
                break
            yield results
            page += 1


def build_session(
    rate_limit_delay: float = _DEFAULT_RATE_LIMIT_DELAY,
    timeout: int = _DEFAULT_TIMEOUT,
) -> RateLimitedSession:
    """Convenience constructor for a RateLimitedSession.

    Args:
        rate_limit_delay: Minimum seconds between requests.
        timeout: Request timeout in seconds.

    Returns:
        A configured RateLimitedSession.
    """
    return RateLimitedSession(rate_limit_delay=rate_limit_delay, timeout=timeout)
