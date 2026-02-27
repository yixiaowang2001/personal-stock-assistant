import { ApiClient } from './request'

export interface IbkrPosition {
  symbol: string
  description: string
  asset_class: string
  currency_primary: string
  quantity: number
  mark_price: number
  position_value: number
  avg_cost: number | null
  unrealized_pnl: number | null
  report_date?: string | null
  side?: string | null
}

export interface IbkrSummary {
  total_position_value?: number
  total_unrealized_pnl?: number
  position_count?: number
  // 期末现金（Cash Report 中的 Ending Cash / Ending Settled Cash）
  ending_cash?: number
  ending_settled_cash?: number
}

export interface IbkrPositionSnapshot {
  as_of_date: string | null
  base_currency: string | null
  summary: IbkrSummary | null
  positions: IbkrPosition[]
  message?: string
}

export interface IbkrTrade {
  trade_date: string | null
  symbol: string
  description: string
  asset_class: string
  currency_primary: string
  side: 'BUY' | 'SELL' | null
  quantity: number
  price: number
  amount: number | null
  // 单笔成交的已实现盈亏（合约货币），来源于 IBKR Flex 报表
  realized_pnl?: number | null
  report_date?: string | null
  exchange?: string | null
}

export const ibkrApi = {
  async getLatestPositions() {
    return ApiClient.get<IbkrPositionSnapshot>('/api/ibkr/positions/latest')
  },

  async refreshPositions() {
    return ApiClient.post<IbkrPositionSnapshot>('/api/ibkr/positions/refresh')
  },

  async getTrades(params?: { symbol?: string; start_date?: string; end_date?: string; limit?: number; offset?: number }) {
    return ApiClient.get<{ trades: IbkrTrade[]; total: number; realized_pnl_total?: number }>(
      '/api/ibkr/trades',
      params,
    )
  },
}
