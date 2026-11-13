<template>
  <section class="page" data-module="resource">
    <header class="page-head">
      <div>
        <h2>资源计量管理</h2>
        <p class="page-desc">按计算引擎、计费周期与结算状态叠加筛选；汇总只统计筛选后的已结算记录，与明细、导出保持一致。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记计量记录</button>
        <button class="btn" type="button" @click="exportRows">导出资源计量清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="search">
      <label class="filter-item">
        <span>记录编号</span>
        <input v-model="filters.keyword" placeholder="按记录编号检索" />
      </label>
      <label class="filter-item">
        <span>计算引擎</span>
        <input v-model="filters.engine" placeholder="按计算引擎检索" />
      </label>
      <label class="filter-item">
        <span>计费周期</span>
        <input v-model="filters.period" placeholder="如 2026-10" />
      </label>
      <label class="filter-item">
        <span>结算状态</span>
        <select v-model="filters.status">
          <option value="">全部状态</option>
          <option v-for="item in statuses" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
      <label class="filter-item">
        <span>定位记录</span>
        <input v-model="locateNo" placeholder="输入记录编号定位" />
      </label>
      <button class="btn ghost" type="button" @click="locateRecord">定位</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>状态</th>
          <th>金额</th>
          <th>结算凭证</th>
          <th>失败原因</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :class="{ 'row-hit': row.id === highlightId }">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status ?? '—' }}</td>
          <td>{{ row['金额'] ?? '—' }}</td>
          <td>{{ row['结算凭证'] ?? '—' }}</td>
          <td>{{ row['失败原因'] ?? '—' }}</td>
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
          <td :colspan="columns.length + 5" class="empty-state">暂无资源计量数据，可先登记计量记录</td>
        </tr>
      </tbody>
    </table>

    <section v-if="pendingList.length" class="pending-panel">
      <h3>待汇总清单（含失败原因）</h3>
      <ul>
        <li v-for="item in pendingList" :key="String(item.id)">
          {{ item['记录编号'] }} · {{ item['计算引擎'] }} · {{ item['作业名称'] }} · {{ item['计费周期'] }}
          <span v-if="item['失败原因']" class="error-text">失败原因：{{ item['失败原因'] }}</span>
          <span v-else>等待结算</span>
        </li>
      </ul>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条资源计量记录，第 {{ page }} / {{ pageCount }} 页</span>
      <span class="page-turn">
        <button class="btn ghost" type="button" :disabled="page <= 1" @click="turnPage(-1)">上一页</button>
        <button class="btn ghost" type="button" :disabled="page >= pageCount" @click="turnPage(1)">下一页</button>
      </span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/resource'
const columns = ["记录编号", "计算引擎", "作业名称", "CPU核秒", "内存GB秒", "存储占用GB", "扫描数据量", "计费周期"]
const actions = ["提交汇总", "结算确认", "归档记录"]
const statuses = ["草稿", "待汇总", "已结算", "已归档"]
const PAGE_SIZE = 20

const rows = ref<Row[]>([])
const total = ref(0)
const page = ref(1)
const errorMessage = ref('')
const locateNo = ref('')
const highlightId = ref<number | null>(null)
const filters = reactive({ keyword: '', engine: '', period: '', status: '' })
const summary = ref<{
  total: number
  settled: { count: number; 金额: number }
  pending: { count: number; items: Row[] }
  failed_count: number
} | null>(null)

const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))
const pendingList = computed<Row[]>(() => summary.value?.pending.items ?? [])
const stats = computed(() => [
  { label: '计量记录总数', value: summary.value?.total ?? 0 },
  { label: '已结算记录', value: summary.value?.settled.count ?? 0 },
  { label: '已结算金额', value: summary.value?.settled.金额 ?? 0 },
  { label: '待汇总记录', value: summary.value?.pending.count ?? 0 },
  { label: '失败记录', value: summary.value?.failed_count ?? 0 },
])

function filterQuery(): URLSearchParams {
  const query = new URLSearchParams()
  if (filters.keyword) query.set('keyword', filters.keyword)
  if (filters.engine) query.set('engine', filters.engine)
  if (filters.period) query.set('period', filters.period)
  if (filters.status) query.set('status', filters.status)
  return query
}

function search() {
  page.value = 1
  void reload()
}

function resetFilters() {
  filters.keyword = ''
  filters.engine = ''
  filters.period = ''
  filters.status = ''
  page.value = 1
  void reload()
}

function turnPage(step: number) {
  const next = page.value + step
  if (next < 1 || next > pageCount.value) return
  page.value = next
  void reload()
}

function exportRows() {
  // 导出与列表共用同一套筛选条件，条数和金额才能吻合
  window.open(`${ENDPOINT}/export?${filterQuery().toString()}`, '_blank')
}

function openCreate() {
  errorMessage.value = '计量记录登记入口尚未接入审批流'
}

async function locateRecord() {
  errorMessage.value = ''
  highlightId.value = null
  const recordNo = locateNo.value.trim()
  if (!recordNo) return
  const query = filterQuery()
  query.set('record_no', recordNo)
  query.set('size', String(PAGE_SIZE))
  try {
    const response = await request(`${ENDPOINT}/locate?${query.toString()}`)
    const payload = await response.json()
    if (!payload.ok) {
      errorMessage.value = payload.message ?? '记录不在当前筛选结果集内'
      return
    }
    page.value = payload.page
    highlightId.value = payload.entry_id
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '定位失败'
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
    if (!payload.ok) {
      errorMessage.value = payload.message ?? '资源计量动作未生效'
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '资源计量操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = filterQuery()
  query.set('page', String(page.value))
  query.set('size', String(PAGE_SIZE))
  try {
    const [listResponse, summaryResponse] = await Promise.all([
      request(`${ENDPOINT}?${query.toString()}`),
      request(`${ENDPOINT}/summary?${filterQuery().toString()}`),
    ])
    if (!listResponse.ok || !summaryResponse.ok) {
      throw new Error('计量记录列表读取失败')
    }
    const payload = await listResponse.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    summary.value = await summaryResponse.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '资源计量列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.pending-panel {
  margin-top: 12px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 14px;
}
.pending-panel h3 {
  margin: 0 0 8px;
  font-size: 13px;
}
.pending-panel ul {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.page-turn {
  display: inline-flex;
  gap: 6px;
}
.row-hit td {
  background: #fff8e6;
}
</style>
