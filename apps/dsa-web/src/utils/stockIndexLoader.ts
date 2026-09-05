/**
 * Stock Index Loader
 *
 * Responsible for loading and parsing stock index data
 */

import type { StockIndexData, StockIndexItem, StockIndexTuple, StockSuggestion, Market } from '../types/stockIndex';
import { stocksApi } from '../api/stocks';
import { INDEX_FIELD } from './stockIndexFields';

export interface IndexLoadResult {
  /** Index data */
  data: StockIndexItem[];
  /** Successfully loaded */
  loaded: boolean;
  /** Error information */
  error?: Error;
  /** Whether fallback mode is used */
  fallback: boolean;
}

/**
 * Load stock index
 *
 * @returns Index load result
 */
export async function loadStockIndex(): Promise<IndexLoadResult> {
  try {
    // 后端对该端点返回 ETag + no-cache：重复加载命中 304，索引刷新（mtime 变化）后自动拉新。
    const response = await fetch('/stocks.index.json');

    if (!response.ok) {
      throw new Error(`Failed to load index: ${response.status} ${response.statusText}`);
    }

    const data: StockIndexData = await response.json();

    // Uncompress format (if array format)
    const items = isCompressedFormat(data)
      ? unpackTuples(data as StockIndexTuple[])
      : data as StockIndexItem[];

    // The shared payload may now carry ``assetType=index`` rows, but the
    // current autocomplete/popular/group consumers must not see them. Filter
    // index rows out before constructing the successful result so stock/ETF
    // behaviour is unchanged.
    const visibleItems = items.filter(item => item.assetType !== 'index');

    return {
      data: visibleItems,
      loaded: true,
      fallback: false,
    };
  } catch (error) {
    console.error('[StockIndexLoader] Failed to load stock index:', error);
    return {
      data: [],
      loaded: false,
      error: error as Error,
      fallback: true,  // Load failed, fallback to old mode
    };
  }
}

/**
 * Remote stock suggestion search (backend /api/v1/stocks/search).
 *
 * 后端打分与本地 searchStocks.ts 一致；用于避免一次性下载全量索引。
 * 服务端不可用时由调用方降级为 loadStockIndex 全量本地搜索。
 */
export async function fetchStockSuggestions(
  query: string,
  limit: number = 10
): Promise<StockSuggestion[]> {
  const response = await stocksApi.search(query, limit);
  return (response.items ?? []).map(item => ({
    canonicalCode: item.canonicalCode,
    displayCode: item.displayCode,
    nameZh: item.nameZh,
    market: (item.market || 'CN') as Market,
    matchType: item.matchType as StockSuggestion['matchType'],
    matchField: item.matchField as StockSuggestion['matchField'],
    score: item.score,
  }));
}

/**
 * Check if data is in compressed format
 */
function isCompressedFormat(data: StockIndexData): data is StockIndexTuple[] {
  if (!Array.isArray(data) || data.length === 0) return false;
  const firstItem = data[0];
  return Array.isArray(firstItem) && typeof firstItem[0] === 'string';
}

/**
 * Uncompress tuple format to object format
 */
function unpackTuples(tuples: StockIndexTuple[]): StockIndexItem[] {
  return tuples.map(tuple => ({
    canonicalCode: tuple[INDEX_FIELD.CANONICAL_CODE],
    displayCode: tuple[INDEX_FIELD.DISPLAY_CODE],
    nameZh: tuple[INDEX_FIELD.NAME_ZH],
    pinyinFull: tuple[INDEX_FIELD.PINYIN_FULL],
    pinyinAbbr: tuple[INDEX_FIELD.PINYIN_ABBR],
    aliases: tuple[INDEX_FIELD.ALIASES],
    market: tuple[INDEX_FIELD.MARKET],
    assetType: tuple[INDEX_FIELD.ASSET_TYPE],
    active: tuple[INDEX_FIELD.ACTIVE],
    popularity: tuple[INDEX_FIELD.POPULARITY],
  }));
}

/**
 * Compress object format to tuple format
 *
 * For reducing index file size
 */
export function compressIndex(items: StockIndexItem[]): StockIndexTuple[] {
  return items.map(item => [
    item.canonicalCode,
    item.displayCode,
    item.nameZh,
    item.pinyinFull,
    item.pinyinAbbr,
    item.aliases || [],
    item.market,
    item.assetType,
    item.active,
    item.popularity,
  ]);
}

/**
 * Find stock in index
 *
 * @param canonicalCode - Canonical code
 * @param index - Stock index
 * @returns Stock index item or null
 */
export function findStockInIndex(
  canonicalCode: string,
  index: StockIndexItem[]
): StockIndexItem | null {
  return index.find(item => item.canonicalCode === canonicalCode) || null;
}

/**
 * Get popular stocks list
 *
 * @param index - Stock index
 * @param limit - Number of results to return
 * @returns Popular stocks list
 */
export function getPopularStocks(
  index: StockIndexItem[],
  limit: number = 20
): StockIndexItem[] {
  return [...index]
    .filter(item => item.active)
    .sort((a, b) => (b.popularity || 0) - (a.popularity || 0))
    .slice(0, limit);
}

/**
 * Group stocks by market
 *
 * @param index - Stock index
 * @returns Map of stocks grouped by market
 */
export function groupStocksByMarket(
  index: StockIndexItem[]
): Map<string, StockIndexItem[]> {
  const grouped = new Map<string, StockIndexItem[]>();

  for (const item of index) {
    if (!item.active) continue;

    const market = item.market;
    if (!grouped.has(market)) {
      grouped.set(market, []);
    }
    const group = grouped.get(market);
    if (group) {
      group.push(item);
    }
  }

  return grouped;
}
