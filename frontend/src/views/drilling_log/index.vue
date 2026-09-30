<template>
  <section class="page" data-module="drilling_log">
    <header class="page-head">
      <div>
        <h2>钻探日志管理</h2>
        <p class="page-desc">整批导入按文件指纹幂等，解析、校验、落库与待办生成同批事务，任一行不通过整批退回；库内结论三处同源。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出钻探日志清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <section class="panel" data-test="import-panel">
      <h3 class="panel-title">整批导入（CSV / JSON）</h3>
      <div class="panel-row">
        <input ref="fileInput" type="file" accept=".csv,.json,.txt" @change="onFileChange" />
        <label class="muted">
          断点续传已确认行号
          <input v-model.number="resumeFrom" type="number" min="0" style="width: 80px; margin-left: 4px" />
        </label>
        <button class="btn" type="button" :disabled="!fileContent || busy" @click="doPreview">导入预览</button>
        <button class="btn primary" type="button" :disabled="!fileContent || busy" @click="doCommit">确认整批提交</button>
        <span v-if="busy" class="muted">处理中…若连接中断，重连后带上「已确认行号」从断点继续</span>
      </div>
      <p class="muted">
        终孔行在「是否终孔/记录类型」列标注（终孔、是、true 均可）；缺孔号的行先隔离，不拖垮整批；
        同号冲突以现场终孔为准，历史班次保留原上报基准；已确认深度不会被覆盖。
      </p>
      <div v-if="fingerprint" class="muted">文件指纹：{{ fingerprint }}</div>

      <div v-if="importMessage" :class="previewOk ? 'ok-text' : 'error-text'" style="margin: 6px 0">{{ importMessage }}</div>

      <template v-if="preview">
        <div v-if="previewErrors.length" class="panel" style="border-color: #fda29b">
          <h4 class="panel-title error-text">整批退回原因（{{ previewErrors.length }}）——未写入任何记录</h4>
          <table class="data-table">
            <thead><tr><th>行号</th><th>日志编号</th><th>原因</th></tr></thead>
            <tbody>
              <tr v-for="(err, idx) in previewErrors" :key="idx">
                <td>{{ err['行号'] ?? err.line }}</td>
                <td>{{ err['日志编号'] || '—' }}</td>
                <td class="error-text">{{ err.reason }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <details v-if="previewRows.length" open>
          <summary class="muted">逐行落库预案（{{ previewRows.length }}）</summary>
          <table class="data-table" style="margin-top: 6px">
            <thead>
              <tr><th>行号</th><th>日志编号</th><th>钻孔编号</th><th>钻进深度</th><th>终孔</th><th>动作</th><th>说明</th></tr>
            </thead>
            <tbody>
              <tr v-for="row in previewRows" :key="row['行号']">
                <td>{{ row['行号'] }}</td>
                <td>{{ row['日志编号'] }}</td>
                <td>{{ row['钻孔编号'] }}</td>
                <td>{{ formatDepth(row['钻进深度']) }}</td>
                <td>{{ row['是否终孔'] ? '是' : '否' }}</td>
                <td><span class="tag" :class="actionClass(row['动作'])">{{ row['动作'] }}</span></td>
                <td class="muted">{{ row['说明'] }}</td>
              </tr>
            </tbody>
          </table>
        </details>

        <details v-if="previewTodos.length" open>
          <summary class="muted">本批将生成 / 更新的偏离待办（{{ previewTodos.length }}）</summary>
          <table class="data-table" style="margin-top: 6px">
            <thead><tr><th>钻孔编号</th><th>类型</th><th>终孔深度</th><th>设计孔深</th><th>偏离率</th><th>说明</th></tr></thead>
            <tbody>
              <tr v-for="(todo, idx) in previewTodos" :key="idx">
                <td>{{ todo['钻孔编号'] }}</td>
                <td>{{ todo['类型'] }}</td>
                <td>{{ formatDepth(todo['终孔深度']) }}</td>
                <td>{{ formatDepth(todo['设计孔深']) }}</td>
                <td>{{ formatRate(todo['偏离率']) }}</td>
                <td class="muted">{{ todo['说明'] }}</td>
              </tr>
            </tbody>
          </table>
        </details>
      </template>
    </section>

    <section class="panel" data-test="quarantine-panel">
      <h3 class="panel-title">缺孔号隔离（{{ quarantineRows.length }}）</h3>
      <p class="muted">这些行未入台账；补齐现场孔号后放行，放行同样走整批事务，校验不过继续隔离。</p>
      <table class="data-table" v-if="quarantineRows.length">
        <thead><tr><th>行号</th><th>日志编号</th><th>钻进深度</th><th>来源文件</th><th>补孔号放行</th></tr></thead>
        <tbody>
          <tr v-for="row in quarantineRows" :key="String(row.id)">
            <td>{{ row['行号'] }}</td>
            <td>{{ row.values?.['日志编号'] }}</td>
            <td>{{ row.values?.['钻进深度'] }}</td>
            <td>{{ row['来源文件'] }}</td>
            <td>
              <form class="inline-form" @submit.prevent="releaseRow(row)">
                <input v-model="releaseInputs[row.id]" placeholder="补录钻孔编号" />
                <button class="btn primary" type="submit">放行</button>
              </form>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">没有待补孔号的隔离记录。</p>
    </section>

    <section class="panel" data-test="deviation-panel">
      <h3 class="panel-title">偏离待办清单（{{ deviationRows.length }}）— 终孔深度与日志台账、钻孔详情同源</h3>
      <table class="data-table" v-if="deviationRows.length">
        <thead><tr><th>钻孔编号</th><th>类型</th><th>终孔深度</th><th>设计孔深</th><th>偏离率</th><th>结论日志</th><th>说明</th><th>处理</th></tr></thead>
        <tbody>
          <tr v-for="row in deviationRows" :key="String(row.id)">
            <td>{{ row['钻孔编号'] }}</td>
            <td>{{ row['类型'] }}</td>
            <td>{{ formatDepth(row['终孔深度']) }}</td>
            <td>{{ formatDepth(row['设计孔深']) }}</td>
            <td>{{ formatRate(row['偏离率']) }}</td>
            <td>{{ row['结论日志编号'] || '—' }}</td>
            <td class="muted">{{ row['说明'] }}</td>
            <td><button class="link" type="button" @click="resolveDeviation(row)">现场确认</button></td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">没有待处理的偏离待办。</p>
    </section>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>库内终孔结论</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <template v-if="row['终孔深度'] != null">
              {{ formatDepth(row['终孔深度']) }}（{{ row['结论日志编号'] }}）
            </template>
            <span v-else class="muted">未终孔</span>
          </td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无钻探日志数据，可先整批导入</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条钻探日志记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
interface PreviewRow {
  行号: number
  日志编号: string
  钻孔编号: string
  钻进深度: number | null
  是否终孔: boolean
  动作: string
  说明: string
}
interface PreviewError { line: number; 行号?: number; 日志编号?: string; reason: string }
interface PreviewResponse {
  ok: boolean
  reused?: boolean
  fingerprint?: string
  message?: string
  rows?: PreviewRow[]
  errors?: PreviewError[]
  todos?: Array<Record<string, string | number | null>>
  quarantine?: Array<Record<string, unknown>>
  summary?: Record<string, number | string>
  resume?: { 已确认行号?: number; 指纹?: string }
}

const ENDPOINT = '/api/drilling_log'
const columns = ["日志编号", "钻孔编号", "钻进深度", "回次进尺", "岩层描述", "水位深度", "钻探人员", "日志状态"]
const actions = ["填写日志", "提交审核", "退回补充"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const stats = reactive([
  { label: '导入批次', value: 0 },
  { label: '已确认终孔', value: 0 },
  { label: '待处理偏离', value: 0 },
  { label: '缺孔号隔离', value: 0 },
])

const fileInput = ref<HTMLInputElement | null>(null)
const fileName = ref('')
const fileContent = ref('')
const fingerprint = ref('')
const resumeFrom = ref(0)
const busy = ref(false)
const preview = ref<PreviewResponse | null>(null)
const importMessage = ref('')

const quarantineRows = ref<Array<Record<string, any>>>([])
const releaseInputs = reactive<Record<number, string>>({})
const deviationRows = ref<Array<Record<string, any>>>([])

const previewRows = computed(() => preview.value?.rows ?? [])
const previewErrors = computed(() => preview.value?.errors ?? [])
const previewTodos = computed(() => preview.value?.todos ?? [])
const previewOk = computed(() => preview.value?.ok ?? false)

function formatDepth(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  const num = Number(value)
  return Number.isFinite(num) ? String(num) : String(value)
}

function formatRate(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  const num = Number(value)
  return Number.isFinite(num) ? `${(num * 100).toFixed(1)}%` : String(value)
}

function actionClass(action: string): string {
  if (action.includes('新增')) return 'insert'
  if (action.includes('终孔覆盖')) return 'override'
  if (action.includes('隔离')) return 'quarantine'
  return 'skip'
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  preview.value = null
  importMessage.value = ''
  fingerprint.value = ''
  if (!file) {
    fileName.value = ''
    fileContent.value = ''
    return
  }
  fileName.value = file.name
  fileContent.value = await file.text()
}

async function callImport(path: string): Promise<{ status: number; body: PreviewResponse }> {
  busy.value = true
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}${path}`, {
      method: 'POST',
      body: JSON.stringify({
        filename: fileName.value,
        content: fileContent.value,
        resume_from: resumeFrom.value || 0,
      }),
    })
    const body = (await response.json()) as PreviewResponse
    if (body.fingerprint) fingerprint.value = body.fingerprint
    return { status: response.status, body }
  } finally {
    busy.value = false
  }
}

async function doPreview() {
  try {
    const { body } = await callImport('/import/preview')
    preview.value = body
    importMessage.value = body.reused
      ? body.message ?? '该文件已成功导入，直接复用首次结果'
      : body.ok
        ? `预览通过：${body.summary ? summarize(body.summary) : '可提交'}；提交为整批事务，任一行不过整批退回`
        : body.message ?? '校验未通过，整批退回'
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导入预览失败'
  }
}

function summarize(summary: Record<string, number | string>): string {
  return `新增 ${summary['新增'] ?? 0}、终孔覆盖 ${summary['终孔覆盖'] ?? 0}、保留跳过 ${summary['保留跳过'] ?? 0}、隔离 ${summary['隔离'] ?? 0}、新增待办 ${summary['待办新增'] ?? 0}`
}

async function doCommit() {
  try {
    const { status, body } = await callImport('/import/commit')
    preview.value = body
    if (status === 200 && body.ok) {
      importMessage.value = body.reused
        ? body.message ?? '文件已导入过，未重复落库'
        : `${body.message ?? '整批导入成功'}（${body.summary ? summarize(body.summary) : ''}）`
      if (body.resume && typeof body.resume === 'object') {
        const confirmed = (body.resume as Record<string, number>)['已确认行号']
        if (typeof confirmed === 'number') resumeFrom.value = confirmed
      }
      if (fileInput.value) fileInput.value.value = ''
      fileContent.value = ''
      fileName.value = ''
      await Promise.all([reload(), loadAux()])
    } else {
      importMessage.value = body.message ?? '整批退回，未写入任何记录'
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导入提交失败，可按已确认行号断点续传'
  }
}

async function releaseRow(row: Record<string, any>) {
  const holeNo = (releaseInputs[row.id as number] || '').trim()
  if (!holeNo) {
    errorMessage.value = '请先补录钻孔编号再放行'
    return
  }
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/quarantine/${row.id}/release`, {
      method: 'POST',
      body: JSON.stringify({ hole_no: holeNo }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      errorMessage.value = payload.message ?? '放行被整批退回，记录仍在隔离'
      return
    }
    await Promise.all([reload(), loadAux()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '隔离放行失败'
  }
}

async function resolveDeviation(row: Record<string, any>) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/deviations/${row.id}/resolve`, {
      method: 'POST',
      body: JSON.stringify({ note: '现场确认' }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      errorMessage.value = payload.message ?? '偏离待办处理失败'
      return
    }
    await loadAux()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '偏离待办处理失败'
  }
}

async function loadAux() {
  try {
    const [statsRes, quarRes, devRes] = await Promise.all([
      request(`${ENDPOINT}/import/stats`),
      request(`${ENDPOINT}/quarantine`),
      request(`${ENDPOINT}/deviations?open_only=true`),
    ])
    const statPayload = await statsRes.json()
    stats[0].value = statPayload['导入批次'] ?? 0
    stats[1].value = statPayload['已确认终孔'] ?? 0
    stats[2].value = statPayload['待处理偏离'] ?? 0
    stats[3].value = statPayload['缺孔号隔离'] ?? 0
    quarantineRows.value = (await quarRes.json()).items ?? []
    deviationRows.value = (await devRes.json()).items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '辅助数据加载失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('钻探日志动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '钻探日志操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('钻探记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '钻探日志列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadAux()
})
</script>
