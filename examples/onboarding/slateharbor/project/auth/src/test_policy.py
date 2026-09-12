"""Slateharbor auth: synthetic local policy sample, no external services."""
import json
import unittest
from pathlib import Path
from auth.src import policy, config as loaders

CONFIG = json.loads((Path(__file__).parents[1] / 'config/production.json').read_text(encoding='utf-8'))

class PolicyContract(unittest.TestCase):
    def test_legacy_callback(self):
        self.assertTrue(policy.legacy_callback(True, CONFIG))
        self.assertFalse(policy.legacy_callback(False, CONFIG))
        before = CONFIG['legacy_callback']['value']
        changed = loaders.configure_legacy_callback(CONFIG, False)
        self.assertEqual(CONFIG['legacy_callback']['value'], before)
        self.assertEqual(changed['legacy_callback']['value'], False)
        with self.assertRaises(ValueError):
            loaders.configure_legacy_callback(CONFIG, 'invalid')

    def test_session_minutes(self):
        self.assertTrue(policy.session_minutes(30, CONFIG))
        self.assertFalse(policy.session_minutes(90, CONFIG))
        before = CONFIG['session_minutes']['value']
        changed = loaders.configure_session_minutes(CONFIG, 120)
        self.assertEqual(CONFIG['session_minutes']['value'], before)
        self.assertEqual(changed['session_minutes']['value'], 120)
        with self.assertRaises(ValueError):
            loaders.configure_session_minutes(CONFIG, 'invalid')

    def test_login_attempts(self):
        self.assertTrue(policy.login_attempts(2, CONFIG))
        self.assertFalse(policy.login_attempts(8, CONFIG))
        before = CONFIG['login_attempts']['value']
        changed = loaders.configure_login_attempts(CONFIG, 10)
        self.assertEqual(CONFIG['login_attempts']['value'], before)
        self.assertEqual(changed['login_attempts']['value'], 10)
        with self.assertRaises(ValueError):
            loaders.configure_login_attempts(CONFIG, 'invalid')

    def test_invite_hours(self):
        self.assertTrue(policy.invite_hours(12, CONFIG))
        self.assertFalse(policy.invite_hours(72, CONFIG))
        before = CONFIG['invite_hours']['value']
        changed = loaders.configure_invite_hours(CONFIG, 168)
        self.assertEqual(CONFIG['invite_hours']['value'], before)
        self.assertEqual(changed['invite_hours']['value'], 168)
        with self.assertRaises(ValueError):
            loaders.configure_invite_hours(CONFIG, 'invalid')

    def test_clock_skew_seconds(self):
        self.assertTrue(policy.clock_skew_seconds(10, CONFIG))
        self.assertFalse(policy.clock_skew_seconds(90, CONFIG))
        before = CONFIG['clock_skew_seconds']['value']
        changed = loaders.configure_clock_skew_seconds(CONFIG, 120)
        self.assertEqual(CONFIG['clock_skew_seconds']['value'], before)
        self.assertEqual(changed['clock_skew_seconds']['value'], 120)
        with self.assertRaises(ValueError):
            loaders.configure_clock_skew_seconds(CONFIG, 'invalid')

    def test_recovery_codes(self):
        self.assertTrue(policy.recovery_codes(3, CONFIG))
        self.assertFalse(policy.recovery_codes(0, CONFIG))
        before = CONFIG['recovery_codes']['value']
        changed = loaders.configure_recovery_codes(CONFIG, 4)
        self.assertEqual(CONFIG['recovery_codes']['value'], before)
        self.assertEqual(changed['recovery_codes']['value'], 4)
        with self.assertRaises(ValueError):
            loaders.configure_recovery_codes(CONFIG, 'invalid')


if __name__ == '__main__':
    unittest.main()
