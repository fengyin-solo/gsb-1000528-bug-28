<template>
  <section class="page" data-module="drilling_log">
    <header class="page-head">
      <div>
        <h2>钻探日志管理</h2>
        <p class="page-desc">
          整批事务导入：解析、校验、落库与偏离待办要么一起成功、要么一起回滚；
          日志台账、钻孔详情、偏离待办三处共用同一份库内结论。
        </p>
      </div>
    </header>

    <nav class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab-item"
        :class="{ active: activeTab === tab.key }"
        type="button"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
        <span v-if="tabBadges[tab.key]" class="tab-badge">{{ tabBadges[tab.key] }}</span>
      </button>
    </nav>

    <!-- 1. 日志台账 -->
    <div v-show="activeTab === 'ledger'">
      <form class="filter-bar" @submit.prevent="reloadLedger">
        <label class="filter-item">
          <span>日志编号 / 钻孔编号</span>
          <input v-model="ledgerKeyword" placeholder="按日志编号或现场钻孔编号检索" />
        </label>
        <label class="filter-item">
          <span>日志状态</span>
          <input v-model="ledgerStatus" placeholder="待填写 / 已填写 / 已审核" />
        </label>
        <button class="btn" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="resetLedgerFilters">重置条件</button>
      </form>

      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in ledgerColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in ledgerRows" :key="String(row.id)">
            <td v-for="column in ledgerColumns" :key="column">{{ row[column] ?? '—' }}</td>
          </tr>
          <tr v-if="!ledgerRows.length">
            <td :colspan="ledgerColumns.length" class="empty-state">暂无钻探日志，可通过「整批导入」上传现场来件</td>
          </tr>
        </tbody>
      </table>
      <footer class="page-foot">
        <span>共 {{ ledgerTotal }} 条钻探日志记录</span>
        <span class="hint">结论列由库内结论引擎回写，与钻孔详情、偏离待办同源</span>
      </footer>
    </div>

    <!-- 2. 整批导入 -->
    <div v-show="activeTab === 'import'" class="import-pane">
      <article class="import-card">
        <h3>第 1 步 · 选择现场来件（CSV / TSV，首行表头）</h3>
        <p class="hint">
          必需列：日志编号、钻孔编号、钻进深度；记录类型填「班次」或「终孔」。
          缺钻孔编号的行会先隔离，其余按现场编号迁入台账。
        </p>
        <input ref="fileInput" type="file" accept=".csv,.tsv,.txt" @change="onFilePicked" />
        <div v-if="importFile" class="file-meta">
          <span>{{ importFile.name }}（{{ importContent.length }} 字符，{{ importLines.length - 1 }} 数据行）</span>
          <span class="hint">指纹：{{ importFingerprint || '计算中…' }}</span>
        </div>
      </article>

      <article class="import-card">
        <h3>第 2 步 · 导入预览（试算后回滚，不写库）</h3>
        <div class="row-actions">
          <button class="btn" type="button" :disabled="!importContent || previewLoading" @click="runPreview">
            {{ previewLoading ? '试算中…' : '生成整批预览' }}
          </button>
          <button class="btn ghost" type="button" :disabled="!importContent || commitLoading" @click="runCommit">
            {{ commitLoading ? '整批提交中…' : '整批提交（事务）' }}
          </button>
          <button class="btn ghost" type="button" :disabled="uploading" @click="runResumableUpload">
            {{ uploading ? uploadHint : '断点续传方式上传并提交' }}
          </button>
        </div>

        <div v-if="preview" class="preview-box">
          <p>
            有效行 <strong>{{ preview.有效行数 }}</strong> ·
            缺孔号隔离 <strong>{{ preview.隔离行数 }}</strong> ·
            指纹 <code>{{ preview.文件指纹.slice(0, 16) }}…</code>
          </p>
          <table class="data-table compact">
            <thead>
              <tr><th>行号</th><th>日志编号</th><th>钻孔编号</th><th>钻进深度</th><th>记录类型</th><th>岩层描述</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in preview.待入库" :key="`p-${row.行号}`">
                <td>{{ row.行号 }}</td><td>{{ row.日志编号 }}</td><td>{{ row.钻孔编号 }}</td>
                <td>{{ row.钻进深度 }}</td><td>{{ row.记录类型 || '班次' }}</td><td>{{ row.岩层描述 || '—' }}</td>
              </tr>
            </tbody>
          </table>
          <h4>结论 / 待办预览（提交后回写三处）</h4>
          <ul class="preview-conclusions">
            <li v-for="(data, code) in preview.结论预览" :key="code">
              {{ code }}：库内结论 {{ data.库内结论深度 ?? '—' }}m（{{ data.结论来源 ?? '待数据' }}），
              {{ data.比对结论 }}，设计 {{ data.设计孔深 ?? '—' }}m
            </li>
          </ul>
        </div>
      </article>

      <article v-if="importResult" class="import-card" :class="importResult.ok ? 'ok' : 'reject'">
        <h3>{{ importResult.ok ? '整批提交成功' : '整批退回' }}</h3>
        <p>{{ importResult.message }}</p>
        <ul v-if="importResult.errors" class="error-list">
          <li v-for="(err, index) in importResult.errors" :key="index">
            第 {{ err.行号 }} 行 · {{ err.日志编号 || err.钻孔编号 || '' }}：{{ err.原因 }}
          </li>
        </ul>
        <button v-if="importResult.ok" class="btn ghost" type="button" @click="afterCommit">查看台账与待办</button>
      </article>
    </div>

    <!-- 3. 偏离待办 -->
    <div v-show="activeTab === 'todos'">
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in todoColumns" :key="column">{{ column }}</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in todoRows" :key="String(row.id)">
            <td v-for="column in todoColumns" :key="column">{{ row[column] ?? '—' }}</td>
            <td class="row-actions">
              <button
                v-if="row.状态 === '待处理'"
                class="link"
                type="button"
                @click="closeTodo(row)"
              >确认关闭</button>
              <button v-else class="link" type="button" @click="reopenTodo(row)">重新打开</button>
            </td>
          </tr>
          <tr v-if="!todoRows.length">
            <td :colspan="todoColumns.length + 1" class="empty-state">没有偏离待办：孔深、回次、未登记孔均正常</td>
          </tr>
        </tbody>
      </table>
      <footer class="page-foot"><span>共 {{ todoRows.length }} 条待办，与台账、钻孔详情同一份结论</span></footer>
    </div>

    <!-- 4. 缺孔号隔离 -->
    <div v-show="activeTab === 'quarantine'">
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in quarantineColumns" :key="column">{{ column }}</th>
            <th>补孔号迁移</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in quarantineRows" :key="String(row.id)">
            <td v-for="column in quarantineColumns" :key="column">{{ row[column] ?? '—' }}</td>
            <td class="row-actions">
              <input
                :value="quarantineCodes[String(row.id)] ?? ''"
                placeholder="现场钻孔编号"
                @input="setQuarantineCode(String(row.id), ($event.target as HTMLInputElement).value)"
              />
              <button class="link" type="button" @click="resolveQuarantine(row)">按现场编号迁入台账</button>
            </td>
          </tr>
          <tr v-if="!quarantineRows.length">
            <td :colspan="quarantineColumns.length + 1" class="empty-state">没有待补号的隔离行</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 5. 批次记录 -->
    <div v-show="activeTab === 'batches'">
      <table class="data-table">
        <thead>
          <tr><th v-for="column in batchColumns" :key="column">{{ column }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in batchRows" :key="String(row.id)">
            <td v-for="column in batchColumns" :key="column">{{ row[column] ?? '—' }}</td>
          </tr>
          <tr v-if="!batchRows.length">
            <td :colspan="batchColumns.length" class="empty-state">还没有任何导入批次</td>
          </tr>
        </tbody>
      </table>
    </div>

    <footer class="page-foot">
      <span v-if="globalError" class="error-text">{{ globalError }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request, sha256Hex, splitIntoLines } from '@/api/client'

const ENDPOINT = '/api/drilling_import'
const LOG_ENDPOINT = '/api/drilling_log'

type Row = Record<string, string | number | boolean | null>
type PreviewData = {
  有效行数: number
  隔离行数: number
  文件指纹: string
  待入库: Array<Record<string, string | number>>
  结论预览: Record<string, Record<string, unknown>>
}
type CommitResult = {
  ok: boolean
  message: string
  errors?: Array<{ 行号: number; 日志编号?: string; 钻孔编号?: string; 原因: string }>
}

const tabs = [
  { key: 'ledger', label: '日志台账' },
  { key: 'import', label: '整批导入' },
  { key: 'todos', label: '偏离待办' },
  { key: 'quarantine', label: '缺孔号隔离' },
  { key: 'batches', label: '批次记录' },
] as const
type TabKey = (typeof tabs)[number]['key']
const activeTab = ref<TabKey>('ledger')
const globalError = ref('')

const ledgerColumns = ['日志编号', '钻孔编号', '钻进深度', '记录类型', '岩层描述', '钻探人员', '库内结论深度', '结论来源', '日志状态']
const todoColumns = ['钻孔编号', '偏离类型', '偏离说明', '结论深度', '设计深度', '关联日志编号', '状态']
const quarantineColumns = ['文件名', '行号', '日志编号', '钻进深度', '记录类型', '状态', '隔离时间']
const batchColumns = ['文件名', '批次号', '状态', '总行数', '有效行数', '隔离行数', '待处理待办', '退回次数', '导入时间']

const ledgerRows = ref<Row[]>([])
const ledgerTotal = ref(0)
const ledgerKeyword = ref('')
const ledgerStatus = ref('')

const todoRows = ref<Row[]>([])
const quarantineRows = ref<Row[]>([])
const batchRows = ref<Row[]>([])
const quarantineCodes = ref<Record<string, string>>({})

const fileInput = ref<HTMLInputElement | null>(null)
const importFile = ref<File | null>(null)
const importContent = ref('')
const importLines = ref<string[]>([])
const importFingerprint = ref('')
const preview = ref<PreviewData | null>(null)
const importResult = ref<CommitResult | null>(null)
const previewLoading = ref(false)
const commitLoading = ref(false)
const uploading = ref(false)
const uploadHint = ref('断点续传方式上传并提交')

const tabBadges = computed<Record<string, number>>(() => ({
  ledger: ledgerTotal.value,
  todos: todoRows.value.filter((row) => row.状态 === '待处理').length,
  quarantine: quarantineRows.value.length,
  import: 0,
  batches: batchRows.value.length,
}))

async function readError(response: Response, fallback: string): Promise<string> {
  try {
    const payload = await response.json()
    const detail = payload.detail
    if (detail && typeof detail === 'object' && detail.kind === 'batch_rejected') {
      return detail.message
    }
    return typeof detail === 'string' ? detail : fallback
  } catch {
    return fallback
  }
}

async function readBatchRejection(response: Response): Promise<CommitResult> {
  const payload = await response.json()
  const detail = payload.detail ?? {}
  return { ok: false, message: detail.message ?? '整批退回，台账未做任何改动', errors: detail.errors ?? [] }
}

function switchTab(key: TabKey) {
  activeTab.value = key
  globalError.value = ''
  if (key === 'ledger') void reloadLedger()
  if (key === 'todos') void reloadTodos()
  if (key === 'quarantine') void reloadQuarantine()
  if (key === 'batches') void reloadBatches()
}

async function reloadLedger() {
  const params = new URLSearchParams()
  if (ledgerKeyword.value) params.set('keyword', ledgerKeyword.value)
  if (ledgerStatus.value) params.set('status', ledgerStatus.value)
  const response = await request(`${LOG_ENDPOINT}?${params.toString()}`)
  if (!response.ok) {
    globalError.value = '钻探日志列表读取失败'
    return
  }
  const payload = await response.json()
  ledgerRows.value = payload.items ?? []
  ledgerTotal.value = payload.total ?? 0
}

function resetLedgerFilters() {
  ledgerKeyword.value = ''
  ledgerStatus.value = ''
  void reloadLedger()
}

async function reloadTodos() {
  const response = await request(`${ENDPOINT}/deviations`)
  if (response.ok) todoRows.value = (await response.json()).items ?? []
}

async function reloadQuarantine() {
  const response = await request(`${ENDPOINT}/quarantine`)
  if (response.ok) quarantineRows.value = (await response.json()).items ?? []
}

async function reloadBatches() {
  const response = await request(`${ENDPOINT}/batches`)
  if (response.ok) batchRows.value = (await response.json()).items ?? []
}

function setQuarantineCode(id: string, code: string) {
  quarantineCodes.value[id] = code
}

async function onFilePicked(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  importFile.value = file
  importResult.value = null
  preview.value = null
  const content = await file.text()
  importContent.value = content
  importLines.value = splitIntoLines(content)
  importFingerprint.value = await sha256Hex(content)
}

async function runPreview() {
  if (!importFile.value || !importContent.value) return
  previewLoading.value = true
  globalError.value = ''
  try {
    const response = await request(`${ENDPOINT}/preview`, {
      method: 'POST',
      body: JSON.stringify({
        file_name: importFile.value.name,
        content: importContent.value,
        fingerprint: importFingerprint.value,
      }),
    })
    if (response.status === 422) {
      importResult.value = await readBatchRejection(response)
      preview.value = null
      return
    }
    if (!response.ok) {
      globalError.value = await readError(response, '导入预览失败')
      return
    }
    preview.value = await response.json()
  } finally {
    previewLoading.value = false
  }
}

async function runCommit() {
  if (!importFile.value || !importContent.value) return
  commitLoading.value = true
  globalError.value = ''
  try {
    const response = await request(`${ENDPOINT}/commit`, {
      method: 'POST',
      body: JSON.stringify({
        file_name: importFile.value.name,
        content: importContent.value,
        fingerprint: importFingerprint.value,
      }),
    })
    if (response.status === 422) {
      importResult.value = await readBatchRejection(response)
      await reloadBatches()
      return
    }
    if (!response.ok) {
      globalError.value = await readError(response, '整批提交失败')
      return
    }
    const data = await response.json()
    importResult.value = { ok: true, message: data.message }
    await Promise.all([reloadLedger(), reloadTodos(), reloadQuarantine(), reloadBatches()])
  } finally {
    commitLoading.value = false
  }
}

async function runResumableUpload() {
  if (!importFile.value || !importContent.value) return
  uploading.value = true
  globalError.value = ''
  try {
    const create = await request(`${ENDPOINT}/sessions`, {
      method: 'POST',
      body: JSON.stringify({ file_name: importFile.value.name }),
    })
    if (!create.ok) throw new Error('无法建立上传会话')
    const session = await create.json()
    const sid = session.session_id
    const lines = importLines.value

    // 模拟断线：先只送到一半，再从服务器确认的偏移量继续。
    const half = Math.max(1, Math.ceil(lines.length / 2))
    let ack = await postRows(sid, 0, lines.slice(0, half))
    uploadHint.value = `已确认 ${half} 行，模拟断线…`
    const resume = await getReceived(sid)
    ack = await postRows(sid, resume, lines.slice(resume))
    uploadHint.value = `续传完成，共确认 ${ack.received} 行，正在整批提交…`

    const response = await request(`${ENDPOINT}/sessions/${sid}/commit`, { method: 'POST' })
    if (response.status === 422) {
      importResult.value = await readBatchRejection(response)
      return
    }
    if (!response.ok) {
      globalError.value = await readError(response, '断点续传提交失败')
      return
    }
    const data = await response.json()
    importResult.value = { ok: true, message: `${data.message}（经断点续传，从第 ${resume} 行继续）` }
    await Promise.all([reloadLedger(), reloadTodos(), reloadQuarantine(), reloadBatches()])
  } catch (error) {
    globalError.value = error instanceof Error ? error.message : '断点续传失败'
  } finally {
    uploading.value = false
    uploadHint.value = '断点续传方式上传并提交'
  }
}

async function postRows(sessionId: string, offset: number, rows: string[]) {
  const response = await request(`${ENDPOINT}/sessions/${sessionId}/rows`, {
    method: 'POST',
    body: JSON.stringify({ offset, rows }),
  })
  if (!response.ok) throw new Error(await readError(response, '分片上传被拒绝'))
  return response.json()
}

async function getReceived(sessionId: string): Promise<number> {
  const response = await request(`${ENDPOINT}/sessions/${sessionId}`)
  if (!response.ok) throw new Error('会话已失效，请重新选择文件')
  return (await response.json()).received
}

async function closeTodo(row: Row) {
  const response = await request(`${ENDPOINT}/deviations/${row.id}/status`, {
    method: 'POST',
    body: JSON.stringify({ status: '已处理' }),
  })
  if (response.ok) await reloadTodos()
}

async function reopenTodo(row: Row) {
  const response = await request(`${ENDPOINT}/deviations/${row.id}/status`, {
    method: 'POST',
    body: JSON.stringify({ status: '待处理' }),
  })
  if (response.ok) await reloadTodos()
}

async function resolveQuarantine(row: Row) {
  const code = (quarantineCodes.value[String(row.id)] ?? '').trim()
  if (!code) {
    globalError.value = '请先填写现场钻孔编号再迁入台账'
    return
  }
  const response = await request(`${ENDPOINT}/quarantine/${row.id}/resolve`, {
    method: 'POST',
    body: JSON.stringify({ 钻孔编号: code }),
  })
  if (!response.ok) {
    globalError.value = await readError(response, '隔离行迁移失败')
    return
  }
  await Promise.all([reloadQuarantine(), reloadLedger(), reloadTodos()])
}

function afterCommit() {
  activeTab.value = 'ledger'
  void reloadLedger()
}

onMounted(() => {
  void reloadLedger()
  void reloadTodos()
  void reloadQuarantine()
  void reloadBatches()
})

</script>
