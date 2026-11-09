<script setup lang="ts">
import { ref } from 'vue'
import { CheckCircle2, RefreshCw } from 'lucide-vue-next'

const props = defineProps<{ loading: boolean; error: string }>()
const emit = defineEmits<{
  decision: [payload: { accepted: boolean; acceptance_comment: string; accepted_by: string }]
}>()
const comment = ref('')
const actor = ref('企服演示员')
const validationError = ref('')
const commentInput = ref<HTMLTextAreaElement | null>(null)

function submit(accepted: boolean) {
  if (props.loading) return
  if (!comment.value.trim()) {
    validationError.value = accepted ? '请先填写验收意见，再确认验收。' : '请先填写退回原因和返工要求，再退回工程部。'
    commentInput.value?.focus()
    return
  }
  validationError.value = ''
  emit('decision', {
    accepted,
    acceptance_comment: comment.value.trim(),
    accepted_by: actor.value.trim() || '企服演示员',
  })
}
</script>

<template>
  <div class="workflow-form" aria-label="企服验收操作">
    <div class="action-context"><CheckCircle2 :size="16" /><span>工程部已提交维修结果，请企服确认</span></div>
    <label>
      <span>验收意见／退回原因（必填）</span>
      <textarea ref="commentInput" v-model="comment" rows="3" :disabled="loading"
        :aria-invalid="!!validationError" aria-describedby="acceptance-form-hint acceptance-form-error"
        placeholder="验收通过请填写现场结论；退回请说明未解决的问题和返工要求"
        @input="validationError = ''"></textarea>
    </label>
    <p id="acceptance-form-hint" class="muted">退回后工单回到“处理中”，当前维修人员继续处理；此前的维修和验收记录会保留。</p>
    <label>
      <span>验收人员</span>
      <input v-model="actor" :disabled="loading" placeholder="企服演示员" />
    </label>
    <div v-if="validationError || error" id="acceptance-form-error" class="notice error-notice" role="alert">{{ validationError || error }}</div>
    <button class="primary-button" type="button" :disabled="loading" @click="submit(true)">
      <CheckCircle2 :size="15" />{{ loading ? '正在提交…' : '验收并关闭工单' }}
    </button>
    <button class="secondary-button" type="button" :disabled="loading" @click="submit(false)">
      <RefreshCw :size="15" />{{ loading ? '正在提交…' : '退回工程部返工' }}
    </button>
  </div>
</template>
