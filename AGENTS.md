# AGENTS.md

Instructions for AI coding agents working on `Host Load` (`app.mrlunev.hostload`), a JavaScript application for the BUSY Bar. See [README.md](./README.md) for the full build, packaging and manifest reference.

The app runs on the bar itself, in the firmware's embedded [JerryScript](https://compat-table.github.io/compat-table/es2016plus/#jerryscript3_0_0) runtime. It is not Node.js and not a browser.

## Firmware API boundary

The BUSY Bar firmware is an external dependency of this app.

- Never modify or patch BUSY Bar firmware source code as part of work on this project. Treat the firmware repository as read-only reference material.
- If the required capability is not available in the target firmware, use a supported fallback or degrade gracefully. If neither is possible, report the missing capability and required API version; do not implement a firmware change.
- Before treating a capability as missing, check the target device at `http://<device-host>/docs` and `GET /api/version`, or inspect the matching firmware OpenAPI definitions.
- If the target firmware lacks a required capability, use a supported fallback, degrade gracefully, or report the required API version. Do not invent an endpoint or silently depend on unreleased firmware behavior.

## Entry point and lifecycle

`src/main.ts` or `src/main.js` is the entry point. The build emits it as `scripts/main.js`, the file the firmware starts. It is a script, not a module: the firmware runs it from top to bottom once, it must not export anything, and it starts the app by calling `main()` at the end.

**`main()` is not run in an infinite loop.** It is a one-time launch. The firmware does not keep the app alive because `main()` was called, and it does not restart it when `main()` returns. The app stays alive only while something the script started is still pending:

- an input listener registered with `listen("input", ...)`;
- a pending `setTimeout` or a running `setInterval`;
- a `fetch` request that has not settled yet.

When `main()` returns, the app keeps running while at least one of those remains. Once the last listener is unbound, every interval is cleared, every timeout is cleared or has fired, and every outstanding `fetch` has settled, the JavaScript process exits and the app closes. The bar takes the screen back.

That makes cleanup part of the app design, not an afterthought:

- Keep the reason the app stays alive explicit: one listener or timer per thing the app is doing.
- To quit, release everything: clear every timer, unbind every listener, and let outstanding fetches settle. There is no exit function — releasing the last handle is the exit.
- Do the same cleanup on every path that ends a mode or the app, not only in the `back` handler.
- Do not unbind the last handler from inside that handler's own callback: the process would stop while the callback is still running. Schedule it instead, as the template does with `setTimeout(unbind, 0)`.
- `listen()` accepts one handler at a time and throws if a handler is already attached. Unbind the old one before attaching another.
- Keep `main()` synchronous and start asynchronous work from it explicitly. Give every promise a rejection handler (`void work().catch(report)` or `try`/`catch`); an unhandled rejection must not be the only error path.
- Do not keep the app open with a dummy long-running timer "just in case", and never busy-wait to stay alive.

The shape of a minimal app:

```ts
function main(): void {
  const timer = setInterval(() => {
    // redraw
  }, 1000);

  const unbind = listen("input", (event) => {
    if (event.key !== "back" || event.action !== "short") return;

    clearInterval(timer);
    // The unbind is the last handle: schedule it so this callback returns first.
    setTimeout(unbind, 0);
  });
}

main();
```

## Conserve device resources

The BUSY Bar is a small embedded device. The JavaScript runtime, the web server that serves the bar's HTTP API, and the rest of the firmware share the same CPU and memory. An app that is greedy slows down the whole bar, not just itself.

### Memory

- `heap_size_kib` in `src/appmeta/manifest.json` is the entire heap for the app: 1–512 KiB, 128 KiB by default. Keep the default unless measurements show the app needs more; a larger heap takes memory from other subsystems. Prefer reducing allocations over raising the heap.
- Avoid large buffers, long-lived caches, and unbounded arrays. Keep only the state the app needs and what is currently on screen.
- Send display elements from the data you already have instead of keeping parallel copies of every frame.
- Parse only the fields you need from responses; do not retain whole payloads.
- Logging costs memory and time. Do not log inside tight loops or per-frame redraws.

### Requests and timers

- Every API call is an HTTP request handled by the device's web server. It costs CPU and memory on the bar and can be slow. Poll on human time scales (seconds or minutes), never per frame and never in a loop.
- Keep at most one display request in flight. When one is still unanswered, skip or coalesce the next update rather than queueing it.
- Do not retry immediately on failure. Use a bounded backoff, and stop retrying while the device reports that it is offline or still connecting.
- Send only what changed. Elements are addressed by `id`, so update an element instead of redrawing the whole screen; clear removed ids once with `DisplayClear` instead of resending an empty frame.
- Run timers only while something needs updating, and clear them when the app is idle. Do not use `setInterval(..., 0)` or a `while` loop as a wait.
- Prefer coarse, aligned timers: redraw on the minute rather than every 100 ms, and refresh remote data every few minutes rather than every few seconds.
- Package images, animations and sounds with the app instead of downloading them at runtime, and keep them small. Do not decode large files into memory.

When in doubt, choose the cheaper option: a slower refresh, fewer requests, less data on screen.

## Display, input and settings

- The screen is 72×16 pixels. Build a tree with `row`, `column` and `stack` from `@busy-app/busy-lib`, render it with `render()`, and send the elements with `device.DisplayDraw({ application_name: APP, priority: 50, elements })`. Use `manifest.id` as `APP`.
- Give every element a stable `id`. The device replaces elements with the same id, so only changed elements need to be sent.
- Remove elements that disappear with `device.DisplayClear({ application_name: APP, element_ids: [...] })`.
- `priority` is 1–100. Keep it at 50 unless the app deliberately needs to take the screen from another one.
- Colors are `#RRGGBBAA`.
- `listen("input", handler)` delivers `ok`, `start` and `back` with a `press`, `release`, `short`, `long` or `repeat` action, and `encoder` with a direction and a `delta` of `1` or `-1`. It returns an unbind function.
- **Use `short` as the default button action, not `press`.** `short` fires once, after a normal press and release; `press` fires immediately on key down and is followed by `short` on release, so treating `press` as a tap reacts before the gesture is complete and can double-fire. Use `long` and `repeat` for hold gestures, and `press`/`release` only when the down and up edges matter.
- Settings are described in `src/appmeta/settings.json`. Read them with `loadValues()` from `@shared/settings` and fall back to the defaults in code when it returns `null`; persist with `saveValues()`. Do not put secrets in settings files.
- **Do not raise `version` in `settings.json`.** The firmware discards every stored value when it changes (there are no migrations for JS apps): people updating would lose the computer address the installer wrote. New fields are added under the same version; a fresh install gets their defaults, an update shows them empty until the settings are reset or saved.
- While the front shows SET ADDRESS or NO GLANCES, the back shows what to do (`src/help.ts`, after Net Traffic): the bar's web addresses over Wi-Fi and USB, then **Apps → Host Load → Install**. The help elements are part of the message frame, so going back to the graph clears them.

## Code rules

- The runtime is not Node.js and not a browser: no `fs`, `path`, `process`, `document` or `window`. `console`, `fetch` with `Request`/`Response`/`Headers`/`URL`, timers, `localStorage`, `Date`, `JSON` and standard ECMAScript are available.
- `localStorage` persists small values per app, but every `setItem` rewrites the whole store to flash. Keep values small and writes rare; use `@shared/settings` for user-visible settings.
- `src/` is the app and the only folder the build scans. `shared/` holds app-level helpers imported as `@shared/*`. The entry point must stay named `main.ts` or `main.js`.
- TypeScript is strict when the app is written in `.ts`; run `npm run typecheck`. The entry point may be plain JavaScript (`src/main.js`) instead.
- Third-party packages must run in this runtime: no Node built-ins and no DOM. Prefer `@busy-app/busy-lib` and the existing `@shared/*` helpers over adding dependencies.
- Never edit `dist/`; it is generated by `npm run build`.
- Report failures with `console.error` and a stable app prefix, and keep one failed request from stopping timers or input handling.
- Put resources under `src/images`, `src/animations` and `src/sounds` in the formats the bar reads (`.png`/`.image`, `.anim`, `.snd`). Other formats are packaged into `resources/` and cannot be displayed by the bar.
- Keep this file and `README.md` accurate when conventions change.

## Before finishing

Run `npm run typecheck` and `npm run build`, then check:

- [ ] The app quits cleanly when `back` (or the app's quit action) is used: no timer or listener outlives it.
- [ ] No path leaks an interval, timeout or listener; errors do not leave timers running.
- [ ] Memory use is bounded: no unbounded caches, buffers or per-frame copies.
- [ ] Button activation uses `short`, not `press`; holds use `long` and `repeat`.
- [ ] Requests are infrequent and do not pile up; failures back off instead of retrying in a tight loop.
- [ ] Frames send only changed elements and are redrawn only when something visible changes.
- [ ] `src/appmeta/manifest.json` has a valid `id`, `name` and semver `version`, and `api_version` is no higher than needed.

## Handover

State on 2026-11-23, for the next agent:

- Released: 1.0.2 on GitHub (23.11), after 1.0.1 (`e1bee66`). 1.0.2 includes `cb55368`, which fixes Glances not starting after a Windows restart: every start, at install and at login, goes through the `start-glances.py` launcher with a supervisor. It also pins Glances to 4.5.9 and adds update steps of 0.5 s and 1 s (Glances with `--time 0.5 --cached-time 0` and a 0.5 s CPU cache).
- Checked on the self-built test bar (U5, firmware 1.4.1), plugged by USB into the Windows test PC: graphs at 2 s and 0.5 s, autostart through a simulated login, two full installer runs. macOS and Linux were checked by the installer session; the stock bar was not used.
- 1.0.2 also brings the install group in the settings, the back-screen help and the new back icon. Checked on a test bar (1.4.1, by USB): Not set up yet, and a full installer run on macOS with graphs live; and in the emulator: No answer from Glances, and back to the graph. The stock bar was not connected.
- Left before the catalog: the catalog pull request. `catalog.json` and `screenshots/` are committed; `busy-emulator check` has no failures.
- Decisions for the owner: the default step (2 s now, 0.5 s available); whether to keep `BusyBar` from busy-lib in `shared/device.ts` (its 3 s `AbortController` timeout throws on the device; other apps use a thin `fetch` client).
- The test bar has `refresh` set to 0.5 s; set it back to 2 s if that matters.
