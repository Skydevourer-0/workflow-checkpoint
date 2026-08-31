import { execFile } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";

export const name = "workflow-checkpoint-dsh";
export const inject = [];

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
}
