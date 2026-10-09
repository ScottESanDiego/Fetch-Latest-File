"""Assertion helpers only; never replace Home Assistant modules."""

import unittest


class Assertions:
    """Retain readable assertions while fixtures supply real HA instances."""

    longMessage = True
    failureException = AssertionError
    _formatMessage = unittest.TestCase._formatMessage
    assertEqual = unittest.TestCase.assertEqual
    assertNotEqual = unittest.TestCase.assertNotEqual
    assertTrue = unittest.TestCase.assertTrue
    assertFalse = unittest.TestCase.assertFalse
    assertLess = unittest.TestCase.assertLess
    assertLessEqual = unittest.TestCase.assertLessEqual
    assertGreaterEqual = unittest.TestCase.assertGreaterEqual
    assertIn = unittest.TestCase.assertIn
    assertNotIn = unittest.TestCase.assertNotIn
    assertRaises = unittest.TestCase.assertRaises
    assertLogs = unittest.TestCase.assertLogs
    fail = unittest.TestCase.fail

    def _getAssertEqualityFunc(self, first, second):
        return self._baseAssertEqual

    _baseAssertEqual = unittest.TestCase._baseAssertEqual
