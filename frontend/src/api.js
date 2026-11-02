let token = sessionStorage.getItem('media_token') || ''
export function setToken(value) { token = value; value ? sessionStorage.setItem('media_token', value) : sessionStorage.removeItem('media_token') }
export async function api(path, options = {}) {
  const headers = { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers }
  if (token) headers.Authorization = `Bearer ${token}`
  if (options.method === 'POST') headers['Idempotency-Key'] ||= crypto.randomUUID()
  const response = await fetch(`/api/v1${path}`, { ...options, headers, body: options.body ? JSON.stringify(options.body) : undefined })
  if (!response.ok) {
    let data = {}; try { data = await response.json() } catch {}
    const message = typeof data.detail === 'string' ? data.detail : data.detail?.map?.(e => e.msg).join('；')
    const error = new Error(message || `请求失败 (${response.status})`); error.status = response.status; throw error
  }
  return response.status === 204 ? null : response.json()
}
export async function download(path, name, preview = false) {
  const response = await fetch(path, { headers: { Authorization: `Bearer ${token}` } })
  if (!response.ok) throw new Error('文件下载失败，请刷新后重试')
  const url = URL.createObjectURL(await response.blob())
  if (preview) return url
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = name; anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
export async function stream(taskId, afterId, onEvent, signal) {
  const response = await fetch(`/api/v1/tasks/${taskId}/stream?after_id=${afterId}`, { headers: { Authorization: `Bearer ${token}` }, signal })
  if (!response.ok) throw new Error('进度连接失败，请刷新任务查看持久化结果')
  const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ''
  try {
    while (true) {
      const { value, done } = await reader.read(); buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
      let end
      while ((end = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, end); buffer = buffer.slice(end + 2)
        const data = frame.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
        if (data) onEvent(JSON.parse(data))
      }
      if (done) break
    }
  } finally { reader.releaseLock() }
}
