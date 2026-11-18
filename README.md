# Python agents on Cloudflare: container vs Python Worker

One small tool-calling agent, written in Python, running in two places so you can compare them side by side:

- **Container**: a FastAPI app in a Docker image, started and managed by a Durable Object. It is a full Linux environment, so it can run subprocesses, install any pip package and use the filesystem.
- **Python Worker**: the same agent loop running in a Workers isolate. No container to boot, but no subprocesses either.

Both call the same model on Workers AI. Ask both the same question and compare the answers, the tool traces and the timings.

```
                         ┌───────────────────────── gateway Worker (TypeScript) ─────────────────────────┐
 browser ── /api/run/* ─▶│                                                                                │
                         │  /api/run/worker ──▶ Python Worker ──── env.AI ───────────────┐                │
                         │                      (agent/core.py)                           ▼                │
                         │  /api/run/container ─▶ AgentContainer (Durable Object)     Workers AI           │
                         │                          │  getTcpPort(8080)                   ▲                │
                         │                          ▼                                     │                │
                         │                   Python container (FastAPI + agent/core.py)   │                │
                         │                          │ http://ai.internal  ── intercepted ─┘ ModelGateway   │
                         └────────────────────────────────────────────────────────────────────────────────┘
```

## What to look at

| File | What it shows |
| --- | --- |
| `agent/core.py` | The agent loop. No network or Cloudflare imports, so it is identical in both runtimes. |
| `container/server.py` | Wraps the loop in FastAPI. Model calls go to `http://ai.internal`. |
| `container/tools.py` | `run_python`: runs model-written code in a subprocess. Only possible in a container. |
| `container/Dockerfile`, `container/requirements.txt` | Add any pip package or system dependency here. |
| `src/index.ts` | The Durable Object that starts the container, waits for it to be ready and proxies requests. Also `ModelGateway`, which gives the container model access. |
| `python-worker/src/entry.py` | The Python Worker. Same loop, calls `env.AI` directly, fewer tools. |
| `wrangler.jsonc` | Container, Durable Object, service binding and assets config. |

### No API keys in the container

The container starts with `enableInternet: false`. Its only way out is `http://ai.internal`, which the Durable Object registers with `interceptOutboundHttp()`. That request is handed to the `ModelGateway` entrypoint in the gateway Worker, which calls Workers AI through the `AI` binding. The model is pinned in the Worker, so code inside the container cannot pick a different one. See [Handle outbound traffic](https://developers.cloudflare.com/containers/configuration/outbound-traffic/).

## Container or Python Worker?

| | Container | Python Worker |
| --- | --- | --- |
| Start | Boots on first request (seconds), sleeps after 2 idle minutes | Much faster, no image to boot |
| Run model-written code | Yes, in a subprocess (`run_python`) | No |
| Packages | Anything pip or apt can install | [Supported packages](https://developers.cloudflare.com/workers/languages/python/packages/) only |
| Filesystem, processes, native binaries | Yes | No |
| Model access | Through an intercepted hostname, no keys inside | `env.AI` binding |
| Good fit | Agents that execute code, use native libraries or need a long-lived process | Lightweight request/response agents and glue logic |

The demo's tool list makes the difference concrete: the container has `calculate`, `current_time` and `run_python`; the Worker has the first two.

## Run it

You need Node 20+, [uv](https://docs.astral.sh/uv/), Docker running locally and a Cloudflare account with Workers AI.

```bash
npx wrangler login
npm install
npm run types                                  # generate Env types (also run by `npm run check`)
(cd python-worker && uv run pywrangler sync)   # vendor the Python Workers SDK
npm run dev                                    # gateway + container + Python Worker on :8787
```

Open <http://localhost:8787> and try the example prompts. The first container request builds and boots the image, so it takes a few seconds.

Workers AI calls always use your real account, even in local dev, and are billed to it. If you have several accounts, set `CLOUDFLARE_ACCOUNT_ID`.

Run the agent-loop tests, which need no network or Cloudflare account:

```bash
python3 -m unittest discover -s tests -t .
```

## Deploy

```bash
npm run deploy
```

This deploys the Python Worker first (the gateway has a service binding to it), then the gateway and container. Docker must be running, since Wrangler builds and pushes the image. After the first deploy, wait a few minutes for the container to provision.

The Python Worker is configured with `workers_dev: false` and `preview_urls: false`: it is only reachable through the gateway's service binding, so it has no public URL.

The gateway has no authentication of its own, and every request it handles uses Workers AI. Put it behind [Cloudflare Access](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/) before sharing the URL. Until you do, its `workers.dev` URL is open to anyone who finds it.

## Security notes

This is a demo, so these are limits to know about, not guarantees.

- **`run_python` runs model-written code.** The container is the sandbox. The tool adds a 10 s timeout, a 256 MB memory limit, a CPU limit, an output cap, at most 2 concurrent runs, a fresh working directory per run, and a non-root user with a read-only `/app`. That stops a runaway script from taking down the agent server. It is not a boundary against a determined attacker: [processes in a deployed container may keep root-level capabilities even as a non-root user](https://developers.cloudflare.com/containers/api/durable-object-container/#exec), so treat anything inside the container as untrusted-code-adjacent and never put secrets in it.
- **Code in the container can call the model.** `http://ai.internal` is the container's only way out, and that includes code the model writes. The gateway pins the model, accepts only `POST /v1/chat/completions`, and caps request size, message count, tool count and output tokens. It does not cap the number of calls, so a loop in `run_python` could spend your Workers AI quota. Add a per-container budget if that matters to you.
- **One container serves everyone.** The gateway uses a single instance (`getByName("agent")`), so one user's `run_python` could leave files in `/tmp` or affect the next user. Route by session or user id to give each their own container.
- **The calculator is bounded.** It only evaluates arithmetic, rejects results over 4096 bits and expressions over 200 characters, and has tests for the chained-power case.

## Make it your own

- **Different tools**: add a `Tool` in `agent/core.py` (both runtimes) or `container/tools.py` (container only).
- **More Python packages in the container**: add them to `container/requirements.txt`. Swap the hand-rolled loop in `agent/core.py` for the framework you prefer; the container only needs to answer `GET /health` and `POST /run` on port 8080.
- **Different model**: change `MODEL` in `wrangler.jsonc` and `python-worker/wrangler.jsonc`. It must support function calling.
- **More containers**: see the note under Security about the single shared instance.

## Notes

- `python-worker/src/agent/` is a generated copy of `agent/`, made by `npm run sync` (run automatically by `dev` and `deploy`). Edit `agent/`, not the copy.
- Built with the [Durable Object Container API](https://developers.cloudflare.com/containers/api/durable-object-container/), [Python Workers](https://developers.cloudflare.com/workers/languages/python/) and [Workers AI](https://developers.cloudflare.com/workers-ai/).
