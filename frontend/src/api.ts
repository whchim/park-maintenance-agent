export type TicketStatus = 'pending' | 'accepted' | 'processing' | 'pending_review' | 'completed'
export type Priority = 'normal' | 'urgent' | 'critical'

export interface Ticket {
  id: number
  ticket_no: string
  location: string
  issue_type: string
  description: string
  priority: Priority
  status: TicketStatus
  assignee: string | null
  created_at: string
  accepted_at: string | null
  submitted_at: string | null
}

export interface TicketEvent {
  id: number
  ticket_id: number
  from_status: string | null
  to_status: string
  actor_type: string
  actor_id: string | null
  reason: string | null
  occurred_at: string
  is_inferred: boolean
}

export interface RepairPersonnel {
  id: number
  personnel_no: string
  display_name: string
  department: string
  role: string
  skills: string[]
  responsible_area: string | null
  is_active: boolean
  created_at: string
}

export interface RepairParseResult {
  location: string | null
  issue_type: string | null
  description: string
  suggested_priority: Priority | null
  missing_fields: string[]
  mode: string
  ai?: AiMetadata
  follow_up_question: string
  evidence: Record<string, string>
  orchestration?: Orchestration
}

export interface Orchestration {
  framework: string
  graph_version: string
  persistence: string
  model_calls?: number
  tool_calls?: number
  stop_reason?: string
  steps: { node: string; status: string; summary: string; tool?: string; mode?: string }[]
}

export interface AiMetadata {
  agent?: string
  mode?: string
  provider?: string | null
  model?: string | null
  fallback_reason?: string | null
  latency_ms?: number
  usage?: { total_tokens?: number }
}

export interface AiRuntime {
  api_features?: string[]
  configured_mode: string
  ready: boolean
  model: string | null
  reason: string | null
  connectivity_verified: boolean
  environment?: { name: string; synthetic_data: boolean; volatile: boolean }
}

export interface DispatchSuggestion {
  mode: string
  ai?: AiMetadata
  recommended_personnel: string | null
  recommended_name?: string | null
  reason: string
  requires_human_confirmation: boolean
  orchestration?: Orchestration
}

export interface AgentToolCall {
  tool_name: string
  status: string
  input: Record<string, unknown>
  output: unknown
  error: string | null
  started_at: string
  completed_at: string | null
}

export interface AgentStep {
  run_id: string
  agent_name: string
  status: string
  input_text: string
  output: unknown
  error: string | null
  started_at: string
  completed_at: string | null
  tool_calls: AgentToolCall[]
}

export interface CoordinationResult {
  run_id: string
  status: string
  mode: string
  summary: string
  intent: Record<string, unknown>
  created_ticket: { id: number; ticket_no: string; status: string } | null
  created_renovation_task: { id: number; task_no: string; status: string } | null
  dispatch_recommendation: {
    recommended_personnel: string | null
    recommended_name?: string
    reason: string
    candidates?: string[]
    requires_human_confirmation?: boolean
  } | null
  steps: AgentStep[]
}

export interface WorkflowEvent {
  event_id: string
  workflow_id: string
  event_type: string
  source_agent: string
  target_agent: string
  business_id: string | null
  ticket_id: number | null
  payload: Record<string, unknown>
  workflow_status: string
  requires_human: boolean
  idempotency_key: string
  status: string
  error: string | null
  occurred_at: string
}

export interface WorkflowRun {
  workflow_id: string
  channel: string
  requester_id: string | null
  status: 'waiting_input' | 'waiting_human' | 'ready_for_service' | 'completed' | 'failed'
  current_step: string
  original_message: string
  draft: RepairParseResult
  missing_fields: string[]
  ticket_id: number | null
  ticket: Ticket | null
  events: WorkflowEvent[]
  attachments: ImageAttachment[]
}

export interface ImageAttachment {
  id: string
  name: string
  media_type: string
  size_bytes: number
  width: number
  height: number
  purpose: 'intake' | 'repair'
  source_channel: string
  url: string
  workflow_id: string | null
  ticket_id: number | null
  repair_record_id: number | null
  created_at: string
}

// 文件作为二进制发送，不能沿用JSON请求头或把图片塞进模型文字上下文。
export const uploadImage = (file: File, purpose: 'intake' | 'repair', signal?: AbortSignal) =>
  request<ImageAttachment>(`/api/v1/attachments/images?purpose=${purpose}&filename=${encodeURIComponent(file.name)}`, {
    method: 'POST', headers: { 'Content-Type': file.type }, body: file, signal,
  })
export const getTicketImages = (id: number) => request<ImageAttachment[]>(`/api/v1/tickets/${id}/attachments`)
export interface RepairFeedback {
  id: number
  ticket_id: number
  repair_description: string
  photo_urls: string[]
  submitted_at: string
}
export const getTicketRepairs = (id: number) => request<RepairFeedback[]>(`/api/v1/tickets/${id}/repair-records`)

export interface AcceptanceRecord {
  id: number
  ticket_id: number
  acceptance_comment: string
  accepted_by: string | null
  accepted: boolean
  accepted_at: string
}
export const getTicketAcceptances = (id: number) => request<AcceptanceRecord[]>(`/api/v1/tickets/${id}/acceptance-records`)

export interface AgentCapability {
  agent_name: string
  department: string
  status: string
  capabilities: string[]
  write_guards: string[]
}

export interface InvestmentDraft {
  mode: string
  ai?: AiMetadata
  evidence?: Record<string, string>
  intent_reason?: string | null
  customer_name: string | null
  contact_name: string | null
  contact_phone: string | null
  source: string | null
  intent_level: 'A' | 'B' | 'C' | null
  follow_up_interval_days: number | null
  missing_fields: string[]
  follow_up_subject: string
  follow_up_description: string
}

export interface InvestmentConfirmation {
  workflow_id: string
  customer: {
    id: number
    customer_no: string
    customer_name: string
    stage: string
    source: string | null
  }
  follow_up_task: {
    id: number
    task_no: string
    subject: string
    status: string
  }
  handoff_event_id: string | null
}

export interface CustomerSummary {
  id: number
  customer_no: string
  customer_name: string
  stage: string
}

export interface HandoffDocument {
  contract_no: string
  contract_version: string
  contact_sheet_ref: string
  contact_sheet_version: string
  location: string
  contact_name: string
  contact_phone: string
  service_requirements: string
  agreements: string
}

export interface CustomerHandoff {
  id: number
  customer_id: number
  customer_name: string
  workflow_id: string
  status: 'draft' | 'submitted' | 'returned' | 'accepted'
  revision: number
  document: HandoffDocument
  check: { mode: string; missing_labels: string[]; missing_fields: string[] }
  submitted_by: string | null
  submitted_at: string | null
  decided_by: string | null
  decided_at: string | null
  decision_comment: string | null
  events: { event_id: string; event_type: string; payload: Record<string, unknown>; occurred_at: string }[]
}

export const getInvestmentCustomers = () => request<CustomerSummary[]>('/api/v1/agents/investment/customers')
export const getCustomerHandoffs = () => request<CustomerHandoff[]>('/api/v1/customer-handoffs')
export const createCustomerHandoff = (customerId: number) => request<CustomerHandoff>(
  `/api/v1/customers/${customerId}/handoff`, { method: 'POST' },
)
export const saveCustomerHandoff = (handoff: CustomerHandoff, document: HandoffDocument) => request<CustomerHandoff>(
  `/api/v1/customer-handoffs/${handoff.id}`, {
    method: 'PUT', body: JSON.stringify({ expected_revision: handoff.revision, document }),
  },
)
export const submitCustomerHandoff = (handoff: CustomerHandoff, payload: {
  actor: string; contract_signed: boolean; idempotency_key: string
}) => request<CustomerHandoff>(`/api/v1/customer-handoffs/${handoff.id}/submit`, {
  method: 'POST', body: JSON.stringify({ ...payload, expected_revision: handoff.revision }),
})
export const decideCustomerHandoff = (handoff: CustomerHandoff, payload: {
  actor: string; accepted: boolean; comment: string; idempotency_key: string
}) => request<CustomerHandoff>(`/api/v1/customer-handoffs/${handoff.id}/decide`, {
  method: 'POST', body: JSON.stringify({ ...payload, expected_revision: handoff.revision }),
})

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => null)
    throw new Error(detail?.detail ?? `请求失败 (${response.status})`)
  }
  return response.json() as Promise<T>
}

export const getTickets = () => request<Ticket[]>('/api/v1/tickets')
export const getTicketEvents = (id: number) =>
  request<TicketEvent[]>(`/api/v1/tickets/${id}/events`)
export const getRepairPersonnel = () =>
  request<RepairPersonnel[]>('/api/v1/repair-personnel')
export const parseRepair = (text: string) =>
  request<RepairParseResult>('/api/v1/repairs/parse', {
    method: 'POST',
    body: JSON.stringify({ text }),
  })
export const coordinateRequest = (message: string) =>
  request<CoordinationResult>('/api/v1/agent/coordinate', {
    method: 'POST',
    body: JSON.stringify({ message }),
  })

export const startFeishuRepairWorkflow = (text: string, eventId: string) =>
  request<WorkflowRun>('/api/v1/integrations/feishu/repair-events', {
    method: 'POST',
    body: JSON.stringify({ event_id: eventId, open_id: 'demo_open_id', text }),
  })

// Web 和飞书只共享流程引擎，入口来源必须如实记录。
export const startRepairWorkflow = (message: string, idempotencyKey: string, attachmentIds: string[] = []) =>
  request<WorkflowRun>('/api/v1/agent/runs', {
    method: 'POST',
    body: JSON.stringify({ message, channel: 'web', requester_id: 'web_demo_user', idempotency_key: idempotencyKey, attachment_ids: attachmentIds }),
  })

export const continueRepairWorkflow = (workflowId: string, message: string, attachmentIds: string[] = []) =>
  request<WorkflowRun>(`/api/v1/agent/runs/${workflowId}/messages`, {
    method: 'POST',
    body: JSON.stringify({ message, idempotency_key: crypto.randomUUID(), attachment_ids: attachmentIds }),
  })

export const confirmRepairWorkflow = (workflowId: string, payload: {
  location: string
  issue_type: string
  description: string
  priority: Priority
}) => request<WorkflowRun>(`/api/v1/agent/runs/${workflowId}/confirm-ticket`, {
  method: 'POST',
  body: JSON.stringify({ ...payload, idempotency_key: crypto.randomUUID() }),
})

export const getAgentCapabilities = () =>
  request<AgentCapability[]>('/api/v1/agents/capabilities')

export const getAgentRuntime = () => request<AiRuntime>('/api/v1/agents/runtime')
export const getDispatchSuggestion = (ticketId: number) =>
  request<DispatchSuggestion>(`/api/v1/tickets/${ticketId}/dispatch-suggestion`, { method: 'POST' })

export const parseInvestmentIntake = (message: string, source?: string) =>
  request<InvestmentDraft>('/api/v1/agents/investment/intake', {
    method: 'POST',
    body: JSON.stringify({ message, source }),
  })

export const confirmInvestmentIntake = (payload: {
  customer_name: string
  contact_name?: string | null
  contact_phone?: string | null
  source?: string | null
  owner?: string | null
  intent_level: 'A' | 'B' | 'C'
  follow_up_subject: string
  follow_up_description: string
}) => request<InvestmentConfirmation>('/api/v1/agents/investment/confirm', {
  method: 'POST',
  body: JSON.stringify({ ...payload, idempotency_key: crypto.randomUUID() }),
})

export interface FollowUpTask {
  id: number
  task_no: string
  customer_id: number
  customer_name: string
  intent_level: 'A' | 'B' | 'C' | null
  subject: string
  description: string
  owner: string | null
  status: string
  due_at: string | null
}

export interface FollowUpRecord {
  id: number
  task_id: number
  actor: string
  outcome: string
  occurred_at: string
}

export const getFollowUpTasks = () =>
  request<FollowUpTask[]>('/api/v1/agents/investment/follow-ups')
export const getFollowUpRecords = (customerId: number) =>
  request<FollowUpRecord[]>(`/api/v1/agents/investment/customers/${customerId}/follow-up-records`)
export const completeFollowUp = (taskId: number, payload: {
  actor: string
  outcome: string
  idempotency_key: string
}) => request<{ record: FollowUpRecord; next_task: { id: number; due_at: string } | null }>(
  `/api/v1/agents/investment/follow-ups/${taskId}/complete`,
  { method: 'POST', body: JSON.stringify(payload) },
)

export const createTicket = (payload: {
  location: string
  issue_type: string
  description: string
  priority: Priority
  attachment_ids?: string[]
}) => request<Ticket>('/api/v1/tickets', {
  method: 'POST',
  body: JSON.stringify(payload),
})

export const assignTicket = (id: number, personnel_no: string, assigned_by = 'ENG-LEAD-001') =>
  request(`/api/v1/tickets/${id}/assignments`, {
    method: 'POST',
    body: JSON.stringify({ personnel_no, assigned_by }),
  })

export const acceptTicket = (id: number, personnel_no: string) =>
  request<Ticket>(`/api/v1/tickets/${id}/accept`, {
    method: 'POST',
    body: JSON.stringify({ personnel_no }),
  })

export const startProcessing = (id: number, personnel_no: string) =>
  request<Ticket>(`/api/v1/tickets/${id}/start-processing`, {
    method: 'POST',
    body: JSON.stringify({ personnel_no }),
  })

export const submitRepair = (id: number, payload: {
  personnel_no: string
  repair_description: string
  photo_urls: string[]
  attachment_ids?: string[]
}) => request(`/api/v1/tickets/${id}/repair-records`, {
  method: 'POST',
  body: JSON.stringify(payload),
})

export const submitAcceptance = (id: number, payload: {
  acceptance_comment: string
  accepted_by: string
  accepted: boolean
}) => request(`/api/v1/tickets/${id}/acceptance`, {
  method: 'POST',
  body: JSON.stringify(payload),
})
