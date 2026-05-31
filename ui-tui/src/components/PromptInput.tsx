import React from "react";
import { Box, Text } from "ink";
import TextInput from "ink-text-input";

export interface PromptInputProps {
  value: string;
  onChange: (v: string) => void;
  onSubmit: (v: string) => void;
  disabled: boolean;
}

/** 底部输入框。busy 时禁用，显示等待提示。 */
export function PromptInput({ value, onChange, onSubmit, disabled }: PromptInputProps) {
  return (
    <Box>
      <Text color={disabled ? "gray" : "cyan"}>{"> "}</Text>
      {disabled
        ? <Text color="gray">（agent 正在工作，等待结束）</Text>
        : <TextInput value={value} onChange={onChange} onSubmit={onSubmit} placeholder="跟 cb-agent 说点什么…" />}
    </Box>
  );
}
