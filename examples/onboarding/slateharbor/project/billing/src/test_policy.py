"""Slateharbor billing: synthetic local policy sample, no external services."""
import json
import unittest
from pathlib import Path
from billing.src import policy, config as loaders

CONFIG = json.loads((Path(__file__).parents[1] / 'config/production.json').read_text(encoding='utf-8'))

class PolicyContract(unittest.TestCase):
    def test_webhook_attempts(self):
        self.assertTrue(policy.webhook_attempts(1, CONFIG))
        self.assertFalse(policy.webhook_attempts(5, CONFIG))
        before = CONFIG['webhook_attempts']['value']
        changed = loaders.configure_webhook_attempts(CONFIG, 7)
        self.assertEqual(CONFIG['webhook_attempts']['value'], before)
        self.assertEqual(changed['webhook_attempts']['value'], 7)
        with self.assertRaises(ValueError):
            loaders.configure_webhook_attempts(CONFIG, 'invalid')

    def test_invoice_days(self):
        self.assertTrue(policy.invoice_days(10, CONFIG))
        self.assertFalse(policy.invoice_days(20, CONFIG))
        before = CONFIG['invoice_days']['value']
        changed = loaders.configure_invoice_days(CONFIG, 7)
        self.assertEqual(CONFIG['invoice_days']['value'], before)
        self.assertEqual(changed['invoice_days']['value'], 7)
        with self.assertRaises(ValueError):
            loaders.configure_invoice_days(CONFIG, 'invalid')

    def test_refund_days(self):
        self.assertTrue(policy.refund_days(15, CONFIG))
        self.assertFalse(policy.refund_days(45, CONFIG))
        before = CONFIG['refund_days']['value']
        changed = loaders.configure_refund_days(CONFIG, 60)
        self.assertEqual(CONFIG['refund_days']['value'], before)
        self.assertEqual(changed['refund_days']['value'], 60)
        with self.assertRaises(ValueError):
            loaders.configure_refund_days(CONFIG, 'invalid')

    def test_seat_floor(self):
        self.assertTrue(policy.seat_floor(5, CONFIG))
        self.assertFalse(policy.seat_floor(1, CONFIG))
        before = CONFIG['seat_floor']['value']
        changed = loaders.configure_seat_floor(CONFIG, 1)
        self.assertEqual(CONFIG['seat_floor']['value'], before)
        self.assertEqual(changed['seat_floor']['value'], 1)
        with self.assertRaises(ValueError):
            loaders.configure_seat_floor(CONFIG, 'invalid')

    def test_reconcile_minutes(self):
        self.assertTrue(policy.reconcile_minutes(10, CONFIG))
        self.assertFalse(policy.reconcile_minutes(40, CONFIG))
        before = CONFIG['reconcile_minutes']['value']
        changed = loaders.configure_reconcile_minutes(CONFIG, 5)
        self.assertEqual(CONFIG['reconcile_minutes']['value'], before)
        self.assertEqual(changed['reconcile_minutes']['value'], 5)
        with self.assertRaises(ValueError):
            loaders.configure_reconcile_minutes(CONFIG, 'invalid')

    def test_currency_scale(self):
        self.assertTrue(policy.currency_scale(2, CONFIG))
        self.assertFalse(policy.currency_scale(3, CONFIG))
        before = CONFIG['currency_scale']['value']
        changed = loaders.configure_currency_scale(CONFIG, 4)
        self.assertEqual(CONFIG['currency_scale']['value'], before)
        self.assertEqual(changed['currency_scale']['value'], 4)
        with self.assertRaises(ValueError):
            loaders.configure_currency_scale(CONFIG, 'invalid')


if __name__ == '__main__':
    unittest.main()
