import { ApiClient } from './request'
import type { IbkrPosition } from './ibkr'

export interface PortfolioRecommendationItem {
  ticker: string
  name?: string | null
  action: string

  current_price?: number | null
  current_shares?: number | null
  current_value?: number | null

  target_allocation?: number | null
  suggested_shares?: number | null

  reason?: string | null
  risk?: string | null
}

export interface PortfolioRecommendationData {
  base_currency: string | null
  as_of_date: string | null
  total_value: number | null
  cash_before: number | null
  cash_after: number | null
  cash_allocation?: number | null
  cash_reason?: string | null
  analysis?: string | null
  sector_advice?: string | null
  items: PortfolioRecommendationItem[]
  used_model?: string | null
  mode?: 'llm' | 'rule_fallback' | string | null
}

export const portfolioApi = {
  async generateRecommendations(reportIds: string[], modelName?: string) {
    return ApiClient.post<PortfolioRecommendationData>('/api/portfolio/recommendations', {
      report_ids: reportIds,
      model_name: modelName,
    })
  },
}

