"""Independent regressions from real Desktop protocol review."""
import json
import os
import subprocess
import unittest

try:
    from . import test_desktop_backend as fixtures
except ImportError:
    import test_desktop_backend as fixtures


@unittest.skipUnless(fixtures.DESKTOP_AVAILABLE, 'Desktop Electron runtime required')
class TestDesktopReview(fixtures.Base):
    def test_unverified_or_queued_session_never_receives_prompt(self):
        for scenario in ('list_fail', 'missing_row', 'wrong_workspace', 'queued', 'snapshot_queued'):
            with self.subTest(scenario=scenario):
                host, code, result, _ = self.start(scenario=scenario, timeout=5)
                self.assertEqual(code, 1, result)
                self.assertEqual(host.calls('session/prompt'), [])
                self.assertEqual(host.calls('session/cancel'), [])

    def test_foreign_message_during_our_turn_fails_without_cancelling_user(self):
        host, code, result, _ = self.start(scenario='mixed_user', timeout=5)
        self.assertEqual(code, 1, result)
        self.assertIn('another user', result['error'])
        self.assertEqual(host.calls('session/cancel'), [])

    def test_incomplete_turn_and_terminated_carrier_are_not_success(self):
        for scenario in ('max_tokens', 'ended_stream', 'close_before_snapshot', 'end_before_snapshot'):
            with self.subTest(scenario=scenario):
                _, code, result, _ = self.start(scenario=scenario, timeout=5)
                self.assertEqual(code, 1, result)
                self.assertNotIn('timeout', result['error'].lower())
                self.assertEqual(result['status'], 'dsh_failed')
                # No `final` event was seen, so the reply is only raw text and
                # the turn is not classified as finished.
                self.assertFalse(result['classification']['finished'])
                events = [row['event'] for row in self.bridge.events(result['task_id'])
                          if isinstance(row.get('event'), dict)]
                self.assertFalse(any(event.get('type') == 'final' for event in events))

    def test_an_orderly_stream_end_after_a_completed_turn_is_success(self):
        # The Host may end the logical follow stream right after our turn/end.
        # A turn that reported `completed` with text has finished: the orderly
        # end must not be rewritten into a transport failure, and no
        # session/cancel may be needed.
        host, code, result, _ = self.start('mine', scenario='end_after_turn', timeout=10)
        self.assertEqual(code, 0, result)
        self.assertEqual(result['status'], 'awaiting_review')
        self.assertIn('DSH_RESULT: finished the fake turn', result['reply'])
        events = [row['event'] for row in self.bridge.events(result['task_id'])
                  if isinstance(row.get('event'), dict)]
        self.assertTrue(any(event.get('type') == 'turn_end' for event in events))
        self.assertTrue(any(event.get('type') == 'final' for event in events))
        self.assertFalse(any(event.get('type') == 'error' for event in events))
        self.assertEqual(host.calls('session/cancel'), [])

    def test_repeated_clean_turn_completion_never_reports_a_closed_stream(self):
        # Regression for the intermittent failure where the stream closed
        # between our completed turn/end and the final event: the old guard
        # turned a finished turn into `the Desktop follow stream closed before
        # the turn ended`. Repeat the real transport so the race stays covered
        # deterministically rather than masking it with a sleep.
        for attempt in range(12):
            with self.subTest(attempt=attempt):
                _, code, result, _ = self.start('mine', timeout=10)
                self.assertEqual(code, 0, result)
                self.assertIn('DSH_RESULT: finished the fake turn', result['reply'])

    def test_a_confirmed_cancel_is_reported_as_stopped(self):
        # Cancellation is only "stopped" when the Host ends our turn. Here the
        # Host does end it after session/cancel, so the report may say stopped
        # (unlike test_cancellation_ack_is_not_stop_confirmation).
        host, code, result, _ = self.start('mine', scenario='cancel_confirms_stop', timeout=1)
        self.assertEqual(code, 1, result)
        self.assertIn('confirmed', result['error'].lower())
        events = [row['event'] for row in self.bridge.events(result['task_id'])
                  if isinstance(row.get('event'), dict)]
        cancel = next(event for event in events if event.get('type') == 'cancel')
        self.assertTrue(cancel['stopped'])
        self.assertEqual(len(host.calls('session/cancel')), 1)

    def test_cancellation_ack_is_not_stop_confirmation(self):
        host, code, result, _ = self.start(scenario='never_end', timeout=1)
        self.assertEqual(code, 1, result)
        self.assertIn('may still be', result['error'])
        events = self.bridge.events(result['task_id'])
        cancel = next(row['event'] for row in events if row.get('event', {}).get('type') == 'cancel')
        self.assertTrue(cancel['ok'])
        self.assertFalse(cancel['stopped'])
        self.assertEqual(len(host.calls('session/cancel')), 1)

    def test_tool_details_do_not_leak_credentials_or_reasoning(self):
        _, code, result, _ = self.start(scenario='credential_output', timeout=5)
        self.assertEqual(code, 0, result)
        blob = self.bridge.conversation(result['task_id']) + json.dumps(self.bridge.events(result['task_id']))
        for secret in ('SECRET-AUTH-SENTINEL', 'PRIVATE-REASONING-SENTINEL', 'SECRET-TOOL-OUTPUT-SENTINEL'):
            self.assertNotIn(secret, blob)

    def test_bad_descriptor_never_echoes_raw_authentication(self):
        self.bridge.endpoint_file.parent.mkdir(parents=True)
        self.bridge.endpoint_file.write_text('{"authenticatedUrl": "SECRET-MALFORMED-DESCRIPTOR', encoding='utf-8')
        result = subprocess.run(
            [str(fixtures.ELECTRON), str(fixtures.HELPER), str(self.bridge.endpoint_file), 'doctor'],
            env={**os.environ, 'ELECTRON_RUN_AS_NODE': '1'}, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(b'SECRET-MALFORMED-DESCRIPTOR', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
