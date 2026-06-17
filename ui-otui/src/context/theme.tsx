/**
 * ThemeProvider：把静态暗色主题对象通过 Solid context 注入组件树。
 *
 * 当前只有单一暗色主题（theme.ts），所以 context 直接透传该对象。将来要加
 * light / 多主题切换时，把响应式 signal 放进这里即可，组件侧 useTheme() 接口不变。
 */

import { createContext, useContext, type ParentProps } from "solid-js";
import { theme as defaultTheme } from "../theme.js";

const ThemeContext = createContext(defaultTheme);

export function ThemeProvider(props: ParentProps) {
  return (
    <ThemeContext.Provider value={defaultTheme}>
      {props.children}
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  return useContext(ThemeContext);
}
