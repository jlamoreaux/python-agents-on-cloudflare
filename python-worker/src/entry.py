from js import Object
from pyodide.ffi import to_js
from workers import Response, WorkerEntrypoint

from agent.core import BASE_TOOLS, run_agent


class Default(WorkerEntrypoint):
    async def chat(self, messages, tools):
        payload = to_js(
            {"messages": messages, "tools": tools, "max_tokens": 1024},
            dict_converter=Object.fromEntries,
        )
        result = await self.env.AI.run(self.env.MODEL, payload)
        return result.to_py() if hasattr(result, "to_py") else result

    async def fetch(self, request):
        if request.method != "POST":
            return Response.json({"ok": True})

        try:
            body = await request.json()
        except Exception:
            body = None
        message = str(body.get("message", "")).strip()[:1000] if isinstance(body, dict) else ""
        if not message:
            return Response.json({"error": "message is required"}, status=400)

        try:
            # Same loop as the container, but only the tools that work inside an isolate.
            result = await run_agent(message, self.chat, BASE_TOOLS)
        except Exception as error:
            return Response.json({"error": f"agent failed: {error!r}"[:300]}, status=502)

        return Response.json(
            {**result, "runtime": "python-worker", "tools": [t.name for t in BASE_TOOLS]}
        )
