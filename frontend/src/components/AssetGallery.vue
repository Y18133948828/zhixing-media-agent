<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { api, download } from '../api'
const props = defineProps(['task','canEdit'])
const emit = defineEmits(['rerender', 'error'])
const images = ref([]), packText = ref(''), editing = ref(false), theme = ref('clean'), html = ref(''), preview = ref(false)
const targetPage = ref(0)
const pack = computed(() => props.task?.state?.xhs_pack || {})
function release() { images.value.forEach(image => URL.revokeObjectURL(image.url)); images.value = [] }
let generation = 0
watch(() => [props.task?.id, props.task?.checkpoint_id], async () => {
  const current = ++generation; release(); editing.value = false
  if (!props.task) return
  html.value = props.task.state.wechat_html_content || ''
  if(!props.task.state.xhs_pack?.slides)return
  packText.value = JSON.stringify(pack.value, null, 2); theme.value = props.task.state.task_input?.theme || props.task.input.theme
  if(props.task.status!=='READY'){editing.value=true;return}
  try {
    const assets = await api(`/tasks/${props.task.id}/assets`)
    const loaded = await Promise.all(assets.filter(a => a.kind === 'xhs_image').sort((a,b)=>a.filename.localeCompare(b.filename)).map(async a => ({ ...a, url: await download(a.download_url, '', true) })))
    if (current !== generation) { loaded.forEach(image => URL.revokeObjectURL(image.url)); return }
    images.value = loaded
    html.value = props.task.state.wechat_html_content || ''
  } catch (error) { emit('error', error.message) }
}, { immediate: true })
onBeforeUnmount(() => { generation++; release() })
async function exportAll() { try { await download(`/api/v1/tasks/${props.task.id}/export`, '小红书与公众号图文包.zip') } catch (e) { emit('error', e.message) } }
function rerender() { try { emit('rerender', { pack: JSON.parse(packText.value), theme: theme.value, ...(targetPage.value?{page:targetPage.value}:{}) }) } catch { emit('error', '卡片 JSON 格式不正确，请检查后提交') } }
async function copyCaption() { try { await navigator.clipboard.writeText(`${pack.value.title}\n\n${pack.value.caption}\n\n${pack.value.tags?.map(t => '#'+t).join(' ')}`) } catch { emit('error', '浏览器未允许复制，请手工选择配文') } }
</script>
<template><section class="panel"><div class="section-heading"><div><span class="eyebrow">CONTENT PACKAGE</span><h2>{{task.status==='READY'?'图文已就绪':'修改卡片后重新生成'}}</h2></div><button class="primary" :disabled="task.status!=='READY'" @click="exportAll">下载完整图文包 ↓</button></div><div class="image-grid"><figure v-for="(image, index) in images" :key="image.id"><img :src="image.url" :alt="`第 ${index+1} 张知识卡片`" loading="lazy"><figcaption><span>{{ String(index+1).padStart(2,'0') }} / {{ images.length }}</span><a :href="image.url" :download="image.filename">下载图片</a><button v-if="canEdit!==false" @click="emit('rerender',{page:index+1})">重绘此页</button></figcaption></figure></div><div v-if="pack.slides" class="caption-box"><div class="section-heading"><h3>{{ pack.title }}</h3><button @click="copyCaption">复制小红书配文</button></div><p class="preserve">{{ pack.caption }}</p><div class="tags"><span v-for="tag in pack.tags" :key="tag">#{{ tag }}</span></div></div><div class="actions wrap"><button v-if="task.state.wechat_html_content" @click="preview = !preview">{{ preview ? '收起' : '预览' }}公众号排版</button><button v-if="canEdit!==false&&pack.slides" @click="editing = !editing">编辑卡片内容 / 重新渲染</button></div><iframe v-if="preview" class="wechat-preview" title="公众号文章预览" sandbox="" :srcdoc="`<!doctype html><meta charset='utf-8'><div style='max-width:700px;margin:30px auto;padding:20px'>${html}</div>`"></iframe><div v-if="editing" class="card-editor"><p class="muted">修改后的文案会用于新一轮图片生成；历史文件保留。技术事实请与定稿核对。</p><label>卡片主题<select v-model="theme"><option value="clean">清爽知识卡</option><option value="dark">深色技术卡</option></select></label><label>重绘范围<select v-model.number="targetPage"><option :value="0">整组重绘</option><option v-for="(_,i) in pack.slides" :key="i" :value="i+1">只重绘第 {{i+1}} 页</option></select></label><label>卡片结构 · JSON<textarea v-model="packText" rows="18" spellcheck="false" class="code-input"></textarea></label><button class="primary" @click="rerender">{{targetPage?'保存并重绘指定页':'保存修改并重新生成'}}</button></div></section></template>
