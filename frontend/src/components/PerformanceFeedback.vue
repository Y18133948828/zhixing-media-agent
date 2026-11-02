<script setup>
import { ref, watch } from 'vue'
import { api } from '../api'
const props = defineProps(['taskId'])
const emit = defineEmits(['error'])
const records = ref([]), busy = ref(false), saved = ref(false)
const defaults = () => ({platform:'xiaohongshu',views:0,likes:0,saves:0,comments:0,note:''})
const form = ref(defaults())
function choosePlatform(){const row=records.value.find(r=>r.platform===form.value.platform);form.value={...defaults(),...row,platform:form.value.platform};saved.value=false}
watch(()=>props.taskId,async id=>{try{records.value=await api(`/tasks/${id}/feedback`);choosePlatform()}catch(e){emit('error',e.message)}},{immediate:true})
async function save(){busy.value=true;saved.value=false;try{await api(`/tasks/${props.taskId}/feedback`,{method:'PUT',body:{...form.value,observed_at:new Date().toISOString()}});records.value=await api(`/tasks/${props.taskId}/feedback`);saved.value=true}catch(e){emit('error',e.message)}finally{busy.value=false}}
</script>
<template><section class="panel"><div class="section-heading"><div><span class="eyebrow">FEEDBACK LOOP</span><h2>把内容表现带回下一次选题</h2></div><span class="pill">人工录入 · 累计数据</span></div><p class="muted">发布后记录账号的真实表现。保存会覆盖该平台上一次录入的累计值，新任务会参考同课程的历史样本。</p><form @submit.prevent="save"><label>发布平台<select v-model="form.platform" @change="choosePlatform"><option value="xiaohongshu">小红书</option><option value="wechat">微信公众号</option></select></label><div class="feedback-grid"><label v-for="field in [{id:'views',name:'阅读量'},{id:'likes',name:'点赞数'},{id:'saves',name:'收藏数'},{id:'comments',name:'评论数'}]" :key="field.id">{{ field.name }}<input v-model.number="form[field.id]" type="number" min="0" max="2147483647" step="1" required></label></div><label>复盘备注<textarea v-model="form.note" maxlength="2000" rows="2" placeholder="例如：案例拆解类内容的收藏较多，下次继续讲具体步骤。"></textarea></label><div class="actions"><span v-if="saved" class="muted" role="status">平台数据已保存</span><button class="primary" :disabled="busy">{{ busy?'正在保存…':'保存平台数据' }}</button></div></form></section></template>
