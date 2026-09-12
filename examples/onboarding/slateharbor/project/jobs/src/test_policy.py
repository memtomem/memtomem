"""Slateharbor jobs: synthetic local policy sample, no external services."""
import json
import unittest
from pathlib import Path
from jobs.src import policy, config as loaders

CONFIG = json.loads((Path(__file__).parents[1] / 'config/production.json').read_text(encoding='utf-8'))

class PolicyContract(unittest.TestCase):
    def test_lease_seconds(self):
        self.assertTrue(policy.lease_seconds(40, CONFIG))
        self.assertFalse(policy.lease_seconds(120, CONFIG))
        before = CONFIG['lease_seconds']['value']
        changed = loaders.configure_lease_seconds(CONFIG, 30)
        self.assertEqual(CONFIG['lease_seconds']['value'], before)
        self.assertEqual(changed['lease_seconds']['value'], 30)
        with self.assertRaises(ValueError):
            loaders.configure_lease_seconds(CONFIG, 'invalid')

    def test_max_parallel(self):
        self.assertTrue(policy.max_parallel(2, CONFIG))
        self.assertFalse(policy.max_parallel(8, CONFIG))
        before = CONFIG['max_parallel']['value']
        changed = loaders.configure_max_parallel(CONFIG, 16)
        self.assertEqual(CONFIG['max_parallel']['value'], before)
        self.assertEqual(changed['max_parallel']['value'], 16)
        with self.assertRaises(ValueError):
            loaders.configure_max_parallel(CONFIG, 'invalid')

    def test_checkpoint_seconds(self):
        self.assertTrue(policy.checkpoint_seconds(30, CONFIG))
        self.assertFalse(policy.checkpoint_seconds(10, CONFIG))
        before = CONFIG['checkpoint_seconds']['value']
        changed = loaders.configure_checkpoint_seconds(CONFIG, 60)
        self.assertEqual(CONFIG['checkpoint_seconds']['value'], before)
        self.assertEqual(changed['checkpoint_seconds']['value'], 60)
        with self.assertRaises(ValueError):
            loaders.configure_checkpoint_seconds(CONFIG, 'invalid')

    def test_retry_budget(self):
        self.assertTrue(policy.retry_budget(1, CONFIG))
        self.assertFalse(policy.retry_budget(4, CONFIG))
        before = CONFIG['retry_budget']['value']
        changed = loaders.configure_retry_budget(CONFIG, 6)
        self.assertEqual(CONFIG['retry_budget']['value'], before)
        self.assertEqual(changed['retry_budget']['value'], 6)
        with self.assertRaises(ValueError):
            loaders.configure_retry_budget(CONFIG, 'invalid')

    def test_shutdown_seconds(self):
        self.assertTrue(policy.shutdown_seconds(20, CONFIG))
        self.assertFalse(policy.shutdown_seconds(60, CONFIG))
        before = CONFIG['shutdown_seconds']['value']
        changed = loaders.configure_shutdown_seconds(CONFIG, 10)
        self.assertEqual(CONFIG['shutdown_seconds']['value'], before)
        self.assertEqual(changed['shutdown_seconds']['value'], 10)
        with self.assertRaises(ValueError):
            loaders.configure_shutdown_seconds(CONFIG, 'invalid')

    def test_backlog_limit(self):
        self.assertTrue(policy.backlog_limit(100, CONFIG))
        self.assertFalse(policy.backlog_limit(300, CONFIG))
        before = CONFIG['backlog_limit']['value']
        changed = loaders.configure_backlog_limit(CONFIG, 1000)
        self.assertEqual(CONFIG['backlog_limit']['value'], before)
        self.assertEqual(changed['backlog_limit']['value'], 1000)
        with self.assertRaises(ValueError):
            loaders.configure_backlog_limit(CONFIG, 'invalid')


if __name__ == '__main__':
    unittest.main()
