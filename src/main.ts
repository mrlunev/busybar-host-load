/**
 * Host Load — live CPU and RAM graphs of a computer on the same Wi-Fi.
 *
 * The computer runs Glances in web server mode (`glances -w`). The bar polls its
 * REST API, keeps one sample per graph column and redraws the front display.
 */

import { device } from "@shared/device";

import manifest from "./appmeta/manifest.json";
import { type Config, EMPTY_CONFIG, readConfig, sameConfig } from "./config";
import { readLoad } from "./glances";
import { HISTORY } from "./layout";
import { type Frame, loadFrame, messageFrame } from "./screen";

/** The name the app draws under; the device clears elements by this id. */
const APP = manifest.id;

/** A sample must not outlive its slot, or polls would pile up. */
const REQUEST_TIMEOUT_MS = 4000;

/**
 * Settings saved while the app runs do not restart it, so they are read again
 * this often. It is a local request, cheap next to the network poll.
 */
const SETTINGS_REFRESH_MS = 10000;

/** Consecutive failed polls before the graph is dropped and NO GLANCES shown. */
const GIVE_UP_AFTER = 3;

/** Retries back off after failures, but never wait longer than this between tries. */
const MAX_RETRY_MS = 15000;

/** Appends a sample and drops the one that just scrolled off the left edge. */
function push(history: number[], value: number): void {
  history.push(value);
  while (history.length > HISTORY) history.shift();
}

function main(): void {
  const cpuHistory: number[] = [];
  const ramHistory: number[] = [];

  let config: Config = EMPTY_CONFIG;
  let configReadAt = -SETTINGS_REFRESH_MS;
  /** What is on screen now, by element id, as last sent. */
  let onScreen: Record<string, string> = {};
  let polling = false;
  let failures = 0;
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const report = (where: string, err: unknown) =>
    console.error(
      `${APP}: ${where}: ${err instanceof Error ? err.message : String(err)}`,
    );

  function resetHistory(): void {
    cpuHistory.length = 0;
    ramHistory.length = 0;
  }

  /**
   * Sends only the elements that differ from what is already on screen, and
   * clears the ids the new frame no longer has. Backgrounds and labels go out
   * once; after that a tick costs the two graphs and the two values.
   */
  async function show(frame: Frame): Promise<void> {
    const next: Record<string, string> = {};
    const changed = [];
    for (const element of frame.elements) {
      const sent = JSON.stringify(element);
      next[element.id] = sent;
      if (onScreen[element.id] !== sent) changed.push(element);
    }
    const gone = Object.keys(onScreen).filter((id) => !(id in next));

    if (changed.length > 0) {
      await device.DisplayDraw({ application_name: APP, priority: 50, elements: changed });
    }
    if (gone.length > 0) {
      await device.DisplayClear({ application_name: APP, element_ids: gone });
    }
    onScreen = next;
  }

  /** Re-reads the settings when they are due; a new address starts a fresh graph. */
  async function refreshConfig(): Promise<void> {
    const now = Date.now();
    if (now - configReadAt < SETTINGS_REFRESH_MS) return;
    configReadAt = now;
    try {
      const next = await readConfig(APP);
      if (sameConfig(next, config)) return;
      if (next.host !== config.host || next.port !== config.port) {
        resetHistory();
        failures = 0;
      }
      config = next;
    } catch (err) {
      report("settings", err);
    }
  }

  /** One poll: sample, append, redraw. Failures show a message and back off. */
  async function tick(): Promise<void> {
    if (polling) return;
    polling = true;
    try {
      await refreshConfig();

      if (config.host === "") {
        resetHistory();
        await show(messageFrame("SET ADDRESS", "IN SETTINGS"));
        return;
      }

      try {
        const load = await readLoad(config.host, config.port, REQUEST_TIMEOUT_MS);
        push(cpuHistory, load.cpu);
        push(ramHistory, load.ram);
        failures = 0;
      } catch (err) {
        if (failures === 0) report("glances", err);
        failures += 1;
        // A dropped poll or two leaves the graph as it is: at the slow settings
        // it holds many minutes, too much to lose to one lost packet.
        // With nothing drawn yet there is nothing to keep, so say so at once.
        if (failures < GIVE_UP_AFTER && cpuHistory.length > 0) return;
        // Glances is really gone: the gap is shown as a fresh start, not stitched over.
        resetHistory();
        await show(messageFrame("NO GLANCES", config.host));
        // A wrong address is the usual cause, so pick up corrected settings on the next try.
        configReadAt = -SETTINGS_REFRESH_MS;
        return;
      }

      await show(loadFrame(cpuHistory, ramHistory));
    } finally {
      polling = false;
    }
  }

  /**
   * Self-rescheduling timeout rather than an interval: a slow or failing poll
   * then delays the next one instead of stacking up behind it.
   */
  function schedule(): void {
    if (stopped) return;
    let delay = config.intervalMs;
    if (failures > 0) {
      const backedOff = delay * Math.pow(2, Math.min(failures, 3));
      delay = Math.max(delay, Math.min(backedOff, MAX_RETRY_MS));
    }
    timer = setTimeout(() => {
      void tick()
        .catch((err: unknown) => report("tick", err))
        .then(schedule);
    }, delay);
  }

  function quit(): void {
    if (stopped) return;
    stopped = true;
    if (timer !== undefined) clearTimeout(timer);
    // Not from inside the handler's own callback: with nothing else left, the
    // unbind would stop the app while this callback is still running.
    setTimeout(unbind, 0);
  }

  const unbind = listen("input", (event) => {
    if (event.key === "back" && event.action === "short") quit();
  });

  void tick()
    .catch((err: unknown) => report("tick", err))
    .then(schedule);
}

main();
