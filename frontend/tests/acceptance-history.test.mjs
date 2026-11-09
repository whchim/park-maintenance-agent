import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { pathToFileURL, fileURLToPath } from 'node:url'
import test from 'node:test'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'

// 编译实际组件，检查真实模板的可见输出；不复制展示逻辑。
const require = createRequire(import.meta.url)
const filename = fileURLToPath(new URL('../src/components/AcceptanceHistory.vue', import.meta.url))
const { descriptor } = parse(readFileSync(filename, 'utf8'), { filename })
const compiled = compileScript(descriptor, { id: 'acceptance-history-test', inlineTemplate: true, templateOptions: { ssr: true } })
const code = ts.transpileModule(compiled.content, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText.replace(/from (["'])(vue(?:\/server-renderer)?)\1/g,
  (_, quote, name) => `from ${JSON.stringify(pathToFileURL(require.resolve(name)).href)}`)
const component = (await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)).default
const render = props => renderToString(createSSRApp(component, { records: [], loading: false, error: '', ...props }))
const text = html => html.replace(/<!--[\s\S]*?-->/g, '').replace(/<[^>]+>/g, '')

test('renders all rework rounds, people, timestamps and complete escaped comments', async () => {
  const records = [
    { id: 1, ticket_id: 1, accepted: false, accepted_by: '企服甲', acceptance_comment: '接头仍漏水\n请更换密封件', accepted_at: '2026-11-09T09:00:00' },
    { id: 2, ticket_id: 1, accepted: false, accepted_by: null, acceptance_comment: '<script>压力测试未通过</script>', accepted_at: '2026-11-09T10:00:00' },
    { id: 3, ticket_id: 1, accepted: true, accepted_by: '企服乙', acceptance_comment: '现场复核正常，验收通过', accepted_at: '2026-11-09T11:00:00' },
  ]
  const html = await render({ records })
  const visible = text(html)
  for (const expected of ['3 次验收 · 2 次退回', '第 1 次验收', '第 2 次验收', '第 3 次验收', '验收人：企服甲', '验收人：未记录', '验收人：企服乙', '接头仍漏水\n请更换密封件', '现场复核正常，验收通过']) {
    assert.ok(visible.includes(expected), expected)
  }
  assert.equal((visible.match(/退回返工/g) || []).length, 2)
  assert.ok(html.includes('datetime="2026-11-09T10:00:00"'))
  assert.ok(html.includes('&lt;script&gt;压力测试未通过&lt;/script&gt;'))
  assert.ok(!html.includes('<script>'))
  assert.ok(visible.indexOf('企服甲') < visible.indexOf('企服乙'))
})

test('distinguishes empty history from loading and request failure', async () => {
  assert.ok(text(await render({})).includes('暂无验收记录'))
  const loading = text(await render({ loading: true }))
  assert.ok(loading.includes('正在加载验收记录'))
  assert.ok(!loading.includes('暂无验收记录'))
  const failed = text(await render({ error: '验收记录加载失败' }))
  assert.ok(failed.includes('验收记录加载失败'))
  assert.ok(!failed.includes('暂无验收记录'))
  assert.ok(!failed.includes('0 次验收'))
})
