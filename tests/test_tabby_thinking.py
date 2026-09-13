from __future__ import annotations

import json
import unittest

from st_proxy.llm.tabbyapi import adapt_chat_request


class TabbyThinkingTests(unittest.TestCase):
    def adapt(self, **payload):
        return json.loads(adapt_chat_request(json.dumps(payload).encode()))

    def unchanged(self, payload):
        raw = json.dumps(payload, indent=2).encode()
        self.assertEqual(adapt_chat_request(raw), raw)

    def test_levels_preserve_total_limit_and_other_fields(self):
        for effort, expected in (("minimal", 10), ("low", 25), ("medium", 50)):
            for key in ("max_tokens", "max_completion_tokens", "max_length"):
                with self.subTest(effort=effort, key=key):
                    payload = {
                        "reasoning_effort": effort, key: 101, "model": "synthetic",
                        "messages": [{"role": "user", "content": "å test"}],
                        "temperature": 0.71, "unknown": {"keep": True}, "stream": True,
                    }
                    self.assertEqual(self.adapt(**payload), {
                        **payload, "reasoning_budget_tokens": expected,
                    })
        self.assertEqual(self.adapt(reasoning_effort=" LOW ", max_tokens=3)[
            "reasoning_budget_tokens"], 0)

    def test_output_alias_priority_and_no_usable_limit(self):
        result = self.adapt(reasoning_effort="low", max_tokens=100,
                            max_completion_tokens=200, max_length=300)
        self.assertEqual(result["reasoning_budget_tokens"], 25)
        for value in (None, 0, -1, "invalid", 1.5):
            with self.subTest(value=value):
                self.unchanged({"reasoning_effort": "low", "max_tokens": value,
                                "max_completion_tokens": 200})
        self.unchanged({"reasoning_effort": "minimal"})
        self.assertEqual(self.adapt(reasoning_effort="medium", max_tokens="100")[
            "reasoning_budget_tokens"], 50)

    def test_high_unset_and_unknown_do_not_invent_budget(self):
        for effort in (None, "", "high", "unset", "unexpected"):
            self.unchanged({"reasoning_effort": effort, "max_tokens": 100})
        self.unchanged({"max_tokens": 100})

    def test_explicit_native_budget_wins_over_level_and_compatibility(self):
        for key in ("reasoning_budget_tokens", "reasoning_budget", "thinking_budget",
                    "thinking_token_budget"):
            for value in (0, 7, "7", "invalid"):
                with self.subTest(key=key, value=value):
                    self.unchanged({key: value, "thinking_budget_tokens": 8,
                                    "reasoning_effort": "low", "max_tokens": 100})
        self.unchanged({"reasoning": {"max_tokens": 9}, "thinking_budget_tokens": 8,
                        "reasoning_effort": "low", "max_tokens": 100})

    def test_native_null_and_negative_keep_tabby_fallback(self):
        for value in (None, -1, "-1"):
            self.unchanged({"reasoning_budget_tokens": value, "thinking_budget": 99,
                            "reasoning_effort": "low", "max_tokens": 100})
            self.unchanged({"reasoning": {"max_tokens": value},
                            "reasoning_effort": "low", "max_tokens": 100})
            self.unchanged({"reasoning_budget_tokens": value,
                            "reasoning": {"max_tokens": 17}, "thinking_budget_tokens": 8,
                            "reasoning_effort": "low", "max_tokens": 100})

    def test_compatibility_budget_beats_level_and_ignored_native_alias(self):
        for value in (0, 7, None, -1):
            payload = {"thinking_budget_tokens": value, "reasoning_effort": "low",
                       "max_tokens": 100}
            result = self.adapt(**payload)
            self.assertNotIn("thinking_budget_tokens", result)
            self.assertEqual(result["reasoning_budget_tokens"], value)
        result = self.adapt(reasoning_budget_tokens=None, thinking_budget=99,
                            thinking_budget_tokens=7, reasoning_effort="low", max_tokens=100)
        self.assertEqual(result["reasoning_budget_tokens"], 7)
        self.assertEqual(result["thinking_budget"], 99)

    def test_effort_uses_tabby_request_precedence(self):
        payload = {"reasoning": {"effort": "minimal"}, "max_tokens": 100}
        self.assertEqual(self.adapt(**payload)["reasoning_budget_tokens"], 10)
        payload["reasoning_effort"] = "low"
        self.assertEqual(self.adapt(**payload)["reasoning_budget_tokens"], 25)
        payload["chat_template_kwargs"] = {"reasoning_effort": "medium"}
        self.assertEqual(self.adapt(**payload)["reasoning_budget_tokens"], 50)
        payload["template_vars"] = {}
        self.assertEqual(self.adapt(**payload)["reasoning_budget_tokens"], 25)

    def test_none_disables_thinking_without_using_zero_budget(self):
        for effort in ("none", "off", " NONE "):
            result = self.adapt(reasoning_effort=effort, max_tokens=100)
            self.assertIs(result["enable_thinking"], False)
            self.assertNotIn("reasoning_budget_tokens", result)
        result = self.adapt(reasoning_effort="none", reasoning_budget_tokens=17)
        self.assertIs(result["enable_thinking"], False)
        self.assertEqual(result["reasoning_budget_tokens"], 17)

    def test_explicit_toggles_keep_native_priority(self):
        for controls in (
            {"enable_thinking": True}, {"enable_thinking": False},
            {"reasoning": {"enabled": True}},
            {"reasoning": {"enabled": False}, "enable_thinking": True},
            {"enable_thinking": True, "chat_template_kwargs": {"enable_thinking": False}},
            {"template_vars": {"enable_thinking": True},
             "chat_template_kwargs": {"enable_thinking": False}},
        ):
            self.unchanged({"reasoning_effort": "none", "max_tokens": 100, **controls})
        self.unchanged({"reasoning_effort": "low", "max_tokens": 100,
                        "chat_template_kwargs": {"enable_thinking": False}})

    def test_constraints_and_prompt_schema_are_preserved(self):
        for constraint in (
            {"response_format": {"type": "json_schema", "json_schema": {"type": "object"}}},
            {"json_schema": {"type": "object"}}, {"regex_pattern": "[a-z]+"},
            {"grammar_string": 'root ::= "ok"'},
            {"messages": [{"role": "user", "content": 'Return {"type":"object"}'}]},
        ):
            payload = {"reasoning_effort": "low", "max_tokens": 100, **constraint}
            self.assertEqual(self.adapt(**payload), {**payload, "reasoning_budget_tokens": 25})

    def test_invalid_json_and_shapes_remain_backend_responsibility(self):
        for raw in (b"not json", b"[]", b"null", b"\xff", b'{"reasoning": "invalid"}',
                    b'{"template_vars": [], "reasoning_effort":"low","max_tokens":100}'):
            self.assertEqual(adapt_chat_request(raw), raw)
