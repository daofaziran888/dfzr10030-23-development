<template>
  <section class="page" data-module="log_viewer">
    <header class="page-head">
      <div>
        <h2>采集日志管理</h2>
        <p class="page-desc">维护日志条目，围绕日志编号、关联任务、日志级别、记录时间做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记日志条目</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出采集日志清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集日志数据，可先登记日志条目</td>
        </tr>
      </tbody>
    </table>

    <section class="dependency-panel" data-testid="log-dependency-panel">
      <header class="panel-head">
        <h3>依赖等待清单 · 日志侧核对</h3>
        <div class="page-actions">
          <button class="btn" type="button" data-testid="sync-waiting-logs" @click="syncWaitingLogs">为延迟依赖补日志</button>
          <button class="btn" type="button" data-testid="sync-workbench" @click="syncWorkbench">生成待审核工作台项</button>
          <button class="btn" type="button" data-testid="refresh-verify" @click="loadDependency">刷新核对</button>
        </div>
      </header>
      <div class="stat-row">
        <article v-for="item in verifySummary" :key="item.label" class="stat-card">
          <span class="stat-label">{{ item.label }}</span>
          <strong class="stat-value">{{ item.value }}</strong>
        </article>
      </div>
      <p v-if="panelMessage" class="panel-message" data-testid="log-panel-message">{{ panelMessage }}</p>
      <ul class="snapshot-list">
        <li v-for="item in waitingItems" :key="`${item.plan_id}-${item.快照版本}`" class="snapshot-row" data-testid="log-waiting-row">
          <strong>{{ item.计划编号 }} · {{ item.阶段 }}</strong>
          <span>依赖任务：{{ item.依赖任务 }}（{{ item.依赖完成状态 }}）</span>
          <span>等待原因：{{ item.等待原因 }}</span>
          <span>
            关联任务日志：
            <button v-if="item.关联任务日志编号" class="link" type="button" @click="fillKeyword(item.关联任务日志编号)">
              LOG #{{ item.关联任务日志编号 }}
            </button>
            <template v-else>未记录</template>
          </span>
          <span>
            依赖回执：
            <button v-if="item.依赖回执编号" class="link" type="button" :data-testid="`open-receipt-${item.依赖回执编号}`" @click="openReceipt(item.依赖回执编号)">
              #{{ item.依赖回执编号 }}
            </button>
            <template v-else>未回执</template>
          </span>
        </li>
        <li v-if="!waitingItems.length" class="empty-state">等待清单为空，所有依赖已完成</li>
      </ul>

      <header class="panel-head"><h4>运行计划核对状态</h4></header>
      <ul class="snapshot-list">
        <li v-for="item in verifyItems" :key="`${item.plan_id}-${item.快照版本}-verify`" class="snapshot-row" data-testid="verify-row">
          <strong>{{ item.计划编号 }} · v{{ item.快照版本 }} · {{ item.核对状态 }}</strong>
          <span>计划状态：{{ item.计划状态 }} / {{ item.阶段 }}</span>
          <span>依赖完成状态：{{ item.依赖完成状态 }}</span>
          <span>核对说明：{{ item.核对说明 }}</span>
        </li>
        <li v-if="!verifyItems.length" class="empty-state">暂无运行计划可核对</li>
      </ul>

      <div v-if="receiptDetail" class="receipt-box" data-testid="log-receipt-detail">
        <h4>依赖回执 #{{ receiptDetail.receipt.id }}</h4>
        <p>任务 {{ receiptDetail.receipt.任务编号 }} · {{ receiptDetail.receipt.完成状态 }} · 分区 {{ receiptDetail.receipt.依赖分区 }}</p>
        <p class="panel-message">{{ receiptDetail.receipt.回执说明 || '无回执说明' }}</p>
        <button class="btn ghost" type="button" data-testid="close-log-receipt" @click="receiptDetail = null">关闭回执</button>
      </div>

      <header class="panel-head"><h4>待审核工作台项</h4></header>
      <ul class="snapshot-list">
        <li v-for="item in workbenchItems" :key="String(item.id)" class="snapshot-row" data-testid="workbench-row">
          <strong>{{ item.计划编号 }} · {{ item.审核状态 }}</strong>
          <span>依赖任务：{{ item.依赖任务 }}（{{ item.依赖完成状态 }}）</span>
          <span>等待原因：{{ item.等待原因 }}</span>
          <span>关联任务日志：{{ item.关联任务日志编号 || '未记录' }}</span>
        </li>
        <li v-if="!workbenchItems.length" class="empty-state">暂无待审核工作台项，可先同步生成</li>
      </ul>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记日志条目</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集日志记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type WaitingItem = {
  计划编号: string
  阶段: string
  依赖任务: string
  依赖完成状态: string
  等待原因: string
  plan_id: number
  快照版本: number
  依赖回执编号: number | null
  关联任务日志编号: string | null
}
type VerifyItem = {
  计划编号: string
  plan_id: number
  快照版本: number
  计划状态: string
  阶段: string
  依赖完成状态: string
  核对状态: string
  核对说明: string
}

const ENDPOINT = '/api/log_viewer'
const columns = ["日志编号", "关联任务", "日志级别", "记录时间", "日志内容", "错误堆栈", "处理状态", "处理人"]
const actions = ["开始处理", "标记已处理", "忽略日志"]
const statuses = ["未处理", "处理中", "已处理", "已忽略"]
const metricNames = ["未处理日志", "处理中日志", "已处理日志"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const waitingItems = ref<WaitingItem[]>([])
const verifyItems = ref<VerifyItem[]>([])
const workbenchItems = ref<Row[]>([])
const verifySummary = ref<{ label: string; value: number }[]>([])
const panelMessage = ref('')
const receiptDetail = ref<{ receipt: Row; logs: Row[] } | null>(null)
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'log_viewer.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
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
      throw new Error('采集日志动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集日志操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('日志条目列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    await loadDependency()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集日志列表读取失败'
  }
}

async function loadDependency() {
  try {
    const [waitingResponse, verifyResponse, workbenchResponse] = await Promise.all([
      request(`${ENDPOINT}/dependency-waiting`),
      request(`${ENDPOINT}/verify`),
      request(`${ENDPOINT}/workbench`),
    ])
    if (waitingResponse.ok) waitingItems.value = (await waitingResponse.json()).items ?? []
    if (verifyResponse.ok) {
      const payload = await verifyResponse.json()
      verifyItems.value = payload.items ?? []
      const summary = payload.summary ?? {}
      verifySummary.value = [
        { label: '运行计划', value: summary['运行计划'] ?? 0 },
        { label: '等待清单', value: summary['等待清单'] ?? 0 },
        { label: '核对一致', value: summary['一致'] ?? 0 },
        { label: '待核对', value: summary['待核对'] ?? 0 },
        { label: '缺日志', value: summary['缺日志'] ?? 0 },
        { label: '冲突', value: summary['冲突'] ?? 0 },
        { label: '未排期', value: summary['未排期'] ?? 0 },
      ]
    }
    if (workbenchResponse.ok) workbenchItems.value = (await workbenchResponse.json()).items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '依赖等待清单读取失败'
  }
}

async function syncWaitingLogs() {
  await postDependency(`${ENDPOINT}/sync-waiting-logs`, {})
}

async function syncWorkbench() {
  await postDependency(`${ENDPOINT}/sync-workbench`, {})
}

async function postDependency(url: string, body: unknown) {
  panelMessage.value = ''
  try {
    const response = await request(url, { method: 'POST', body: JSON.stringify(body) })
    const payload = await response.json()
    panelMessage.value = payload.message || ''
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
  await reload()
}

function fillKeyword(logId: string) {
  filters.value['日志编号'] = logId
  void reload()
}

async function openReceipt(receiptId: number) {
  try {
    const response = await request(`/api/schedule/receipts/${receiptId}`)
    if (!response.ok) throw new Error('依赖回执读取失败')
    receiptDetail.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '依赖回执读取失败'
  }
}

onMounted(() => {
  const keyword = new URLSearchParams(window.location.search).get('keyword')
  if (keyword) filters.value['日志编号'] = keyword
  void reload()
})
</script>
