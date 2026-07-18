/**
 * ThemeProvider：把终端原生主题对象通过 Solid context 注入组件树。
 *
 * 当前主题使用终端默认色与 ANSI 索引色，因此无需单独维护深浅模式。将来若增加
 * 用户可选主题，可在这里加入响应式 signal，组件侧 useTheme() 接口不需要变化。
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
