<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import CustomerHandoffPanel from './components/CustomerHandoffPanel.vue'
import InvestmentWorkspace from './components/InvestmentWorkspace.vue'
import { useWorkspaceNavigation, workspaces } from './composables/useWorkspaceNavigation'
import AiModeNotice from './components/AiModeNotice.vue'
import AgentTrace from './components/AgentTrace.vue'
import ImageUpload from './components/ImageUpload.vue'
import AcceptanceHistory from './components/AcceptanceHistory.vue'
import AcceptanceForm from './components/AcceptanceForm.vue'
import {
  AlertCircle,
  ArrowDownUp,
  Bot,
  CheckCircle2,
  ClipboardList,
  ClipboardCheck,
  Clock3,
  Play,
  Plus,
  RefreshCw,
  Search,
  Send,
  UserRound,
  Wrench,
  X,
} from 'lucide-vue-next'
import {
  acceptTicket,
  assignTicket,
  confirmRepairWorkflow,
  continueRepairWorkflow,
  createTicket,
  getAgentCapabilities,
  getAgentRuntime,
  getDispatchSuggestion,
  getRepairPersonnel,
  parseRepair,
  getTicketEvents,
  getTicketImages,
  getTicketRepairs,
  getTicketAcceptances,
  getTickets,
  startProcessing,
  submitAcceptance,
  submitRepair,
  startRepairWorkflow,
  type RepairPersonnel,
  type RepairParseResult,
  type AiRuntime,
  type DispatchSuggestion,
  type Priority,
  type AgentCapability,
  type WorkflowRun,
  type Ticket,
  type TicketEvent,
  type ImageAttachment,
  type RepairFeedback,
  type AcceptanceRecord,
} from './api'

const { activeWorkspace, visitedWorkspaces, navigate } = useWorkspaceNavigation()
const currentWorkspace = computed(() => workspaces[activeWorkspace.value])

const tickets = ref<Ticket[]>([])
const events = ref<TicketEvent[]>([])
const personnel = ref<RepairPersonnel[]>([])
const selectedId = ref<number | null>(null)
const search = ref('')
const loadingTickets = ref(true)
const loadingEvents = ref(false)
const syncError = ref('')
let ticketRequestVersion = 0
let eventRequestVersion = 0
let backgroundRefreshing = false
let syncTimer: ReturnType<typeof setInterval> | undefined
const error = ref('')
const actionError = ref('')
const actionLoading = ref(false)
const showCreateForm = ref(false)
const showCoordinator = ref(false)
const coordinatorInput = ref('A栋三楼卫生间漏水，水滴到插座附近，请尽快处理。')
const coordinatorLoading = ref(false)
const coordinatorError = ref('')
const workflowResult = ref<WorkflowRun | null>(null)
const workflowPriority = ref<Priority | ''>('')
const intakeImages = ref<ImageAttachment[]>([])
const intakeUploading = ref(false)
const createImages = ref<ImageAttachment[]>([])
const createUploading = ref(false)
const repairImages = ref<ImageAttachment[]>([])
const repairUploading = ref(false)
const ticketImages = ref<ImageAttachment[]>([])
const repairFeedback = ref<RepairFeedback[]>([])
const acceptanceHistory = ref<AcceptanceRecord[]>([])
const acceptanceHistoryError = ref('')
const capabilities = ref<AgentCapability[]>([])
const aiRuntime = ref<AiRuntime | null>(null)
const outdatedBackend = computed(() => aiRuntime.value !== null && !aiRuntime.value.api_features?.includes('image_attachments'))
const dispatchSuggestion = ref<DispatchSuggestion | null>(null)
const dispatchLoading = ref(false)
const aiInput = ref('')
const aiLoading = ref(false)
const aiResult = ref<RepairParseResult | null>(null)
const assignPersonnelNo = ref('')
const repairDescription = ref('')
const repairPhotoUrls = ref('')
const createForm = ref({
  location: '',
  issue_type: '',
  description: '',
  priority: 'normal' as Priority,
})

const selectedTicket = computed(() => tickets.value.find((ticket) => ticket.id === selectedId.value) ?? null)
const filteredTickets = computed(() => {
  const query = search.value.trim().toLowerCase()
  if (!query) return tickets.value
  return tickets.value.filter((ticket) =>
    [ticket.ticket_no, ticket.location, ticket.issue_type, ticket.description]
      .some((value) => value.toLowerCase().includes(query)),
  )
})
const personnelByNo = computed(() =>
  new Map(personnel.value.map((person) => [person.personnel_no, person.display_name])),
)
const selectedAssignee = computed(() => selectedTicket.value?.assignee ?? '')

const statusLabels: Record<string, string> = {
  pending: '待接单',
  accepted: '已接单',
  processing: '处理中',
  pending_review: '待验收',
  completed: '已完成',
}

const priorityLabels: Record<string, string> = {
  normal: '普通',
  urgent: '加急',
  critical: '特急',
}

const eventLabels: Record<string, string> = {
  pending: '工单已创建',
  accepted: '工程人员已接单',
  processing: '开始处理',
  pending_review: '维修结果已提交',
  completed: '验收完成',
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

function formatActor(event: TicketEvent) {
  if (event.actor_type === 'migration') return '历史记录'
  if (event.actor_type === 'person' && event.actor_id) {
    const displayName = personnelByNo.value.get(event.actor_id)
    return displayName ? `${displayName} · ${event.actor_id}` : event.actor_id
  }
  return event.actor_id || event.actor_type
}

async function loadTickets(silent = false) {
  const version = ++ticketRequestVersion
  if (!silent) { loadingTickets.value = true; error.value = '' }
  try {
    const nextTickets = await getTickets()
    if (version !== ticketRequestVersion) return false
    tickets.value = nextTickets
    if (!tickets.value.some((ticket) => ticket.id === selectedId.value)) {
      selectedId.value = tickets.value[0]?.id ?? null
    }
    return true
  } catch (cause) {
    if (version !== ticketRequestVersion) return false
    if (silent) syncError.value = '自动同步失败，请点击刷新重试'
    else error.value = cause instanceof Error ? cause.message : '工单列表加载失败'
    return false
  } finally {
    if (version === ticketRequestVersion && !silent) loadingTickets.value = false
  }
}

async function loadPersonnel() {
  try {
    personnel.value = await getRepairPersonnel()
  } catch {
    // 工程人员目录只影响显示名称，目录不可用时仍保留稳定编号
    personnel.value = []
  }
}

function getErrorMessage(cause: unknown, fallback: string) {
  return cause instanceof Error ? cause.message : fallback
}

async function runAction(action: () => Promise<unknown>) {
  actionLoading.value = true
  actionError.value = ''
  try {
    await action()
    await loadTickets()
    if (selectedId.value !== null) await loadEvents(selectedId.value)
    return true
  } catch (cause) {
    actionError.value = getErrorMessage(cause, '操作失败，请稍后重试')
    return false
  } finally {
    actionLoading.value = false
  }
}

async function createNewTicket() {
  if (createUploading.value || actionLoading.value) return
  actionLoading.value = true
  actionError.value = ''
  try {
    const ticket = await createTicket({ ...createForm.value, attachment_ids: createImages.value.map(image => image.id) })
    createImages.value = []
    createForm.value = { location: '', issue_type: '', description: '', priority: 'normal' }
    showCreateForm.value = false
    await loadTickets()
    selectedId.value = ticket.id
    await loadEvents(ticket.id)
  } catch (cause) {
    actionError.value = getErrorMessage(cause, '工单创建失败，请检查填写内容')
  } finally {
    actionLoading.value = false
  }
}

async function parseRepairDescription() {
  if (!aiInput.value.trim()) {
    actionError.value = '请先输入报修描述'
    return
  }
  aiLoading.value = true
  actionError.value = ''
  try {
    const result = await parseRepair(aiInput.value)
    aiResult.value = result
    createForm.value.description = result.description
    if (result.location) createForm.value.location = result.location
    if (result.issue_type) createForm.value.issue_type = result.issue_type
    if (result.suggested_priority) createForm.value.priority = result.suggested_priority
  } catch (cause) {
    actionError.value = getErrorMessage(cause, '智能解析失败，请改用手动填写')
  } finally {
    aiLoading.value = false
  }
}

function closeCreateForm() {
  if (createUploading.value || actionLoading.value) return
  showCreateForm.value = false
  aiInput.value = ''
  aiResult.value = null
  createImages.value = []
}

async function runCoordination() {
  if (intakeUploading.value || coordinatorLoading.value) return
  if (!coordinatorInput.value.trim() && !intakeImages.value.length) {
    coordinatorError.value = '请输入需要协调的业务内容'
    return
  }
  coordinatorLoading.value = true
  coordinatorError.value = ''
  try {
    if (!workflowResult.value) {
      workflowResult.value = await startRepairWorkflow(
        coordinatorInput.value,
        `demo-${Date.now()}`,
        intakeImages.value.map(image => image.id),
      )
      coordinatorInput.value = ''
      intakeImages.value = []
    } else if (workflowResult.value.status === 'waiting_input') {
      workflowResult.value = await continueRepairWorkflow(
        workflowResult.value.workflow_id,
        coordinatorInput.value,
        intakeImages.value.map(image => image.id),
      )
      coordinatorInput.value = ''
      intakeImages.value = []
    }
  } catch (cause) {
    coordinatorError.value = getErrorMessage(cause, '协同执行失败')
  } finally {
    coordinatorLoading.value = false
  }
}

async function confirmWorkflowTicket() {
  if (!workflowResult.value) return
  const draft = workflowResult.value.draft
  if (!draft.location || !draft.issue_type || !draft.description || !workflowPriority.value) {
    coordinatorError.value = '结构化信息仍不完整，不能确认建单'
    return
  }
  coordinatorLoading.value = true
  coordinatorError.value = ''
  try {
    workflowResult.value = await confirmRepairWorkflow(workflowResult.value.workflow_id, {
      location: draft.location,
      issue_type: draft.issue_type,
      description: draft.description,
      priority: workflowPriority.value,
    })
    await loadTickets()
    if (workflowResult.value.ticket_id !== null) {
      selectedId.value = workflowResult.value.ticket_id
      await loadEvents(workflowResult.value.ticket_id)
    }
  } catch (cause) {
    coordinatorError.value = getErrorMessage(cause, '确认建单失败')
  } finally {
    coordinatorLoading.value = false
  }
}

function resetCoordinator() {
  if (intakeUploading.value || coordinatorLoading.value) return
  showCoordinator.value = false
  coordinatorError.value = ''
  workflowResult.value = null
  workflowPriority.value = ''
  intakeImages.value = []
  coordinatorInput.value = 'A栋三楼卫生间漏水，水滴到插座附近，请尽快处理。'
}

function workflowStatusLabel(status: WorkflowRun['status']) {
  const labels: Record<WorkflowRun['status'], string> = {
    waiting_input: '等待补充信息',
    waiting_human: '等待人工确认',
    ready_for_service: '已确认，已交给工单服务',
    completed: '流程完成',
    failed: '流程失败',
  }
  return labels[status]
}

function workflowEventLabel(eventType: string) {
  const labels: Record<string, string> = {
    'repair.intake.received': '飞书入口收到报修消息',
    'repair.intake.reviewed': '企服 Agent 完成信息完整性初审',
    'repair.intake.supplemented': '提交人补充报修信息',
    'repair.ticket.confirmed': '人工确认并创建维修工单',
  }
  return labels[eventType] || eventType
}

function assignSelectedTicket() {
  if (selectedId.value === null || !assignPersonnelNo.value) {
    actionError.value = '请先选择工程人员'
    return
  }
  void runAction(() => assignTicket(selectedId.value as number, assignPersonnelNo.value))
}

async function suggestDispatch() {
  if (selectedId.value === null || dispatchLoading.value) return
  const ticketId = selectedId.value
  dispatchLoading.value = true
  actionError.value = ''
  try {
    const result = await getDispatchSuggestion(ticketId)
    // 等待期间切换了工单时，不把旧工单的建议显示到新工单。
    if (selectedId.value === ticketId) dispatchSuggestion.value = result
  } catch (cause) {
    if (selectedId.value === ticketId) actionError.value = getErrorMessage(cause, '工程建议生成失败')
  } finally { dispatchLoading.value = false }
}

function acceptSelectedTicket() {
  if (selectedId.value === null || !selectedAssignee.value) {
    actionError.value = '请先分派工程人员'
    return
  }
  void runAction(() => acceptTicket(selectedId.value as number, selectedAssignee.value))
}

function startSelectedTicket() {
  if (selectedId.value === null || !selectedAssignee.value) return
  void runAction(() => startProcessing(selectedId.value as number, selectedAssignee.value))
}

function submitRepairResult() {
  if (repairUploading.value || actionLoading.value) return
  if (selectedId.value === null || !selectedAssignee.value || !repairDescription.value.trim()) {
    actionError.value = '请填写维修说明'
    return
  }
  const photoUrls = repairPhotoUrls.value
    .split('\n')
    .map((url) => url.trim())
    .filter(Boolean)
  const ticketId = selectedId.value
  void runAction(() => submitRepair(ticketId, {
    personnel_no: selectedAssignee.value,
    repair_description: repairDescription.value,
    photo_urls: photoUrls,
    attachment_ids: repairImages.value.map(image => image.id),
  })).then((succeeded) => {
    if (succeeded && selectedId.value === ticketId) { repairImages.value = []; repairPhotoUrls.value = ''; repairDescription.value = '' }
  })
}

function submitAcceptanceDecision(payload: { accepted: boolean; acceptance_comment: string; accepted_by: string }) {
  if (actionLoading.value || selectedId.value === null) return
  const ticketId = selectedId.value
  void runAction(() => submitAcceptance(ticketId, payload))
}

async function loadEvents(ticketId: number, silent = false) {
  const version = ++eventRequestVersion
  if (!silent) { loadingEvents.value = true; error.value = '' }
  try {
    // 历史接口不可用时单独提示，避免旧后端导致整个工单详情无法展示。
    const [nextEvents, nextImages, nextRepairs, nextAcceptances] = await Promise.all([
      getTicketEvents(ticketId), getTicketImages(ticketId), getTicketRepairs(ticketId),
      getTicketAcceptances(ticketId)
        .then(records => ({ records, error: '' }))
        .catch(cause => ({ records: [] as AcceptanceRecord[], error: getErrorMessage(cause, '验收记录加载失败') })),
    ])
    if (selectedId.value !== ticketId || version !== eventRequestVersion) return false
    events.value = nextEvents
    ticketImages.value = nextImages
    repairFeedback.value = nextRepairs
    acceptanceHistory.value = nextAcceptances.records
    acceptanceHistoryError.value = nextAcceptances.error
    return true
  } catch (cause) {
    if (selectedId.value !== ticketId || version !== eventRequestVersion) return false
    if (silent) syncError.value = '工单详情同步失败，请点击刷新重试'
    else {
      events.value = []
      acceptanceHistory.value = []
      acceptanceHistoryError.value = '工单详情加载失败，无法读取验收记录'
      error.value = cause instanceof Error ? cause.message : '事件时间线加载失败'
    }
    return false
  } finally {
    if (version === eventRequestVersion && !silent) loadingEvents.value = false
  }
}

async function refresh() {
  if (await loadTickets()) {
    if (selectedId.value === null || await loadEvents(selectedId.value)) syncError.value = ''
  }
}

async function refreshExternalChanges() {
  if (document.visibilityState === 'hidden' || activeWorkspace.value !== 'repairs'
      || backgroundRefreshing || loadingTickets.value || loadingEvents.value || actionLoading.value) return
  backgroundRefreshing = true
  const previousTicket = JSON.stringify(selectedTicket.value)
  const previousAssignee = selectedAssignee.value
  try {
    if (!await loadTickets(true)) return
    // 外部状态变更只更新展示，不清空尚未提交的维修说明和图片。
    if (JSON.stringify(selectedTicket.value) !== previousTicket) dispatchSuggestion.value = null
    if (selectedAssignee.value !== previousAssignee) assignPersonnelNo.value = selectedAssignee.value
    if (selectedId.value !== null && !await loadEvents(selectedId.value, true)) return
    syncError.value = ''
  } finally { backgroundRefreshing = false }
}

watch(activeWorkspace, (workspace) => {
  if (workspace === 'repairs') void refreshExternalChanges()
})

watch(selectedId, (id) => {
  actionError.value = ''
  repairImages.value = []
  repairPhotoUrls.value = ''
  repairDescription.value = ''
  ticketImages.value = []
  repairFeedback.value = []
  acceptanceHistory.value = []
  acceptanceHistoryError.value = ''
  dispatchSuggestion.value = null
  if (id !== null) {
    const ticket = tickets.value.find((item) => item.id === id)
    assignPersonnelNo.value = ticket?.assignee ?? ''
  }
  if (id !== null) void loadEvents(id)
  else events.value = []
})

onMounted(() => {
  void loadTickets()
  void loadPersonnel()
  void getAgentCapabilities().then((result) => { capabilities.value = result }).catch(() => { capabilities.value = [] })
  void getAgentRuntime().then((result) => { aiRuntime.value = result }).catch(() => { aiRuntime.value = null })
  syncTimer = setInterval(() => { void refreshExternalChanges() }, 5000)
  window.addEventListener('focus', refreshExternalChanges)
  document.addEventListener('visibilitychange', refreshExternalChanges)
})

onBeforeUnmount(() => {
  clearInterval(syncTimer)
  window.removeEventListener('focus', refreshExternalChanges)
  document.removeEventListener('visibilitychange', refreshExternalChanges)
  ++ticketRequestVersion
  ++eventRequestVersion
})
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <div class="brand-lockup">
        <div class="brand-mark"><Wrench :size="18" /></div>
        <div>
          <div class="brand-name">园区智维</div>
          <div class="brand-subtitle">运营协同平台</div>
        </div>
      </div>

      <div class="nav-label">业务工作区</div>
      <nav class="workspace-nav" aria-label="业务工作区">
        <a class="nav-item" :class="{ active: activeWorkspace === 'repairs' }" :href="workspaces.repairs.hash"
          :aria-current="activeWorkspace === 'repairs' ? 'page' : undefined" title="维修协同" @click.prevent="navigate('repairs')">
          <ClipboardList :size="17" /><span>维修协同</span><span class="nav-count">{{ tickets.length }}</span>
        </a>
        <a class="nav-item" :class="{ active: activeWorkspace === 'investment' }" :href="workspaces.investment.hash"
          :aria-current="activeWorkspace === 'investment' ? 'page' : undefined" title="招商客户" @click.prevent="navigate('investment')">
          <UserRound :size="17" /><span>招商客户</span>
        </a>
        <a class="nav-item" :class="{ active: activeWorkspace === 'handoffs' }" :href="workspaces.handoffs.hash"
          :aria-current="activeWorkspace === 'handoffs' ? 'page' : undefined" title="客户交接" @click.prevent="navigate('handoffs')">
          <ClipboardCheck :size="17" /><span>客户交接</span>
        </a>
      </nav>

      <div class="sidebar-footer">
        <span class="status-dot"></span>
        <span>服务运行中</span>
        <span class="api-address">{{ aiRuntime?.environment?.name === 'isolated_demo' ? '隔离演示' : aiRuntime?.environment?.name === 'portfolio_demo' ? '求职演示' : '业务后端' }}</span>
      </div>
    </aside>

    <main class="main-area">
      <div v-if="aiRuntime?.environment?.name === 'isolated_demo'" class="demo-environment" role="status">
        隔离演示 · 仅使用模拟数据，重启后清空。请勿录入真实客户或人员资料。
      </div>
      <div v-else-if="aiRuntime?.environment?.name === 'portfolio_demo'" class="demo-environment" role="status">
        求职演示 · 仅使用模拟数据，访问者共享演示记录，数据会保留。请勿录入真实客户或人员资料。
      </div>
      <header class="topbar">
        <div class="breadcrumbs"><span>运营协同</span><span class="crumb-divider">/</span><strong>{{ currentWorkspace.title }}</strong></div>
        <div v-if="activeWorkspace === 'repairs'" class="topbar-actions">
          <button class="secondary-button" type="button" @click="showCoordinator = true">
            <Bot :size="15" />
            AI 报修受理
          </button>
          <button class="secondary-button" type="button" @click="showCreateForm = !showCreateForm">
            <Plus :size="15" />
            新建工单
          </button>
          <button class="icon-button" type="button" title="刷新工单" aria-label="刷新工单" @click="refresh">
            <RefreshCw :size="17" :class="{ spinning: loadingTickets || loadingEvents }" />
          </button>
        </div>
      </header>
      <div v-if="outdatedBackend" class="notice error-notice" role="alert">
        当前服务未声明图片与维修查询能力，请检查前后端版本是否配套，更新后刷新页面。
      </div>

      <section class="page-heading">
        <div>
          <h1>{{ currentWorkspace.title }}</h1>
          <p>{{ currentWorkspace.description }}</p>
        </div>
        <div class="heading-meta"><span class="live-indicator"></span>{{ currentWorkspace.collaboration }}<span v-if="activeWorkspace === 'repairs'" role="status"> · {{ syncError || '每5秒自动同步' }}</span></div>
      </section>

      <div v-if="error && activeWorkspace === 'repairs'" class="notice error-notice" role="alert">
        <AlertCircle :size="17" />
        <span>{{ error }}</span>
        <button type="button" class="text-button" @click="refresh">重试</button>
      </div>

      <div v-if="showCreateForm" v-show="activeWorkspace === 'repairs'" class="modal-backdrop" role="presentation" @click.self="closeCreateForm" @keydown.esc="closeCreateForm">
        <form class="create-modal" role="dialog" aria-modal="true" aria-labelledby="create-ticket-title" @submit.prevent="createNewTicket">
          <div class="modal-header">
            <div>
              <div class="eyebrow">企服智能报修</div>
              <h2 id="create-ticket-title">发起维修工单</h2>
              <p>用一句话描述问题，系统自动整理工单字段</p>
            </div>
            <button class="modal-close" type="button" aria-label="关闭" @click="closeCreateForm"><X :size="16" /></button>
          </div>

          <div class="ai-entry-card">
            <div class="ai-entry-title"><span class="ai-spark">✦</span><strong>报修内容结构化</strong></div>
            <AiModeNotice :result="aiResult" :runtime="aiRuntime" />
            <p>整理报修信息并检查缺项，结果请人工核对后再建单。</p>
            <textarea v-model="aiInput" rows="3" placeholder="例如：A栋三楼卫生间漏水，地面积水，请尽快处理"></textarea>
            <div class="ai-entry-footer">
              <span class="muted">解析结果会自动填入下方工单字段</span>
              <button class="secondary-button" type="button" :disabled="aiLoading" @click="parseRepairDescription">
                <Wrench :size="14" />{{ aiLoading ? '解析中…' : '智能识别' }}
              </button>
            </div>
            <div v-if="aiResult" class="ai-result">
              <span>位置：{{ aiResult.location || '未识别' }}</span>
              <span>类型：{{ aiResult.issue_type || '未识别' }}</span>
              <span>建议优先级：{{ aiResult.suggested_priority ? priorityLabels[aiResult.suggested_priority] : '未识别' }}</span>
              <span v-if="aiResult.missing_fields.length">待补充：{{ aiResult.missing_fields.join('、') }}</span>
            </div>
          </div>

          <div class="form-grid">
            <label><span>报修位置</span><input v-model="createForm.location" required placeholder="例如：A栋三楼卫生间" /></label>
            <label><span>故障类型</span><input v-model="createForm.issue_type" required placeholder="例如：漏水、照明故障" /></label>
            <label><span>确认优先级</span><select v-model="createForm.priority"><option value="normal">普通</option><option value="urgent">加急</option><option value="critical">特急</option></select></label>
            <label class="form-wide"><span>问题描述</span><textarea v-model="createForm.description" required rows="2" placeholder="描述故障现象和影响范围"></textarea></label>
          </div>
          <ImageUpload v-model="createImages" :disabled="actionLoading" @busy-change="createUploading = $event" />
          <p class="muted">请核对建议和工单字段，创建后按确认优先级保存。</p>
          <div class="form-actions"><button class="secondary-button" type="button" :disabled="createUploading || actionLoading" @click="closeCreateForm">取消</button><button class="primary-button" type="submit" :disabled="actionLoading || createUploading"><Send :size="15" />{{ actionLoading ? '提交中…' : '确认并创建工单' }}</button></div>
        </form>
      </div>

      <div v-if="actionError && activeWorkspace === 'repairs' && (selectedTicket?.status !== 'pending_review' || showCreateForm)" class="notice error-notice" role="alert">
        <AlertCircle :size="17" />
        <span>{{ actionError }}</span>
      </div>

      <div v-if="showCoordinator" v-show="activeWorkspace === 'repairs'" class="modal-backdrop" role="presentation" @click.self="resetCoordinator" @keydown.esc="resetCoordinator">
        <section class="coordinator-modal" role="dialog" aria-modal="true" aria-labelledby="coordinator-title">
          <div class="modal-header">
            <div>
              <div class="eyebrow">Supervisor Workflow</div>
              <h2 id="coordinator-title">企服报修协同</h2>
              <p>报修信息进入企服 Agent，补齐信息后由人工确认建单。</p>
            </div>
            <button class="modal-close" type="button" aria-label="关闭" @click="resetCoordinator"><X :size="16" /></button>
          </div>

          <AiModeNotice :result="workflowResult?.draft" :runtime="aiRuntime" />
          <AgentTrace :trace="workflowResult?.draft.orchestration" />
          <p class="muted">AI 辅助整理报修信息；关键动作由人确认。Web 与飞书共用业务后端，建单按人工确认优先级保存。</p>
          <p v-if="workflowResult?.draft.follow_up_question" class="coordination-summary">企服追问：{{ workflowResult.draft.follow_up_question }}</p>
          <label class="coordinator-input">
            <span>{{ workflowResult?.status === 'waiting_input' ? '补充缺失信息' : '报修内容' }}</span>
            <textarea v-model="coordinatorInput" rows="4" placeholder="例如：A栋三楼卫生间漏水，请说明具体位置和影响"></textarea>
          </label>
          <ImageUpload v-if="!workflowResult || workflowResult.status === 'waiting_input'" v-model="intakeImages"
            :disabled="coordinatorLoading" :existing-count="workflowResult?.attachments.length || 0" @busy-change="intakeUploading = $event" />
          <ImageUpload v-if="workflowResult?.attachments.length" :model-value="workflowResult.attachments" readonly title="已保存的报修图片" />
          <div class="coordinator-actions">
            <span class="muted">{{ workflowResult ? workflowStatusLabel(workflowResult.status) : '入口：Web · 引擎：园区业务后端' }}</span>
            <button class="primary-button" type="button" :disabled="coordinatorLoading || intakeUploading || !!workflowResult && workflowResult.status !== 'waiting_input'" @click="runCoordination">
              <Bot :size="15" />{{ coordinatorLoading ? '处理中…' : workflowResult?.status === 'waiting_input' ? '提交补充' : workflowResult ? '已完成初审' : '开始初审' }}
            </button>
          </div>
          <div v-if="coordinatorError" class="notice error-notice" role="alert"><AlertCircle :size="16" />{{ coordinatorError }}</div>

          <section v-if="workflowResult" class="coordination-result">
            <div class="coordination-summary">
              <CheckCircle2 :size="18" />
              <div><strong>{{ workflowStatusLabel(workflowResult.status) }}</strong><p>流程 {{ workflowResult.workflow_id }} · 当前步骤 {{ workflowResult.current_step }}</p></div>
            </div>
            <div class="workflow-draft">
              <label><span>报修位置</span><input v-model="workflowResult.draft.location" :disabled="workflowResult.status !== 'waiting_human'" placeholder="待补充" /></label>
              <label><span>故障类型</span><input v-model="workflowResult.draft.issue_type" :disabled="workflowResult.status !== 'waiting_human'" placeholder="待补充" /></label>
              <div><span>缺失字段</span><strong>{{ workflowResult.missing_fields.length ? workflowResult.missing_fields.join('、') : '无' }}</strong></div>
              <label class="form-wide"><span>结构化描述</span><textarea v-model="workflowResult.draft.description" :disabled="workflowResult.status !== 'waiting_human'" rows="2"></textarea></label>
            </div>
            <details v-if="workflowResult.draft.evidence && Object.keys(workflowResult.draft.evidence).length"><summary>查看提取依据</summary><p v-for="(quote, field) in workflowResult.draft.evidence" :key="field">{{ field === 'location' ? '位置' : '故障' }}：{{ quote }}</p></details>
            <div v-if="workflowResult.status === 'waiting_human'" class="workflow-confirm">
              <label><span>确认优先级</span><select v-model="workflowPriority"><option value="" disabled>请选择</option><option value="normal">普通</option><option value="urgent">加急</option><option value="critical">特急</option></select></label>
              <button class="primary-button" type="button" :disabled="coordinatorLoading" @click="confirmWorkflowTicket"><CheckCircle2 :size="15" />确认并创建工单</button>
            </div>
            <div v-if="workflowResult.ticket" class="created-work-items"><div><span>维修工单</span><strong>{{ workflowResult.ticket.ticket_no }}</strong><small>待工程部接单</small></div></div>
            <div class="agent-trace-heading"><h3>结构化交接事件</h3><span>{{ workflowResult.events.length }} 条</span></div>
            <ol class="agent-trace">
              <li v-for="(event, index) in workflowResult.events" :key="event.event_id" class="agent-step">
                <span class="agent-step-number">{{ index + 1 }}</span>
                <div class="agent-step-body">
                  <div class="agent-step-title"><strong>{{ event.event_type === 'repair.intake.received' ? (workflowResult.channel === 'feishu' ? '飞书入口收到报修消息' : 'Web 入口收到报修消息') : event.event_type === 'repair.assignment.requested' ? '已交接工程部，等待负责人委派' : workflowEventLabel(event.event_type) }}</strong><span class="success-label">{{ event.status }}</span></div>
                  <p>{{ event.source_agent }} → {{ event.target_agent }} · {{ event.requires_human ? '需要人工确认' : '系统处理' }}</p>
                </div>
              </li>
            </ol>
          </section>
        </section>
      </div>

      <InvestmentWorkspace v-if="visitedWorkspaces.has('investment')" v-show="activeWorkspace === 'investment'"
        :active="activeWorkspace === 'investment'" :ai-runtime="aiRuntime" :capabilities="capabilities" @handoffs="navigate('handoffs')" />

      <section v-if="visitedWorkspaces.has('handoffs')" v-show="activeWorkspace === 'handoffs'" class="business-card handoff-workspace" aria-label="客户交接工作区">
        <CustomerHandoffPanel :active="activeWorkspace === 'handoffs'" />
      </section>

      <section v-show="activeWorkspace === 'repairs'" class="workspace" aria-label="维修协同工作区">
        <div class="ticket-panel">
          <div class="panel-heading">
            <div>
              <h2>全部工单</h2>
              <span class="muted">{{ filteredTickets.length }} 条记录</span>
            </div>
            <ArrowDownUp :size="16" class="muted-icon" />
          </div>
          <label class="search-box">
            <Search :size="16" />
            <input v-model="search" type="search" placeholder="搜索编号、位置或内容" />
            <kbd>/</kbd>
          </label>

          <div v-if="loadingTickets" class="empty-state">正在加载工单…</div>
          <div v-else-if="filteredTickets.length === 0" class="empty-state">没有匹配的工单</div>
          <div v-else class="ticket-list">
            <button
              v-for="ticket in filteredTickets"
              :key="ticket.id"
              class="ticket-row"
              :class="{ selected: ticket.id === selectedId }"
              type="button"
              @click="selectedId = ticket.id"
            >
              <div class="ticket-row-top">
                <span class="ticket-number">{{ ticket.ticket_no }}</span>
                <span class="priority-dot" :class="`priority-${ticket.priority}`" :title="priorityLabels[ticket.priority]"></span>
              </div>
              <div class="ticket-location">{{ ticket.location }}</div>
              <div class="ticket-row-bottom">
                <span class="status-label" :class="`status-${ticket.status}`">{{ statusLabels[ticket.status] }}</span>
                <span class="ticket-date">{{ formatDate(ticket.created_at) }}</span>
              </div>
            </button>
          </div>
        </div>

        <section v-if="selectedTicket" class="detail-panel">
          <div class="detail-heading">
            <div class="detail-title-group">
              <div class="eyebrow">工单详情</div>
              <h2>{{ selectedTicket.ticket_no }}</h2>
              <div class="detail-location">{{ selectedTicket.location }} · {{ selectedTicket.issue_type }}</div>
            </div>
            <span class="status-label large" :class="`status-${selectedTicket.status}`">
              {{ statusLabels[selectedTicket.status] }}
            </span>
          </div>

          <div class="summary-grid">
            <div class="summary-item">
              <span class="summary-label">优先级</span>
              <span class="summary-value"><span class="priority-dot" :class="`priority-${selectedTicket.priority}`"></span>{{ priorityLabels[selectedTicket.priority] }}</span>
            </div>
            <div class="summary-item">
              <span class="summary-label">责任人</span>
              <span class="summary-value">{{ selectedTicket.assignee || '待分派' }}</span>
            </div>
            <div class="summary-item">
              <span class="summary-label">创建时间</span>
              <span class="summary-value">{{ formatDate(selectedTicket.created_at) }}</span>
            </div>
          </div>

          <div class="description-block">
            <div class="section-label">问题描述</div>
            <p>{{ selectedTicket.description }}</p>
          </div>

          <section class="workflow-panel">
            <div class="workflow-heading">
              <div>
                <h3>流程操作</h3>
                <span class="muted">按业务角色推进当前工单</span>
              </div>
              <ClipboardCheck :size="17" class="muted-icon" />
            </div>

            <div v-if="selectedTicket.status === 'pending'" class="workflow-step dispatch-step">
              <button class="secondary-button" type="button" :disabled="dispatchLoading || actionLoading" @click="suggestDispatch"><Bot :size="15" />{{ dispatchLoading ? '正在分析…' : '生成工程委派建议' }}</button>
              <section v-if="dispatchSuggestion" class="coordination-result">
                <AiModeNotice :result="dispatchSuggestion" />
                <AgentTrace :trace="dispatchSuggestion.orchestration" />
                <p>建议人员：{{ dispatchSuggestion.recommended_name || dispatchSuggestion.recommended_personnel || '待负责人指定' }}</p>
                <p>{{ dispatchSuggestion.reason }}</p>
                <p class="muted">建议不会自动选中或分派；请负责人核对后在下方操作。</p>
              </section>
              <label>
                <span>分派工程人员</span>
                <select v-model="assignPersonnelNo">
                  <option value="" disabled>请负责人选择工程人员</option>
                  <option v-for="person in personnel" :key="person.personnel_no" :value="person.personnel_no">
                    {{ person.display_name }} · {{ person.personnel_no }}
                  </option>
                </select>
              </label>
              <button class="primary-button" type="button" :disabled="actionLoading || !assignPersonnelNo" @click="assignSelectedTicket">
                <UserRound :size="15" />
                分派工单
              </button>
              <button v-if="selectedAssignee" class="secondary-button" type="button" :disabled="actionLoading" @click="acceptSelectedTicket">
                <CheckCircle2 :size="15" />
                工程人员接单
              </button>
            </div>

            <div v-else-if="selectedTicket.status === 'accepted'" class="workflow-step action-row">
              <div class="action-context"><UserRound :size="16" /><span>当前责任人：{{ personnelByNo.get(selectedAssignee) || selectedAssignee }}</span></div>
              <button class="primary-button" type="button" :disabled="actionLoading" @click="startSelectedTicket">
                <Play :size="15" />
                开始维修
              </button>
            </div>

            <div v-else-if="selectedTicket.status === 'processing'" class="workflow-form">
              <div class="action-context"><UserRound :size="16" /><span>维修人员：{{ personnelByNo.get(selectedAssignee) || selectedAssignee }}</span></div>
              <label>
                <span>维修说明</span>
                <textarea v-model="repairDescription" rows="2" placeholder="描述维修措施和结果"></textarea>
              </label>
              <ImageUpload :key="selectedTicket.id" v-model="repairImages" purpose="repair" title="维修反馈图片（可选）"
                :disabled="actionLoading" @busy-change="repairUploading = $event" />
              <details class="legacy-photo-paths"><summary>兼容已有照片路径</summary>
                <label><span>照片路径（每行一条，可选）</span><textarea v-model="repairPhotoUrls" rows="2" placeholder="photos/before.jpg&#10;photos/after.jpg"></textarea></label>
              </details>
              <button class="primary-button" type="button" :disabled="actionLoading || repairUploading" @click="submitRepairResult">
                <Send :size="15" />
                提交维修结果
              </button>
            </div>

            <AcceptanceForm v-else-if="selectedTicket.status === 'pending_review'" :key="selectedTicket.id"
              :loading="actionLoading" :error="actionError" @decision="submitAcceptanceDecision" />

            <div v-else class="action-context completed-context">
              <CheckCircle2 :size="16" />
              <span>工单已完成，完整处理记录已归档</span>
            </div>
          </section>

          <section v-if="repairFeedback.length" class="repair-feedback-history" aria-label="维修反馈记录">
            <h4>维修反馈记录</h4>
            <article v-for="record in repairFeedback" :key="record.id">
              <span class="muted">维修记录 #{{ record.id }} · {{ formatDate(record.submitted_at) }}</span>
              <p>{{ record.repair_description }}</p>
              <ImageUpload v-if="ticketImages.some(image => image.repair_record_id === record.id)"
                :model-value="ticketImages.filter(image => image.repair_record_id === record.id)" readonly title="本次维修反馈图" />
            </article>
          </section>
          <ImageUpload v-if="ticketImages.some(image => image.purpose === 'intake')"
            :model-value="ticketImages.filter(image => image.purpose === 'intake')" readonly title="报修现场图片" />
          <AcceptanceHistory :records="acceptanceHistory" :loading="loadingEvents" :error="acceptanceHistoryError" />
          <div class="timeline-section">
            <div class="timeline-heading">
              <div>
                <h3>处理时间线</h3>
                <span class="muted">状态流转与操作记录</span>
              </div>
              <Clock3 :size="17" class="muted-icon" />
            </div>

            <div v-if="loadingEvents" class="empty-state timeline-empty">正在加载事件…</div>
            <div v-else-if="events.length === 0" class="empty-state timeline-empty">暂无处理事件</div>
            <ol v-else class="timeline">
              <li v-for="(event, index) in events" :key="event.id" class="timeline-item">
                <span class="timeline-marker" :class="{ last: index === events.length - 1 }"></span>
                <div class="timeline-content">
                  <div class="timeline-line-one">
                    <strong>{{ event.from_status === 'pending_review' && event.to_status === 'processing' ? '验收退回，继续维修' : eventLabels[event.to_status] ?? statusLabels[event.to_status] ?? event.to_status }}</strong>
                    <span v-if="event.is_inferred" class="inferred-label">历史推断</span>
                  </div>
                  <p v-if="event.reason" class="timeline-reason">{{ event.reason }}</p>
                  <div class="timeline-meta">
                    <span>{{ formatActor(event) }}</span>
                    <span class="meta-separator">·</span>
                    <time :datetime="event.occurred_at">{{ formatDate(event.occurred_at) }}</time>
                  </div>
                </div>
              </li>
            </ol>
          </div>
        </section>
        <section v-else class="detail-panel no-selection">
          <ClipboardList :size="28" />
          <p>{{ loadingTickets ? '正在加载工单…' : '选择一条工单查看处理时间线' }}</p>
        </section>
      </section>
    </main>
  </div>
</template>
