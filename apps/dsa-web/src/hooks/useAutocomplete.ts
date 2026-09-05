/**
 * useAutocomplete Hook
 *
 * Manage autocomplete interaction logic
 */

import { useState, useCallback, useRef, useEffect } from 'react';
import type { StockIndexItem, StockSuggestion } from '../types/stockIndex';
import { searchStocks } from '../utils/searchStocks';
import { SEARCH_CONFIG } from '../utils/stockIndexFields';

export interface UseAutocompleteOptions {
  /** Minimum query length */
  minLength?: number;
  /** Debounce delay (milliseconds) */
  debounceMs?: number;
  /** Limit on number of results to return */
  limit?: number;
  /**
   * 远程搜索函数（后端 /api/v1/stocks/search）。提供后：
   * - 本地 index 尚未加载（懒加载模式）时走远程搜索；
   * - 远程失败时回调 onRemoteSearchFailed（由调用方触发全量索引加载），
   *   索引就绪后自动切回本地搜索。
   */
  remoteSearch?: (query: string, limit: number) => Promise<StockSuggestion[]>;
  /** 远程搜索失败回调（用于触发本地全量索引降级加载） */
  onRemoteSearchFailed?: () => void;
}

export interface UseAutocompleteResult {
  /** Current query string */
  query: string;
  /** Set query string */
  setQuery: (value: string) => void;
  /** Search suggestions list */
  suggestions: StockSuggestion[];
  /** Whether to show suggestions list */
  isOpen: boolean;
  /** Highlighted item index */
  highlightedIndex: number;
  /** Set highlighted item index */
  setHighlightedIndex: (index: number) => void;
  /** Highlight previous item */
  highlightPrevious: () => void;
  /** Highlight next item */
  highlightNext: () => void;
  /** Select suggestion item */
  handleSelect: (suggestion: StockSuggestion) => void;
  /** Close suggestions list */
  close: () => void;
  /** Reset state */
  reset: () => void;
  /** Whether IME is composing */
  isComposing: boolean;
  /** Set IME composing state */
  setIsComposing: (composing: boolean) => void;
  /** Whether runtime fallback mode is active */
  runtimeFallback: boolean;
  /** Runtime error captured from search flow */
  error: Error | null;
}

/**
 * Autocomplete Hook
 *
 * @param index - Stock index
 * @param options - Configuration options
 * @returns Autocomplete state and methods
 */
export function useAutocomplete(
  index: StockIndexItem[],
  options: UseAutocompleteOptions = {}
): UseAutocompleteResult {
  const {
    minLength = SEARCH_CONFIG.MIN_QUERY_LENGTH,
    debounceMs = SEARCH_CONFIG.DEBOUNCE_MS,
    limit = SEARCH_CONFIG.DEFAULT_LIMIT,
    remoteSearch,
    onRemoteSearchFailed,
  } = options;

  const [query, setQuery] = useState('');
  const [suggestions, setSuggestions] = useState<StockSuggestion[]>([]);
  const [isOpen, setIsOpen] = useState(false);
  const [highlightedIndex, setHighlightedIndex] = useState<number>(-1);
  const [isComposing, setIsComposing] = useState(false);
  const [runtimeFallback, setRuntimeFallback] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  // Use ref to store debounce timer
  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 远程请求序号：只应用最后一次输入的结果，避免竞态乱序
  const remoteRequestIdRef = useRef(0);
  const queryRef = useRef('');
  queryRef.current = query;

  // Search function (debounced)
  const search = useCallback((q: string) => {
    if (runtimeFallback) {
      return;
    }

    if (q.length < minLength) {
      setSuggestions([]);
      setIsOpen(false);
      setHighlightedIndex(-1);
      return;
    }

    if (remoteSearch && index.length === 0) {
      const requestId = ++remoteRequestIdRef.current;
      remoteSearch(q, limit)
        .then(results => {
          if (requestId !== remoteRequestIdRef.current) return;
          setSuggestions(results);
          setIsOpen(results.length > 0);
          setHighlightedIndex(-1);
        })
        .catch(caught => {
          if (requestId !== remoteRequestIdRef.current) return;
          console.error('Remote stock search failed; falling back to local index.', caught);
          onRemoteSearchFailed?.();
        });
      return;
    }

    try {
      const results = searchStocks(q, index, { limit });
      setSuggestions(results);
      setIsOpen(results.length > 0);
      setHighlightedIndex(-1);
    } catch (caught) {
      const runtimeError = caught instanceof Error ? caught : new Error('Autocomplete search failed');
      console.error('Autocomplete search failed. Falling back to plain input.', runtimeError);
      setError(runtimeError);
      setRuntimeFallback(true);
      setSuggestions([]);
      setIsOpen(false);
      setHighlightedIndex(-1);
    }
  }, [index, minLength, limit, runtimeFallback, remoteSearch, onRemoteSearchFailed]);

  // 本地索引在远程失败后加载完成时，用当前输入重新搜索一次
  useEffect(() => {
    if (index.length > 0 && queryRef.current.length >= minLength && !runtimeFallback) {
      search(queryRef.current);
    }
    // 仅在索引从空变为可用时触发
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [index.length]);

  // Input handling (with debounce)
  const handleInputChange = useCallback((value: string) => {
    setQuery(value);

    // Clear previous timer
    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current);
    }

    if (runtimeFallback) {
      return;
    }

    // Set new timer
    debounceTimerRef.current = setTimeout(() => {
      search(value);
    }, debounceMs);
  }, [search, debounceMs, runtimeFallback]);

  // Select suggestion item
  const handleSelect = useCallback((suggestion: StockSuggestion) => {
    setQuery(suggestion.displayCode);
    setIsOpen(false);
    setSuggestions([]);
    setHighlightedIndex(-1);
  }, []);

  // Highlight previous item
  const highlightPrevious = useCallback(() => {
    setHighlightedIndex(prev => {
      if (prev <= 0) return suggestions.length - 1;
      return prev - 1;
    });
  }, [suggestions.length]);

  // Highlight next item
  const highlightNext = useCallback(() => {
    setHighlightedIndex(prev => {
      if (prev >= suggestions.length - 1) return 0;
      return prev + 1;
    });
  }, [suggestions.length]);

  // Close dropdown
  const close = useCallback(() => {
    setIsOpen(false);
    setHighlightedIndex(-1);
  }, []);

  // Reset
  const reset = useCallback(() => {
    setQuery('');
    setSuggestions([]);
    setIsOpen(false);
    setHighlightedIndex(-1);
  }, []);

  // Cleanup timer (on component unmount)
  useEffect(() => {
    return () => {
      if (debounceTimerRef.current) {
        clearTimeout(debounceTimerRef.current);
      }
    };
  }, []);

  return {
    query,
    setQuery: handleInputChange,
    suggestions,
    isOpen,
    highlightedIndex,
    setHighlightedIndex,
    highlightPrevious,
    highlightNext,
    handleSelect,
    close,
    reset,
    isComposing,
    setIsComposing,
    runtimeFallback,
    error,
  };
}

/**
 * Get default exported Hook
 */
export default useAutocomplete;
