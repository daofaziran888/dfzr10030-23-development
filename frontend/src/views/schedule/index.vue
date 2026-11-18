<template>
  <section class="page" data-module="schedule">
    <header class="page-head">
      <div>
        <h2>采集调度管理</h2>
        <p class="page-desc">
          维护调度计划，并把计划绑定到关联任务与依赖完成状态；依赖未完成时计划停在等待阶段，运行计划与等待清单可直接核对。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记调度计划</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出采集调度清单</button>
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
            <button class="link" type="button" :data-testid="`inspect-${row.id}`" @click="inspect(row)">依赖情况</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集调度数据，可先登记调度计划</td>
        </tr>
      </tbody>
    </table>

    <section v-if="selected" class="dependency-panel" data-testid="dependency-panel">
      <header class="panel-head">
        <h3>依赖完成情况 · {{ selected.计划编号 }}</h3>
        <button class="btn ghost" type="button" data-testid="close-dependency" @click="selected = null">收起</button>
      </header>
      <dl class="dependency-grid">
        <div><dt>关联任务</dt><dd data-testid="dep-related">{{ selected.关联任务 || '—' }}</dd></div>
        <div><dt>依赖任务</dt><dd data-testid="dep-task">{{ selected.依赖任务 || '—' }}</dd></div>
        <div><dt>依赖完成状态</dt><dd data-testid="dep-state">{{ selected.依赖完成状态 || '—' }}</dd></div>
        <div><dt>计划状态 / 阶段</dt><dd data-testid="dep-stage">{{ selected.计划状态 }} / {{ selected.阶段 }}</dd></div>
        <div class="span-2"><dt>依赖完成依据</dt><dd data-testid="dep-evidence">{{ selected.依赖完成依据 || '—' }}</dd></div>
        <div class="span-2"><dt>等待原因</dt><dd data-testid="dep-reason">{{ selected.等待原因 || '无（依赖已完成）' }}</dd></div>
        <div><dt>缺失分区</dt><dd data-testid="dep-partition">{{ selected.缺失分区 || '—' }}</dd></div>
        <div>
          <dt>关联任务日志</dt>
          <dd>
            <button
              v-if="selected.关联任务日志编号"
              class="link"
              type="button"
              data-testid="dep-log-link"
              @click="openLog(selected.关联任务日志编号)"
            >LOG #{{ selected.关联任务日志编号 }}</button>
            <span v-else data-testid="dep-log-missing">暂无（可在采集日志页同步）</span>
          </dd>
        </div>
        <div>
          <dt>依赖回执</dt>
          <dd>
            <button
              v-if="selected.依赖回执编号"
              class="link"
              type="button"
              data-testid="dep-receipt-link"
              @click="openReceipt(selected.依赖回执编号)"
            >回执 #{{ selected.依赖回执编号 }}</button>
            <span v-else data-testid="dep-receipt-missing">暂无回执</span>
          </dd>
        </div>
      </dl>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="recalculate" @click="recalculate">按适用范围重算</button>
        <button class="btn" type="button" data-testid="compare" @click="compare">比较延迟边界</button>
        <button class="btn" type="button" data-testid="generate-batch" @click="generateBatch">产出运行批次</button>
        <button class="btn" type="button" data-testid="complete" @click="complete">标记完成</button>
        <button class="btn" type="button" data-testid="archive" @click="archive">归档</button>
        <button class="btn" type="button" data-testid="restore" @click="restore">恢复</button>
      </div>
      <p v-if="panelMessage" class="panel-message" data-testid="panel-message">{{ panelMessage }}</p>

      <div v-if="comparison" class="compare-box" data-testid="compare-box">
        <p class="compare-scene">{{ comparison.比较场景 }}</p>
        <ul class="snapshot-list">
          <li v-for="option in comparison.选项" :key="option.选项" class="snapshot-row" :data-testid="`compare-${option.选项}`">
            <strong>{{ option.选项 }}</strong>
            <span>计划状态：{{ option.计划状态 }}</span>
            <span>阶段：{{ option.阶段 }}</span>
            <span>产出运行批次：{{ option.是否产出运行批次 ? '是' : '否' }}</span>
            <span>读取依赖回执：{{ option.是否读取依赖回执 ? '是' : '否' }}</span>
          </li>
        </ul>
        <p class="panel-message">{{ comparison.结论 }}</p>
      </div>

      <div v-if="receiptDetail" class="receipt-box" data-testid="receipt-detail">
        <h4>依赖回执 #{{ receiptDetail.receipt.id }}</h4>
        <p>任务 {{ receiptDetail.receipt.任务编号 }} · {{ receiptDetail.receipt.完成状态 }} · 分区 {{ receiptDetail.receipt.依赖分区 }}</p>
        <p class="panel-message">{{ receiptDetail.receipt.回执说明 || '无回执说明' }}</p>
        <p>关联采集日志：{{ receiptDetail.logs.length ? receiptDetail.logs.map((log) => `LOG #${log.id}（${log.状态 || log.status}）`).join('、') : '暂无' }}</p>
        <button class="btn ghost" type="button" data-testid="close-receipt" @click="receiptDetail = null">关闭回执</button>
      </div>
    </section>

    <section class="dependency-panel" data-testid="run-plan-panel">
      <header class="panel-head">
        <h3>调度运行计划</h3>
        <label class="inline-check">
          <input v-model="includeHistory" type="checkbox" data-testid="toggle-history" @change="loadRunPlans" />
          查看历史版本
        </label>
      </header>
      <ul class="snapshot-list">
        <li v-for="item in runPlans" :key="`${item.plan_id}-${item.快照版本}`" class="snapshot-row" data-testid="run-plan-row">
          <strong>{{ item.计划编号 }} · v{{ item.快照版本 }}</strong>
          <span>关联任务：{{ item.关联任务 }}</span>
          <span>依赖任务：{{ item.依赖任务 }}</span>
          <span>计划状态：{{ item.计划状态 }} / {{ item.阶段 }}</span>
          <span>依赖完成状态：{{ item.依赖完成状态 }}</span>
          <span>等待原因：{{ item.等待原因 || '—' }}</span>
        </li>
        <li v-if="!runPlans.length" class="empty-state">暂无运行计划</li>
      </ul>
    </section>

    <section class="dependency-panel" data-testid="waiting-panel">
      <header class="panel-head"><h3>依赖等待清单</h3></header>
      <ul class="snapshot-list">
        <li v-for="item in waitingList" :key="`${item.plan_id}-${item.快照版本}`" class="snapshot-row" data-testid="waiting-row">
          <strong>{{ item.计划编号 }} · {{ item.阶段 }}</strong>
          <span>依赖任务：{{ item.依赖任务 }}（{{ item.依赖完成状态 }}）</span>
          <span>缺失分区：{{ item.缺失分区 }}</span>
          <span>等待原因：{{ item.等待原因 }}</span>
          <span>
            关联任务日志：
            <button
              v-if="item.关联任务日志编号"
              class="link"
              type="button"
              @click="openLog(item.关联任务日志编号)"
            >LOG #{{ item.关联任务日志编号 }}</button>
            <template v-else>—</template>
          </span>
        </li>
        <li v-if="!waitingList.length" class="empty-state">当前没有依赖未完成的计划</li>
      </ul>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记调度计划</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <label class="create-field">
          <span>延迟策略</span>
          <select v-model="createValues['延迟策略']" data-testid="create-延迟策略">
            <option v-for="policy in delayPolicies" :key="policy" :value="policy">{{ policy }}</option>
          </select>
        </label>
        <label class="create-field">
          <span>适用范围</span>
          <select v-model="createValues['适用范围']" data-testid="create-适用范围">
            <option v-for="scope in scopes" :key="scope" :value="scope">{{ scope }}</option>
          </select>
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
      <span>共 {{ total }} 条采集调度记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type DependencyDetail = {
  计划编号: string
  关联任务: string
  依赖任务: string
  依赖完成状态: string
  依赖完成依据: string
  缺失分区: string
  等待原因: string | null
  计划状态: string
  阶段: string
  依赖回执编号: number | null
  关联任务日志编号: string | null
}
type CompareOption = {
  选项: string
  计划状态: string
  阶段: string
  是否产出运行批次: boolean
  是否读取依赖回执: boolean
}

const ENDPOINT = '/api/schedule'
const columns = ["计划编号", "关联任务", "调度周期", "开始时间", "结束时间", "依赖任务", "失败策略", "计划状态"]
const actions = ["确认排期", "启动调度", "暂停调度"]
const statuses = ["草稿", "已排期", "运行中", "已完成", "已暂停"]
const metricNames = ["调度计划总数", "已排期计划", "已完成计划"]
const delayPolicies = ['停在已排期', '进入暂运行']
const scopes = ['仅本计划', '同依赖任务']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const selected = ref<DependencyDetail | null>(null)
const selectedId = ref<number | null>(null)
const panelMessage = ref('')
const comparison = ref<{ 比较场景: string; 选项: CompareOption[]; 结论: string } | null>(null)
const receiptDetail = ref<{ receipt: Row; logs: Row[] } | null>(null)
const runPlans = ref<Row[]>([])
const waitingList = ref<Row[]>([])
const includeHistory = ref(false)

const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[3]).length },
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
    link.download = 'schedule.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  createValues.value['延迟策略'] = delayPolicies[0]
  createValues.value['适用范围'] = scopes[0]
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
    const payload = await response.json()
    if (payload.ok === false) {
      errorMessage.value = payload.message || '采集调度动作未生效'
    }
    await reload()
    if (selectedId.value === Number(row.id)) await inspect(row)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集调度操作失败'
  }
}

async function inspect(row: Row) {
  panelMessage.value = ''
  comparison.value = null
  receiptDetail.value = null
  try {
    const response = await request(`${ENDPOINT}/${row.id}/dependency`)
    if (!response.ok) throw new Error('依赖情况读取失败')
    selected.value = (await response.json()) as DependencyDetail
    selectedId.value = Number(row.id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '依赖情况读取失败'
  }
}

async function recalculate() {
  if (selectedId.value === null) return
  await postPanel(`${ENDPOINT}/${selectedId.value}/recalculate`, {})
}

async function compare() {
  if (selectedId.value === null) return
  panelMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${selectedId.value}/compare`)
    if (!response.ok) throw new Error('延迟边界比较失败')
    comparison.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '延迟边界比较失败'
  }
}

async function generateBatch() {
  if (selectedId.value === null) return
  await postPanel(`${ENDPOINT}/${selectedId.value}/batches`, undefined)
}

async function complete() {
  if (selectedId.value === null) return
  await postPanel(`${ENDPOINT}/${selectedId.value}/complete`, undefined)
}

async function archive() {
  if (selectedId.value === null) return
  await postPanel(`${ENDPOINT}/${selectedId.value}/archive`, undefined)
}

async function restore() {
  if (selectedId.value === null) return
  await postPanel(`${ENDPOINT}/${selectedId.value}/restore`, undefined)
}

async function postPanel(url: string, body: unknown) {
  errorMessage.value = ''
  try {
    const response = await request(url, {
      method: 'POST',
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    const payload = await response.json()
    panelMessage.value = payload.message || ''
    if (payload.ok === false) errorMessage.value = payload.message || '操作未生效'
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
  await reload()
  if (selected.value) {
    const detail = await request(`${ENDPOINT}/${selectedId.value}/dependency`)
    if (detail.ok) selected.value = await detail.json()
  }
}

async function openReceipt(receiptId: number) {
  try {
    const response = await request(`${ENDPOINT}/receipts/${receiptId}`)
    if (!response.ok) throw new Error('依赖回执读取失败')
    receiptDetail.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '依赖回执读取失败'
  }
}

function openLog(logId: string | number) {
  window.location.href = `/log_viewer?keyword=${encodeURIComponent(String(logId))}`
}

async function loadRunPlans() {
  const query = includeHistory.value ? '?include_history=true' : ''
  try {
    const response = await request(`${ENDPOINT}/run-plans${query}`)
    if (!response.ok) throw new Error('运行计划读取失败')
    runPlans.value = (await response.json()).items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '运行计划读取失败'
  }
}

async function loadWaiting() {
  try {
    const response = await request(`${ENDPOINT}/waiting-list`)
    if (!response.ok) throw new Error('依赖等待清单读取失败')
    waitingList.value = (await response.json()).items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '依赖等待清单读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('调度计划列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    await Promise.all([loadRunPlans(), loadWaiting()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集调度列表读取失败'
  }
}

onMounted(reload)
</script>
