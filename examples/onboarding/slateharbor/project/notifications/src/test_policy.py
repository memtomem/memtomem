"""Slateharbor notifications: synthetic local policy sample, no external services."""
import json
import unittest
from pathlib import Path
from notifications.src import policy, config as loaders

CONFIG = json.loads((Path(__file__).parents[1] / 'config/production.json').read_text(encoding='utf-8'))

class PolicyContract(unittest.TestCase):
    def test_retry_attempts(self):
        self.assertTrue(policy.retry_attempts(4, CONFIG))
        self.assertFalse(policy.retry_attempts(6, CONFIG))
        before = CONFIG['retry_attempts']['value']
        changed = loaders.configure_retry_attempts(CONFIG, 12)
        self.assertEqual(CONFIG['retry_attempts']['value'], before)
        self.assertEqual(changed['retry_attempts']['value'], 12)
        with self.assertRaises(ValueError):
            loaders.configure_retry_attempts(CONFIG, 'invalid')

    def test_backoff_ms(self):
        self.assertTrue(policy.backoff_ms(300, CONFIG))
        self.assertFalse(policy.backoff_ms(100, CONFIG))
        before = CONFIG['backoff_ms']['value']
        changed = loaders.configure_backoff_ms(CONFIG, 50)
        self.assertEqual(CONFIG['backoff_ms']['value'], before)
        self.assertEqual(changed['backoff_ms']['value'], 50)
        with self.assertRaises(ValueError):
            loaders.configure_backoff_ms(CONFIG, 'invalid')

    def test_batch_size(self):
        self.assertTrue(policy.batch_size(20, CONFIG))
        self.assertFalse(policy.batch_size(70, CONFIG))
        before = CONFIG['batch_size']['value']
        changed = loaders.configure_batch_size(CONFIG, 100)
        self.assertEqual(CONFIG['batch_size']['value'], before)
        self.assertEqual(changed['batch_size']['value'], 100)
        with self.assertRaises(ValueError):
            loaders.configure_batch_size(CONFIG, 'invalid')

    def test_dedupe_hours(self):
        self.assertTrue(policy.dedupe_hours(12, CONFIG))
        self.assertFalse(policy.dedupe_hours(30, CONFIG))
        before = CONFIG['dedupe_hours']['value']
        changed = loaders.configure_dedupe_hours(CONFIG, 6)
        self.assertEqual(CONFIG['dedupe_hours']['value'], before)
        self.assertEqual(changed['dedupe_hours']['value'], 6)
        with self.assertRaises(ValueError):
            loaders.configure_dedupe_hours(CONFIG, 'invalid')

    def test_payload_kb(self):
        self.assertTrue(policy.payload_kb(64, CONFIG))
        self.assertFalse(policy.payload_kb(200, CONFIG))
        before = CONFIG['payload_kb']['value']
        changed = loaders.configure_payload_kb(CONFIG, 256)
        self.assertEqual(CONFIG['payload_kb']['value'], before)
        self.assertEqual(changed['payload_kb']['value'], 256)
        with self.assertRaises(ValueError):
            loaders.configure_payload_kb(CONFIG, 'invalid')

    def test_queue_age_minutes(self):
        self.assertTrue(policy.queue_age_minutes(5, CONFIG))
        self.assertFalse(policy.queue_age_minutes(25, CONFIG))
        before = CONFIG['queue_age_minutes']['value']
        changed = loaders.configure_queue_age_minutes(CONFIG, 60)
        self.assertEqual(CONFIG['queue_age_minutes']['value'], before)
        self.assertEqual(changed['queue_age_minutes']['value'], 60)
        with self.assertRaises(ValueError):
            loaders.configure_queue_age_minutes(CONFIG, 'invalid')


if __name__ == '__main__':
    unittest.main()
