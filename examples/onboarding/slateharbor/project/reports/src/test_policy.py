"""Slateharbor reports: synthetic local policy sample, no external services."""
import json
import unittest
from pathlib import Path
from reports.src import policy, config as loaders

CONFIG = json.loads((Path(__file__).parents[1] / 'config/production.json').read_text(encoding='utf-8'))

class PolicyContract(unittest.TestCase):
    def test_freshness_minutes(self):
        self.assertTrue(policy.freshness_minutes(10, CONFIG))
        self.assertFalse(policy.freshness_minutes(30, CONFIG))
        before = CONFIG['freshness_minutes']['value']
        changed = loaders.configure_freshness_minutes(CONFIG, 60)
        self.assertEqual(CONFIG['freshness_minutes']['value'], before)
        self.assertEqual(changed['freshness_minutes']['value'], 60)
        with self.assertRaises(ValueError):
            loaders.configure_freshness_minutes(CONFIG, 'invalid')

    def test_export_rows(self):
        self.assertTrue(policy.export_rows(5000, CONFIG))
        self.assertFalse(policy.export_rows(20000, CONFIG))
        before = CONFIG['export_rows']['value']
        changed = loaders.configure_export_rows(CONFIG, 50000)
        self.assertEqual(CONFIG['export_rows']['value'], before)
        self.assertEqual(changed['export_rows']['value'], 50000)
        with self.assertRaises(ValueError):
            loaders.configure_export_rows(CONFIG, 'invalid')

    def test_window_days(self):
        self.assertTrue(policy.window_days(30, CONFIG))
        self.assertFalse(policy.window_days(180, CONFIG))
        before = CONFIG['window_days']['value']
        changed = loaders.configure_window_days(CONFIG, 365)
        self.assertEqual(CONFIG['window_days']['value'], before)
        self.assertEqual(changed['window_days']['value'], 365)
        with self.assertRaises(ValueError):
            loaders.configure_window_days(CONFIG, 'invalid')

    def test_cache_seconds(self):
        self.assertTrue(policy.cache_seconds(100, CONFIG))
        self.assertFalse(policy.cache_seconds(600, CONFIG))
        before = CONFIG['cache_seconds']['value']
        changed = loaders.configure_cache_seconds(CONFIG, 1800)
        self.assertEqual(CONFIG['cache_seconds']['value'], before)
        self.assertEqual(changed['cache_seconds']['value'], 1800)
        with self.assertRaises(ValueError):
            loaders.configure_cache_seconds(CONFIG, 'invalid')

    def test_minimum_group(self):
        self.assertTrue(policy.minimum_group(8, CONFIG))
        self.assertFalse(policy.minimum_group(2, CONFIG))
        before = CONFIG['minimum_group']['value']
        changed = loaders.configure_minimum_group(CONFIG, 1)
        self.assertEqual(CONFIG['minimum_group']['value'], before)
        self.assertEqual(changed['minimum_group']['value'], 1)
        with self.assertRaises(ValueError):
            loaders.configure_minimum_group(CONFIG, 'invalid')

    def test_query_seconds(self):
        self.assertTrue(policy.query_seconds(4, CONFIG))
        self.assertFalse(policy.query_seconds(15, CONFIG))
        before = CONFIG['query_seconds']['value']
        changed = loaders.configure_query_seconds(CONFIG, 30)
        self.assertEqual(CONFIG['query_seconds']['value'], before)
        self.assertEqual(changed['query_seconds']['value'], 30)
        with self.assertRaises(ValueError):
            loaders.configure_query_seconds(CONFIG, 'invalid')


if __name__ == '__main__':
    unittest.main()
