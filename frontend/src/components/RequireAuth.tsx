import { useEffect } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "@/store/auth";
import { Loader2 } from "lucide-react";

/** 路由守卫：未登录 → 重定向到 ``/login?next=<原路径>``。

    P3 / a1 用户合同：``访问 /setup 未登录 → 必须被重定向到 /login``。
    本组件被 ``main.tsx`` 包在 ``/setup`` / ``/interview/:sid`` /
    ``/report/:sid`` / ``/history`` 这些受保护路由外面。
*/
export function RequireAuth({ children }: { children: React.ReactNode }) {
  const me = useAuth((s) => s.me);
  const initialized = useAuth((s) => s.initialized);
  const init = useAuth((s) => s.init);
  const location = useLocation();

  useEffect(() => {
    if (!initialized) {
      void init();
    }
  }, [initialized, init]);

  if (!initialized) {
    return (
      <div className="flex items-center justify-center py-20 text-sm text-muted-foreground">
        <Loader2 className="h-4 w-4 mr-2 animate-spin" />
        正在校验登录状态…
      </div>
    );
  }

  if (!me) {
    const next = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/login?next=${next}`} replace />;
  }

  return <>{children}</>;
}
