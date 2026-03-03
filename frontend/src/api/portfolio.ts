import { ApiClient } from './request'
import type { IbkrPosition } from './ibkr'

export interface PortfolioRecommendationItem {
  stock_symbol: string
  stock_name?: string | null
  action: string
  target_position_percent?: number | null
  suggested_trade_shares?: number | null
  rationale?: string | null
  risk_note?: string | null
}

export interface PortfolioRecommendationData {
  base_currency: string | null
  cash: number | null
  as_of_date: string | null
  positions: IbkrPosition[]
  overall_comment?: string | null
  evaluation_summary?: string | null
  recommendations: PortfolioRecommendationItem[]
  used_model?: string | null
}

export const portfolioApi = {
  async generateRecommendations(reportIds: string[], modelName?: string) {
    return ApiClient.post<PortfolioRecommendationData>('/api/portfolio/recommendations', {
      report_ids: reportIds,
      model_name: modelName,
    })
  },
}

