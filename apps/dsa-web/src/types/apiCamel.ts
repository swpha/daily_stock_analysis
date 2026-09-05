/**
 * OpenAPI 线格式 -> 前端运行时形状 的类型映射工具。
 *
 * 后端 FastAPI 返回 snake_case JSON；`src/api/utils.ts` 的 toCamelCase 在
 * 运行时把键转成 camelCase。`src/types/api-generated.ts` 由 openapi-typescript
 * 从后端 openapi.json 生成（线格式、snake_case）。两者用 Camelize 连接：
 *
 *   type UiStockSearchResponse = Camelize<components['schemas']['StockSearchResponse']>;
 *
 * 这样响应类型永远与后端 schema 同源，消灭手写类型的漂移。
 * 重新生成：仓库根 `python scripts/dump_openapi.py` 后在本目录 `npm run generate:api-types`。
 */

/** snake_case -> camelCase（仅键名层面；不影响值类型） */
type CamelizeKey<K extends string> = K extends `${infer Head}_${infer Rest}`
  ? `${Head}${CamelizeKey<Capitalize<Rest>>}`
  : K;

/** 递归把对象（含嵌套/数组/联合）的键转为 camelCase；null/基础类型原样透传。 */
export type Camelize<T> = T extends (infer U)[]
  ? Camelize<U>[]
  : T extends ReadonlyMap<infer K, infer V>
    ? ReadonlyMap<K, Camelize<V>>
    : T extends Date
      ? T
      : T extends object
        ? { [K in keyof T as CamelizeKey<K & string>]: Camelize<T[K]> }
        : T;
