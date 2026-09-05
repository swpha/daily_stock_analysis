/**
 * useStockIndex Hook
 *
 * Manage stock index loading and state
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import type { StockIndexItem } from '../types/stockIndex';
import { loadStockIndex } from '../utils/stockIndexLoader';
import type { IndexLoadResult } from '../utils/stockIndexLoader';

export interface UseStockIndexOptions {
  /**
   * 懒加载模式：不自动拉取全量索引，仅在调用 load() 时加载。
   * 用于"远程搜索优先、失败后降级本地索引"的场景。
   */
  lazy?: boolean;
}

export interface UseStockIndexResult {
  /** Stock index data */
  index: StockIndexItem[];
  /** Is loading */
  loading: boolean;
  /** Load error */
  error: Error | null;
  /** Whether fallback mode is used */
  fallback: boolean;
  /** Is loaded */
  loaded: boolean;
  /** 手动触发加载（lazy 模式使用；幂等，已加载/加载中时为 no-op） */
  load: () => void;
}

/**
 * Stock index loading Hook
 *
 * @returns Index state and data
 */
export function useStockIndex(
  enabled = true,
  options: UseStockIndexOptions = {}
): UseStockIndexResult {
  const lazy = options.lazy === true;
  const [index, setIndex] = useState<StockIndexItem[]>([]);
  const [loading, setLoading] = useState(enabled && !lazy);
  const [error, setError] = useState<Error | null>(null);
  const [fallback, setFallback] = useState(false);
  const [loadTick, setLoadTick] = useState(lazy ? 0 : 1);
  const inFlightRef = useRef(false);
  const settledRef = useRef(false);

  const load = useCallback(() => {
    if (!enabled || inFlightRef.current || settledRef.current) {
      return;
    }
    setLoadTick(tick => tick + 1);
  }, [enabled]);

  useEffect(() => {
    if (!enabled || loadTick === 0) {
      return;
    }
    if (settledRef.current) {
      return;
    }

    let mounted = true;
    inFlightRef.current = true;
    setLoading(true);
    setError(null);

    async function loadIndex() {
      const result: IndexLoadResult = await loadStockIndex();

      if (mounted) {
        inFlightRef.current = false;
        settledRef.current = true;
        setIndex(result.data);
        setFallback(result.fallback);
        if (result.error) {
          setError(result.error);
        }
        setLoading(false);
      }
    }

    loadIndex();

    return () => {
      mounted = false;
    };
  }, [enabled, loadTick]);

  return {
    index: enabled ? index : [],
    loading: enabled ? loading : false,
    error: enabled ? error : null,
    fallback: enabled ? fallback : false,  // Whether fallback
    loaded: enabled ? !loading : false,
    load,
  };
}

/**
 * Get default exported Hook
 */
export default useStockIndex;
