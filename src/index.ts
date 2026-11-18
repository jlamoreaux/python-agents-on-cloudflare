import { DurableObject, WorkerEntrypoint } from "cloudflare:workers";

const PORT = 8080;
const INACTIVITY_TIMEOUT_MS = 2 * 60 * 1000;
const READY_DEADLINE_MS = 90 * 1000;
const MAX_MESSAGE_CHARS = 1000;

// Limits on what code inside the container may send to the model.
const MAX_MODEL_MESSAGES = 50;
const MAX_MODEL_TOOLS = 16;
const MAX_MODEL_BODY_BYTES = 200 * 1024;
const MAX_MODEL_OUTPUT_TOKENS = 1024;

/**
 * Stands in for the Internet inside the container. The container calls
 * http://ai.internal/v1/chat/completions, the platform routes that here, and we
 * forward it to Workers AI. The model is pinned here, so the container cannot
 * pick another one. Everything in the container, including code the model
 * writes, can reach this endpoint, so it validates and bounds what it forwards.
 */
export class ModelGateway extends WorkerEntrypoint<Env> {
	async fetch(request: Request): Promise<Response> {
		const url = new URL(request.url);
		if (request.method !== "POST" || url.pathname !== "/v1/chat/completions") {
			return Response.json({ error: "Not found" }, { status: 404 });
		}

		const text = await request.text();
		if (text.length > MAX_MODEL_BODY_BYTES) {
			return Response.json({ error: "Request too large" }, { status: 413 });
		}

		let body: { messages?: unknown; tools?: unknown };
		try {
			body = JSON.parse(text);
		} catch {
			return Response.json({ error: "Invalid JSON" }, { status: 400 });
		}
		const { messages, tools } = body;
		if (!Array.isArray(messages) || messages.length === 0 || messages.length > MAX_MODEL_MESSAGES) {
			return Response.json({ error: `messages must be 1-${MAX_MODEL_MESSAGES} items` }, { status: 400 });
		}
		if (tools !== undefined && (!Array.isArray(tools) || tools.length > MAX_MODEL_TOOLS)) {
			return Response.json({ error: `tools must be at most ${MAX_MODEL_TOOLS} items` }, { status: 400 });
		}

		try {
			const result = await this.env.AI.run(this.env.MODEL as keyof AiModels, {
				messages,
				tools,
				max_tokens: MAX_MODEL_OUTPUT_TOKENS,
			} as never);
			return Response.json(result);
		} catch (error) {
			console.error("Workers AI call failed", error);
			return Response.json({ error: String(error) }, { status: 502 });
		}
	}
}

export class AgentContainer extends DurableObject<Env> {
	// True once the container has answered a health check. Reset when it exits.
	private ready = false;
	private starting: Promise<boolean> | undefined;
	private monitoring = false;

	constructor(ctx: DurableObjectState, env: Env) {
		super(ctx, env);
		const container = ctx.container;
		if (container?.running) {
			// The Durable Object restarted (for example after a deploy) while the container kept running.
			void ctx.blockConcurrencyWhile(() => container.setInactivityTimeout(INACTIVITY_TIMEOUT_MS));
			this.observeExit();
		}
	}

	async fetch(request: Request): Promise<Response> {
		const coldStartMs = await this.ensureReady();

		const url = new URL(request.url);
		url.protocol = "http:";
		url.host = "container";
		const response = await this.ctx.container!.getTcpPort(PORT).fetch(new Request(url, request));

		const headers = new Headers(response.headers);
		headers.set("x-cold-start-ms", String(coldStartMs));
		return new Response(response.body, { status: response.status, headers });
	}

	/** Returns how long this request waited for a container it started, or 0 if it did not start one. */
	private async ensureReady(): Promise<number> {
		if (this.ready && this.ctx.container!.running) return 0;
		this.ready = false;

		const startedAt = Date.now();
		const leader = !this.starting;
		this.starting ??= this.startAndWaitForPort().finally(() => {
			this.starting = undefined;
		});
		const startedContainer = await this.starting;
		return leader && startedContainer ? Date.now() - startedAt : 0;
	}

	private async startAndWaitForPort(): Promise<boolean> {
		const container = this.ctx.container!;

		// Safe to repeat: registering a target again just replaces its handler. Doing it
		// whenever we (re)check readiness also covers a Durable Object that restarted
		// while the container was running.
		await container.interceptOutboundHttp("ai.internal", this.ctx.exports.ModelGateway({ props: {} }));

		let started = false;
		if (!container.running) {
			container.start({
				image: container.images.base,
				instance: "lite",
				enableInternet: false,
			});
			started = true;
		}
		this.observeExit();
		await container.setInactivityTimeout(INACTIVITY_TIMEOUT_MS);

		// running means "start requested", not "server is listening".
		const port = container.getTcpPort(PORT);
		const deadline = Date.now() + READY_DEADLINE_MS;
		let lastError: unknown;
		while (Date.now() < deadline) {
			if (!container.running) {
				throw new Error(`Container exited before it became ready${lastError ? `: ${lastError}` : ""}`);
			}
			try {
				const response = await port.fetch("http://container/health", {
					signal: AbortSignal.timeout(1000),
				});
				await response.body?.cancel();
				if (response.ok) {
					this.ready = true;
					return started;
				}
			} catch (error) {
				lastError = error;
			}
			await scheduler.wait(200);
		}
		throw new Error(`Container did not become ready within ${READY_DEADLINE_MS / 1000}s: ${lastError}`);
	}

	/** Log when the container exits and make the next request check readiness again. */
	private observeExit(): void {
		if (this.monitoring) return;
		this.monitoring = true;
		this.ctx.waitUntil(
			this.ctx
				.container!.monitor()
				.then(() => console.log("Container exited"))
				.catch((error: unknown) => console.error("Container failed:", error))
				.finally(() => {
					this.monitoring = false;
					this.ready = false;
				}),
		);
	}
}

export default {
	async fetch(request: Request, env: Env): Promise<Response> {
		const match = new URL(request.url).pathname.match(/^\/api\/run\/(container|worker)$/);
		if (!match || request.method !== "POST") {
			return Response.json({ error: "Not found" }, { status: 404 });
		}
		if (!request.headers.get("content-type")?.includes("application/json")) {
			return Response.json({ error: "Content-Type must be application/json" }, { status: 415 });
		}

		const body = (await request.json().catch(() => null)) as { message?: unknown } | null;
		const message = typeof body?.message === "string" ? body.message.trim() : "";
		if (!message || message.length > MAX_MESSAGE_CHARS) {
			return Response.json({ error: `message must be 1-${MAX_MESSAGE_CHARS} characters` }, { status: 400 });
		}

		const init = {
			method: "POST",
			headers: { "content-type": "application/json" },
			body: JSON.stringify({ message }),
		};
		const startedAt = Date.now();

		try {
			const response =
				match[1] === "container"
					? await env.AGENT_CONTAINER.getByName("agent").fetch("http://container/run", init)
					: await env.PYTHON_AGENT.fetch("http://python-worker/run", init);

			if (!response.ok) {
				return Response.json(
					{ error: `${match[1]} returned ${response.status}: ${(await response.text()).slice(0, 300)}` },
					{ status: 502 },
				);
			}
			const data = (await response.json()) as Record<string, unknown>;
			return Response.json({
				...data,
				total_ms: Date.now() - startedAt,
				cold_start_ms: Number(response.headers.get("x-cold-start-ms") ?? 0),
			});
		} catch (error) {
			console.error(`${match[1]} request failed`, error);
			return Response.json({ error: String(error) }, { status: 502 });
		}
	},
} satisfies ExportedHandler<Env>;
