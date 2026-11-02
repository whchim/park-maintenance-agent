<script setup lang="ts">
import type { Orchestration } from '../api'
defineProps<{ trace?: Orchestration }>()
</script>

<template>
  <details v-if="trace?.steps?.length" class="agent-trace" open>
    <summary>协作执行记录</summary>
    <ol>
      <li v-for="(step, index) in trace.steps" :key="index">
        {{ step.summary }}
        <small v-if="step.status === 'blocked'">（已拦截）</small>
        <small v-else-if="step.status === 'failed'">（失败）</small>
      </li>
    </ol>
    <p v-if="trace.model_calls != null">本次模型请求 {{ trace.model_calls }} 次 · 只读查询 {{ trace.tool_calls ?? 0 }} 次</p>
  </details>
</template>

<style scoped>
.agent-trace { margin: 12px 0; padding: 12px; border: 1px solid #d5dce6; border-radius: 8px; }
summary { cursor: pointer; font-weight: 600; }
ol { margin: 10px 0; padding-left: 22px; }
li { margin: 7px 0; line-height: 1.5; }
p, small { color: #526077; font-size: 12px; }
</style>
