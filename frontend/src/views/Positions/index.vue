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

    <el-card class="tabs-card" shadow="never">
      <el-tabs v-model="activeTab">
        <el-tab-pane label="持仓信息" name="info">
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
                <span v-if="realizedPnlTotal != null" style="margin-left: 12px; font-size: 13px;">
                  已实现盈亏：
                  <span :style="{ color: realizedPnlTotal >= 0 ? '#67C23A' : '#F56C6C' }">
                    {{ currencySymbol }}{{ formatAmount(realizedPnlTotal) }}
                  </span>
                </span>
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
        </el-tab-pane>

        <el-tab-pane label="持仓推荐" name="recommend">
          <div class="recommend-tab">
            <el-alert
              type="info"
              :closable="false"
              show-icon
              class="recommend-alert"
            >
              <template #title>
                基于 IBKR 持仓与已完成报告的组合级别推荐
              </template>
              <div class="recommend-desc">
                <p>步骤 1：选择本次用于生成持仓推荐的大模型。</p>
                <p>步骤 2：按需选择最多 10 份已完成的股票分析报告（同一股票代码仅可选择 1 份，可选步骤）。</p>
                <p>步骤 3：点击“生成持仓推荐”，系统会结合当前 IBKR 持仓与可用资金，给出组合层面和个股层面的综合建议。</p>
                <p>说明：本功能仅使用 IBKR 持仓快照和报告摘要，不包含历史成交，不构成投资建议，仅供个人复盘与记录。</p>
              </div>
            </el-alert>

            <el-card class="reports-select-card" shadow="never">
              <div class="settings-body">
                <div class="model-row">
                  <div class="model-label">选择模型</div>
                  <div class="model-control">
                    <el-select
                      v-model="selectedModel"
                      size="small"
                      class="model-select"
                      placeholder="选择持仓推荐模型"
                      filterable
                      :disabled="!availableModels.length"
                    >
                      <el-option
                        v-for="model in availableModels"
                        :key="model.model_name"
                        :label="model.model_display_name || model.model_name"
                        :value="model.model_name"
                      >
                        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
                          <span style="flex:1;">{{ model.model_display_name || model.model_name }}</span>
                          <div style="display:flex;align-items:center;gap:4px;">
                            <el-tag
                              v-if="model.capability_level"
                              :type="getCapabilityTagType(model.capability_level)"
                              size="small"
                              effect="plain"
                            >
                              {{ getCapabilityText(model.capability_level) }}
                            </el-tag>
                            <span style="font-size:12px;color:#909399;">{{ model.provider }}</span>
                          </div>
                        </div>
                      </el-option>
                    </el-select>
                  </div>
                </div>

                <el-collapse v-model="reportsCollapseActive" class="reports-collapse">
                  <el-collapse-item name="reports">
                    <template #title>
                      <div class="collapse-header">
                        <span>选择报告（可选报告最多 10 份，以下仅展示已完成的单股分析报告）</span>
                        <span class="count-text">可选已选 {{ selectedReports.length }} / 10</span>
                      </div>
                    </template>

                <div
                  v-if="positionsMissingReports.length > 0"
                  class="report-warning"
                >
                  部分持仓暂未找到对应的个股报告（例如：
                  {{ positionsMissingReports.map((p) => p.symbol).join('、') }}），
                  本次持仓推荐结果可能不够准确，建议补齐相关报告后再生成。
                </div>

                <div class="reports-filters">
                  <el-button
                    size="small"
                    :type="filterTodayOnly ? 'primary' : 'default'"
                    @click="onToggleTodayFilter"
                  >
                    今天
                  </el-button>
                      <el-input
                        v-model="reportSearchKeyword"
                        placeholder="搜索股票代码或名称"
                        clearable
                        size="small"
                        class="filter-item"
                        @change="onReportFilterChange"
                        @clear="onReportFilterChange"
                      >
                        <template #prefix>
                          <el-icon><TrendCharts /></el-icon>
                        </template>
                      </el-input>
                      <el-select
                        v-model="reportMarketFilter"
                        placeholder="市场筛选"
                        clearable
                        size="small"
                        class="filter-item"
                        @change="onReportFilterChange"
                      >
                        <el-option label="A股" value="A股" />
                        <el-option label="港股" value="港股" />
                        <el-option label="美股" value="美股" />
                      </el-select>
                    </div>

                    <el-table
                      ref="reportTableRef"
                      :data="optionalReportsForTable"
                      v-loading="reportsLoading"
                      style="width: 100%;"
                      size="small"
                      @select="onReportRowSelect"
                      @selection-change="onReportSelectionChange"
                    >
                      <el-table-column type="selection" width="50" :selectable="isReportRowSelectable" />
                      <el-table-column prop="stock_code" label="代码" width="110">
                        <template #default="{ row }">
                          <span>{{ row.stock_code }}</span>
                        </template>
                      </el-table-column>
                      <el-table-column prop="stock_name" label="名称" width="140" show-overflow-tooltip />
                      <el-table-column prop="market_type" label="市场" width="80">
                        <template #default="{ row }">
                          <el-tag size="small" type="success" v-if="row.market_type === 'A股'">A股</el-tag>
                          <el-tag size="small" type="warning" v-else-if="row.market_type === '港股'">港股</el-tag>
                          <el-tag size="small" type="info" v-else-if="row.market_type === '美股'">美股</el-tag>
                          <el-tag size="small" v-else>{{ row.market_type || '-' }}</el-tag>
                        </template>
                      </el-table-column>
                      <el-table-column prop="created_at" label="创建时间" width="170">
                        <template #default="{ row }">
                          {{ formatReportTime(row.created_at) }}
                        </template>
                      </el-table-column>
                      <el-table-column prop="model_info" label="模型" width="160">
                        <template #default="{ row }">
                          <el-tag v-if="row.model_info && row.model_info !== 'Unknown'" size="small" type="info">
                            {{ row.model_info }}
                          </el-tag>
                          <span v-else class="text-muted">-</span>
                        </template>
                      </el-table-column>
                      <el-table-column prop="summary" label="摘要" min-width="260" show-overflow-tooltip>
                        <template #default="{ row }">
                          <span>{{ row.summary || '（暂无摘要）' }}</span>
                        </template>
                      </el-table-column>
                    </el-table>

                    <div class="pagination-wrapper" v-if="reportTotal > reportPageSize">
                      <el-pagination
                        v-model:current-page="reportPage"
                        v-model:page-size="reportPageSize"
                        :page-sizes="[10, 20, 50, 100]"
                        :total="reportTotal"
                        layout="total, sizes, prev, pager, next, jumper"
                        @size-change="onReportPageSizeChange"
                        @current-change="onReportPageChange"
                      />
                    </div>
                  </el-collapse-item>
                </el-collapse>

                <div class="generate-row">
                  <el-button
                    type="primary"
                    :disabled="allReportIdsForSubmit.length === 0 || generating"
                    :loading="generating"
                    @click="onGenerateRecommendations"
                  >
                    生成持仓推荐
                  </el-button>
                </div>
              </div>
            </el-card>

            <el-card class="recommend-result-card" shadow="never">
              <template #header>
                <div class="card-header">
                  <span>持仓推荐</span>
                  <span class="sub-text" v-if="recommendationResult">
                    报表日期：{{ recommendationResult.as_of_date || snapshot?.as_of_date || '-' }}，
                    基准货币：{{ recommendationResult.base_currency || snapshot?.base_currency || '-' }}
                    <template v-if="displayModelName">
                      ，使用模型：{{ displayModelName }}
                    </template>
                  </span>
                </div>
              </template>

              <div v-if="!recommendationResult">
                <el-empty description="请选择上方报告并点击“生成推荐”获取结果" />
              </div>
              <div v-else>
                <div v-if="overallAnalysis" class="overall-comment">
                  <div class="overall-title">组合层面说明</div>
                  <div class="overall-text">
                    {{ overallAnalysis }}
                  </div>
                </div>

                <div v-if="sectorAdviceText" class="evaluation-summary">
                  <div class="evaluation-title">行业配置建议</div>
                  <div class="evaluation-text">
                    {{ sectorAdviceText }}
                  </div>
                </div>

                <div v-if="cashAllocationValue != null" class="cash-config">
                  <div class="cash-title">现金配置</div>
                  <div class="cash-text">
                    现金配置：{{ (cashAllocationValue * 100).toFixed(1) }}%
                    <template v-if="cashReasonText">
                      ，{{ cashReasonText }}
                    </template>
                    <template v-else>
                      ，用于应对市场波动和等待新机会（自动推算）
                    </template>
                  </div>
                </div>

                <el-table
                  :data="recommendationRows"
                  size="small"
                  style="width: 100%; margin-top: 8px;"
                  @row-click="onRecommendationRowClick"
                >
                  <el-table-column prop="stock_symbol" label="代码" width="110" />
                  <el-table-column prop="stock_name" label="名称" width="140" show-overflow-tooltip />
                  <el-table-column label="当前持仓" width="180">
                    <template #default="{ row }">
                      <span v-if="row.currentPosition">
                        {{ formatQuantity(row.currentPosition.quantity) }} 股 /
                        {{ currencySymbol }}{{ formatAmount(row.currentPosition.position_value) }}
                      </span>
                      <span v-else class="text-muted">当前无持仓</span>
                    </template>
                  </el-table-column>
                  <el-table-column label="当前价" width="110" align="right">
                    <template #default="{ row }">
                      <span v-if="row.current_price != null">
                        {{ currencySymbol }}{{ formatPrice(row.current_price) }}
                      </span>
                      <span v-else-if="row.currentPosition">
                        {{ currencySymbol }}{{ formatPrice(row.currentPosition.mark_price) }}
                      </span>
                      <span v-else class="text-muted">待补充价格</span>
                    </template>
                  </el-table-column>
                  <el-table-column label="操作建议" width="140">
                    <template #default="{ row }">
                      <el-tag :type="getActionTagType(row.action)" size="small">
                        {{ formatActionText(row.action) }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column prop="target_position_percent" label="目标仓位" width="120">
                    <template #default="{ row }">
                      <span v-if="row.target_position_percent != null">
                        {{ (row.target_position_percent * 100).toFixed(1) }}%
                      </span>
                      <span v-else class="text-muted">-</span>
                    </template>
                  </el-table-column>
                  <el-table-column prop="suggested_trade_shares" label="建议股数" width="110">
                    <template #default="{ row }">
                      <span v-if="row.suggested_trade_shares != null">
                        {{ formatQuantity(row.suggested_trade_shares) }}
                      </span>
                      <span v-else class="text-muted">-</span>
                    </template>
                  </el-table-column>
                  <el-table-column prop="rationale" label="理由" min-width="260" show-overflow-tooltip />
                  <el-table-column prop="risk_note" label="风险提示" min-width="220" show-overflow-tooltip />
                </el-table>

                <div v-if="activeRecommendation" class="recommend-detail-card">
                  <el-card shadow="never">
                    <template #header>
                      <div class="detail-header">
                        <span class="detail-title">
                          {{ activeRecommendation.stock_symbol }}
                          <span v-if="activeRecommendation.stock_name" class="detail-name">
                            - {{ activeRecommendation.stock_name }}
                          </span>
                        </span>
                        <el-tag :type="getActionTagType(activeRecommendation.action)" size="small">
                          {{ formatActionText(activeRecommendation.action) }}
                        </el-tag>
                      </div>
                    </template>
                    <div class="detail-body">
                      <div class="detail-section">
                        <div class="detail-section-title">当前价格（参考）</div>
                        <div class="detail-section-content">
                          <span v-if="activeRecommendation.current_price != null">
                            {{ currencySymbol }}{{ formatPrice(activeRecommendation.current_price) }}
                          </span>
                          <span v-else-if="activeRecommendation.currentPosition">
                            {{ currencySymbol }}{{ formatPrice(activeRecommendation.currentPosition.mark_price) }}
                          </span>
                          <span v-else class="text-muted">暂无价格信息</span>
                        </div>
                      </div>
                      <div class="detail-section">
                        <div class="detail-section-title">操作原因</div>
                        <div class="detail-section-content">
                          <span v-if="activeRecommendation.rationale">
                            {{ activeRecommendation.rationale }}
                          </span>
                          <span v-else class="text-muted">暂无详细原因。</span>
                        </div>
                      </div>
                      <div class="detail-section">
                        <div class="detail-section-title">风险提示</div>
                        <div class="detail-section-content">
                          <span v-if="activeRecommendation.risk_note">
                            {{ activeRecommendation.risk_note }}
                          </span>
                          <span v-else class="text-muted">暂无额外风险说明。</span>
                        </div>
                      </div>
                    </div>
                  </el-card>
                </div>
              </div>
            </el-card>
          </div>
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { TrendCharts, Refresh } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { ibkrApi } from '@/api/ibkr'
import type { IbkrPositionSnapshot, IbkrTrade } from '@/api/ibkr'
import { portfolioApi } from '@/api/portfolio'
import { configApi } from '@/api/config'

const router = useRouter()
const authStore = useAuthStore()

const activeTab = ref<'info' | 'recommend'>('info')

const snapshot = ref<IbkrPositionSnapshot | null>(null)
const loading = ref(false)
const refreshing = ref(false)

const trades = ref<IbkrTrade[]>([])
const tradesLoading = ref(false)
const tradesTotal = ref(0)
const tradePage = ref(1)
const tradePageSize = ref(20)
const tradeSymbol = ref('')
const realizedPnlTotal = ref<number | null>(null)

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

// 持仓推荐：报告选择与推荐结果
const reportTableRef = ref<any>(null)
const reportsLoading = ref(false)
const reportSearchKeyword = ref('')
const reportMarketFilter = ref('')
const reportPage = ref(1)
const reportPageSize = ref(10)
const reportTotal = ref(0)
const availableReports = ref<any[]>([])
const selectedReports = ref<any[]>([])
const reportsLoaded = ref(false)
const reportsCollapseActive = ref<string[]>(['reports'])
const filterTodayOnly = ref(false)

// 持仓对应报告（自动选择，不占 10 个名额，不可取消）
const positionReports = ref<any[]>([])
const positionReportsLoading = ref(false)
const positionReportIds = computed(() => positionReports.value.map((r: any) => r.id).filter(Boolean))

// 持仓中缺少对应报告的股票列表（用于提示）
const positionsMissingReports = computed(() => {
  const pos = positions.value || []
  if (!pos.length) return []
  const haveSymbols = new Set(
    positionReports.value
      .map((r: any) => r.stock_code || r.stock_symbol)
      .filter(Boolean),
  )
  const missing: { symbol: string; name: string }[] = []
  for (const p of pos as any[]) {
    const symbol = p?.symbol
    if (!symbol) continue
    if (haveSymbols.has(symbol)) continue
    const name = p.description || p.localSymbol || ''
    missing.push({ symbol, name })
  }
  return missing
})

// 持仓推荐：模型选择
const availableModels = ref<any[]>([])
const selectedModel = ref<string>('')
const modelsLoaded = ref(false)

const generating = ref(false)
const recommendationResult = ref<any | null>(null)
const activeRecommendation = ref<any | null>(null)

const overallAnalysis = computed(() => {
  if (!recommendationResult.value) return ''
  return recommendationResult.value.analysis || recommendationResult.value.overall_comment || ''
})

const sectorAdviceText = computed(() => {
  if (!recommendationResult.value) return ''
  return recommendationResult.value.sector_advice || recommendationResult.value.evaluation_summary || ''
})

const stockAllocationSum = computed(() => {
  if (!recommendationResult.value) return 0
  return recommendationRows.value.reduce((sum, row: any) => {
    const v = row.target_position_percent
    if (typeof v === 'number' && !Number.isNaN(v)) {
      return sum + v
    }
    return sum
  }, 0)
})

const cashAllocationValue = computed(() => {
  if (!recommendationResult.value) return null
  const explicit = recommendationResult.value.cash_allocation
  if (typeof explicit === 'number' && !Number.isNaN(explicit)) {
    return explicit
  }
  const sum = stockAllocationSum.value
  if (sum >= 0 && sum <= 1.05) {
    return Math.max(0, 1 - sum)
  }
  return null
})

const cashReasonText = computed(() => {
  if (!recommendationResult.value) return ''
  return recommendationResult.value.cash_reason || ''
})

const recommendationRows = computed(() => {
  if (!recommendationResult.value) return []
  const bySymbol: Record<string, any> = {}
  for (const p of positions.value) {
    if (p && p.symbol) {
      bySymbol[p.symbol] = p
    }
  }
  const sourceItems = recommendationResult.value.items || recommendationResult.value.recommendations || []
  return (sourceItems as any[]).map((item: any) => {
    const symbol = item.ticker || item.stock_symbol
    return {
      ...item,
      stock_symbol: symbol,
      stock_name: item.stock_name ?? item.name,
      target_position_percent:
        item.target_position_percent ?? item.target_allocation ?? null,
      suggested_trade_shares:
        item.suggested_trade_shares ?? item.suggested_shares ?? null,
      rationale: item.rationale ?? item.reason,
      risk_note: item.risk_note ?? item.risk,
      currentPosition: bySymbol[symbol] || null,
    }
  })
})

const filteredReports = computed(() => {
  if (!filterTodayOnly.value) return availableReports.value
  const today = new Date()
  const y = today.getFullYear()
  const m = String(today.getMonth() + 1).padStart(2, '0')
  const d = String(today.getDate()).padStart(2, '0')
  const todayStr = `${y}-${m}-${d}`
  return availableReports.value.filter((r: any) => {
    const created = r?.created_at
    if (!created) return false
    try {
      const dt = new Date(created)
      if (Number.isNaN(dt.getTime())) return false
      const yy = dt.getFullYear()
      const mm = String(dt.getMonth() + 1).padStart(2, '0')
      const dd = String(dt.getDate()).padStart(2, '0')
      return `${yy}-${mm}-${dd}` === todayStr
    } catch {
      return false
    }
  })
})

// 报告表格数据（含持仓对应报告）
const optionalReportsForTable = computed(() => {
  return filteredReports.value
})

// 自动选中的持仓对应报告 ID 集合
const autoSelectedReportIdSet = computed(() => {
  return new Set(positionReportIds.value.map((id: any) => String(id)))
})

// 提交时使用的全部报告 ID（持仓报告 + 可选报告，去重）
const allReportIdsForSubmit = computed(() => {
  const ids = new Set<string>()
  for (const id of positionReportIds.value) {
    if (id) ids.add(String(id))
  }
  for (const r of selectedReports.value) {
    if (r?.id) ids.add(String(r.id))
  }
  return Array.from(ids)
})

const displayModelName = computed(() => {
  if (recommendationResult.value?.used_model) {
    return recommendationResult.value.used_model
  }
  if (selectedModel.value) {
    return selectedModel.value
  }
  return ''
})

function onRecommendationRowClick(row: any) {
  activeRecommendation.value = row
}

async function fetchCompletedReports() {
  try {
    reportsLoading.value = true
    const params = new URLSearchParams({
      page: reportPage.value.toString(),
      page_size: reportPageSize.value.toString(),
    })
    if (reportSearchKeyword.value) {
      params.append('search_keyword', reportSearchKeyword.value)
    }
    if (reportMarketFilter.value) {
      params.append('market_filter', reportMarketFilter.value)
    }

    const response = await fetch(`/api/reports/list?${params.toString()}`, {
      headers: {
        Authorization: `Bearer ${authStore.token}`,
        'Content-Type': 'application/json',
      },
    })

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`)
    }

    const result = await response.json()
    if (result.success) {
      const list = Array.isArray(result.data?.reports) ? result.data.reports : []
      // 仅保留已完成的报告
      availableReports.value = list.filter((r: any) => r.status === 'completed')
      reportTotal.value = result.data?.total ?? availableReports.value.length
      reportsLoaded.value = true
    } else {
      throw new Error(result.message || '获取报告列表失败')
    }
  } catch (e: any) {
    ElMessage.error(e?.message || '获取报告列表失败')
  } finally {
    reportsLoading.value = false
  }
}

async function fetchPositionReports() {
  const pos = positions.value
  if (!pos?.length) {
    positionReports.value = []
    return
  }
  const symbols = [...new Set(pos.map((p: any) => p?.symbol).filter(Boolean))]
  if (!symbols.length) {
    positionReports.value = []
    return
  }
  try {
    positionReportsLoading.value = true
    const results: any[] = []
    await Promise.all(
      symbols.map(async (symbol: string) => {
        const params = new URLSearchParams({
          stock_code: symbol,
          page: '1',
          page_size: '5',
        })
        const response = await fetch(`/api/reports/list?${params.toString()}`, {
          headers: {
            Authorization: `Bearer ${authStore.token}`,
            'Content-Type': 'application/json',
          },
        })
        if (!response.ok) return
        const result = await response.json()
        if (result?.success && Array.isArray(result.data?.reports) && result.data.reports.length > 0) {
          const completed = result.data.reports.find((r: any) => r.status === 'completed')
          if (completed) results.push(completed)
        }
      }),
    )
    positionReports.value = results
  } catch {
    positionReports.value = []
  } finally {
    positionReportsLoading.value = false
  }
}

function onReportFilterChange() {
  reportPage.value = 1
  fetchCompletedReports()
}

function onReportPageSizeChange(size: number) {
  reportPageSize.value = size
  reportPage.value = 1
  fetchCompletedReports()
}

function onReportPageChange(page: number) {
  reportPage.value = page
  fetchCompletedReports()
}

function onToggleTodayFilter() {
  filterTodayOnly.value = !filterTodayOnly.value
}

function onReportSelectionChange(selection: any[]) {
  // 选择变更由 onReportRowSelect 基于行级别事件维护 selectedReports
  // 这里不做额外处理，避免分页或程序性切换时清空已选状态
}

function onReportRowSelect(selection: any[], row: any) {
  const autoSet = autoSelectedReportIdSet.value
  if (!row || !row.id) return
  const idStr = String(row.id)
  if (autoSet.has(idStr)) return

  const nowSelected = selection.some(
    (item) => item && String(item.id) === idStr && !autoSet.has(String(item.id)),
  )

  const symbol = row.stock_code || row.stock_symbol

  // 当前已在选中列表中的条目
  const existedSameId = selectedReports.value.some(
    (item: any) => String(item.id) === idStr,
  )

  // 处理“取消选择”：从全局已选中移除
  if (!nowSelected) {
    if (existedSameId) {
      selectedReports.value = selectedReports.value.filter(
        (item: any) => String(item.id) !== idStr,
      )
    }
    return
  }

  // 下面是“勾选”逻辑
  const existedSameSymbol =
    symbol &&
    selectedReports.value.find(
      (item: any) =>
        (item.stock_code || item.stock_symbol) === symbol &&
        String(item.id) !== idStr,
    )

  if (existedSameSymbol) {
    ElMessage.warning('同一股票代码只能选择一份报告')
    if (reportTableRef.value) {
      reportTableRef.value.toggleRowSelection(row, false)
    }
    return
  }

  if (!existedSameId && selectedReports.value.length >= 10) {
    ElMessage.warning('最多只能选择 10 份报告')
    if (reportTableRef.value) {
      reportTableRef.value.toggleRowSelection(row, false)
    }
    return
  }

  if (!existedSameId) {
    selectedReports.value = [...selectedReports.value, row]
  }
}

function isReportRowSelectable(row: any) {
  const autoSet = autoSelectedReportIdSet.value
  if (!row || !row.id) return true
  return !autoSet.has(String(row.id))
}

function syncReportSelections() {
  const table = reportTableRef.value
  if (!table) return
  const autoSet = autoSelectedReportIdSet.value
  const currentSelection: any[] = table.getSelectionRows ? table.getSelectionRows() : []
  const currentIds = new Set(currentSelection.map((item: any) => String(item.id)))
  for (const row of optionalReportsForTable.value) {
    if (!row || !row.id) continue
    const idStr = String(row.id)
    const manualSelected = selectedReports.value.some(
      (item: any) => String(item.id) === idStr,
    )
    const shouldSelect = autoSet.has(idStr) || manualSelected
    const isSelected = currentIds.has(idStr)
    if (shouldSelect && !isSelected) {
      table.toggleRowSelection(row, true)
    } else if (!shouldSelect && isSelected && !autoSet.has(idStr)) {
      table.toggleRowSelection(row, false)
    }
  }
}

watch(
  () => [optionalReportsForTable.value, positionReportIds.value],
  () => {
    nextTick(() => {
      syncReportSelections()
    })
  },
)

function formatReportTime(time: string | null | undefined) {
  if (!time) return '-'
  try {
    const d = new Date(time)
    if (Number.isNaN(d.getTime())) return time
    const y = d.getFullYear()
    const m = String(d.getMonth() + 1).padStart(2, '0')
    const day = String(d.getDate()).padStart(2, '0')
    const hh = String(d.getHours()).padStart(2, '0')
    const mm = String(d.getMinutes()).padStart(2, '0')
    return `${y}-${m}-${day} ${hh}:${mm}`
  } catch {
    return time
  }
}

function formatActionText(action: string | null | undefined) {
  const a = (action || '').toLowerCase()
  if (a === 'increase' || a === 'buy' || a === 'add') return '增持'
  if (a === 'decrease' || a === 'reduce') return '减持'
  if (a === 'exit' || a === 'close') return '清仓'
  if (a === 'avoid') return '暂不参与'
  if (a === 'hold' || !a) return '观望/持有'
  return action || '观望/持有'
}

function getActionTagType(action: string | null | undefined) {
  const a = (action || '').toLowerCase()
  if (a === 'increase' || a === 'buy' || a === 'add') return 'success'
  if (a === 'decrease' || a === 'reduce') return 'warning'
  if (a === 'exit' || a === 'close') return 'danger'
  if (a === 'avoid') return 'info'
  return 'info'
}

function getCapabilityText(level: number): string {
  const texts: Record<number, string> = {
    1: '⚡基础',
    2: '📊标准',
    3: '🎯高级',
    4: '🔥专业',
    5: '👑旗舰',
  }
  return texts[level] || '📊标准'
}

function getCapabilityTagType(level: number): 'success' | 'info' | 'warning' | 'danger' {
  if (level >= 4) return 'danger'
  if (level >= 3) return 'warning'
  if (level >= 2) return 'success'
  return 'info'
}

async function onGenerateRecommendations() {
  const ids = allReportIdsForSubmit.value
  if (!ids.length) {
    ElMessage.warning('请至少保留或选择 1 份报告（持仓对应报告已自动计入）')
    return
  }

  try {
    generating.value = true
    const res = await portfolioApi.generateRecommendations(ids, selectedModel.value || undefined)
    if (res.success && res.data) {
      recommendationResult.value = res.data
      activeRecommendation.value = null
      ElMessage.success('持仓推荐生成成功')
    } else {
      throw new Error(res.message || '生成持仓推荐失败')
    }
  } catch (e: any) {
    const msg = e?.message || '生成持仓推荐失败'
    ElMessage.error(msg)
  } finally {
    generating.value = false
  }
}

watch(
  () => activeTab.value,
  (val) => {
    if (val === 'recommend' && !reportsLoaded.value && !reportsLoading.value) {
      fetchCompletedReports()
    }
    if (val === 'recommend' && !modelsLoaded.value) {
      initializePortfolioModels()
    }
    if (val === 'recommend' && snapshot.value?.positions?.length) {
      fetchPositionReports()
    }
  },
)

async function initializePortfolioModels() {
  try {
    modelsLoaded.value = true
    const [defaults, llmConfigs] = await Promise.all([
      configApi.getDefaultModels(),
      configApi.getLLMConfigs(),
    ])
    const enabledModels = (llmConfigs || []).filter((m: any) => m && m.enabled)
    availableModels.value = enabledModels
    if (!selectedModel.value && defaults?.quick_analysis_model) {
      selectedModel.value = defaults.quick_analysis_model
    } else if (!selectedModel.value && enabledModels.length) {
      selectedModel.value = enabledModels[0].model_name
    }
  } catch (e: any) {
    modelsLoaded.value = false
    // 静默失败，仅在控制台记录，避免影响持仓推荐主体功能
    // eslint-disable-next-line no-console
    console.error('加载持仓推荐模型配置失败', e)
  }
}

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
      if (typeof res.data.realized_pnl_total === 'number' && !Number.isNaN(res.data.realized_pnl_total)) {
        realizedPnlTotal.value = res.data.realized_pnl_total
      } else {
        realizedPnlTotal.value = 0
      }
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

  .tabs-card {
    margin-top: 0;
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

  .recommend-tab {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .recommend-alert {
    margin-bottom: 0;
  }

  .recommend-desc {
    font-size: 13px;
    line-height: 1.6;
    color: var(--el-text-color-regular);
  }

  .reports-select-card .settings-body {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .reports-select-card .model-row {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .reports-select-card .model-label {
    width: 72px;
    font-size: 13px;
    color: var(--el-text-color-regular);
  }

  .reports-select-card .model-control {
    flex: 1;
  }

  .reports-select-card .model-select {
    width: 260px;
  }

  .reports-filters {
    margin-bottom: 8px;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }

  .reports-filters .filter-item {
    width: 220px;
  }

  .reports-select-card .collapse-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    width: 100%;
    font-size: 13px;
    font-weight: 400;
    color: var(--el-text-color-regular);
  }

  .reports-select-card .count-text {
    font-size: 13px;
    color: var(--el-text-color-secondary);
  }

  .reports-select-card .generate-row {
    display: flex;
    justify-content: flex-start;
    margin-top: 4px;
  }

  .recommend-result-card .card-header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }

  .recommend-result-card .card-header .sub-text {
    font-size: 12px;
    color: var(--el-text-color-secondary);
  }

  .overall-comment {
    margin-bottom: 12px;
  }

  .overall-title {
    font-size: 14px;
    font-weight: 600;
    margin-bottom: 6px;
  }

  .overall-text {
    font-size: 13px;
    line-height: 1.6;
    color: var(--el-text-color-regular);
    white-space: pre-wrap;
  }

  .evaluation-summary {
    margin-bottom: 12px;
  }

  .evaluation-title {
    font-size: 14px;
    font-weight: 600;
    margin-bottom: 6px;
  }

  .evaluation-text {
    font-size: 13px;
    line-height: 1.6;
    color: var(--el-text-color-regular);
    white-space: pre-wrap;
  }

  .cash-config {
    margin-bottom: 12px;
  }

  .cash-title {
    font-size: 14px;
    font-weight: 600;
    margin-bottom: 6px;
  }

  .cash-text {
    font-size: 13px;
    line-height: 1.6;
    color: var(--el-text-color-regular);
    white-space: pre-wrap;
  }

  .position-reports-block {
    margin-bottom: 12px;
    padding: 10px 12px;
    background: var(--el-fill-color-light);
    border-radius: 4px;
  }

  .position-reports-title {
    font-size: 13px;
    font-weight: 600;
    margin-bottom: 6px;
    color: var(--el-text-color-regular);
  }

  .position-reports-list {
    font-size: 13px;
    line-height: 1.6;
    color: var(--el-text-color-secondary);
  }

  .position-report-item {
    display: block;
  }

  .report-warning {
    margin-bottom: 8px;
    padding: 8px 10px;
    font-size: 12px;
    line-height: 1.5;
    border-radius: 4px;
    background-color: var(--el-color-warning-light-9);
    color: var(--el-color-warning-dark-2);
  }

  .recommend-detail-card {
    margin-top: 12px;
  }

  .detail-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  .detail-title {
    font-weight: 500;
  }

  .detail-name {
    margin-left: 4px;
    color: var(--el-text-color-secondary);
    font-weight: normal;
  }

  .detail-body {
    display: flex;
    flex-direction: column;
    gap: 12px;
    font-size: 13px;
    line-height: 1.6;
  }

  .detail-section-title {
    font-weight: 500;
    margin-bottom: 4px;
  }

  .detail-section-content {
    white-space: pre-wrap;
  }

  .text-muted {
    color: var(--el-text-color-secondary);
    font-size: 12px;
  }
}
</style>
