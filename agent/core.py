"""A tiny tool-calling agent loop.

No network calls and no Cloudflare imports live here. The runtime that hosts the
agent (a container or a Python Worker) injects two things:

  chat(messages, tools) -> dict   an OpenAI-style chat completion response
  tools                           a list of Tool objects the model may call

That keeps the loop identical in both runtimes, so any difference you measure
comes from the runtime, not from the agent.
"""

from __future__ import annotations

import ast
import inspect
import json
import operator
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

SYSTEM_PROMPT = (
    "You are a concise assistant. Use a tool whenever it gives a more reliable "
    "answer than guessing. If you need a tool you do not have, say so plainly."
)
MAX_STEPS = 6
MAX_RESULT_CHARS = 4000


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Any]

    def spec(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


_BINARY = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
MAX_EXPRESSION_CHARS = 200
MAX_INT_BITS = 4096


def _check_size(value: float) -> float:
    if isinstance(value, int) and value.bit_length() > MAX_INT_BITS:
        raise ValueError("result too large")
    return value


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and _is_number(node.value):
        return _check_size(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow):
            # Check the size before computing: 9 ** 999999 would otherwise run for ages.
            if isinstance(left, int) and isinstance(right, int) and right > 0:
                if right * max(left.bit_length(), 1) > MAX_INT_BITS:
                    raise ValueError("result too large")
            elif abs(right) > 1000:
                raise ValueError("exponent too large")
        return _check_size(_BINARY[type(node.op)](left, right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand))
    raise ValueError("only numbers and + - * / // % ** are allowed")


def calculate(expression: str) -> str:
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise ValueError("expression too long")
    return str(_eval(ast.parse(expression.strip(), mode="eval").body))


def current_time() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


BASE_TOOLS = [
    Tool(
        name="calculate",
        description="Evaluate an arithmetic expression such as '(12 + 3) * 4'.",
        parameters={
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
        fn=calculate,
    ),
    Tool(
        name="current_time",
        description="Get the current date and time in UTC.",
        parameters={"type": "object", "properties": {}},
        fn=current_time,
    ),
]


def _ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


def _parse_args(raw: Any) -> dict:
    args = json.loads(raw) if isinstance(raw, str) and raw.strip() else raw
    if not args:
        return {}
    if not isinstance(args, dict):
        raise ValueError("arguments must be a JSON object")
    return dict(args)


async def _call_tool(tool: Tool | None, args: dict) -> str:
    if tool is None:
        return "error: unknown tool"
    try:
        result = tool.fn(**args)
        if inspect.isawaitable(result):
            result = await result
        return str(result)[:MAX_RESULT_CHARS]
    except Exception as exc:
        return f"error: {exc}"


async def run_agent(message: str, chat, tools: list[Tool], max_steps: int = MAX_STEPS) -> dict:
    """Loop: ask the model, run any tools it requests, repeat until it answers."""
    by_name = {t.name: t for t in tools}
    specs = [t.spec() for t in tools]
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": message},
    ]
    steps: list[dict] = []
    reply = ""

    for _ in range(max_steps):
        started = time.perf_counter()
        response = await chat(messages, specs)
        reply_message = response["choices"][0]["message"]
        calls = reply_message.get("tool_calls") or []
        steps.append({"kind": "model", "ms": _ms(started), "tool_calls": len(calls)})

        reply = reply_message.get("content") or ""
        if not calls:
            break

        calls = [{**call, "id": call.get("id") or f"call_{i}"} for i, call in enumerate(calls)]
        messages.append({"role": "assistant", "content": reply, "tool_calls": calls})
        for call in calls:
            started = time.perf_counter()
            function = call.get("function") or {}
            name = function.get("name", "")
            try:
                args = _parse_args(function.get("arguments"))
                result = await _call_tool(by_name.get(name), args)
            except ValueError as exc:
                args, result = {}, f"error: {exc}"
            steps.append(
                {"kind": "tool", "name": name, "args": args, "result": result, "ms": _ms(started)}
            )
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    else:
        reply = reply or "Stopped after reaching the step limit."

    return {"reply": reply, "steps": steps}
