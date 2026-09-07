import { execFile } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";

export const name = "workflow-checkpoint-dsh";
export const inject = ["tools"];

const SOURCE = { kind: "plugin", plugin: "workflow-checkpoint-dsh" };

function userMessage(text) {
  return { id: randomUUID(), role: "user", content: [{ type: "text", text }], source: SOURCE };
}
const CHECKPOINT_PY = join(homedir(), ".cc-switch", "skills", "workflow-checkpoint", "scripts", "checkpoint.py");

function run(file, args, cwd) {
  return new Promise((resolve, reject) => {
    execFile(file, args, { timeout: 8000, maxBuffer: 1024 * 1024, windowsHide: true, cwd }, (error, stdout) => {
      if (error) reject(error);
      else resolve((stdout || "").trim());
    });
  });
}

function extractContext(raw) {
  if (!raw) return "";
  try {
    const parsed = JSON.parse(raw);
    const ac = parsed?.hookSpecificOutput?.additionalContext;
    if (typeof ac === "string") return ac.trim();
  } catch {}
  return raw.trim();
}

// report / ledger aggregate all scopes (they read every scope's ledger.jsonl
// under ~/.cc-switch/workflows), so they do NOT depend on the session cwd.
function runCheckpoint(args) {
  return run("python", [CHECKPOINT_PY, ...args], process.cwd());
}

function textBlock(text) {
  return [{ type: "text", text }];
}

const TOOLS = [
  {
    name: "checkpoint_report",
    description:
      "Generate an evidence report from the workflow-checkpoint evidence ledger (aggregates all scopes), sliced by done_at date range and/or theme. Use when writing progress reports, mid-term reviews, 转正答辩, or 述职 documents — returns evidence-anchored entries (headline + change + decision + evidence).",
    parameters: {
      type: "object",
      properties: {
        since: { type: "string", description: "Start date YYYY-MM-DD (inclusive). Omit for no lower bound." },
        until: { type: "string", description: "End date YYYY-MM-DD (inclusive). Omit for no upper bound." },
        theme: { type: "string", description: "Filter/group by theme tag. Omit for all themes." },
      },
    },
    output: {
      schema: { type: "string" },
      render(args, value) {
        return textBlock(String(value));
      },
    },
    async execute(args, exec) {
      const argv = ["report"];
      if (args?.since) argv.push("--since", args.since);
      if (args?.until) argv.push("--until", args.until);
      if (args?.theme) argv.push("--theme", args.theme);
      try {
        return await runCheckpoint(argv);
      } catch (err) {
        return "checkpoint_report failed: " + (err && err.message ? err.message : String(err));
      }
    },
  },
  {
    name: "checkpoint_ledger",
    description:
      "List raw entries from the workflow-checkpoint evidence ledger (aggregates all scopes), optionally filtered by theme.",
    parameters: {
      type: "object",
      properties: {
        theme: { type: "string", description: "Filter by theme tag. Omit for all." },
      },
    },
    output: {
      schema: { type: "string" },
      render(args, value) {
        return textBlock(String(value));
      },
    },
    async execute(args, exec) {
      const argv = ["ledger", "list"];
      if (args?.theme) argv.push("--theme", args.theme);
      try {
        return await runCheckpoint(argv);
      } catch (err) {
        return "checkpoint_ledger failed: " + (err && err.message ? err.message : String(err));
      }
    },
  },
];

export function apply(ctx) {
  ctx.on("agent/session-start", ({ agent }) => {
    const cwd = agent?.session?.header?.cwd ?? process.cwd();
    run("python", [CHECKPOINT_PY, "list", "--hook"], cwd)
      .then((raw) => {
        const text = extractContext(raw);
        if (!text) return;
        agent.inject(userMessage(text));
      })
      .catch((error) => {
        ctx.logger.warn("workflow-checkpoint-dsh: session-start hook failed: " + String(error));
      });
  });

  ctx.effect(() => {
    const disposers = TOOLS.map((def) => ctx.tools.register(def));
    return () => {
      for (const d of disposers) {
        try {
          d();
        } catch {}
      }
    };
  });
}
