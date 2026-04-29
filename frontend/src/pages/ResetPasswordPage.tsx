import { useEffect, useState } from "react";
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
import { ShieldCheck } from "lucide-react";

/** P6：通过 ``?token=...`` 着陆，输入新密码 → 后端校验 token 哈希 →
 * 写新 ``password_hash`` + 撤销该用户全部现存 session。本页**不**自动登录，
 * 跳到 ``/login`` 让用户重新输入密码 —— 防"攻击者发动重置 → 用户点链接 →
 * 攻击者趁同会话偷用"的搭便车攻击。
 */
export default function ResetPasswordPage() {
  const nav = useNavigate();
  const [params] = useSearchParams();
  const tokenFromUrl = params.get("token") || "";
  const passwordResetConfirm = useAuth((s) => s.passwordResetConfirm);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!tokenFromUrl) {
      setError("缺少 token，请通过邮件中的链接打开本页");
    }
  }, [tokenFromUrl]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (!tokenFromUrl) {
      setError("缺少 token，请通过邮件中的链接打开本页");
      return;
    }
    if (password.length < 6) {
      setError("密码长度至少 6 位");
      return;
    }
    if (password !== confirm) {
      setError("两次输入的密码不一致");
      return;
    }
    setLoading(true);
    try {
      await passwordResetConfirm(tokenFromUrl, password);
      setPassword("");
      setConfirm("");
      setDone(true);
      // 不自动登录，给用户 2 秒提示后跳到 /login
      setTimeout(() => nav("/login", { replace: true }), 1500);
    } catch (err: any) {
      const status = err?.response?.status;
      if (status === 401) {
        setError("链接无效或已过期，请回到登录页重新申请重置");
      } else if (status === 400) {
        setError(err?.response?.data?.detail || "密码不合规");
      } else {
        setError("重置失败：" + (err?.response?.data?.detail || err.message));
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="max-w-md mx-auto pt-12">
      <Card>
        <CardHeader>
          <CardTitle>重置密码</CardTitle>
          <CardDescription>
            为账号设置新密码。重置成功后所有现存登录会话都会失效，您需要在
            登录页用新密码重新登录。
          </CardDescription>
        </CardHeader>
        <CardContent>
          {done ? (
            <div
              className="rounded-md border border-emerald-300/40 bg-emerald-50 px-3 py-3 text-sm text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100"
              data-testid="text-reset-done"
            >
              密码已重置成功，正在跳转到登录页…
            </div>
          ) : (
            <form onSubmit={submit} className="space-y-4">
              <div className="space-y-1.5">
                <Label htmlFor="reset-password">新密码</Label>
                <Input
                  id="reset-password"
                  type="password"
                  autoComplete="new-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="至少 6 位"
                  autoFocus
                  data-testid="input-reset-password"
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="reset-password-confirm">确认新密码</Label>
                <Input
                  id="reset-password-confirm"
                  type="password"
                  autoComplete="new-password"
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  placeholder="再次输入新密码"
                  data-testid="input-reset-password-confirm"
                />
              </div>
              {error && (
                <div
                  className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive"
                  data-testid="text-reset-error"
                >
                  {error}
                </div>
              )}
              <Button
                type="submit"
                className="w-full"
                disabled={loading || !tokenFromUrl}
                data-testid="btn-reset-submit"
              >
                <ShieldCheck className="h-4 w-4 mr-2" />
                {loading ? "重置中…" : "重置密码"}
              </Button>
              <p className="text-sm text-center text-muted-foreground">
                <Link
                  to="/login"
                  className="underline hover:text-foreground"
                  data-testid="link-back-to-login"
                >
                  返回登录页
                </Link>
              </p>
            </form>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
