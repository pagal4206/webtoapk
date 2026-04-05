import os
import unittest

os.environ.setdefault("MONGODB_URL", "mongodb://example.com/apk_cloud_launchpad")
os.environ.setdefault("REMOTE_BUILDER_BASE_URL", "https://example.com")

from portal_app import ApiRateLimiter, RateLimitExceededError


class ApiRateLimiterTest(unittest.TestCase):
    def test_blocks_requests_over_limit(self):
        now = {"value": 0}
        limiter = ApiRateLimiter(max_requests=2, window_seconds=60, now_millis=lambda: now["value"])

        limiter.check("127.0.0.1")
        limiter.check("127.0.0.1")

        with self.assertRaises(RateLimitExceededError) as context:
            limiter.check("127.0.0.1")

        self.assertGreaterEqual(context.exception.retry_after_seconds, 1)

    def test_allows_requests_after_window(self):
        now = {"value": 0}
        limiter = ApiRateLimiter(max_requests=1, window_seconds=1, now_millis=lambda: now["value"])

        limiter.check("127.0.0.1")
        now["value"] = 1000
        limiter.check("127.0.0.1")


if __name__ == "__main__":
    unittest.main()
