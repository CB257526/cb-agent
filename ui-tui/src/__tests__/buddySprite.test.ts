import { describe, expect, it } from "vitest";
import { isNarrowBuddyLayout, shouldRenderBuddy } from "../buddy/BuddySprite.js";
import type { BuddyState } from "../types.js";

const baseState: BuddyState = {
  enabled: true,
  status: "ready",
  muted: false,
  companion: {
    name: "Waddles",
    personality: "测试用 Buddy",
    seed: "seed",
    hatched_at: 1,
    rarity: "common",
    species: "duck",
    eye: "·",
    hat: "none",
    shiny: false,
    stats: {},
    sprite: ["duck"],
    frames: [["duck"]],
    face: "(·>",
    rarity_stars: "*",
  },
};

describe("BuddySprite helpers", () => {
  it("只有启用、未静音且已有 companion 时才渲染", () => {
    expect(shouldRenderBuddy(baseState)).toBe(true);
    expect(shouldRenderBuddy(null)).toBe(false);
    expect(shouldRenderBuddy({ ...baseState, enabled: false })).toBe(false);
    expect(shouldRenderBuddy({ ...baseState, muted: true })).toBe(false);
    expect(shouldRenderBuddy({ ...baseState, companion: null })).toBe(false);
  });

  it("窄终端使用单行降级布局", () => {
    expect(isNarrowBuddyLayout(80)).toBe(true);
    expect(isNarrowBuddyLayout(90)).toBe(false);
    expect(isNarrowBuddyLayout(120)).toBe(false);
  });
});
