import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { pathToFileURL, fileURLToPath } from 'node:url'
import test from 'node:test'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import { createSSRApp } from 'vue'
import { renderToString } from 'vue/server-renderer'

const require = createRequire(import.meta.url)
const filename = fileURLToPath(new URL('../src/components/AcceptanceForm.vue', import.meta.url))
const source = readFileSync(filename, 'utf8')
async function loadComponent(inlineTemplate) {
  const { descriptor } = parse(source, { filename })
  const script = compileScript(descriptor, {
    id: 'acceptance-form-test', inlineTemplate,
    ...(inlineTemplate ? { templateOptions: { ssr: true } } : {}),
  })
  const code = ts.transpileModule(script.content, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText.replace(/from (["'])(vue(?:\/server-renderer)?|lucide-vue-next)\1/g,
    (_, quote, name) => `from ${JSON.stringify(pathToFileURL(require.resolve(name)).href)}`)
  return (await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)).default
}
const scriptComponent = await loadComponent(false)
const renderedComponent = await loadComponent(true)
function setup(loading = false) {
  const events = []
  const props = { loading, error: '' }
  const form = scriptComponent.setup(props, { expose() {}, emit: (...event) => events.push(event) })
  return { form, events, props }
}

test('blank return explains why next to the form and focuses the required field', () => {
  const { form, events } = setup()
  let focused = false
  form.comment.value = '  \n '
  form.commentInput.value = { focus() { focused = true } }
  form.submit(false)
  assert.equal(events.length, 0)
  assert.ok(focused)
  assert.equal(form.validationError.value, '请先填写退回原因和返工要求，再退回工程部。')
  form.submit(true)
  assert.equal(events.length, 0)
  assert.equal(form.validationError.value, '请先填写验收意见，再确认验收。')
})

test('return emits boolean false with trimmed reason and operator, never approval', () => {
  const { form, events } = setup()
  form.submit(false)
  form.comment.value = '  接头仍漏水\n请重新密封  '
  form.actor.value = '  企服甲  '
  form.submit(false)
  assert.equal(form.validationError.value, '')
  assert.deepEqual(events, [['decision', {
    accepted: false, acceptance_comment: '接头仍漏水\n请重新密封', accepted_by: '企服甲',
  }]])
})

test('approval and loading have separate behavior', () => {
  const { form, events, props } = setup(true)
  form.comment.value = '现场复核正常'
  form.actor.value = ' '
  form.submit(false)
  assert.equal(events.length, 0)
  props.loading = false
  form.submit(true)
  assert.deepEqual(events, [['decision', {
    accepted: true, acceptance_comment: '现场复核正常', accepted_by: '企服演示员',
  }]])
})

test('actual form renders required guidance, inline API errors and busy buttons', async () => {
  const html = await renderToString(createSSRApp(renderedComponent, { loading: false, error: '网络请求失败' }))
  assert.ok(html.includes('验收意见／退回原因（必填）'))
  assert.ok(html.includes('退回后工单回到“处理中”'))
  assert.ok(html.includes('role="alert"'))
  assert.ok(html.includes('网络请求失败'))
  assert.ok(html.includes('aria-describedby="acceptance-form-hint acceptance-form-error"'))
  const busy = await renderToString(createSSRApp(renderedComponent, { loading: true, error: '' }))
  assert.equal((busy.match(/正在提交/g) || []).length, 2)
  assert.ok(busy.includes('disabled'))
})
