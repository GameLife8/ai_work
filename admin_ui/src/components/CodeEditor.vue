<!--
  通用 CodeMirror 6 包装组件
  使用：
    <CodeEditor v-model="code" language="yaml" :height="320" />
    <CodeEditor :model-value="json" language="json" readonly :height="200" />

  Language 支持：yaml / json / plain
  风格：清新浅色，柔和光标，自动换行可选
-->
<template>
  <div ref="hostRef" class="cm-host" :style="{ minHeight: heightPx }"></div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { EditorState } from '@codemirror/state'
import {
  EditorView,
  highlightActiveLine,
  highlightActiveLineGutter,
  keymap,
  lineNumbers,
  placeholder as cmPlaceholder,
} from '@codemirror/view'
import { defaultKeymap, history, historyKeymap, indentWithTab } from '@codemirror/commands'
import {
  bracketMatching,
  defaultHighlightStyle,
  foldGutter,
  foldKeymap,
  HighlightStyle,
  indentOnInput,
  syntaxHighlighting,
} from '@codemirror/language'
import { tags } from '@lezer/highlight'
import { yaml as yamlLang } from '@codemirror/lang-yaml'
import { json as jsonLang } from '@codemirror/lang-json'

const props = defineProps({
  modelValue: { type: String, default: '' },
  language: { type: String, default: 'plain' }, // 'yaml' | 'json' | 'plain'
  readonly: { type: Boolean, default: false },
  height: { type: [String, Number], default: 280 },
  placeholder: { type: String, default: '' },
  wrap: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])

const hostRef = ref(null)
let view = null
let isExternalUpdate = false

const heightPx = typeof props.height === 'number' ? `${props.height}px` : props.height

function langExt() {
  if (props.language === 'yaml') return [yamlLang()]
  if (props.language === 'json') return [jsonLang()]
  return []
}

// 自定义清新简约配色（覆盖 defaultHighlightStyle 的部分 token）
const freshHighlight = HighlightStyle.define([
  { tag: tags.keyword, color: '#0d9488', fontWeight: '500' },
  { tag: tags.string, color: '#0e7490' },
  { tag: tags.number, color: '#b45309' },
  { tag: tags.bool, color: '#b45309' },
  { tag: tags.null, color: '#94a3b8' },
  { tag: tags.propertyName, color: '#1f2937' },
  { tag: tags.comment, color: '#94a3b8', fontStyle: 'italic' },
  { tag: tags.atom, color: '#0d9488' },
  { tag: tags.meta, color: '#6b7280' },
])

const baseTheme = EditorView.theme(
  {
    '&': {
      fontSize: '12.5px',
      backgroundColor: '#fbfcfd',
      color: '#0f172a',
      borderRadius: '8px',
      overflow: 'hidden',
    },
    '.cm-scroller': {
      fontFamily: 'ui-monospace, "SF Mono", "JetBrains Mono", Consolas, monospace',
      lineHeight: '1.55',
    },
    '.cm-content': { padding: '10px 0', caretColor: '#14b8a6' },
    '.cm-gutters': {
      backgroundColor: '#f1f5f9',
      color: '#94a3b8',
      border: 'none',
      borderRight: '1px solid #e2e8f0',
    },
    '.cm-activeLineGutter': { backgroundColor: '#e0f2f1' },
    '.cm-activeLine': { backgroundColor: 'rgba(20, 184, 166, 0.04)' },
    '.cm-cursor': { borderLeftColor: '#14b8a6' },
    '.cm-selectionBackground, ::selection': {
      backgroundColor: 'rgba(20, 184, 166, 0.18) !important',
    },
    '.cm-matchingBracket': {
      backgroundColor: 'rgba(20, 184, 166, 0.15) !important',
      color: 'inherit',
    },
    '.cm-placeholder': { color: '#cbd5e1' },
  },
  { dark: false },
)

function buildExtensions() {
  const exts = [
    lineNumbers(),
    foldGutter(),
    highlightActiveLineGutter(),
    highlightActiveLine(),
    history(),
    indentOnInput(),
    bracketMatching(),
    syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
    syntaxHighlighting(freshHighlight),
    keymap.of([...defaultKeymap, ...historyKeymap, ...foldKeymap, indentWithTab]),
    baseTheme,
    EditorView.editable.of(!props.readonly),
    EditorState.readOnly.of(props.readonly),
    EditorView.updateListener.of((u) => {
      if (u.docChanged && !isExternalUpdate) {
        emit('update:modelValue', u.state.doc.toString())
      }
    }),
  ]
  if (props.placeholder) exts.push(cmPlaceholder(props.placeholder))
  if (props.wrap) exts.push(EditorView.lineWrapping)
  exts.push(...langExt())
  return exts
}

onMounted(() => {
  const state = EditorState.create({
    doc: props.modelValue || '',
    extensions: buildExtensions(),
  })
  view = new EditorView({ state, parent: hostRef.value })
})

watch(
  () => props.modelValue,
  (v) => {
    if (!view) return
    const cur = view.state.doc.toString()
    if (v !== cur) {
      isExternalUpdate = true
      view.dispatch({
        changes: { from: 0, to: cur.length, insert: v ?? '' },
      })
      isExternalUpdate = false
    }
  },
)

watch(
  () => props.readonly,
  () => {
    // 只读切换时简单地重建一次（成本可忽略，view 是轻量的）
    if (view) {
      view.destroy()
      view = new EditorView({
        state: EditorState.create({
          doc: props.modelValue || '',
          extensions: buildExtensions(),
        }),
        parent: hostRef.value,
      })
    }
  },
)

onBeforeUnmount(() => {
  view?.destroy()
  view = null
})
</script>

<style scoped>
.cm-host {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: #fbfcfd;
  transition: border-color .15s, box-shadow .15s;
}
.cm-host:focus-within {
  border-color: var(--border-focus);
  box-shadow: var(--shadow-focus);
}
:deep(.cm-editor) { outline: none !important; }
:deep(.cm-editor.cm-focused) { outline: none !important; }
</style>
