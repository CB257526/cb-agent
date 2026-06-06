import { describe, expect, it } from "vitest";
import { parseBuddyCardText } from "../buddy/BuddyCard.js";

const cardText = `* COMMON DUCK

    __      
  <(· )___  
   (  ._>   
    \`--´    

Waddles - 喜欢橡皮鸭调试法，看到可疑变量就想嘎两声。

DEBUGGING  ######....  58
PATIENCE   ###.......  30
CHAOS      #####.....  45
WISDOM     ####......  44
SNARK      #.........   9`;

describe("BuddyCard parser", () => {
  it("解析 /buddy status 返回的 Buddy 文本卡片", () => {
    const parsed = parseBuddyCardText(cardText);

    expect(parsed).not.toBeNull();
    expect(parsed?.rarity).toBe("common");
    expect(parsed?.species).toBe("duck");
    expect(parsed?.name).toBe("Waddles");
    expect(parsed?.sprite).toHaveLength(4);
    expect(parsed?.stats.map((s) => s.name)).toEqual([
      "DEBUGGING",
      "PATIENCE",
      "CHAOS",
      "WISDOM",
      "SNARK",
    ]);
  });

  it("兼容 hatch/rehatch 的提示前缀", () => {
    const parsed = parseBuddyCardText(`Buddy 已孵化：\n\n${cardText}`);

    expect(parsed?.name).toBe("Waddles");
    expect(parsed?.stats[0]).toMatchObject({ name: "DEBUGGING", value: 58 });
  });

  it("非 Buddy 文本返回 null，交给普通系统消息渲染", () => {
    expect(parseBuddyCardText("Buddy 已取消静音。")).toBeNull();
    expect(parseBuddyCardText("普通 system 消息")).toBeNull();
  });
});
