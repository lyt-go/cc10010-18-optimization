<template>
  <section class="page" data-module="dependency">
    <header class="page-head">
      <div>
        <h2>依赖解析管理</h2>
        <p class="page-desc">管理ETL作业间的依赖关系，定义时间偏移、触发条件和等待策略。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记依赖关系</button>
        <button class="btn" type="button" @click="exportRows">导出依赖解析清单</button>
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
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>共享预警水位</th>
          <th>关键下游延迟</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span :class="waterClass(row['共享预警水位'])">{{ row['共享预警水位'] ?? '正常' }}</span>
          </td>
          <td>
            <span v-if="row['关键下游延迟']" class="error-text">
              延迟{{ row['关键下游延迟原因'] ? `（${row['关键下游延迟原因']}）` : '' }}
            </span>
            <span v-else>—</span>
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
          <td :colspan="columns.length + 3" class="empty-state">暂无依赖解析数据，可先登记依赖关系</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条依赖解析记录</span>
      <span class="page-desc">预警水位与基线时间线读取同一份基线预警结果数据（工作日/节假日/统一UTC口径）</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>

const ENDPOINT = '/api/dependency'
const columns = ["关系编号", "上游作业", "下游作业", "依赖类型", "偏移量分钟", "触发条件", "等待超时", "跳过标志"]
const actions = ["提交关系评审", "生效关系", "解除关系"]
const statuses = ["草稿", "关系评审中", "已生效", "已解除"]
const stats = [{"label": "依赖关系总数", "value": 0}, {"label": "已生效关系", "value": 0}, {"label": "评审中关系", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

function waterClass(level: unknown): string {
  if (level === '已破线') return 'error-text'
  if (level === '即将破线') return 'warn-text'
  return ''
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '依赖关系登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('依赖解析动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '依赖解析操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    // 列表与共享水位分别读取，水位来自同一份基线预警结果数据
    const [listResp, waterResp] = await Promise.all([
      request(`${ENDPOINT}?${query}`),
      request(`${ENDPOINT}/shared-water-level`),
    ])
    if (!listResp.ok) throw new Error('依赖关系列表读取失败')
    const payload = await listResp.json()
    const list: Row[] = payload.items ?? []

    const waterMap = new Map<string, Row>()
    if (waterResp.ok) {
      const waterPayload = await waterResp.json()
      for (const item of (waterPayload.items ?? []) as Row[]) {
        waterMap.set(String(item['下游作业']), item)
      }
    }
    rows.value = list.map((row) => {
      const shared = waterMap.get(String(row['下游作业']))
      return shared ? { ...row, ...shared } : row
    })
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '依赖解析列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.warn-text {
  color: #b8860b;
  font-weight: 600;
}
</style>
