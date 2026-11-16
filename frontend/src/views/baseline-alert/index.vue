<template>
  <section class="page" data-module="baseline-alert">
    <header class="page-head">
      <div>
        <h2>基线预警重算</h2>
        <p class="page-desc">
          从基线承诺时间与依赖触发数据重算预警：工作日规则 + 节假日偏移 + 统一UTC时区，
          禁止按自然日直接破线；基线管理与依赖解析共享同一份预警水位。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" :disabled="recomputing" @click="recompute">
          {{ recomputing ? '重算中…' : '重算基线预警' }}
        </button>
        <button class="btn" type="button" @click="loadAll">刷新预警结果</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p v-if="lastMessage" class="page-desc">{{ lastMessage }}</p>

    <form class="filter-bar" @submit.prevent="loadAll">
      <label class="filter-item">
        <span>预警水位</span>
        <select v-model="waterLevel">
          <option value="">全部</option>
          <option value="正常">正常</option>
          <option value="即将破线">即将破线</option>
          <option value="已破线">已破线</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
    </form>

    <h3>基线预警结果数据（唯一预警水位来源）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in alertColumns" :key="column">{{ column }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in alerts" :key="String(row.id)">
          <td>{{ row['基线编号'] ?? '—' }}</td>
          <td>{{ row['作业名称'] ?? '—' }}</td>
          <td>
            <span :class="waterClass(row['预警水位'])">{{ row['预警水位'] ?? '—' }}</span>
          </td>
          <td>{{ row['承诺时间本地'] ?? '—' }}</td>
          <td>{{ row['剩余分钟'] ?? '—' }}</td>
          <td>{{ row['工作日余量分钟'] ?? '—' }}</td>
          <td>{{ row['自然日差_仅展示_不参与判定'] ?? '—' }}</td>
          <td>
            <span v-if="row['关键下游延迟']" class="error-text">延迟（{{ row['关键下游延迟次数'] }}）</span>
            <span v-else>—</span>
          </td>
          <td>{{ row['规则版本'] ?? '—' }}</td>
          <td>{{ row['重算版本'] ?? '—' }}</td>
          <td>
            <span v-if="row['冻结标志']">历史冻结</span>
            <span v-else>随口径重算</span>
          </td>
        </tr>
        <tr v-if="!alerts.length">
          <td :colspan="alertColumns.length" class="empty-state">暂无预警结果，请先执行重算</td>
        </tr>
      </tbody>
    </table>

    <h3>依赖触发数据（晚到触发将修正已生成预警）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in triggerColumns" :key="column">{{ column }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in triggers" :key="String(row.id)">
          <td>{{ row['下游作业'] ?? '—' }}</td>
          <td>{{ row['上游作业'] ?? '—' }}</td>
          <td>{{ row['触发时间UTC'] ?? '—' }}</td>
          <td>{{ row['应到时间UTC'] ?? '—' }}</td>
          <td>{{ row['关键下游标志'] ? '关键' : '普通' }}</td>
          <td>{{ row['到达批次'] ?? '—' }}</td>
        </tr>
        <tr v-if="!triggers.length">
          <td :colspan="triggerColumns.length" class="empty-state">暂无依赖触发数据</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>

const alertColumns = [
  '基线编号', '作业名称', '预警水位', '承诺时间本地', '剩余分钟',
  '工作日余量分钟', '自然日差(仅展示)', '关键下游延迟', '规则版本', '重算版本', '判定策略',
]
const triggerColumns = ['下游作业', '上游作业', '触发时间UTC', '应到时间UTC', '下游级别', '到达批次']

const alerts = ref<Row[]>([])
const triggers = ref<Row[]>([])
const waterLevel = ref('')
const recomputing = ref(false)
const lastMessage = ref('')

const stats = computed(() => [
  { label: '预警结果总数', value: alerts.value.length },
  { label: '正常', value: alerts.value.filter((r) => r['预警水位'] === '正常').length },
  { label: '即将破线', value: alerts.value.filter((r) => r['预警水位'] === '即将破线').length },
  { label: '已破线', value: alerts.value.filter((r) => r['预警水位'] === '已破线').length },
  { label: '关键下游延迟', value: alerts.value.filter((r) => r['关键下游延迟']).length },
])

function waterClass(level: unknown): string {
  if (level === '已破线') return 'error-text'
  if (level === '即将破线') return 'warn-text'
  return ''
}

async function loadAll() {
  try {
    const query = waterLevel.value ? `?water_level=${encodeURIComponent(waterLevel.value)}&size=100` : '?size=100'
    const alertResp = await request(`/api/baseline/alerts${query}`)
    if (!alertResp.ok) throw new Error('预警结果读取失败')
    const alertPayload = await alertResp.json()
    alerts.value = alertPayload.items ?? []

    const triggerResp = await request('/api/baseline/triggers?size=100')
    if (!triggerResp.ok) throw new Error('依赖触发数据读取失败')
    const triggerPayload = await triggerResp.json()
    triggers.value = triggerPayload.items ?? []
  } catch (error) {
    lastMessage.value = error instanceof Error ? error.message : '基线预警数据读取失败'
  }
}

async function recompute() {
  recomputing.value = true
  lastMessage.value = ''
  try {
    const resp = await request('/api/baseline/alerts/recompute', {
      method: 'POST',
      body: JSON.stringify({ values: {} }),
    })
    if (!resp.ok) throw new Error('重算请求未生效')
    const payload = await resp.json()
    const s = payload.entry?.stats ?? {}
    lastMessage.value =
      `重算完成：扫描 ${s.scanned ?? 0} 条，新增 ${s.created ?? 0}，更新 ${s.updated ?? 0}，` +
      `历史冻结 ${s.frozen ?? 0}，新通知关键下游 ${s.notified_once ?? 0} 个`
    await loadAll()
  } catch (error) {
    lastMessage.value = error instanceof Error ? error.message : '基线预警重算失败'
  } finally {
    recomputing.value = false
  }
}

onMounted(loadAll)
</script>

<style scoped>
.warn-text {
  color: #b8860b;
  font-weight: 600;
}
</style>
