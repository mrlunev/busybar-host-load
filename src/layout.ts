/**
 * Screen geometry and palette.
 *
 * The front display is 72x16. It is split into two equal halves that are laid out
 * the same way, not mirrored: a label in the left corner, the current value in the
 * right corner, and a graph filling the full width underneath.
 */

export const SCREEN_W = 72;
export const SCREEN_H = 16;

/** Each half is 36 px wide: CPU is x 0..35, RAM is x 36..71. */
export const HALF_W = SCREEN_W / 2;

/** First row of the graph. 100% sits here, 0% sits on the last row of the screen. */
export const GRAPH_TOP = 6;
export const GRAPH_H = SCREEN_H - GRAPH_TOP;

/** One sample per column, so a half holds exactly HALF_W samples. */
export const HISTORY = HALF_W;

/**
 * The device font is 5 px tall but carries 2 px of bearing above the glyphs, so a
 * label anchored at y = -2 puts its ink on rows 0..4.
 */
export const TEXT_Y = -2;

/** Text anchored with `top_right` ends 2 px left of x, so add it back. */
export const RIGHT_ANCHOR_BIAS = 2;

/** Both halves keep a 1 px inset from their own left and right edge. */
export const INSET = 1;

export interface HalfTheme {
  /** Vertical gradient of the half's background, top to bottom. */
  readonly bgTop: string;
  readonly bgBottom: string;
  /** Vertical gradient of the filled area under the line. */
  readonly areaTop: string;
  readonly areaBottom: string;
  /** Vertical gradient of the line itself. */
  readonly lineTop: string;
  readonly lineBottom: string;
  /** Label colour, matching the half. */
  readonly label: string;
}

export const CPU_THEME: HalfTheme = {
  bgTop: "#4E000C",
  bgBottom: "#000000",
  areaTop: "#890D1F",
  areaBottom: "#280006",
  lineTop: "#FF5C74",
  lineBottom: "#EE1939",
  label: "#FF5C74",
};

export const RAM_THEME: HalfTheme = {
  bgTop: "#052059",
  bgBottom: "#000000",
  areaTop: "#14419A",
  areaBottom: "#010D2F",
  lineTop: "#659AFF",
  lineBottom: "#3378FF",
  label: "#659AFF",
};

export const VALUE_COLOR = "#FFFFFF";
export const MESSAGE_COLOR = "#FFFFFF";
export const MESSAGE_DIM_COLOR = "#FFFFFF";

/** Adds the opaque alpha byte the draw API expects on every colour. */
export function opaque(rgb: string): string {
  return `${rgb}FF`;
}

/** Adds an explicit alpha byte, 0..255. */
export function alpha(rgb: string, a: number): string {
  const clamped = Math.max(0, Math.min(255, Math.round(a)));
  return `${rgb}${clamped.toString(16).toUpperCase().padStart(2, "0")}`;
}
