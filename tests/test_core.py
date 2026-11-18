"""Run with: python3 -m unittest discover -s tests -t ."""

import asyncio
import json
import unittest

from agent.core import BASE_TOOLS, calculate, run_agent


def scripted(*replies):
    """A fake model that returns the given assistant messages in order."""
    queue = list(replies)

    async def chat(messages, tools):
        return {"choices": [{"message": queue.pop(0)}]}

    return chat


def tool_call(name, args, call_id="call_1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class CalculateTests(unittest.TestCase):
    def test_arithmetic(self):
        self.assertEqual(calculate("(12 + 3) * 4"), "60")

    def test_rejects_code(self):
        with self.assertRaises(ValueError):
            calculate("__import__('os').system('id')")

    def test_rejects_huge_exponent(self):
        with self.assertRaises(ValueError):
            calculate("9 ** 9999")


class CalculateLimitTests(unittest.TestCase):
    def test_chained_powers_are_rejected_quickly(self):
        with self.assertRaises(ValueError):
            calculate("((9**1000)**1000)**1000")

    def test_huge_multiplication_chain_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate("(" + "9" * 70 + ")*(" + "9" * 70 + ")*" + "9" * 70 + "*" + "9" * 70)

    def test_long_expression_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate("1+" * 150 + "1")

    def test_booleans_are_not_numbers(self):
        with self.assertRaises(ValueError):
            calculate("True + 1")

    def test_negative_and_float_powers_still_work(self):
        self.assertEqual(calculate("2 ** -1"), "0.5")
        self.assertEqual(calculate("2.5 ** 2"), "6.25")


class AgentLoopTests(unittest.TestCase):
    def test_malformed_tool_calls_do_not_crash_the_run(self):
        calls = [
            {"function": {"name": "calculate", "arguments": "[1, 2]"}},  # not an object, no id
            {"function": {"name": "calculate", "arguments": "{not json"}},
            {"function": {"name": "calculate", "arguments": {"expression": "2+2"}}},
        ]
        chat = scripted({"content": None, "tool_calls": calls}, {"content": "done"})
        result = asyncio.run(run_agent("x", chat, BASE_TOOLS))
        tool_results = [s["result"] for s in result["steps"] if s["kind"] == "tool"]
        self.assertTrue(tool_results[0].startswith("error:"))
        self.assertTrue(tool_results[1].startswith("error:"))
        self.assertEqual(tool_results[2], "4")
        self.assertEqual(result["reply"], "done")

    def test_stops_at_the_step_limit(self):
        call = tool_call("current_time", {})

        async def chat(messages, tools):
            return {"choices": [{"message": {"content": None, "tool_calls": [call]}}]}

        result = asyncio.run(run_agent("x", chat, BASE_TOOLS, max_steps=3))
        self.assertEqual(sum(1 for s in result["steps"] if s["kind"] == "model"), 3)
        self.assertIn("step limit", result["reply"])

    def test_long_tool_results_are_truncated(self):
        from agent.core import MAX_RESULT_CHARS, Tool

        big = Tool("big", "x", {"type": "object", "properties": {}}, lambda: "a" * (MAX_RESULT_CHARS * 2))
        chat = scripted({"content": None, "tool_calls": [tool_call("big", {})]}, {"content": "ok"})
        result = asyncio.run(run_agent("x", chat, [big]))
        self.assertEqual(len(result["steps"][1]["result"]), MAX_RESULT_CHARS)

    def test_answers_without_tools(self):
        chat = scripted({"content": "hi"})
        result = asyncio.run(run_agent("hello", chat, BASE_TOOLS))
        self.assertEqual(result["reply"], "hi")
        self.assertEqual([s["kind"] for s in result["steps"]], ["model"])

    def test_runs_a_tool_then_answers(self):
        chat = scripted(
            {"content": None, "tool_calls": [tool_call("calculate", {"expression": "6*7"})]},
            {"content": "42"},
        )
        result = asyncio.run(run_agent("6*7?", chat, BASE_TOOLS))
        self.assertEqual(result["reply"], "42")
        tool_step = result["steps"][1]
        self.assertEqual((tool_step["name"], tool_step["result"]), ("calculate", "42"))

    def test_unknown_tool_is_reported_not_raised(self):
        chat = scripted(
            {"content": None, "tool_calls": [tool_call("nope", {})]},
            {"content": "sorry"},
        )
        result = asyncio.run(run_agent("x", chat, BASE_TOOLS))
        self.assertEqual(result["steps"][1]["result"], "error: unknown tool")

    def test_dict_arguments_are_accepted(self):
        call = {"id": "c", "type": "function", "function": {"name": "calculate", "arguments": {"expression": "1+1"}}}
        chat = scripted({"content": None, "tool_calls": [call]}, {"content": "2"})
        result = asyncio.run(run_agent("x", chat, BASE_TOOLS))
        self.assertEqual(result["steps"][1]["result"], "2")


if __name__ == "__main__":
    unittest.main()
