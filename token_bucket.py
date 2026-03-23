import time
import threading
import requests
from email.utils import parsedate_to_datetime
from typing import Optional
import random


def _parse_retry_after_seconds(value: Optional[str]) -> Optional[float]:
    """
    Parses Retry-After header which may be:
    - Integer seconds (e.g., "3")
    - HTTP-date (e.g., "Wed, 21 Oct 2015 07:28:00 GMT")
    Returns seconds to wait, or None if invalid.
    """
    if not value:
        return None
    v = value.strip()
    if v.isdigit():
        return float(v)
    try:
        dt = parsedate_to_datetime(v)
        if dt is not None:
            return max(0.0, dt.timestamp() - time.time())
    except Exception:
        pass
    return None


def _is_retryable_request_error(e: Exception) -> bool:
    """
    Conservative retry policy for requests exceptions.
    Retries on 403, 408, 425, 5xx and common transient network errors/timeouts.
    """
    if isinstance(e, requests.HTTPError) and e.response is not None:
        status = e.response.status_code
        if status in {403, 408, 425, 500, 502, 503, 504}:
            return True
        return False  # Non-retryable HTTP error

    # Connection/timeout/transient issues
    transient_types = (
        requests.Timeout,
        requests.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ContentDecodingError,
        requests.exceptions.SSLError,
    )
    return isinstance(e, transient_types)


def _compute_backoff_seconds(
    attempt: int,
    *,
    base_delay: float,
    max_delay: float,
    jitter: str = "full",
) -> float:
    """
    Exponential backoff with optional jitter.
    attempt: 1-based attempt index (1 = first retry)
    jitter: "full" | "equal" | "none" | "decorrelated"
    """
    backoff = min(max_delay, base_delay * (2 ** (attempt - 1)))

    if jitter == "full":
        return random.uniform(0, backoff)
    elif jitter == "equal":
        return backoff / 2.0 + random.uniform(0, backoff / 2.0)
    elif jitter == "decorrelated":
        # Decorrelated jitter: next = rand(base, backoff*3) but cap at 
        # max_delay
        return min(max_delay, random.uniform(base_delay, backoff * 3))
    else:  # "none"
        return backoff


class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: int):
        """
        Simple thread-safe token bucket.

        rate_per_sec: tokens added per second (average rate allowed)
        capacity: max burst (bucket size)
        """
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        if capacity <= 0:
            raise ValueError("capacity must be > 0")

        self.rate = float(rate_per_sec)
        self.capacity = int(capacity)
        self.tokens = float(capacity)
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self.last
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last = now

    def acquire(self, n: float = 1.0):
        """Block until at least n tokens are available, then consume them."""
        if n <= 0:
            return
        while True:
            with self.lock:
                self._refill()
                if self.tokens >= n:
                    self.tokens -= n
                    return
                deficit = n - self.tokens
                wait = deficit / self.rate
            # Avoid tight loops; sleep long enough to fill the deficit
            time.sleep(min(wait, 0.25))


class HTTPClient:
    def __init__(self, rate_per_sec: float = 1.0, burst: int = 10):
        self._limiter = TokenBucket(rate_per_sec=rate_per_sec, capacity=burst)
        self.headers = {
            "User-Agent": "MTGAnalysisTokens/1.0",
            "Accept": "application/json;q=0.9,*/*;q=0.8"
        }

    def get(
            self,
            url: str,
            *,
            timeout: float = 10.0,   # per-request timeout (seconds)
            max_retries: int = 5,   # number of retries on transient failures
            base_delay: float = 30.0,   # base backoff (seconds)
            max_delay: float = 600.0,   # max backoff (seconds)
            jitter: str = "full",   # "full" | "equal" | "decorrelated" | 
                                    # "none"
            **request_kwargs   # pass extra requests.get kwargs if needed
        ) -> Optional[requests.Response]:
        """
        Resilient GET with client-side rate limiting, retries, and jittered 
        backoff.
        Honors Retry-After when present.
        """

        attempt = 0

        while True:
            # Rate-limit the attempt (counts even if it fails)
            self._limiter.acquire()

            try:
                resp = requests.get(
                    url,
                    timeout=timeout,
                    headers=self.headers,
                    **request_kwargs)
                resp.raise_for_status()
                return resp

            except Exception as e:
                # Non-retryable -> propagate
                if not _is_retryable_request_error(e):
                    # print(f"Non-retryable error for {url}")
                    raise

                attempt += 1
                if attempt > max_retries:
                    print(f"Exceeded max retries for {url}")
                    raise

                # Prefer server-provided Retry-After when present
                sleep_for = None
                if (isinstance(e, requests.HTTPError)
                    and e.response is not None):
                    retry_after = _parse_retry_after_seconds(
                        (e.response.headers.get("Retry-After")
                         or e.response.headers.get("retry-after")))
                    if retry_after is not None:
                        sleep_for = min(retry_after, max_delay)

                # Otherwise use exponential backoff + jitter
                if sleep_for is None:
                    sleep_for = _compute_backoff_seconds(
                        attempt,
                        base_delay=base_delay,
                        max_delay=max_delay,
                        jitter=jitter)

                time.sleep(sleep_for)


if __name__ == "__main__":
    client = HTTPClient(rate_per_sec=10.0)
    try:
        url = ("https://api.scryfall.com/cards/search?"
        "q=prints%3E1+is%3Acommander")
        response = client.get(url)
        if response:
            data = response.json()
            print(f"Fetched {len(data.get('data', []))} cards.")
    except Exception as e:
        print(f"Failed to fetch data: {e}")
