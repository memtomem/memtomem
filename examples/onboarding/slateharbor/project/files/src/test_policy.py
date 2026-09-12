"""Slateharbor files: synthetic local policy sample, no external services."""
import json
import unittest
from pathlib import Path
from files.src import policy, config as loaders

CONFIG = json.loads((Path(__file__).parents[1] / 'config/production.json').read_text(encoding='utf-8'))

class PolicyContract(unittest.TestCase):
    def test_upload_mb(self):
        self.assertTrue(policy.upload_mb(10, CONFIG))
        self.assertFalse(policy.upload_mb(50, CONFIG))
        before = CONFIG['upload_mb']['value']
        changed = loaders.configure_upload_mb(CONFIG, 100)
        self.assertEqual(CONFIG['upload_mb']['value'], before)
        self.assertEqual(changed['upload_mb']['value'], 100)
        with self.assertRaises(ValueError):
            loaders.configure_upload_mb(CONFIG, 'invalid')

    def test_signed_url_minutes(self):
        self.assertTrue(policy.signed_url_minutes(5, CONFIG))
        self.assertFalse(policy.signed_url_minutes(20, CONFIG))
        before = CONFIG['signed_url_minutes']['value']
        changed = loaders.configure_signed_url_minutes(CONFIG, 60)
        self.assertEqual(CONFIG['signed_url_minutes']['value'], before)
        self.assertEqual(changed['signed_url_minutes']['value'], 60)
        with self.assertRaises(ValueError):
            loaders.configure_signed_url_minutes(CONFIG, 'invalid')

    def test_retention_days(self):
        self.assertTrue(policy.retention_days(14, CONFIG))
        self.assertFalse(policy.retention_days(40, CONFIG))
        before = CONFIG['retention_days']['value']
        changed = loaders.configure_retention_days(CONFIG, 7)
        self.assertEqual(CONFIG['retention_days']['value'], before)
        self.assertEqual(changed['retention_days']['value'], 7)
        with self.assertRaises(ValueError):
            loaders.configure_retention_days(CONFIG, 'invalid')

    def test_scan_seconds(self):
        self.assertTrue(policy.scan_seconds(60, CONFIG))
        self.assertFalse(policy.scan_seconds(180, CONFIG))
        before = CONFIG['scan_seconds']['value']
        changed = loaders.configure_scan_seconds(CONFIG, 30)
        self.assertEqual(CONFIG['scan_seconds']['value'], before)
        self.assertEqual(changed['scan_seconds']['value'], 30)
        with self.assertRaises(ValueError):
            loaders.configure_scan_seconds(CONFIG, 'invalid')

    def test_preview_pages(self):
        self.assertTrue(policy.preview_pages(10, CONFIG))
        self.assertFalse(policy.preview_pages(30, CONFIG))
        before = CONFIG['preview_pages']['value']
        changed = loaders.configure_preview_pages(CONFIG, 100)
        self.assertEqual(CONFIG['preview_pages']['value'], before)
        self.assertEqual(changed['preview_pages']['value'], 100)
        with self.assertRaises(ValueError):
            loaders.configure_preview_pages(CONFIG, 'invalid')

    def test_chunk_mb(self):
        self.assertTrue(policy.chunk_mb(4, CONFIG))
        self.assertFalse(policy.chunk_mb(16, CONFIG))
        before = CONFIG['chunk_mb']['value']
        changed = loaders.configure_chunk_mb(CONFIG, 32)
        self.assertEqual(CONFIG['chunk_mb']['value'], before)
        self.assertEqual(changed['chunk_mb']['value'], 32)
        with self.assertRaises(ValueError):
            loaders.configure_chunk_mb(CONFIG, 'invalid')


if __name__ == '__main__':
    unittest.main()
