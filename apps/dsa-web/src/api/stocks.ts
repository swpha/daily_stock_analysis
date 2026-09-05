import apiClient from './index';
import { toCamelCase } from './utils';
import type { components } from '../types/api-generated';
import type { Camelize } from '../types/apiCamel';

/** 后端 schema 权威来源：openapi-typescript 生成，勿手改（见 src/types/apiCamel.ts 头注）。 */
type WireStockSearchItem = components['schemas']['StockSearchItem'];
type WireStockSearchResponse = components['schemas']['StockSearchResponse'];

/** 运行时经 toCamelCase 转换后的形状；字段与后端 StockSearchItem 同源。 */
export type StockSearchItem = Camelize<WireStockSearchItem>;
export type StockSearchResponse = Camelize<WireStockSearchResponse>;

export type ExtractItem = {
  code?: string | null;
  name?: string | null;
  confidence: string;
};

export type ExtractFromImageResponse = {
  codes: string[];
  items?: ExtractItem[];
  rawText?: string;
};

export const stocksApi = {
  async extractFromImage(file: File): Promise<ExtractFromImageResponse> {
    const formData = new FormData();
    formData.append('file', file);

    const headers: { [key: string]: string | undefined } = { 'Content-Type': undefined };
    const response = await apiClient.post(
      '/api/v1/stocks/extract-from-image',
      formData,
      {
        headers,
        timeout: 60000, // Vision API can be slow; 60s
      },
    );

    const data = response.data as { codes?: string[]; items?: ExtractItem[]; raw_text?: string };
    return {
      codes: data.codes ?? [],
      items: data.items,
      rawText: data.raw_text,
    };
  },

  async parseImport(file?: File, text?: string): Promise<ExtractFromImageResponse> {
    if (file) {
      const formData = new FormData();
      formData.append('file', file);
      const headers: { [key: string]: string | undefined } = { 'Content-Type': undefined };
      const response = await apiClient.post('/api/v1/stocks/parse-import', formData, { headers });
      const data = response.data as { codes?: string[]; items?: ExtractItem[] };
      return { codes: data.codes ?? [], items: data.items };
    }
    if (text) {
      const response = await apiClient.post('/api/v1/stocks/parse-import', { text });
      const data = response.data as { codes?: string[]; items?: ExtractItem[] };
      return { codes: data.codes ?? [], items: data.items };
    }
    throw new Error('请提供文件或粘贴文本');
  },

  /**
   * 后端股票联想搜索（q 为空时返回热门列表）。
   * 服务端打分与前端本地 searchStocks.ts 规则一致。
   */
  async search(q: string, limit = 10): Promise<StockSearchResponse> {
    const response = await apiClient.get('/api/v1/stocks/search', {
      params: { q, limit },
    });
    return toCamelCase<StockSearchResponse>(response.data);
  },
};
