import apiClient from './index';
import { toCamelCase } from './utils';
import type { components } from '../types/api-generated';
import type { Camelize } from '../types/apiCamel';

/**
 * 类型权威来源：openapi-typescript 生成（后端 usage schema）。
 * 响应经 toCamelCase 转换，UI 形状 = Camelize<线格式>。
 */
type Wire = components['schemas'];

export type UsagePeriod = 'today' | 'month' | 'all';

export type UsageCallTypeBreakdown = Camelize<Wire['CallTypeBreakdown']>;

export type UsageModelBreakdown = Camelize<Wire['ModelBreakdown']>;

export type UsageCallRecord = Camelize<Wire['UsageCallRecord']>;

// period 在后端是自由 string，运行时保证是三种取值之一，保持窄类型
export type UsageDashboard = Omit<
  Camelize<Wire['UsageDashboardResponse']>,
  'period'
> & { period: UsagePeriod };

export const usageApi = {
  getDashboard: async (params: { period?: UsagePeriod; limit?: number } = {}): Promise<UsageDashboard> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/usage/dashboard', {
      params: {
        period: params.period ?? 'month',
        limit: params.limit ?? 50,
      },
    });

    return toCamelCase<UsageDashboard>(response.data);
  },
};
