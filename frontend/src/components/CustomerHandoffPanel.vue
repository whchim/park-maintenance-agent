<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  createCustomerHandoff, decideCustomerHandoff, getCustomerHandoffs, getInvestmentCustomers,
  saveCustomerHandoff, submitCustomerHandoff,
  type CustomerHandoff, type CustomerSummary, type HandoffDocument,
} from '../api'

const props = defineProps<{ active: boolean }>()
const customers = ref<CustomerSummary[]>([])
const handoffs = ref<CustomerHandoff[]>([])
const selectedCustomerId = ref<number | null>(null)
const selected = ref<CustomerHandoff | null>(null)
const document = ref<HandoffDocument | null>(null)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const submitter = ref('招商演示员')
const receiver = ref('企服演示员')
const signed = ref(false)
const comment = ref('')
// 同一次失败请求保留重试键；修改字段或版本后才使用新键。
const requestKeys = new Map<string, string>()
const editable = computed(() => selected.value && ['draft', 'returned'].includes(selected.value.status))
const dirty = computed(() => JSON.stringify(document.value) !== JSON.stringify(selected.value?.document || null))
const statusLabels = { draft: '招商完善联系单', submitted: '待企服接收', returned: '企服退回待修改', accepted: '企服已接收' }
const eventLabels: Record<string, string> = {
  'customer.handoff.submitted': '招商确认签约并提交联系单',
  'customer.handoff.returned': '企服退回，招商补充修改',
  'customer.handoff.accepted': '企服确认接收',
}
const fields: { key: keyof HandoffDocument; label: string; multiline?: boolean }[] = [
  { key: 'contract_no', label: '已签约合同编号' },
  { key: 'contract_version', label: '合同版本' },
  { key: 'contact_sheet_ref', label: '最终版客户联系单引用（文件编号或文档地址）' },
  { key: 'contact_sheet_version', label: '联系单版本' },
  { key: 'location', label: '入驻位置' },
  { key: 'contact_name', label: '客户联系人' },
  { key: 'contact_phone', label: '联系方式' },
  { key: 'service_requirements', label: '服务需求，无需求请明确填无', multiline: true },
  { key: 'agreements', label: '约定事项，无约定请明确填无', multiline: true },
]

function apply(handoff: CustomerHandoff | null) {
  selected.value = handoff
  document.value = handoff ? { ...handoff.document } : null
  signed.value = false
  comment.value = ''
}

async function load() {
  const [customerRows, handoffRows] = await Promise.all([getInvestmentCustomers(), getCustomerHandoffs()])
  customers.value = customerRows
  handoffs.value = handoffRows
}

async function refresh() {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await load()
    // 切换工作区与刷新列表时，保留未保存的联系单、签约确认和企服意见。
    if (selected.value && !dirty.value && !signed.value && !comment.value.trim()) {
      apply(handoffs.value.find(h => h.id === selected.value?.id) || null)
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '客户交接列表加载失败'
  } finally { busy.value = false }
}

async function run(action: () => Promise<CustomerHandoff>, success: string) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  notice.value = ''
  try {
    apply(await action())
    notice.value = success
    await load()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '客户交接操作失败'
  } finally { busy.value = false }
}

function keyFor(action: string, payload: unknown): string {
  const identity = JSON.stringify([action, selected.value?.id, selected.value?.revision, payload])
  const key = requestKeys.get(identity) || crypto.randomUUID()
  requestKeys.set(identity, key)
  return key
}

function create() {
  if (selectedCustomerId.value === null) return
  const id = selectedCustomerId.value
  void run(() => createCustomerHandoff(id), '交接草稿已打开；尚未提交企服')
}

function save() {
  if (!selected.value || !document.value) return
  const handoff = selected.value
  const snapshot = { ...document.value }
  void run(() => saveCustomerHandoff(handoff, snapshot), '草稿已保存，已检查缺失字段')
}

function submit() {
  if (!selected.value || dirty.value || !signed.value || !submitter.value.trim()) return
  const handoff = selected.value
  const payload = { actor: submitter.value.trim(), contract_signed: signed.value }
  const idempotency_key = keyFor('submit', payload)
  void run(() => submitCustomerHandoff(handoff, { ...payload, idempotency_key }), '招商已提交，请企服核对同一版本的联系单')
}

function decide(accepted: boolean) {
  if (!selected.value || !receiver.value.trim() || !comment.value.trim()) return
  const handoff = selected.value
  const payload = { actor: receiver.value.trim(), accepted, comment: comment.value.trim() }
  const idempotency_key = keyFor('decide', payload)
  void run(() => decideCustomerHandoff(handoff, { ...payload, idempotency_key }), accepted ? '企服已接收客户交接' : '已退回招商补充，原提交内容保留在交接记录中')
}

watch(() => props.active, active => { if (active) void refresh() }, { immediate: true })
</script>

<template>
  <section class="customer-handoff-panel">
    <div class="agent-mode-note">演示联系单字段，待正式模板映射。完整性检查为规则回退，不核验文件真实性；操作人当前为演示身份，未连接登录权限。</div>
    <div class="coordinator-actions">
      <label class="handoff-customer-select">选择客户
        <select v-model="selectedCustomerId" :disabled="busy"><option :value="null" disabled>请选择已有客户</option><option v-for="customer in customers" :key="customer.id" :value="customer.id">{{ customer.customer_name }} · {{ customer.stage }}</option></select>
      </label>
      <button class="secondary-button" type="button" :disabled="busy || selectedCustomerId === null" @click="create">打开交接草稿</button>
      <button class="secondary-button" type="button" :disabled="busy" @click="refresh">刷新</button>
    </div>
    <p v-if="error" class="notice error-notice" role="alert">{{ error }}</p>
    <p v-if="notice" class="coordination-summary" role="status">{{ notice }}</p>
    <p v-if="busy" class="muted" role="status">正在处理交接…</p>
    <div class="handoff-queue" aria-label="客户交接列表">
      <button v-for="handoff in handoffs" :key="handoff.id" class="secondary-button" type="button" :disabled="busy" @click="apply(handoff)">{{ handoff.customer_name }} · {{ statusLabels[handoff.status] }}</button>
      <p v-if="!busy && !handoffs.length" class="muted">暂无交接单；创建意向客户不会自动发起正式交接。</p>
    </div>
    <section v-if="selected && document" class="coordination-result">
      <h3>{{ selected.customer_name }} · {{ statusLabels[selected.status] }}</h3>
      <p class="muted">修改版本 {{ selected.revision }} · {{ selected.workflow_id }}</p>
      <p v-if="selected.decision_comment" class="coordination-summary">企服意见：{{ selected.decision_comment }}（{{ selected.decided_by }}）</p>
      <fieldset class="handoff-fields" :disabled="busy || !editable">
        <div class="workflow-draft">
          <label v-for="field in fields" :key="field.key" :class="{ 'form-wide': field.multiline }"><span>{{ field.label }}</span>
            <textarea v-if="field.multiline" v-model="document[field.key]" rows="2"></textarea>
            <input v-else v-model="document[field.key]" />
          </label>
        </div>
      </fieldset>
      <div v-if="editable">
        <p v-if="dirty" class="muted">有未保存修改，请先保存草稿再提交。</p>
        <p v-else-if="selected.check.missing_labels.length" class="muted">需补充：{{ selected.check.missing_labels.join('、') }}</p>
        <p v-else class="muted">必要字段已填写，请人工核对最终版联系单。</p>
        <button class="secondary-button" type="button" :disabled="busy" @click="save">保存并检查完整性</button>
        <label class="coordinator-input"><span>招商提交人（演示身份）</span><input v-model="submitter" :disabled="busy" /></label>
        <label class="handoff-attestation"><input v-model="signed" type="checkbox" :disabled="busy" />我确认客户已签约，以上为核对后的最终版联系单信息</label>
        <button class="primary-button" type="button" :disabled="busy || dirty || !signed || !submitter.trim() || !!selected.check.missing_labels.length" @click="submit">确认签约并提交企服</button>
      </div>
      <div v-if="selected.status === 'submitted'" class="workflow-form">
        <p class="muted">待企服核对；招商提交人：{{ selected.submitted_by }}。提交后的联系单被冻结。</p>
        <label><span>企服接收人（演示身份）</span><input v-model="receiver" :disabled="busy" /></label>
        <label><span>接收意见或退回原因</span><textarea v-model="comment" :disabled="busy" rows="2"></textarea></label>
        <div class="coordinator-actions">
          <button class="secondary-button" type="button" :disabled="busy || !receiver.trim() || !comment.trim()" @click="decide(false)">退回招商补充</button>
          <button class="primary-button" type="button" :disabled="busy || !receiver.trim() || !comment.trim()" @click="decide(true)">企服确认接收</button>
        </div>
      </div>
      <section aria-label="交接版本历史">
        <h3>交接记录与版本快照</h3>
        <article v-for="event in selected.events" :key="event.event_id" class="follow-up-card">
          <strong>{{ eventLabels[event.event_type] || event.event_type }}</strong>
          <p>{{ event.payload.actor }} · {{ event.occurred_at }} · 修改版本 {{ event.payload.revision }}</p>
          <p v-if="event.payload.comment">{{ event.payload.comment }}</p>
          <details><summary>查看当次提交内容</summary><pre class="handoff-snapshot">{{ JSON.stringify(event.payload.document, null, 2) }}</pre></details>
        </article>
      </section>
    </section>
  </section>
</template>

<style scoped>
.handoff-fields { border: 0; padding: 0; margin: 0 0 12px; min-width: 0; }
.handoff-queue { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 16px; }
.handoff-customer-select { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
select { max-width: 100%; padding: 8px; border: 1px solid #dfe6e5; border-radius: 4px; }
.handoff-attestation { display: flex; align-items: flex-start; gap: 8px; margin: 14px 0; font-size: 12px; line-height: 1.6; }
.handoff-snapshot { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 11px; }
</style>
