/**
 * Builds the graph of one half as an XPM2 bitmap.
 *
 * The draw API has no line or polygon primitive, so the graph is drawn pixel by
 * pixel into a HALF_W x GRAPH_H bitmap and sent as a single `xpmbitmap` element.
 * Pixels left blank are transparent, which lets the half's background gradient
 * show through.
 *
 * XPM2 allows at most 32 colours. A ten-row area gradient plus a ten-row line
 * gradient plus the transparent entry comes to 21, so both gradients fit.
 */

import { GRAPH_H, HALF_W, type HalfTheme } from "./layout";

/** One character per colour, so `cpp` stays 1. Index 0 is reserved for transparent. */
const PALETTE_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEF";

/** Mixes two #RRGGBB colours; `t` runs 0 (from) to 1 (to). */
function mix(from: string, to: string, t: number): string {
  let out = "#";
  for (let i = 1; i < 7; i += 2) {
    const a = parseInt(from.substring(i, i + 2), 16);
    const b = parseInt(to.substring(i, i + 2), 16);
    const v = Math.round(a + (b - a) * t);
    out += v.toString(16).toUpperCase().padStart(2, "0");
  }
  return out;
}

/** Gradient sampled once per graph row, top to bottom. */
function ramp(from: string, to: string): string[] {
  const out: string[] = [];
  for (let r = 0; r < GRAPH_H; r += 1) {
    out.push(mix(from, to, GRAPH_H > 1 ? r / (GRAPH_H - 1) : 0));
  }
  return out;
}

/** Row a percentage lands on: 100% on row 0 (screen row GRAPH_TOP), 0% on the last row. */
function rowFor(percent: number): number {
  const clamped = Math.max(0, Math.min(100, percent));
  return GRAPH_H - 1 - Math.round((clamped / 100) * (GRAPH_H - 1));
}

/**
 * Renders `samples` (oldest first, newest last) into XPM2 source. The newest
 * sample sits at the right edge, so the graph scrolls right to left as samples
 * are appended. Fewer samples than columns leaves the left columns transparent.
 */
export function graphXpm(samples: readonly number[], theme: HalfTheme): string {
  const areaRamp = ramp(theme.areaTop, theme.areaBottom);
  const lineRamp = ramp(theme.lineTop, theme.lineBottom);

  // Colour per pixel, by row, so a cell only needs to say "line" or "area".
  const BLANK = 0;
  const LINE = 1;
  const AREA = 2;
  const grid: number[][] = [];
  for (let r = 0; r < GRAPH_H; r += 1) {
    const row: number[] = [];
    for (let x = 0; x < HALF_W; x += 1) row.push(BLANK);
    grid.push(row);
  }

  const count = Math.min(samples.length, HALF_W);
  const first = HALF_W - count;
  let previous = -1;

  for (let i = 0; i < count; i += 1) {
    const x = first + i;
    const row = rowFor(samples[samples.length - count + i]);

    // A jump between neighbouring samples would leave a gap, so the column is
    // drawn from the previous row down to this one and the line stays unbroken.
    const top = previous < 0 ? row : Math.min(row, previous);
    const bottom = previous < 0 ? row : Math.max(row, previous);
    for (let r = top; r <= bottom; r += 1) grid[r][x] = LINE;
    for (let r = bottom + 1; r < GRAPH_H; r += 1) grid[r][x] = AREA;

    previous = row;
  }

  // Only the colours actually used are emitted, which keeps the palette small.
  const colors: string[] = [];
  const assign = (color: string): string => {
    let i = colors.indexOf(color);
    if (i < 0) {
      i = colors.length;
      colors.push(color);
    }
    return PALETTE_CHARS[i];
  };

  const rows: string[] = [];
  for (let r = 0; r < GRAPH_H; r += 1) {
    let line = "";
    for (let x = 0; x < HALF_W; x += 1) {
      const cell = grid[r][x];
      if (cell === BLANK) line += " ";
      else line += assign(cell === LINE ? lineRamp[r] : areaRamp[r]);
    }
    rows.push(line);
  }

  const header = `${HALF_W} ${GRAPH_H} ${colors.length + 1} 1`;
  const palette = ["  c none"];
  for (let i = 0; i < colors.length; i += 1) {
    palette.push(`${PALETTE_CHARS[i]} c ${colors[i]}`);
  }

  return ["! XPM2", header, ...palette, ...rows].join("\n");
}
