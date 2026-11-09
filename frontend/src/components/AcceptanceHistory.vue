<script setup lang="ts">
import { computed } from 'vue'
import type { AcceptanceRecord } from '../api'

const props = defineProps<{ records: AcceptanceRecord[]; loading: boolean; error: string }>()
const reworkCount = computed(() => props.records.filter(record => !record.accepted).length)
function formatTime(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).format(new Date(value))
}
</script>

<template>
  <section class="acceptance-history" aria-label="验收与返工历史" aria-live="polite">
    <div class="acceptance-history-heading">
      <h4>验收与返工历史</h4>
      <span v-if="!loading && !error" class="muted">{{ records.length }} 次验收 · {{ reworkCount }} 次退回</span>
    </div>
    <p v-if="error" class="notice error-notice" role="alert">{{ error }}，请点击刷新重试。</p>
    <p v-else-if="loading" class="muted">正在加载验收记录…</p>
    <p v-else-if="!records.length" class="muted">暂无验收记录，提交维修结果并完成验收后将在此留存。</p>
    <ol v-else class="acceptance-history-list">
      <li v-for="(record, index) in records" :key="record.id" class="acceptance-history-item">
        <div class="acceptance-record-heading">
          <strong>第 {{ index + 1 }} 次验收</strong>
          <span class="acceptance-result" :class="record.accepted ? 'passed' : 'returned'">{{ record.accepted ? '验收通过' : '退回返工' }}</span>
        </div>
        <div class="acceptance-record-meta">
          <span>验收人：{{ record.accepted_by?.trim() || '未记录' }}</span>
          <time :datetime="record.accepted_at">{{ formatTime(record.accepted_at) }}</time>
        </div>
        <div class="acceptance-record-comment">
          <span class="muted">{{ record.accepted ? '验收意见' : '退回原因' }}</span>
          <p>{{ record.acceptance_comment }}</p>
        </div>
      </li>
    </ol>
  </section>
</template>
