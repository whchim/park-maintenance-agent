<script setup lang="ts">
import { computed } from 'vue'
import type { AiMetadata, AiRuntime } from '../api'

const props = defineProps<{
  result?: { mode: string; ai?: AiMetadata } | null
  runtime?: AiRuntime | null
}>()
const reasons: Record<string, string> = {
  disabled: '未启用模型', missing_api_key: '尚未配置模型密钥', invalid_configuration: '模型配置需检查',
  timeout: '模型响应超时', network_error: '模型网络请求失败', authentication_failed: '模型鉴权失败，请检查密钥和地域',
  rate_limited: '模型限流或额度不足', provider_http_error: '模型服务暂不可用',
  invalid_output: '模型输出未通过校验', input_too_large: '内容超过本次模型处理范围',
  invalid_tool_call: '模型请求的工具或参数未通过校验',
  missing_tool_evidence: '模型未查询人员依据', budget_exceeded: '已达到本次查询预算',
  daily_budget_exceeded: '今日演示模型调用额度已用完', model_rate_limited: '演示模型调用过于频繁，请稍后再试',
  model_busy: '演示模型正在处理其他请求', budget_unavailable: '模型调用配额暂不可用',
}
const label = computed(() => {
  if (props.result?.mode === 'llm') return `本次由通义千问 ${props.result.ai?.model || ''} 生成建议，请人工核对。`
  if (props.result) return `本次使用规则回退：${reasons[props.result.ai?.fallback_reason || ''] || '按现有规则生成建议'}。`
  if (props.runtime?.ready) return `千问 ${props.runtime.model || ''} 配置已就绪，实际调用待验证。`
  return props.runtime ? `当前使用规则演示：${reasons[props.runtime.reason || ''] || '模型尚未就绪'}。` : '尚未执行解析，实际模式以本次结果为准。'
})
</script>

<template>
  <div class="agent-mode-note" role="status">
    <span>{{ label }}<small v-if="result?.mode === 'llm'"> 耗时 {{ result.ai?.latency_ms ?? '—' }} ms<span v-if="result.ai?.usage?.total_tokens != null"> · {{ result.ai.usage.total_tokens }} tokens</span></small></span>
  </div>
</template>
