import { describe, expect, it } from 'vitest';
import { MIN_VIEWPORT_ASPECT, isViewportTooNarrow } from './viewportGuard';

describe('极窄窗口判定', () => {
  it('阈值是普通手机竖屏 9:20 的 2/3', () => {
    // 手机竖屏写成分数就是 9:20，它的 2/3 是 9:30 = 0.30。这里从 9 与 20 重新算一遍，
    // 而不是把常量换个写法再断言一次 —— 基准取错了（比如误当 16:9）要能在这里断下来。
    expect(MIN_VIEWPORT_ASPECT).toBe((9 / 20) * (2 / 3));
    expect(MIN_VIEWPORT_ASPECT).toBe(0.3);
  });

  it('触发拦截的窗口：255×1112', () => {
    expect(isViewportTooNarrow(255, 1112)).toBe(true);
    // 竖屏被压得更窄的情形同样拦下。
    expect(isViewportTooNarrow(200, 1112)).toBe(true);
    expect(isViewportTooNarrow(280, 1000)).toBe(true);
  });

  it('放行的窗口：真实手机竖屏、平板、任何横屏', () => {
    for (const [width, height] of [[390, 844], [390, 1000], [340, 800], [430, 932],
      [768, 1024], [900, 1000], [1280, 720], [1440, 900], [1440, 500], [255, 720]]) {
      expect(isViewportTooNarrow(width, height), `${width}×${height}`).toBe(false);
    }
  });

  it('边界恰好等于阈值时放行，再窄一点才拦', () => {
    expect(isViewportTooNarrow(300, 1000)).toBe(false);
    expect(isViewportTooNarrow(299, 1000)).toBe(true);
    // 高度变化也走同一条判断，不是只看宽度。
    expect(isViewportTooNarrow(300, 1001)).toBe(true);
    expect(isViewportTooNarrow(300, 999)).toBe(false);
  });

  it('量不出尺寸时不算过窄', () => {
    expect(isViewportTooNarrow(0, 1112)).toBe(false);
    expect(isViewportTooNarrow(255, 0)).toBe(false);
    expect(isViewportTooNarrow(Number.NaN, 1112)).toBe(false);
    expect(isViewportTooNarrow(255, Number.POSITIVE_INFINITY)).toBe(false);
  });
});
