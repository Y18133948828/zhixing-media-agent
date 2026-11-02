<script setup>
import { onMounted, ref } from 'vue'
import { api, setToken } from '../api'
const emit = defineEmits(['login'])
const ssoEnabled = ref(false)
onMounted(async()=>{try{ssoEnabled.value=(await api('/auth/sso/config')).enabled}catch{}})
function sso(){setToken('');window.location.assign('/api/v1/auth/sso/login')}
const register = ref(false), username = ref(''), password = ref(''), busy = ref(false), error = ref('')
async function submit() {
  error.value = ''; busy.value = true
  try { const result = await api(`/auth/${register.value ? 'register' : 'login'}`, { method: 'POST', body: { username: username.value, password: password.value } }); setToken(result.access_token); emit('login', result.user) }
  catch (e) { error.value = e.message } finally { busy.value = false }
}
</script>
<template>
  <div class="auth-layout">
    <section class="auth-story"><div class="brand"><span class="brand-mark">知</span> 知行 · AI 内容工作室</div><p class="eyebrow">AI EDUCATION · CONTENT STUDIO</p><h1>把技术知识，<br>变成值得收藏的内容。</h1><p>从一个好选题出发，协同完成技术长文与小红书知识卡。每一次确认，都由你掌握。</p><div class="auth-flow"><span>选题策划</span><i>→</i><span>人工审稿</span><i>→</i><span>图文交付</span></div><small>本地创作 · 人工审稿 · 火山方舟</small></section>
    <section class="auth-form"><form @submit.prevent="submit"><span class="eyebrow">WELCOME TO YOUR WORKSPACE</span><h2>{{ register ? '创建工作台账号' : '登录工作台' }}</h2><p class="muted">{{ register ? '账号与创作记录保存在本地数据库。' : '接着完成上次的创作。' }}</p><label>用户名<input v-model="username" autocomplete="username" required minlength="3" maxlength="50" placeholder="至少 3 个字符"></label><label>密码<input v-model="password" :autocomplete="register ? 'new-password' : 'current-password'" type="password" required minlength="8" maxlength="100" placeholder="至少 8 个字符"></label><p v-if="error" class="error" role="alert">{{ error }}</p><button class="primary full" :disabled="busy">{{ busy ? '正在处理…' : register ? '创建账号并进入' : '登录' }}</button><button type="button" class="text-button full" @click="register = !register; error = ''">{{ register ? '已有账号？返回登录' : '首次使用？创建本地账号' }}</button><button v-if="ssoEnabled" type="button" class="full" @click="sso">通过企业统一账号登录</button></form></section>
  </div>
</template>
