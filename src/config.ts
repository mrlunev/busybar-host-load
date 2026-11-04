/**
 * The app's settings, as edited in the bar's web interface or written by the
 * installer: where Glances runs and how often to sample it.
 */

import { device } from "@shared/device";
import { loadValues } from "@shared/settings";

/** Port `glances -w` listens on unless told otherwise. */
export const DEFAULT_PORT = 61208;

/** Glances refreshes its REST API about every two seconds; sampling faster only repeats values. */
const DEFAULT_REFRESH_S = 2;
const REFRESH_CHOICES = [2, 5, 10, 30];

export interface Config {
  /** Host name or IP of the computer, empty until configured. */
  readonly host: string;
  readonly port: number;
  readonly intervalMs: number;
}

export const EMPTY_CONFIG: Config = {
  host: "",
  port: DEFAULT_PORT,
  intervalMs: DEFAULT_REFRESH_S * 1000,
};

/**
 * Accepts what people actually type into an address field: `192.168.1.10`,
 * `192.168.1.10:61208`, or a pasted `http://192.168.1.10:61208/`.
 */
export function parseAddress(raw: string): { host: string; port: number } {
  let text = raw.trim();
  const scheme = text.indexOf("://");
  if (scheme >= 0) text = text.substring(scheme + 3);
  const slash = text.indexOf("/");
  if (slash >= 0) text = text.substring(0, slash);

  const colon = text.lastIndexOf(":");
  if (colon > 0 && text.indexOf(":") === colon) {
    const port = parseInt(text.substring(colon + 1), 10);
    if (port >= 1 && port <= 65535) {
      return { host: text.substring(0, colon), port };
    }
    return { host: text.substring(0, colon), port: DEFAULT_PORT };
  }
  return { host: text, port: DEFAULT_PORT };
}

function parseRefresh(value: unknown): number {
  const seconds = typeof value === "string" ? parseInt(value, 10) : Number(value);
  return REFRESH_CHOICES.indexOf(seconds) >= 0 ? seconds : DEFAULT_REFRESH_S;
}

/**
 * Stored settings. The runtime's `Settings` global comes first; where it is
 * missing, the documented `GET /api/apps/settings` endpoint answers instead.
 */
async function readValues(appId: string): Promise<Record<string, unknown>> {
  const local = await loadValues();
  if (local) return local;
  const doc = await device.AppsSettingsGet({ app_id: appId });
  return doc.values;
}

export async function readConfig(appId: string): Promise<Config> {
  const values = await readValues(appId);
  const address = parseAddress(typeof values.host === "string" ? values.host : "");
  return {
    host: address.host,
    port: address.port,
    intervalMs: parseRefresh(values.refresh) * 1000,
  };
}

export function sameConfig(a: Config, b: Config): boolean {
  return a.host === b.host && a.port === b.port && a.intervalMs === b.intervalMs;
}
