import { onBeforeUnmount, ref } from 'vue'

export const workspaces = {
  repairs: { hash: '#/repairs', title: '维修协同', description: '企服报修、工程处理与企服验收', collaboration: '企服 ↔ 工程' },
  investment: { hash: '#/investment', title: '招商客户', description: '客户意向整理、人工建档与回访跟进', collaboration: '招商' },
  handoffs: { hash: '#/handoffs', title: '客户交接', description: '招商提交最终版联系单，企服核对后接收或退回', collaboration: '招商 → 企服' },
} as const

export type Workspace = keyof typeof workspaces

function workspaceFromHash(): Workspace {
  return (Object.keys(workspaces) as Workspace[]).find(key => workspaces[key].hash === window.location.hash) ?? 'repairs'
}

export function useWorkspaceNavigation() {
  const activeWorkspace = ref<Workspace>(workspaceFromHash())
  const visitedWorkspaces = ref(new Set<Workspace>([activeWorkspace.value]))

  function syncFromHash() {
    activeWorkspace.value = workspaceFromHash()
    visitedWorkspaces.value.add(activeWorkspace.value)
  }

  function navigate(workspace: Workspace) {
    // 立即更新视图，同时保留浏览器的前进、后退记录；不重载页面或清空表单。
    activeWorkspace.value = workspace
    visitedWorkspaces.value.add(workspace)
    if (window.location.hash !== workspaces[workspace].hash) window.location.hash = workspaces[workspace].hash
  }

  window.addEventListener('hashchange', syncFromHash)
  onBeforeUnmount(() => window.removeEventListener('hashchange', syncFromHash))

  return { activeWorkspace, visitedWorkspaces, navigate }
}
