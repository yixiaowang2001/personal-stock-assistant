<template>
  <div class="positions-page">
    <div class="page-header">
      <h1 class="page-title">
        <el-icon><TrendCharts /></el-icon>
        持仓分析
      </h1>
      <p class="page-description">
        从 IBKR 同步的真实账户持仓，仅用于个人记录和分析
      </p>
      <div class="header-actions">
        <el-button
          type="primary"
          :icon="Refresh"
          :loading="refreshing"
          @click="onRefresh"
        >
          刷新持仓
        </el-button>
      </div>
    </div>

    <el-alert
      type="info"
      :closable="false"
      show-icon
      style="margin-bottom: 16px;"
    >
      <template #title>说明</template>
      <div style="font-size: 13px; line-height: 1.6;">
        数据来源于 IBKR Flex 报表，可能存在结算/汇率时点差异。本页面仅用于分析展示，不构成投资建议，也不进行下单操作。
      </div>
    </el-alert>

    <el-card v-if="snapshot?.as_of_date || snapshot?.summary" class="summary-card" shadow="never">
      <template #header>
        <span>持仓摘要</span>
      </template>
      <el-descriptions :column="2" border>
        <el-descriptions-item label="报表日期">{{ snapshot?.as_of_date ?? '-' }}</el-descriptions-item>
        <el-descriptions-item label="基准货币">{{ snapshot?.base_currency ?? '-' }}</el-descriptions-item>
        <el-descriptions-item label="持仓市值">
          <span v-if="summaryTotalValue != null">{{ currencySymbol }}{{ formatAmount(summaryTotalValue) }}</span>
          <span v-else>-</span>
        </el-descriptions-item>
        <el-descriptions-item label="未实现盈亏">
          <span
            v-if="summaryTotalPnl != null"
            :style="{ color: summaryTotalPnl >= 0 ? '#67C23A' : '#F56C6C' }"
          >
            {{ currencySymbol }}{{ formatAmount(summaryTotalPnl) }}
          </span>
          <span v-else>-</span>
        </el-descriptions-item>
        <el-descriptions-item label="可用资金（期末结算现金）">
          <span v-if="summaryCash != null">{{ currencySymbol }}{{ formatAmount(summaryCash) }}</span>
          <span v-else>-</span>
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-card class="positions-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span>持仓列表</span>
          <span v-if="positions.length" class="count">({{ positions.length }} 个)</span>
        </div>
      </template>
      <el-table :data="positions" v-loading="loading" stripe style="width: 100%">
        <el-table-column label="代码" width="100">
          <template #default="{ row }">
            <el-link type="primary" @click="viewStock(row.symbol)">{{ row.symbol }}</el-link>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="名称" min-width="140" show-overflow-tooltip />
        <el-table-column label="数量" width="90" align="right">
          <template #default="{ row }">{{ formatQuantity(row.quantity) }}</template>
        </el-table-column>
        <el-table-column label="市价" width="100" align="right">
          <template #default="{ row }">{{ currencySymbol }}{{ formatPrice(row.mark_price) }}</template>
        </el-table-column>
        <el-table-column label="持仓市值" width="120" align="right">
          <template #default="{ row }">{{ currencySymbol }}{{ formatAmount(row.position_value) }}</template>
        </el-table-column>
        <el-table-column label="成本价" width="100" align="right">
          <template #default="{ row }">
            <span v-if="row.avg_cost != null">{{ currencySymbol }}{{ formatPrice(row.avg_cost) }}</span>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column label="未实现盈亏" width="120" align="right">
          <template #default="{ row }">
            <span
              v-if="row.unrealized_pnl != null"
              :style="{ color: row.unrealized_pnl >= 0 ? '#67C23A' : '#F56C6C' }"
            >
              {{ currencySymbol }}{{ formatAmount(row.unrealized_pnl) }}
            </span>
            <span v-else>-</span>
          </template>
        </el-table-column>
        <el-table-column prop="currency_primary" label="货币" width="80" />
      </el-table>

      <div v-if="!loading && positions.length === 0" class="empty-state">
        <el-empty :description="emptyMessage">
          <el-button type="primary" @click="onRefresh">点击刷新获取持仓</el-button>
        </el-empty>
      </div>
    </el-card>

    <el-card class="trades-card" shadow="never" style="margin-top: 16px;">
      <template #header>
        <div class="card-header">
          <span>历史操作</span>
          <span v-if="tradesTotal">（共 {{ tradesTotal }} 笔）</span>
        </div>
      </template>
      <div class="trades-filters">
        <el-input
          v-model="tradeSymbol"
          placeholder="按代码过滤，如 AAPL"
          size="small"
          style="width: 200px; margin-right: 8px;"
          clearable
        />
        <el-button size="small" @click="reloadTrades">查询</el-button>
      </div>
      <el-table
        :data="trades"
        v-loading="tradesLoading"
        stripe
        style="width: 100%; margin-top: 8px;"
        size="small"
      >
        <el-table-column prop="trade_date" label="日期" width="110" />
        <el-table-column label="代码" width="100">
          <template #default="{ row }">
            <el-link type="primary" @click="viewStock(row.symbol)">{{ row.symbol }}</el-link>
          </template>
        </el-table-column>
        <el-table-column prop="description" label="名称" min-width="140" show-overflow-tooltip />
        <el-table-column label="方向" width="80">
          <template #default="{ row }">
            <span :style="{ color: row.side === 'BUY' ? '#67C23A' : row.side === 'SELL' ? '#F56C6C' : '#909399' }">
              {{ row.side === 'BUY' ? '买入' : row.side === 'SELL' ? '卖出' : '-' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="数量" width="90" align="right">
          <template #default="{ row }">{{ formatQuantity(row.quantity) }}</template>
        </el-table-column>
        <el-table-column label="价格" width="100" align="right">
          <template #default="{ row }">{{ currencySymbol }}{{ formatPrice(row.price) }}</template>
        </el-table-column>
        <el-table-column label="卖出盈亏" width="120" align="right">
          <template #default="{ row }">
            <span
              v-if="row.side === 'SELL' && row.realized_pnl != null"
              :style="{ color: Number(row.realized_pnl) >= 0 ? '#67C23A' : '#F56C6C' }"
            >
              {{ currencySymbol }}{{ formatAmount(row.realized_pnl) }}
            </span>
            <span v-else>-</span>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="tradesTotal > tradePageSize" style="margin-top: 8px; text-align: right;">
        <el-pagination
          background
          layout="prev, pager, next"
          :total="tradesTotal"
          :page-size="tradePageSize"
          :current-page="tradePage"
          @current-change="onTradePageChange"
          small
        />
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { TrendCharts, Refresh } from '@element-plus/icons-vue'
import { ibkrApi } from '@/api/ibkr'
import type { IbkrPositionSnapshot, IbkrTrade } from '@/api/ibkr'

const router = useRouter()

const snapshot = ref<IbkrPositionSnapshot | null>(null)
const loading = ref(false)
const refreshing = ref(false)

const trades = ref<IbkrTrade[]>([])
const tradesLoading = ref(false)
const tradesTotal = ref(0)
const tradePage = ref(1)
const tradePageSize = ref(20)
const tradeSymbol = ref('')

const positions = computed(() => snapshot.value?.positions ?? [])

const summaryTotalValue = computed(() => snapshot.value?.summary?.total_position_value ?? null)
const summaryTotalPnl = computed(() => snapshot.value?.summary?.total_unrealized_pnl ?? null)
const summaryCash = computed(() => {
  const s = snapshot.value?.summary
  if (!s) return null
  if (s.ending_settled_cash != null && !Number.isNaN(s.ending_settled_cash)) {
    return Number(s.ending_settled_cash)
  }
  if (s.ending_cash != null && !Number.isNaN(s.ending_cash)) {
    return Number(s.ending_cash)
  }
  return null
})

const currencySymbol = computed(() => {
  const c = snapshot.value?.base_currency
  if (c === 'USD') return '$'
  if (c === 'HKD') return 'HK$'
  if (c === 'CNY') return '¥'
  return snapshot.value?.base_currency ? `${snapshot.value.base_currency} ` : ''
})

const emptyMessage = computed(() => {
  if (snapshot.value?.message) return snapshot.value.message
  return '暂无持仓，请先在 IBKR 中建仓并点击右上角刷新按钮'
})

function formatPrice(n: number | null | undefined) {
  if (n == null || Number.isNaN(n)) return '-'
  return Number(n).toFixed(2)
}

function formatAmount(n: number | null | undefined) {
  if (n == null || Number.isNaN(n)) return '-'
  return Number(n).toFixed(2)
}

function formatQuantity(n: number | undefined) {
  if (n == null || Number.isNaN(n)) return '-'
  const num = Number(n)
  return Number.isInteger(num) ? String(num) : num.toFixed(2)
}

function formatTradeQuantity(n: number | undefined) {
  if (n == null || Number.isNaN(n)) return '-'
  const num = Math.abs(Number(n))
  return Number.isInteger(num) ? String(num) : num.toFixed(2)
}

function viewStock(symbol: string) {
  router.push({ path: `/stocks/${symbol}` })
}

async function fetchLatest() {
  try {
    loading.value = true
    const res = await ibkrApi.getLatestPositions()
    if (res.success && res.data) {
      snapshot.value = res.data
    }
  } catch (e: any) {
    ElMessage.error(e?.message || '获取持仓失败')
  } finally {
    loading.value = false
  }
}

async function fetchTrades() {
  try {
    tradesLoading.value = true
    const res = await ibkrApi.getTrades({
      symbol: tradeSymbol.value || undefined,
      limit: tradePageSize.value,
      offset: (tradePage.value - 1) * tradePageSize.value,
    })
    if (res.success && res.data) {
      trades.value = res.data.trades || []
      tradesTotal.value = res.data.total ?? 0
    }
  } catch (e: any) {
    ElMessage.error(e?.message || '获取历史操作失败')
  } finally {
    tradesLoading.value = false
  }
}

function reloadTrades() {
  tradePage.value = 1
  fetchTrades()
}

function onTradePageChange(page: number) {
  tradePage.value = page
  fetchTrades()
}

async function onRefresh() {
  try {
    refreshing.value = true
    const res = await ibkrApi.refreshPositions()
    if (res?.success && res.data) {
      snapshot.value = res.data
      ElMessage.success('已从 IBKR 同步最新持仓')
    } else if (res?.success && !res.data) {
      ElMessage.warning('未返回持仓数据，请检查后端配置或稍后重试')
    }
  } catch (e: any) {
    const msg = e?.response?.data?.detail ?? e?.message ?? '刷新持仓失败'
    const text = typeof msg === 'string' ? msg : JSON.stringify(msg)
    ElMessage.error(text)
  } finally {
    refreshing.value = false
  }
}

onMounted(() => {
  fetchLatest()
  fetchTrades()
})
</script>

<style lang="scss" scoped>
.positions-page {
  .page-header {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 16px;

    .page-title {
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 0;
      font-size: 24px;
      font-weight: 600;
    }

    .page-description {
      margin: 4px 0 0 0;
      color: var(--el-text-color-secondary);
      font-size: 14px;
      width: 100%;
    }

    .header-actions {
      flex-shrink: 0;
    }
  }

  .summary-card {
    margin-bottom: 16px;
  }

  .positions-card {
    .card-header .count {
      margin-left: 8px;
      font-size: 12px;
      color: var(--el-text-color-secondary);
      font-weight: normal;
    }

    .empty-state {
      padding: 24px 0;
    }
  }

  .trades-card {
    .trades-filters {
      margin-bottom: 4px;
      display: flex;
      align-items: center;
      justify-content: flex-start;
    }
  }
}
</style>
