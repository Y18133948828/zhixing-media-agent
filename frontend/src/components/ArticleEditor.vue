<script setup>
import { computed, ref, watch } from 'vue'
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'
const props = defineProps(['article', 'version', 'busy'])
const emit = defineEmits(['approve', 'reject'])
const draft = ref({}), feedback = ref('')
const md = new MarkdownIt({ html: false, linkify: true })
watch(() => props.article, value => { draft.value = JSON.parse(JSON.stringify(value || {})); feedback.value = '' }, { immediate: true })
const html = computed(() => DOMPurify.sanitize(md.render(draft.value.markdown || '')))
</script>
<template><section class="panel"><div class="section-heading"><div><span class="eyebrow">HUMAN IN THE LOOP</span><h2>审核技术长文 <span class="pill">第 {{ version }} 版</span></h2></div><span class="muted">可直接编辑后定稿</span></div><div class="editor-grid"><div><label>文章标题<input v-model="draft.title" maxlength="60"></label><label>摘要<textarea v-model="draft.digest" rows="2" maxlength="120"></textarea></label><label>正文 · Markdown<textarea v-model="draft.markdown" class="article-input" spellcheck="false"></textarea></label></div><div class="article-preview"><h2>{{ draft.title }}</h2><div class="prose" v-html="html"></div></div></div><div v-if="draft.fact_checks?.length" class="notice"><strong>发布前核实</strong><ul><li v-for="item in draft.fact_checks" :key="item">{{ item }}</li></ul></div><label>修改意见<textarea v-model="feedback" rows="3" placeholder="例如：简化代码示例，用一个初学者能跟做的小实验开头。"></textarea></label><div class="actions"><button :disabled="busy || !feedback.trim()" @click="emit('reject', feedback)">按意见重写</button><button class="primary" :disabled="busy || !draft.title || draft.markdown?.length < 200" @click="emit('approve', draft)">确认定稿，生成图文 →</button></div></section></template>
