import { sanitizeForOutput } from "./sanitize.js";

export function printResult(data: unknown, raw?: boolean): void {
  // Neutralize any attacker-controlled metadata (token name/symbol/description/
  // social links, on-chain URIs, etc.) before it is emitted and read by an AI
  // agent. Defends against indirect prompt injection via token metadata.
  const safe = sanitizeForOutput(data);
  if (raw) {
    console.log(JSON.stringify(safe));
  } else {
    console.log(JSON.stringify(safe, null, 2));
  }
}

export function exitOnError(err: Error): never {
  console.error(`[gmgn-cli] ${err.message}`);
  if (process.env.GMGN_DEBUG) {
    if ((err as NodeJS.ErrnoException).code) {
      console.error(`[gmgn-cli] code: ${(err as NodeJS.ErrnoException).code}`);
    }
    if ((err as { cause?: unknown }).cause) {
      console.error(`[gmgn-cli] cause: ${(err as { cause?: unknown }).cause}`);
    }
    console.error(err.stack ?? "");
  }
  process.exit(1);
}
