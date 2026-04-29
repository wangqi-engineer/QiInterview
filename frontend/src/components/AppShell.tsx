import { useEffect, useState } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { cn } from "@/lib/utils";
import { Brain, History, LogOut, Settings2, User } from "lucide-react";
import { useAuth } from "@/store/auth";
import { Button } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { apiClient } from "@/lib/api";

export function AppShell({ children }: { children: React.ReactNode }) {
  const linkCls = ({ isActive }: { isActive: boolean }) =>
    cn(
      "flex items-center gap-2 rounded-lg px-3 py-2 text-sm transition-colors",
      isActive
        ? "bg-primary text-primary-foreground shadow-sm"
        : "text-muted-foreground hover:bg-secondary hover:text-foreground",
    );

  const me = useAuth((s) => s.me);
  const initialized = useAuth((s) => s.initialized);
  const init = useAuth((s) => s.init);
  const logout = useAuth((s) => s.logout);
  const nav = useNavigate();
  const location = useLocation();

  // 首屏加载时 fire-and-forget 拉一次 /api/auth/me；RequireAuth 也会触发，
  // 这里多 boot 一次是为了让顶栏在公共页面（/login / /register）也能正确显示。
  useEffect(() => {
    if (!initialized) {
      void init();
    }
  }, [initialized, init]);

  const isPublic =
    location.pathname.startsWith("/login") ||
    location.pathname.startsWith("/register");

  // 面试进行中，拦截「预约 / 历史 / 登出」的跳转，先弹窗确认是否结束面试。
  const interviewMatch = location.pathname.match(/^\/interview\/([^/]+)$/);
  const inInterview = !!interviewMatch;
  const sidInInterview = interviewMatch?.[1];
  type PendingNav = { kind: "path"; to: string } | { kind: "logout" };
  const [pendingNav, setPendingNav] = useState<PendingNav | null>(null);
  const [leaving, setLeaving] = useState(false);

  const onLogout = async () => {
    await logout();
    nav("/login", { replace: true });
  };

  const requestNavPath = (to: string) => (e: React.MouseEvent) => {
    if (inInterview) {
      e.preventDefault();
      setPendingNav({ kind: "path", to });
    }
    // 非面试页 NavLink 自行完成跳转
  };

  const requestLogout = () => {
    if (inInterview) {
      setPendingNav({ kind: "logout" });
      return;
    }
    void onLogout();
  };

  const confirmLeave = async () => {
    const p = pendingNav;
    if (!p) return;
    setLeaving(true);
    if (sidInInterview) {
      try {
        await apiClient.endInterview(sidInInterview, "user");
      } catch {
        /* 幂等失败（已结束等）按按钮跳转即可 */
      }
    }
    setLeaving(false);
    setPendingNav(null);
    if (p.kind === "logout") {
      await onLogout();
    } else {
      nav(p.to);
    }
  };

  return (
    <div className="min-h-screen flex flex-col">
      <header className="sticky top-0 z-40 backdrop-blur bg-background/80 border-b">
        <div className="container flex h-14 items-center justify-between">
          <div className="flex items-center gap-2">
            <Brain className="h-6 w-6 text-accent" />
            <span className="font-semibold tracking-tight">
              <span className="text-gradient-brand">QiInterview</span>
              <span className="text-muted-foreground text-xs ml-2">
                AI 模拟面试
              </span>
            </span>
          </div>
          <nav className="flex items-center gap-1">
            {!isPublic && me && (
              <>
                <NavLink to="/setup" className={linkCls} onClick={requestNavPath("/setup")}>
                  <Settings2 className="h-4 w-4" />
                  预约
                </NavLink>
                <NavLink to="/history" className={linkCls} onClick={requestNavPath("/history")}>
                  <History className="h-4 w-4" />
                  历史
                </NavLink>
                <span
                  className="ml-2 inline-flex items-center gap-1 px-2 py-1 text-xs text-muted-foreground"
                  data-testid="header-username"
                  title={me.email || ""}
                >
                  <User className="h-3 w-3" />
                  {me.username || me.email || ""}
                </span>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={requestLogout}
                  data-testid="btn-logout"
                  title="登出"
                >
                  <LogOut className="h-4 w-4" />
                </Button>
              </>
            )}
          </nav>
        </div>
      </header>
      <main className="container flex-1 py-8">{children}</main>
      {/* 面试进行中拦截顶栏跳转：确认后调 endInterview + 跳转目标页面 */}
      <AlertDialog
        open={!!pendingNav}
        onOpenChange={(open) => {
          if (!open && !leaving) setPendingNav(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>结束当前面试？</AlertDialogTitle>
            <AlertDialogDescription>
              跳转前会结束当前面试并生成复盘报告，结束后将无法继续本轮对话。确定继续吗？
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={leaving}>取消</AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault();
                void confirmLeave();
              }}
              disabled={leaving}
              data-testid="btn-confirm-leave-interview"
            >
              {leaving ? "正在结束..." : "是，结束面试"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
