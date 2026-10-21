#!/usr/bin/env python3
"""
Tests for the farewell detection.

Run:  .venv/bin/python tests/test_farewell.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from farewell import farewell_response, is_farewell  # noqa: E402


class FarewellTests(unittest.TestCase):
    def test_common_goodbyes(self):
        for text in [
            "have a nice day",
            "Have a great day!",
            "have a good day",
            "have a wonderful day",
            "have a nice one",
            "goodbye",
            "Good bye",
            "bye",
            "bye bye",
            "see you later",
            "see you",
            "that's all",
            "thats all",
            "that's it",
            "I'm done",
            "I am finished",
            "nothing else",
            "thanks for your help",
            "thank you for your help",
            "talk to you later",
            "ok thanks bye",
            "thanks, goodbye",
            "alright, have a nice day",
        ]:
            with self.subTest(text=text):
                self.assertTrue(is_farewell(text), f"not detected: {text!r}")

    def test_turkish_goodbyes(self):
        for text in [
            "hoşçakal",
            "hoscakal",
            "görüşürüz",
            "güle güle",
            "iyi günler",
            "başka bir şey yok",
        ]:
            with self.subTest(text=text):
                self.assertTrue(is_farewell(text), f"not detected: {text!r}")

    def test_order_questions_are_not_goodbyes(self):
        for text in [
            "Where is my order 1001?",
            "Check order 1,001",
            "My order number is 1,000 and 1.",
            "How do I return a product?",
            "I want to return the AirPods Pro",
            "My name is Sammy. Nice to meet you.",
        ]:
            with self.subTest(text=text):
                self.assertFalse(is_farewell(text), f"false farewell: {text!r}")

    def test_questions_ending_with_a_goodbye_phrase(self):
        for text in [
            "What happens when I say goodbye?",
            "how do I say goodbye",
            "Can I have a nice day?",
        ]:
            with self.subTest(text=text):
                self.assertFalse(is_farewell(text), f"false farewell: {text!r}")

    def test_the_phrase_has_to_be_at_the_end(self):
        for text in [
            "I said goodbye to him yesterday",
            "she told me to have a nice day at work",
            "we are going to say goodbye tomorrow",
        ]:
            with self.subTest(text=text):
                self.assertFalse(is_farewell(text), f"false farewell: {text!r}")

    def test_a_bare_thank_you_is_not_a_goodbye(self):
        # "Thank you" can be the start of a question.
        self.assertFalse(is_farewell("thank you"))
        self.assertFalse(is_farewell("thanks"))

    def test_long_utterances_are_not_goodbyes(self):
        long_text = (
            "I would like to tell you that the order I placed last week with the "
            "tracking number was supposed to arrive before the holiday"
        )
        self.assertFalse(is_farewell(long_text))

    def test_empty_input(self):
        self.assertFalse(is_farewell(""))
        self.assertFalse(is_farewell("   "))

    def test_closing_line(self):
        self.assertIn("DemoShop", farewell_response())
        self.assertEqual(farewell_response("Bye now!"), "Bye now!")


if __name__ == "__main__":
    unittest.main(verbosity=2)
