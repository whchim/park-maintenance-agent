<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { AlertCircle, Bot, CheckCircle2, ClipboardCheck, RefreshCw } from 'lucide-vue-next'
import AiModeNotice from './AiModeNotice.vue'
import {
  completeFollowUp, getFollowUpTasks, getFollowUpRecords, confirmInvestmentIntake,
  parseInvestmentIntake, getInvestmentCustomers,
  type AiRuntime, type AgentCapability, type InvestmentDraft, type InvestmentConfirmation,
  type FollowUpTask, type FollowUpRecord, type CustomerSummary,
} from '../api'

const props = defineProps<{ active: boolean; aiRuntime: AiRuntime | null; capabilities: AgentCapability[] }>()
const emit = defineEmits<{ handoffs: [] }>()
const customers = ref<CustomerSummary[]>([])
const customerSearch = ref('')
const customersLoading = ref(false)
const customersError = ref('')
const filteredCustomers = computed(() => {
  const query = customerSearch.value.trim().toLowerCase()
  return customers.value.filter(customer => !query || [customer.customer_name, customer.customer_no]
    .some(value => value.toLowerCase().includes(query)))
})
const stageLabels: Record<string, string> = { lead: '意向客户', signed: '已签约', moved_in: '已入驻', lost: '已流失' }

const investmentInput = ref('海星科技计划租用园区办公室，联系人李先生，电话13812345678')
const investmentSource = ref('官网咨询')
const investmentLoading = ref(false)
const investmentError = ref('')
const investmentDraft = ref<InvestmentDraft | null>(null)
const investmentConfirmation = ref<InvestmentConfirmation | null>(null)
const investmentIntentLevel = ref<'A' | 'B' | 'C'>('B')
const followUpTasks = ref<FollowUpTask[]>([])
const followUpRecords = ref<FollowUpRecord[]>([])
const followUpCustomerName = ref('')
const followUpLoading = ref(false)
const followUpError = ref('')
const followUpNotice = ref('')
const followUpActor = ref('招商演示员')
const followUpOutcomes = ref<Record<number, string>>({})
// 请求失败后保留本次动作的键，重试不会创建第二份回访记录。
const followUpKeys = new Map<number, string>()

async function loadFollowUpTasks() {
  followUpLoading.value = true
  followUpError.value = ''
  try {
    followUpTasks.value = await getFollowUpTasks()
  } catch (cause) {
    followUpError.value = getErrorMessage(cause, '回访任务加载失败')
  } finally {
    followUpLoading.value = false
  }
}

async function loadFollowUpHistory(task: Pick<FollowUpTask, 'customer_id' | 'customer_name'>) {
  followUpError.value = ''
  followUpRecords.value = []
  followUpCustomerName.value = task.customer_name
  try {
    followUpRecords.value = await getFollowUpRecords(task.customer_id)
  } catch (cause) {
    followUpError.value = getErrorMessage(cause, '回访记录加载失败')
  }
}

async function submitFollowUp(task: FollowUpTask) {
  const outcome = (followUpOutcomes.value[task.id] || '').trim()
  if (!outcome || !followUpActor.value.trim()) {
    followUpError.value = '请填写回访人员和实际回访结果'
    return
  }
  followUpLoading.value = true
  followUpError.value = ''
  followUpNotice.value = ''
  const key = followUpKeys.get(task.id) || crypto.randomUUID()
  followUpKeys.set(task.id, key)
  try {
    const result = await completeFollowUp(task.id, {
      actor: followUpActor.value.trim(), outcome, idempotency_key: key,
    })
    followUpNotice.value = result.next_task
      ? `${task.customer_name}回访已记录，下次回访：${formatDate(result.next_task.due_at)}`
      : `${task.customer_name}回访已记录，未安排后续任务`
    await loadFollowUpTasks()
    await loadFollowUpHistory(task)
  } catch (cause) {
    followUpError.value = getErrorMessage(cause, '回访提交失败')
  } finally {
    followUpLoading.value = false
  }
}

async function runInvestmentIntake() {
  if (investmentLoading.value) return
  if (!investmentInput.value.trim()) {
    investmentError.value = '请输入客户意向'
    return
  }
  investmentLoading.value = true
  investmentError.value = ''
  try {
    investmentDraft.value = await parseInvestmentIntake(investmentInput.value, investmentSource.value)
    investmentIntentLevel.value = investmentDraft.value.intent_level || 'B'
    investmentConfirmation.value = null
  } catch (cause) {
    investmentError.value = getErrorMessage(cause, '招商 Agent 解析失败')
  } finally {
    investmentLoading.value = false
  }
}

async function confirmInvestment() {
  if (investmentLoading.value) return
  if (!investmentDraft.value?.customer_name) {
    investmentError.value = '客户名称缺失，不能创建客户档案'
    return
  }
  investmentLoading.value = true
  investmentError.value = ''
  try {
    investmentConfirmation.value = await confirmInvestmentIntake({
      customer_name: investmentDraft.value.customer_name,
      contact_name: investmentDraft.value.contact_name,
      contact_phone: investmentDraft.value.contact_phone,
      source: investmentDraft.value.source,
      owner: '招商演示员',
      intent_level: investmentIntentLevel.value,
      follow_up_subject: investmentDraft.value.follow_up_subject,
      follow_up_description: investmentDraft.value.follow_up_description,
    })
    await Promise.all([loadFollowUpTasks(), loadCustomers()])
  } catch (cause) {
    investmentError.value = getErrorMessage(cause, '客户确认失败')
  } finally {
    investmentLoading.value = false
  }
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value))
}

function getErrorMessage(cause: unknown, fallback: string) {
  return cause instanceof Error ? cause.message : fallback
}

async function loadCustomers() {
  customersLoading.value = true
  customersError.value = ''
  try { customers.value = await getInvestmentCustomers() }
  catch (cause) { customersError.value = getErrorMessage(cause, '客户列表加载失败') }
  finally { customersLoading.value = false }
}

function refreshWorkspace() {
  if (customersLoading.value || followUpLoading.value || investmentLoading.value) return
  void Promise.all([loadCustomers(), loadFollowUpTasks()])
}

function newCustomerDraft() {
  if (investmentLoading.value) return
  investmentInput.value = ''
  investmentDraft.value = null
  investmentConfirmation.value = null
  investmentIntentLevel.value = 'B'
  investmentError.value = ''
}

// 工作区首次进入和再次进入时只读取最新列表，不清空正在编辑的客户或回访内容。
watch(() => props.active, active => { if (active) refreshWorkspace() }, { immediate: true })
</script>

<template>
  <section class="business-workspace" aria-label="招商客户工作区">
    <div class="workspace-overview">
      <p>招商负责客户建档与跟进；确认签约后，通过客户交接将最终版资料提交企服。</p>
      <div class="workspace-actions">
        <button class="secondary-button" type="button" @click="emit('handoffs')"><ClipboardCheck :size="15" />前往客户交接</button>
        <button class="secondary-button" type="button" :disabled="customersLoading || followUpLoading || investmentLoading" @click="refreshWorkspace"><RefreshCw :size="15" :class="{ spinning: customersLoading || followUpLoading }" />刷新列表</button>
      </div>
    </div>
    <div class="investment-layout">
      <section class="business-card" aria-labelledby="investment-intake-title">
        <div class="card-heading"><div><div class="eyebrow">招商 Agent</div><h2 id="investment-intake-title">客户意向整理</h2></div><span class="section-tag">人工确认后建档</span></div>
          <AiModeNotice :result="investmentDraft" :runtime="aiRuntime" />
          <p class="muted">客户档案、意向等级和跟进任务需要招商人员核对确认。</p>
          <label class="coordinator-input"><span>客户意向记录</span><textarea v-model="investmentInput" rows="4" placeholder="记录客户、联系人、需求或来源"></textarea></label>
          <label class="coordinator-input"><span>线索来源</span><input v-model="investmentSource" placeholder="官网咨询、渠道推荐等" /></label>
          <div class="coordinator-actions"><span class="muted">{{ investmentDraft ? `缺失字段：${investmentDraft.missing_fields.length ? investmentDraft.missing_fields.join('、') : '无'}` : '招商 Agent 草稿阶段不写数据库' }}</span><button class="primary-button" type="button" :disabled="investmentLoading" @click="runInvestmentIntake"><Bot :size="15" />{{ investmentLoading ? '处理中…' : '生成客户草稿' }}</button></div>
          <div v-if="investmentError" class="notice error-notice" role="alert"><AlertCircle :size="16" />{{ investmentError }}</div>
          <section v-if="investmentDraft" class="coordination-result">
            <p v-if="investmentDraft.intent_reason" class="coordination-summary">意向建议依据：{{ investmentDraft.intent_reason }}</p>
            <div class="workflow-draft">
              <div><span>客户名称</span><strong>{{ investmentDraft.customer_name || '待补充' }}</strong></div>
              <div><span>联系人电话</span><strong>{{ investmentDraft.contact_phone || '待补充' }}</strong></div>
              <label><span>人工确认意向等级</span><select v-model="investmentIntentLevel"><option value="A">A · 明确意向 · 每日回访</option><option value="B">B · 考虑中 · 3 日回访</option><option value="C">C · 意向不明确 · 7 日回访</option></select></label>
              <div class="form-wide"><span>跟进任务</span><strong>{{ investmentDraft.follow_up_subject }}</strong><small>{{ investmentDraft.follow_up_description }}</small></div>
            </div>
            <button v-if="!investmentConfirmation" class="primary-button" type="button" :disabled="investmentLoading || !investmentDraft.customer_name" @click="confirmInvestment"><CheckCircle2 :size="15" />人工确认并创建跟进任务</button>
            <div v-else class="coordination-summary"><CheckCircle2 :size="18" /><div><strong>意向客户和跟进任务已创建</strong><p>{{ investmentConfirmation.customer.customer_no }} · 跟进任务 {{ investmentConfirmation.follow_up_task.task_no }}。签约后从“客户交接”提交正式联系单。</p></div></div>
            <button v-if="investmentConfirmation" class="secondary-button" type="button" @click="newCustomerDraft">录入下一位客户</button>
          </section>
      </section>
      <section class="business-card follow-up-workspace" aria-label="客户回访">
            <div class="agent-trace-heading">
              <h3>待回访客户</h3>
              <button class="secondary-button" type="button" :disabled="followUpLoading" @click="loadFollowUpTasks">刷新任务</button>
            </div>
            <label class="coordinator-input"><span>回访人员（演示身份）</span><input v-model="followUpActor" :disabled="followUpLoading" /></label>
            <p v-if="followUpError" class="notice error-notice" role="alert">{{ followUpError }}</p>
            <p v-if="followUpNotice" class="coordination-summary" role="status">{{ followUpNotice }}</p>
            <p v-if="followUpLoading" class="muted" role="status">正在处理回访任务…</p>
            <p v-else-if="!followUpTasks.length && !followUpError" class="muted">暂无待回访任务</p>
            <article v-for="task in followUpTasks" :key="task.id" class="follow-up-card">
              <div class="agent-step-title"><strong>{{ task.customer_name }}</strong><span class="success-label">{{ task.intent_level || '未分级' }}</span></div>
              <p>{{ task.subject }}</p>
              <p class="muted">{{ task.task_no }} · 到期：{{ task.due_at ? formatDate(task.due_at) : '未设置' }} · 负责人：{{ task.owner || '未指定' }}</p>
              <label class="coordinator-input"><span>实际回访结果</span><textarea v-model="followUpOutcomes[task.id]" :disabled="followUpLoading" rows="2" placeholder="填写实际联系情况、客户反馈和后续事项"></textarea></label>
              <div class="coordinator-actions">
                <button class="secondary-button" type="button" :disabled="followUpLoading" @click="loadFollowUpHistory(task)">查看回访记录</button>
                <button class="primary-button" type="button" :disabled="followUpLoading" @click="submitFollowUp(task)">完成回访并安排下次</button>
              </div>
            </article>
            <section v-if="followUpCustomerName" class="follow-up-history" aria-label="回访历史">
              <h3>{{ followUpCustomerName }} · 回访记录</h3>
              <p v-if="!followUpRecords.length" class="muted">暂无已记录的回访结果</p>
              <article v-for="record in followUpRecords" :key="record.id" class="follow-up-card">
                <p>{{ record.outcome }}</p><small>{{ record.actor }} · {{ formatDate(record.occurred_at) }}</small>
              </article>
            </section>
          </section>

    </div>
    <section class="business-card customer-registry" aria-labelledby="customer-registry-title">
      <div class="card-heading"><h2 id="customer-registry-title">客户档案</h2><span class="muted">{{ customers.length }} 位客户</span></div>
      <label class="search-field"><input v-model="customerSearch" aria-label="搜索客户" placeholder="搜索客户名称或编号" /></label>
      <p v-if="customersError" class="notice error-notice" role="alert">{{ customersError }}</p>
      <p v-if="customersLoading" class="muted" role="status">正在加载客户…</p>
      <div v-else-if="filteredCustomers.length" class="customer-table-scroll">
        <table class="customer-table"><thead><tr><th scope="col">客户名称</th><th scope="col">客户编号</th><th scope="col">业务阶段</th><th scope="col">回访</th></tr></thead>
          <tbody><tr v-for="customer in filteredCustomers" :key="customer.id"><td>{{ customer.customer_name }}</td><td>{{ customer.customer_no }}</td><td><span class="section-tag">{{ stageLabels[customer.stage] || customer.stage }}</span></td><td><button class="secondary-button" type="button" :disabled="followUpLoading" @click="loadFollowUpHistory({ customer_id: customer.id, customer_name: customer.customer_name })">查看记录</button></td></tr></tbody>
        </table>
      </div>
      <p v-else-if="!customersError" class="empty-state">{{ customerSearch.trim() ? '没有匹配的客户' : '暂无客户；在上方整理意向并人工确认后建立档案。' }}</p>
    </section>
    <details v-if="capabilities.length" class="business-card agent-capability-list">
      <summary>三部门 Agent 能力状态</summary>
      <div v-for="capability in capabilities" :key="capability.agent_name" class="capability-row"><strong>{{ capability.department }}</strong><span>{{ capability.status }}</span><small>{{ capability.capabilities.join(' · ') }}</small></div>
    </details>
  </section>
</template>
