import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { useAuth } from "@/store/auth";
import { LogIn } from "lucide-react";

/** P6：邮箱 + 密码登录。username 字段在 P6 退化成可选昵称，登录路径上
 * 不再使用 —— 老用户的 ``email IS NULL`` 行会被后端 401 引导到注册页。
 */
export default function LoginPage() {
  const nav = useNavigate();
  const [params] = useSearchParams();
  const next = params.get("next") || "/setup";
  const login = useAuth((s) => s.login);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (!email.trim()) {
      setError("请输入邮箱");
      return;
    }
    if (!password) {
      setError("请输入密码");
      return;
    }
    setLoading(true);
    try {
      await login(email.trim(), password);
      // P4：成功跳转后立刻清空 password state，避免长期驻留 React 内存。
      setPassword("");
      nav(next, { replace: true });
    } catch (err: any) {
      const status = err?.response?.status;
      if (status === 401 || status === 404) {
        setError("邮箱或密码错误");
      } else if (status === 400) {
        setError(err?.response?.data?.detail || "登录参数无效");
      } else {
        setError("登录失败：" + (err?.response?.data?.detail || err.message));
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="max-w-md mx-auto pt-12">
      <Card>
        <CardHeader>
          <CardTitle>登录</CardTitle>
          <CardDescription>
            输入您的邮箱和密码 —— 每个用户的面试历史与 API key 缓存独立隔离。
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="login-email">邮箱</Label>
              <Input
                id="login-email"
                type="email"
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                autoFocus
                data-testid="input-login-email"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="login-password">密码</Label>
              <Input
                id="login-password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="输入密码"
                data-testid="input-login-password"
              />
            </div>
            {error && (
              <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
                {error}
              </div>
            )}
            <Button
              type="submit"
              className="w-full"
              disabled={loading}
              data-testid="btn-login-submit"
            >
              <LogIn className="h-4 w-4 mr-2" />
              {loading ? "登录中…" : "登录"}
            </Button>
            <div className="flex justify-between text-sm text-muted-foreground">
              <Link
                to="/forgot-password"
                className="underline hover:text-foreground"
                data-testid="link-to-forgot"
              >
                忘记密码？
              </Link>
              <Link
                to="/register"
                className="underline hover:text-foreground"
                data-testid="link-to-register"
              >
                去注册
              </Link>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
