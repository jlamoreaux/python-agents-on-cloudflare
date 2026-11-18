const EXAMPLES = [
  "What is (1234 * 5678) / 9?",
  "What is the current time in UTC?",
  "Write and run Python that prints the sum of the primes below 10000.",
  "Run Python to generate 5 random integers from 1 to 100 and report their mean.",
];

const RUNTIMES = ["container", "worker"];
const $ = (id) => document.getElementById(id);
const message = $("message");
const last = { container: null, worker: null };
let busy = false;

function esc(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

const fmt = (ms) => (ms >= 1000 ? (ms / 1000).toFixed(2) + " s" : Math.round(ms) + " ms");

function setBusy(value) {
  busy = value;
  document.querySelectorAll("form button, .chips button").forEach((b) => (b.disabled = value));
}

function setPending(runtime) {
  $("t-" + runtime).className = "tag quiet";
  $("t-" + runtime).textContent = "running";
  $("r-" + runtime).querySelector(".panel-body").innerHTML = '<div class="pending-bar"><div></div></div>';
}

function renderResult(runtime, data) {
  const body = $("r-" + runtime).querySelector(".panel-body");
  const tag = $("t-" + runtime);

  if (data.error || !Array.isArray(data.steps) || !Array.isArray(data.tools)) {
    data = { error: data.error || "Unexpected response from the server" };
    tag.className = "tag err";
    tag.textContent = "error";
    body.innerHTML = '<p class="err-text">' + esc(data.error) + "</p>";
    return;
  }

  tag.className = "tag ok";
  tag.textContent = fmt(data.total_ms);

  const facts = [
    ["runtime", data.runtime],
    data.python ? ["python", data.python] : null,
    ["tools", data.tools.join(", ")],
    data.uptime_s != null ? ["uptime", data.uptime_s + " s"] : null,
  ].filter(Boolean);

  const rows = data.steps
    .map((step) => {
      if (step.kind === "model") {
        const calls = step.tool_calls ? ", " + step.tool_calls + " tool call" + (step.tool_calls > 1 ? "s" : "") : "";
        return '<li><span class="name">model</span><span>' + esc(calls.slice(2) || "answer") + '</span><span class="ms">' + fmt(step.ms) + "</span></li>";
      }
      const args = Object.entries(step.args).map(([k, v]) => (k === "code" ? "code" : k + "=" + JSON.stringify(v))).join(", ");
      const code = step.args.code ? "<pre>" + esc(step.args.code) + "</pre>" : "";
      return (
        '<li><span class="name">' + esc(step.name) + "</span><span>" + esc(args) + '</span><span class="ms">' + fmt(step.ms) + "</span>" +
        code + "<pre>" + esc(step.result) + "</pre></li>"
      );
    })
    .join("");

  body.innerHTML =
    '<p class="reply">' + esc(data.reply || "(empty reply)") + "</p>" +
    '<div class="facts">' + facts.map(([k, v]) => k + " <b>" + esc(v) + "</b>").join("") + "</div>" +
    '<ol class="trace">' + rows + "</ol>";
}

function renderSummary() {
  const c = last.container;
  const w = last.worker;
  $("s-container").textContent = c && !c.error ? fmt(c.total_ms) : "\u2013";
  $("s-worker").textContent = w && !w.error ? fmt(w.total_ms) : "\u2013";
  $("s-cold").textContent = c && !c.error ? fmt(c.cold_start_ms) : "\u2013";
  $("s-tools").textContent = c && w && !c.error && !w.error ? c.tools.length + " / " + w.tools.length : "\u2013";
  $("scope").textContent = "last run: " + new Date().toLocaleTimeString();
}

async function call(runtime, text) {
  try {
    const response = await fetch("/api/run/" + runtime, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    const data = await response.json();
    const valid = Array.isArray(data.steps) && Array.isArray(data.tools);
    return data.error || valid ? data : { error: "Unexpected response from the server" };
  } catch (error) {
    return { error: String(error) };
  }
}

async function run(runtimes) {
  const text = message.value.trim();
  if (busy) return;
  if (!text) {
    message.focus();
    return;
  }
  setBusy(true);
  try {
    runtimes.forEach(setPending);
    await Promise.all(
      runtimes.map(async (runtime) => {
        last[runtime] = await call(runtime, text);
        renderResult(runtime, last[runtime]);
      }),
    );
    renderSummary();
  } finally {
    setBusy(false);
  }
}

$("form").addEventListener("submit", (event) => {
  event.preventDefault();
  run(RUNTIMES);
});
$("only-container").addEventListener("click", () => run(["container"]));
$("only-worker").addEventListener("click", () => run(["worker"]));

message.addEventListener("input", () => {
  $("count").textContent = message.value.length + " / 1000";
});
message.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) run(RUNTIMES);
});

$("chips").innerHTML = EXAMPLES.map((text, i) => '<button type="button" data-i="' + i + '">' + esc(text) + "</button>").join("");
$("chips").addEventListener("click", (event) => {
  const chip = event.target.closest("button");
  if (!chip) return;
  message.value = EXAMPLES[Number(chip.dataset.i)];
  message.dispatchEvent(new Event("input"));
  message.focus();
});
