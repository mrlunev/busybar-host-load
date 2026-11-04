/**
 * Builds the frames the app sends to the display.
 *
 * Elements are built by hand rather than through the layout helpers: every
 * position here is a fixed pixel on a 72x16 screen, so there is nothing for a
 * layout pass to decide.
 */

import type { DisplayDrawParams } from "@busy-app/busy-lib";

import { graphXpm } from "./graph";
import {
  CPU_THEME,
  GRAPH_TOP,
  HALF_W,
  INSET,
  MESSAGE_COLOR,
  MESSAGE_DIM_COLOR,
  type HalfTheme,
  RAM_THEME,
  RIGHT_ANCHOR_BIAS,
  SCREEN_H,
  SCREEN_W,
  TEXT_Y,
  VALUE_COLOR,
  alpha,
  opaque,
} from "./layout";

type Element = DisplayDrawParams["elements"][number];

/** Everything a frame needs: the elements to draw and the ids they occupy. */
export interface Frame {
  readonly elements: Element[];
  readonly ids: string[];
}

const FONT = "small";

/** Advance of one `small` glyph, measured on the device: "CPU 92%" is 29 px wide. */
const SMALL_CHAR_W = 4;

function background(id: string, x: number, theme: HalfTheme): Element {
  return {
    id,
    type: "rectangle",
    display: "front",
    align: "top_left",
    x,
    y: 0,
    width: HALF_W,
    height: SCREEN_H,
    fill: "gradient_v",
    fill_colors: [opaque(theme.bgTop), opaque(theme.bgBottom)],
    border_width: 0,
    z_index: 0,
  };
}

function graph(
  id: string,
  x: number,
  samples: readonly number[],
  theme: HalfTheme,
): Element {
  return {
    id,
    type: "xpmbitmap",
    display: "front",
    align: "top_left",
    x,
    y: GRAPH_TOP,
    data: graphXpm(samples, theme),
    z_index: 1,
  };
}

function label(id: string, x: number, text: string, color: string): Element {
  return {
    id,
    type: "text",
    display: "front",
    align: "top_left",
    x,
    y: TEXT_Y,
    text,
    font: FONT,
    color: opaque(color),
    z_index: 2,
  };
}

function value(id: string, right: number, text: string): Element {
  return {
    id,
    type: "text",
    display: "front",
    align: "top_right",
    x: right + RIGHT_ANCHOR_BIAS,
    y: TEXT_Y,
    text,
    font: FONT,
    color: opaque(VALUE_COLOR),
    z_index: 2,
  };
}

/** Percentages are shown without decimals; there is no room for more. */
function formatPercent(v: number): string {
  return `${Math.round(Math.max(0, Math.min(100, v)))}%`;
}

/**
 * The live screen: two halves, each with its label, its current value and its
 * graph. CPU is the left half, RAM the right one.
 */
export function loadFrame(
  cpuHistory: readonly number[],
  ramHistory: readonly number[],
): Frame {
  const left = 0;
  const right = HALF_W;

  const elements: Element[] = [
    background("bg_cpu", left, CPU_THEME),
    background("bg_ram", right, RAM_THEME),
    graph("g_cpu", left, cpuHistory, CPU_THEME),
    graph("g_ram", right, ramHistory, RAM_THEME),
    label("l_cpu", left + INSET, "CPU", CPU_THEME.label),
    label("l_ram", right + INSET, "RAM", RAM_THEME.label),
    value("v_cpu", left + HALF_W - 1 - INSET, formatPercent(last(cpuHistory))),
    value("v_ram", right + HALF_W - 1 - INSET, formatPercent(last(ramHistory))),
  ];

  return { elements, ids: elements.map((e) => e.id) };
}

function last(history: readonly number[]): number {
  return history.length > 0 ? history[history.length - 1] : 0;
}

/**
 * Shown while there is nothing to graph: no address configured, or Glances not
 * answering. Two short centred lines, because 72x16 fits little else.
 */
export function messageFrame(top: string, bottom: string): Frame {
  const middle = SCREEN_W / 2;
  // The font's 2 px top bearing again: these anchors put the ink on rows 2..6 and 9..13.
  const elements: Element[] = [
    {
      id: "m_top",
      type: "text",
      display: "front",
      align: "top_mid",
      x: middle,
      y: 0,
      text: top,
      font: FONT,
      color: opaque(MESSAGE_COLOR),
      z_index: 2,
    },
    {
      id: "m_bottom",
      type: "text",
      display: "front",
      align: "top_mid",
      x: middle,
      y: 7,
      text: bottom,
      font: FONT,
      color: alpha(MESSAGE_DIM_COLOR, 0x99),
      z_index: 2,
      // A long host name would run off both edges, so it scrolls inside the screen instead.
      ...(bottom.length * SMALL_CHAR_W > SCREEN_W - 2 * INSET
        ? {
            width: SCREEN_W - 2 * INSET,
            scroll_rate: 1200,
            scroll_start_delay: 1000,
            scroll_repeat_delay: 1500,
          }
        : {}),
    },
  ];

  return { elements, ids: elements.map((e) => e.id) };
}
