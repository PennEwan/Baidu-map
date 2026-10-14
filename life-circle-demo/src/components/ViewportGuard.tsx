/**
 * 极窄窗口守卫：窗口宽度窄于普通手机宽高比的 2/3 时，用一屏提示挡住页面。
 *
 * 提示走原生 `dialog.showModal()`：浏览器把底下的页面整体标成 inert，指针、触摸、
 * Tab 与读屏都到不了；Esc 另在捕获阶段吃掉（见下），提示自己关不掉。页面本身仍挂在
 * 原来的位置上 —— 窗口拉宽后提示自动收走，正在跑的任务、填了一半的坐标都还在，
 * 不需要刷新。
 *
 * 拦的是"窗口装不下版式"，不是某一个页面：所以它接在公共入口上，离线演示与
 * 体检工作台两条路径共用同一条判断。
 */
import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { MobileOutlined } from '@ant-design/icons';
import { isViewportTooNarrow } from './viewportGuard';
import styles from './viewportGuard.module.css';

export default function ViewportGuard({ children }: { children: ReactNode }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [tooNarrow, setTooNarrow] = useState(() =>
    isViewportTooNarrow(window.innerWidth, window.innerHeight));

  // 开窗、改尺寸、转屏都要重新判断：与页面其它窗口检测走同一条 resize 路径，
  // 另外接上 orientationchange —— 移动端转屏时 resize 未必先到。
  useEffect(() => {
    const measure = () => setTooNarrow(isViewportTooNarrow(window.innerWidth, window.innerHeight));
    window.addEventListener('resize', measure);
    window.addEventListener('orientationchange', measure);
    return () => {
      window.removeEventListener('resize', measure);
      window.removeEventListener('orientationchange', measure);
    };
  }, []);

  // 在布局阶段同步开关：正常尺寸下不会先闪一帧提示，过窄时也不留可交互的一帧。
  useLayoutEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (tooNarrow && !element.open) element.showModal();
    else if (!tooNarrow && element.open) element.close();
  }, [tooNarrow]);

  // 提示期间锁住文档滚动；恢复时把原来的行内值还回去，不留副作用。
  useEffect(() => {
    if (!tooNarrow) return;
    const { body } = document;
    const previous = body.style.overflow;
    body.style.overflow = 'hidden';
    return () => { body.style.overflow = previous; };
  }, [tooNarrow]);

  /*
   * Esc 必须在捕获阶段吃掉，理由有两条，缺一条都拦不住：
   *
   * - 浏览器处理对话框的关闭请求是有额度的：连按几次之后 cancel 事件的 cancelable
   *   变成 false，preventDefault() 就成了空话，提示被强行关掉且自己回不来。实测
   *   第二次按下就已经关掉了。preventDefault 打在这记 keydown 上，关闭请求根本不会发出。
   * - inert 挡的是指针与焦点，挡不住冒泡。页面自己的 window 级 Esc 快捷键照跑不误
   *   （antd 的抽屉就靠它关闭），会趁提示盖着的时候把底下的界面改掉。
   *   stopImmediatePropagation 让这些监听器连事件都收不到。
   *
   * 不锁别的键：页面自己的按键处理都挂得焦点元素上，而焦点进不来。
   */
  useEffect(() => {
    if (!tooNarrow) return;
    const swallow = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      event.stopImmediatePropagation();
    };
    window.addEventListener('keydown', swallow, true);
    return () => window.removeEventListener('keydown', swallow, true);
  }, [tooNarrow]);

  return <>
    {children}
    {/* 兜底：真被别的路径关掉（浏览器强关、外部脚本 close()），只要窗口仍旧过窄就再打开，
        不让"关掉提示"成为一条能永久逃出守卫的路。Esc 由上面的 keydown 处理。 */}
    <dialog ref={dialog} className={styles.guard} data-testid="viewport-guard"
      aria-labelledby="viewport-guard-title" aria-describedby="viewport-guard-detail"
      onCancel={event => event.preventDefault()}
      onClose={() => { if (tooNarrow && !dialog.current?.open) dialog.current?.showModal(); }}>
      <div className={styles.body}>
        <span className={styles.icon} aria-hidden="true"><MobileOutlined /></span>
        <h2 id="viewport-guard-title">窗口过小</h2>
        <p id="viewport-guard-detail">
          当前窗口过窄，页面无法正常显示。<br />
          请加宽窗口或旋转设备，恢复后会自动继续。
        </p>
        <p className={styles.hint}>窗口宽度达到高度的 30% 以上即可继续使用</p>
      </div>
    </dialog>
  </>;
}
