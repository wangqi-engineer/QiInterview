import { useEffect } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { cn } from "@/lib/utils";
import { Brain, History, LogOut, Settings2, User } from "lucide-react";
import { useAuth } from "@/store/auth";
import { Button } from "@/components/ui/button";

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

  const onLogout = async () => {
    await logout();
    nav("/login", { replace: true });
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
                <NavLink to="/setup" className={linkCls}>
                  <Settings2 className="h-4 w-4" />
                  预约
                </NavLink>
                <NavLink to="/history" className={linkCls}>
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
                  onClick={onLogout}
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
    </div>
  );
}
