import { create } from "zustand";

/**
 * 会话级别的"已结束面试 sid"集合。
 *
 * 背景：`AppShell` 的顶栏拦截（预约 / 历史 / 登出）是基于路由 `/interview/:sid`
 * 是否匹配决定的，但"ended=true"状态原本只活在 `InterviewPage` 的本地 state
 * 里。AppShell 无法感知用户是否已经点过"结束面试"，于是每次点顶栏都会再弹一次
 * "确认结束当前面试？"——这是多余的。
 *
 * 该 store 充当两者之间的跨组件信号：`InterviewPage` 把"已结束"通过 `markEnded`
 * 广播出来，`AppShell` 用 `isEnded` 决定是否跳过弹窗。只保留在内存（刷新即丢）
 * 即可，不需要 persist——因为重新拉 `/interview/:sid` 详情时 ``ended_at`` 会
 * 重新触发 `markEnded`。
 */
interface InterviewStatusState {
  endedSids: Set<string>;
  markEnded: (sid: string) => void;
  isEnded: (sid: string) => boolean;
}

export const useInterviewStatus = create<InterviewStatusState>((set, get) => ({
  endedSids: new Set<string>(),
  markEnded: (sid: string) =>
    set((s) => {
      if (!sid || s.endedSids.has(sid)) return s;
      const next = new Set(s.endedSids);
      next.add(sid);
      return { endedSids: next };
    }),
  isEnded: (sid: string) => (sid ? get().endedSids.has(sid) : false),
}));
