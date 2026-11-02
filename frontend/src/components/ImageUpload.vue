<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import { ImagePlus, X } from 'lucide-vue-next'
import { uploadImage, type ImageAttachment } from '../api'

const props = withDefaults(defineProps<{
  modelValue: ImageAttachment[]
  purpose?: 'intake' | 'repair'
  title?: string
  disabled?: boolean
  readonly?: boolean
  existingCount?: number
}>(), { purpose: 'intake', title: '现场图片（可选）', existingCount: 0 })
const emit = defineEmits<{
  'update:modelValue': [images: ImageAttachment[]]
  'busy-change': [busy: boolean]
}>()
const fileInput = ref<HTMLInputElement | null>(null)
const busy = ref(false)
const dragging = ref(false)
const error = ref('')
const controller = new AbortController()
let active = true

onBeforeUnmount(() => {
  active = false
  controller.abort()
  emit('busy-change', false)
})

async function addFiles(files: File[]) {
  if (props.disabled || props.readonly || busy.value) return
  error.value = ''
  if (files.length + props.modelValue.length + props.existingCount > 5) {
    error.value = '每次最多5张图片，请减少后重试'
    return
  }
  busy.value = true
  emit('busy-change', true)
  const uploaded = [...props.modelValue]
  try {
    for (const file of files) {
      if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) throw new Error('仅支持JPEG、PNG、WebP图片')
      if (file.size > 10 * 1024 * 1024) throw new Error('每张图片最多10MiB')
      const image = await uploadImage(file, props.purpose, controller.signal)
      if (!active) return
      uploaded.push(image)
      emit('update:modelValue', [...uploaded])
    }
  } catch (cause) {
    if (active) error.value = `${cause instanceof Error ? cause.message : '上传失败'}；已成功的图片会保留`
  } finally {
    if (active) {
      busy.value = false
      emit('busy-change', false)
      if (fileInput.value) fileInput.value.value = ''
    }
  }
}

function onDrop(event: DragEvent) {
  dragging.value = false
  if (event.dataTransfer) void addFiles(Array.from(event.dataTransfer.files))
}

function onSelect(event: Event) {
  const input = event.target as HTMLInputElement
  if (input.files) void addFiles(Array.from(input.files))
}
</script>

<template>
  <section class="image-upload" :aria-label="title">
    <h4>{{ title }}</h4>
    <div v-if="!readonly" class="image-dropzone" :class="{ dragging, disabled: disabled || busy }"
      @dragover.prevent="dragging = !disabled && !busy" @dragleave.prevent="dragging = false" @drop.prevent="onDrop">
      <ImagePlus :size="23" aria-hidden="true" />
      <span>{{ busy ? '正在校验并上传图片…' : '将图片拖到这里，或点击选择' }}</span>
      <button class="secondary-button" type="button" :disabled="disabled || busy || modelValue.length + existingCount >= 5" @click="fileInput?.click()">选择图片</button>
      <input ref="fileInput" class="image-file-input" type="file" accept="image/jpeg,image/png,image/webp" multiple :aria-label="`选择${title}`" :disabled="disabled || busy" @change="onSelect" />
      <small>JPEG / PNG / WebP · 每张10MiB以内 · 最多5张</small>
    </div>
    <p v-if="!readonly" class="muted">图片仅作现场证据保存，当前AI只处理文字，不会自动看图判定故障。</p>
    <p v-if="error" class="image-upload-error" role="alert">{{ error }}</p>
    <ul v-if="modelValue.length" class="image-grid">
      <li v-for="image in modelValue" :key="image.id" class="image-card">
        <a :href="image.url" target="_blank" rel="noopener" :aria-label="`查看图片 ${image.name}`"><img :src="image.url" :alt="image.name" loading="lazy" /></a>
        <div><span :title="image.name">{{ image.name }}</span><small>{{ image.purpose === 'repair' ? '维修反馈' : '报修现场' }} · {{ Math.ceil(image.size_bytes / 1024) }}KB</small></div>
        <button v-if="!readonly" class="image-remove" type="button" :disabled="disabled || busy" :aria-label="`移除 ${image.name}`" @click="emit('update:modelValue', modelValue.filter(item => item.id !== image.id))"><X :size="13" /></button>
      </li>
    </ul>
  </section>
</template>
