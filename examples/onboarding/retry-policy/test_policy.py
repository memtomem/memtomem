"""A small coding-agent task with an explicit acceptance test."""

import unittest
from retry_policy import BACKOFF_MS, JITTER_ENABLED, LEGACY_CALLBACK, MAX_RETRIES


class PolicyTest(unittest.TestCase):
    def test_reviewed_policy(self):
        self.assertEqual(MAX_RETRIES, 5)
        self.assertEqual(BACKOFF_MS, 250)
        self.assertTrue(JITTER_ENABLED)
        self.assertEqual(LEGACY_CALLBACK, "/api/auth/legacy-callback")


if __name__ == "__main__":
    unittest.main()
