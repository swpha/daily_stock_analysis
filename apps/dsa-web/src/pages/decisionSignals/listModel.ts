/**
 * 决策信号列表页的模块级模型与纯函数（从 DecisionSignalsPage.tsx 迁出）。
 * 全部为类型、常量与纯函数；页面组件保留在 DecisionSignalsPage.tsx。
 */

import type { StockSearchItem } from '../../api/stocks';
import type { UiTextKey } from '../../i18n/uiText';
import type { DecisionAction, MarketPhaseValue, StockBarItem } from '../../types/analysis';
import type {
  DecisionSignalItem,
  DecisionSignalListParams,
  DecisionSignalMarket,
  DecisionSignalSourceType,
  DecisionSignalStatus,
  DecisionProfile,
  DecisionProfileDisplay,
} from '../../types/decisionSignals';
import type { Market, StockIndexItem } from '../../types/stockIndex';
import { getDecisionProfile } from '../../utils/decisionSignalProfile';
import { parseDecisionSignalDate } from '../../utils/decisionSignalTime';
import { areStockCodesEquivalent } from '../../utils/stockCode';

export const PAGE_SIZE = 20;
export const TIMELINE_PAGE_SIZE = 100;
export const STOCK_CANDIDATE_LIMIT = 8;
export const DAY_MS = 86400_000;

export type ListFilters = {
  market: '' | DecisionSignalMarket;
  stockCode: string;
  action: '' | DecisionAction;
  marketPhase: '' | MarketPhaseValue;
  sourceType: '' | DecisionSignalSourceType;
  sourceReportId: string;
  status: '' | DecisionSignalStatus;
};

export type TimelineRange = '30d' | '90d' | '180d';
export type TimelineStatusFilter = 'all' | 'active';

export type TimelineFilters = {
  market: '' | DecisionSignalMarket;
  range: TimelineRange;
  status: TimelineStatusFilter;
  decisionProfile: '' | DecisionProfileDisplay;
};

export type TimelineMarketSource = 'context' | 'user' | null;

export type TimelineFilterUpdate = {
  filters: TimelineFilters;
  marketSource: TimelineMarketSource;
};

export type AppliedTimelineContext = TimelineFilters & {
  stockCode: string;
};

export type StockContext = {
  code: string;
  displayCode?: string;
  name?: string;
  market?: DecisionSignalMarket;
};

export type StockCandidate = StockContext & {
  source: 'history' | 'popular';
};

export type PendingStatusChange = {
  item: DecisionSignalItem;
  status: Extract<DecisionSignalStatus, 'closed' | 'invalidated' | 'archived'>;
  message: string;
};

export type SelectedSignal = {
  item: DecisionSignalItem;
  source: 'list' | 'latest' | 'timeline' | 'persisted';
};

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export const MARKET_OPTIONS: DecisionSignalMarket[] = ['cn', 'hk', 'us', 'jp', 'kr', 'tw'];
export const ACTION_OPTIONS: DecisionAction[] = ['buy', 'add', 'hold', 'reduce', 'sell', 'watch', 'avoid', 'alert'];
export const PHASE_OPTIONS: MarketPhaseValue[] = ['premarket', 'intraday', 'lunch_break', 'closing_auction', 'postmarket', 'non_trading', 'unknown'];
export const SOURCE_OPTIONS: DecisionSignalSourceType[] = ['analysis', 'agent', 'alert', 'market_review', 'manual'];
export const STATUS_OPTIONS: DecisionSignalStatus[] = ['active', 'expired', 'invalidated', 'closed', 'archived'];

export const STATUS_ACTIONS: Array<PendingStatusChange['status']> = ['closed', 'invalidated', 'archived'];
export const REASSESS_PROFILES: DecisionProfile[] = ['conservative', 'balanced', 'aggressive'];

export const STATUS_LABEL_KEYS: Record<DecisionSignalStatus, UiTextKey> = {
  active: 'decisionSignals.active',
  expired: 'decisionSignals.expired',
  invalidated: 'decisionSignals.invalidated',
  closed: 'decisionSignals.closed',
  archived: 'decisionSignals.archived',
};

export const STATUS_ACTION_LABEL_KEYS: Record<PendingStatusChange['status'], UiTextKey> = {
  closed: 'decisionSignals.close',
  invalidated: 'decisionSignals.invalidate',
  archived: 'decisionSignals.archive',
};

export const STATUS_ACTION_CONFIRM_KEYS: Record<PendingStatusChange['status'], UiTextKey> = {
  closed: 'decisionSignals.closeConfirm',
  invalidated: 'decisionSignals.invalidateConfirm',
  archived: 'decisionSignals.archiveConfirm',
};

export const DEFAULT_LIST_FILTERS: ListFilters = {
  market: '',
  stockCode: '',
  action: '',
  marketPhase: '',
  sourceType: '',
  sourceReportId: '',
  status: 'active',
};

export const DEFAULT_TIMELINE_FILTERS: TimelineFilters = {
  market: '',
  range: '90d',
  status: 'all',
  decisionProfile: '',
};

export const TIMELINE_RANGE_DAYS: Record<TimelineRange, number> = {
  '30d': 30,
  '90d': 90,
  '180d': 180,
};

export function parseSourceReportId(value: string): number | undefined {
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  const parsed = Number(trimmed);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : undefined;
}

export function getInitialFilters(search = typeof window === 'undefined' ? '' : window.location.search): ListFilters {
  const params = new URLSearchParams(search);
  const sourceReportId = parseSourceReportId(params.get('sourceReportId') ?? params.get('source_report_id') ?? '');
  if (sourceReportId === undefined) return DEFAULT_LIST_FILTERS;
  return {
    ...DEFAULT_LIST_FILTERS,
    sourceReportId: String(sourceReportId),
  };
}

export function toListParams(filters: ListFilters, page: number): DecisionSignalListParams {
  const sourceReportId = parseSourceReportId(filters.sourceReportId);
  if (sourceReportId !== undefined) {
    return {
      sourceReportId,
      sourceType: 'analysis',
      page,
      pageSize: PAGE_SIZE,
    };
  }

  return {
    market: filters.market || undefined,
    stockCode: filters.stockCode.trim() || undefined,
    action: filters.action || undefined,
    marketPhase: filters.marketPhase || undefined,
    sourceType: filters.sourceType || undefined,
    status: filters.status || undefined,
    page,
    pageSize: PAGE_SIZE,
  };
}

export function refreshLatestSelection(
  current: SelectedSignal | null,
  latestItems: DecisionSignalItem[],
): SelectedSignal | null {
  if (!current || current.source !== 'latest') return current;
  const refreshed = latestItems.find((item) => item.id === current.item.id);
  return refreshed ? { source: 'latest', item: refreshed } : null;
}

export function refreshTimelineSelection(
  current: SelectedSignal | null,
  timelineItems: DecisionSignalItem[],
): SelectedSignal | null {
  if (!current || current.source !== 'timeline') return current;
  const refreshed = timelineItems.find((item) => item.id === current.item.id);
  return refreshed ? { source: 'timeline', item: refreshed } : null;
}

export function normalizeDecisionSignalMarket(value: unknown): DecisionSignalMarket | undefined {
  const market = String(value ?? '').trim().toUpperCase();
  if (!market || market === 'INDEX' || market === 'ETF' || market === 'UNKNOWN') return undefined;
  if (market === 'CN' || market === 'BSE') return 'cn';
  if (market === 'HK') return 'hk';
  if (market === 'US') return 'us';
  if (market === 'JP') return 'jp';
  if (market === 'KR') return 'kr';
  if (market === 'TW') return 'tw';
  if (MARKET_OPTIONS.includes(market.toLowerCase() as DecisionSignalMarket)) {
    return market.toLowerCase() as DecisionSignalMarket;
  }
  return undefined;
}

export function getCandidateKey(candidate: Pick<StockCandidate, 'code' | 'market'>): string {
  const code = candidate.code.trim().toUpperCase();
  return candidate.market ? `${candidate.market}:${code}` : code;
}

export function toHistoryCandidate(item: StockBarItem): StockCandidate | null {
  const code = String(item.stockCode || '').trim();
  if (!code || code.toUpperCase() === 'MARKET') return null;
  return {
    code,
    displayCode: code,
    name: item.stockName || undefined,
    market: normalizeDecisionSignalMarket(item.marketPhaseSummary?.market),
    source: 'history',
  };
}

/** 把后端 /stocks/search（q 为空的热门模式）响应行映射为索引条目。 */
export function toIndexItemsFromSearch(items: StockSearchItem[]): StockIndexItem[] {
  return items.map((item) => ({
    canonicalCode: item.canonicalCode,
    displayCode: item.displayCode,
    nameZh: item.nameZh,
    market: (item.market || 'CN') as Market,
    assetType: (item.assetType || 'stock') as StockIndexItem['assetType'],
    active: item.active ?? true,
    popularity: item.popularity ?? 0,
  }));
}

export function toPopularCandidates(index: StockIndexItem[], limit = STOCK_CANDIDATE_LIMIT): StockCandidate[] {  const candidates: StockCandidate[] = [];
  const seen = new Set<string>();
  const sorted = [...index]
    .filter((item) => item.active && item.assetType === 'stock')
    .sort((left, right) => (right.popularity ?? 0) - (left.popularity ?? 0));

  for (const item of sorted) {
    const market = normalizeDecisionSignalMarket(item.market);
    const candidate: StockCandidate = {
      code: item.canonicalCode,
      displayCode: item.displayCode,
      name: item.nameZh,
      market,
      source: 'popular',
    };
    const key = getCandidateKey(candidate);
    if (seen.has(key)) continue;
    seen.add(key);
    candidates.push(candidate);
    if (candidates.length >= limit) break;
  }

  return candidates;
}

export function toTimelineParams(filters: TimelineFilters, stockCode: string): DecisionSignalListParams {
  const days = TIMELINE_RANGE_DAYS[filters.range];
  const createdTo = new Date();
  const createdFrom = new Date(createdTo.getTime() - days * DAY_MS);
  return {
    market: filters.market || undefined,
    stockCode,
    createdFrom: createdFrom.toISOString(),
    createdTo: createdTo.toISOString(),
    status: filters.status === 'active' ? 'active' : undefined,
    decisionProfile: filters.decisionProfile || undefined,
    page: 1,
    pageSize: TIMELINE_PAGE_SIZE,
  };
}

export function upsertDecisionSignal(
  current: DecisionSignalItem[],
  item: DecisionSignalItem,
  limit?: number,
): DecisionSignalItem[] {
  const next = [item, ...current.filter((candidate) => candidate.id !== item.id)];
  next.sort((left, right) => {
    const leftTime = parseDecisionSignalDate(left.createdAt)?.getTime() ?? Number.NEGATIVE_INFINITY;
    const rightTime = parseDecisionSignalDate(right.createdAt)?.getTime() ?? Number.NEGATIVE_INFINITY;
    return rightTime - leftTime || right.id - left.id;
  });
  return limit ? next.slice(0, limit) : next;
}

export function itemMatchesStockContext(item: DecisionSignalItem, context: StockContext): boolean {
  return areStockCodesEquivalent(item.stockCode, context.code)
    && (!context.market || item.market === context.market);
}

export function itemMatchesAppliedTimeline(
  item: DecisionSignalItem,
  context: AppliedTimelineContext,
  now = Date.now(),
): boolean {
  if (!areStockCodesEquivalent(item.stockCode, context.stockCode)) return false;
  if (context.market && item.market !== context.market) return false;
  if (context.status === 'active' && item.status !== 'active') return false;
  if (context.decisionProfile && getDecisionProfile(item) !== context.decisionProfile) return false;
  const createdAt = parseDecisionSignalDate(item.createdAt)?.getTime();
  if (createdAt === undefined) return false;
  return createdAt >= now - TIMELINE_RANGE_DAYS[context.range] * DAY_MS && createdAt <= now;
}

export function isSameStockContext(
  previousContext: StockContext | null,
  nextContext: StockContext,
): boolean {
  return previousContext?.code.trim().toUpperCase() === nextContext.code.trim().toUpperCase()
    && previousContext?.market === nextContext.market;
}

export function buildNextTimelineFilters(
  currentFilters: TimelineFilters,
  previousContext: StockContext | null,
  nextContext: StockContext,
  marketSource: TimelineMarketSource,
): TimelineFilterUpdate {
  if (isSameStockContext(previousContext, nextContext)) {
    return { filters: currentFilters, marketSource };
  }
  if (nextContext.market) {
    return {
      filters: { ...currentFilters, market: nextContext.market },
      marketSource: 'context',
    };
  }
  if (marketSource === 'context') {
    return {
      filters: { ...currentFilters, market: '' },
      marketSource: null,
    };
  }
  return { filters: currentFilters, marketSource };
}

export function draftMatchesStockContext(draft: string, context: StockContext | null): context is StockContext {
  if (!context) return false;
  const normalizedDraft = draft.trim().toUpperCase();
  if (!normalizedDraft) return false;
  return normalizedDraft === context.code.trim().toUpperCase()
    || normalizedDraft === String(context.displayCode ?? '').trim().toUpperCase();
}

export function formatStatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '-';
  return Number(value).toFixed(2).replace(/\.?0+$/, '');
}

export function formatStatPercent(value: number | null | undefined): string {
  const formatted = formatStatNumber(value);
  return formatted === '-' ? formatted : `${formatted}%`;
}
