/**
 * Reads CPU and memory load from a Glances web server running on the computer.
 *
 * Glances 4 serves `/api/4/cpu` and `/api/4/mem`; the two fields this app needs
 * are `total` and `percent`. Only those are parsed, so a whole response is never
 * kept around.
 */

/** A pair of load percentages, 0..100. */
export interface Load {
  readonly cpu: number;
  readonly ram: number;
}

/** Glances REST API version this app speaks. */
export const API_VERSION = 4;

/** Rejects a request that never answers, so a stalled poll cannot block the next one. */
function withTimeout<T>(work: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error("timeout"));
    }, ms);

    work.then(
      (value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(value);
      },
      (error: unknown) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        reject(error instanceof Error ? error : new Error(String(error)));
      },
    );
  });
}

function percent(value: unknown, field: string): number {
  if (typeof value !== "number" || !isFinite(value)) {
    throw new Error(`bad ${field}`);
  }
  return Math.max(0, Math.min(100, value));
}

async function readField(
  base: string,
  path: string,
  field: string,
  timeoutMs: number,
): Promise<number> {
  const response = await withTimeout(fetch(`${base}/${path}`), timeoutMs);
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  const body = (await withTimeout(response.json(), timeoutMs)) as Record<
    string,
    unknown
  >;
  return percent(body[field], `${path}.${field}`);
}

/**
 * One sample. Both endpoints are read in sequence rather than in parallel: the
 * bar's runtime is small, and two overlapping sockets buy nothing at 1 Hz.
 */
export async function readLoad(
  host: string,
  port: number,
  timeoutMs: number,
): Promise<Load> {
  const base = `http://${host}:${port}/api/${API_VERSION}`;
  const cpu = await readField(base, "cpu", "total", timeoutMs);
  const ram = await readField(base, "mem", "percent", timeoutMs);
  return { cpu, ram };
}
