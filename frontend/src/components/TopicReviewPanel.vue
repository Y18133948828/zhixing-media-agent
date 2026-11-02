<script setup>
import { ref } from 'vue'
defineProps(['task','busy','canReview'])
const emit=defineEmits(['review'])
const feedback=ref('')
</script>
<template><section class="panel"><span class="eyebrow">TOPIC APPROVAL</span><h2>选题逐级审核</h2><p class="muted">第 {{(task.state.pending_review?.review_index||1)+1}} / {{task.state.pending_review?.review_total}} 级 · 当前审核人：{{task.state.pending_review?.reviewer?.username}}</p><article class="caption-box"><h3>{{task.state.selected_topic.title}}</h3><p>{{task.state.selected_topic.angle}}</p><p>{{task.state.selected_topic.audience_value}}</p><small>{{task.state.selected_topic.evidence}}</small></article><label>审核意见<textarea v-model="feedback" rows="3" maxlength="4000" placeholder="打回时说明需要调整的技术切口或读者价值。"></textarea></label><div class="actions"><button :disabled="busy||!canReview||!feedback.trim()" @click="emit('review','rejected',feedback)">打回重新策划</button><button class="primary" :disabled="busy||!canReview" @click="emit('review','approved',feedback)">通过本级选题审核</button></div></section></template>
