import { useState } from "react";
import { Link } from "react-router-dom";
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
import { Mail } from "lucide-react";

/** P6：忘记密码入口 —— 输入邮箱 → 后端寄出重置链接。
 *
 * UI 永远只显示同一段提示文案，与 ``/auth/password-reset/start`` 后端响应
 * 一律 200 的 anti-enumeration 合同对齐 —— 攻击者无从从 UI 反应区分
 * "邮箱存在 / 邮箱不存在 / 节流命中"。
 */
export default function ForgotPasswordPage() {
  const passwordResetStart = useAuth((s) => s.passwordResetStart);
  const [email, setEmail] = useState("");
  const [info, setInfo] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setInfo("");
    if (!/^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/.test(email.trim())) {
      setError("请输入有效的邮箱地址");
      return;
    }
    setLoading(true);
    try {
      await passwordResetStart(email.trim());
      // 与后端 anti-enumeration 对齐：永远显示一致提示
      setInfo(
        "如果该邮箱已注册，我们会在几秒内寄出一封带重置链接的邮件，请在 30 分钟内打开。",
      );
    } catch (err: any) {
      const status = err?.response?.status;
      if (status === 429) {
        setError("请求过于频繁，请等待几十秒后再试");
      } else if (status === 400) {
        setError(err?.response?.data?.detail || "邮箱格式无效");
      } else if (status === 502) {
        setError("邮件发送失败，请稍后重试或联系管理员");
      } else {
        setError("发送失败：" + (err?.response?.data?.detail || err.message));
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="max-w-md mx-auto pt-12">
      <Card>
        <CardHeader>
          <CardTitle>忘记密码</CardTitle>
          <CardDescription>
            输入注册时使用的邮箱，我们会发送一个一次性重置链接到您的邮箱。
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} className="space-y-4">
            <div className="space-y-1.5">
              <Label htmlFor="forgot-email">邮箱</Label>
              <Input
                id="forgot-email"
                type="email"
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                autoFocus
                data-testid="input-forgot-email"
              />
            </div>
            {info && (
              <div
                className="rounded-md border border-emerald-300/40 bg-emerald-50 px-3 py-2 text-sm text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100"
                data-testid="toast-forgot-sent"
              >
                {info}
              </div>
            )}
            {error && (
              <div className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
                {error}
              </div>
            )}
            <Button
              type="submit"
              className="w-full"
              disabled={loading}
              data-testid="btn-forgot-submit"
            >
              <Mail className="h-4 w-4 mr-2" />
              {loading ? "发送中…" : "发送重置链接"}
            </Button>
            <p className="text-sm text-center text-muted-foreground">
              想起密码了？
              <Link
                to="/login"
                className="ml-1 underline hover:text-foreground"
                data-testid="link-back-to-login"
              >
                去登录
              </Link>
            </p>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
