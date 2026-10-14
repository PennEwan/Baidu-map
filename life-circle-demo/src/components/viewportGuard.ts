/**
 * 极窄窗口的判定：只算宽高比，不设固定像素下限。
 *
 * 窗口被拖成又高又窄时，缺口不在"少了几像素"，而在版式本身 —— 顶栏、左右面板、
 * 地图列都要一个最小宽度才排得开，窗口越窄只会越挤，靠加宽几十像素解决不了。
 * 换成一个比例，横竖屏、缩放、不同缩放比的外接屏就都落在同一条尺子上。
 *
 * 基准是常见手机竖屏 9:20（宽高比 0.45）；取它的 2/3 就是 0.30。
 * 255×1112 这类窗口（约 0.23）会落进来，而 390×844 的真实手机竖屏（约 0.46）
 * 与任何横屏都不会被误伤。
 */
export const MIN_VIEWPORT_ASPECT = 0.3;

/**
 * 内容窗口是否窄到必须拦下。高或宽量不出来时（隐藏的 iframe、尚未布局的首帧）
 * 不算过窄 —— 拿不到尺寸就拦，等于把页面锁死在一个本来就看不见的窗口上。
 */
export function isViewportTooNarrow(width: number, height: number): boolean {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return false;
  return width / height < MIN_VIEWPORT_ASPECT;
}
