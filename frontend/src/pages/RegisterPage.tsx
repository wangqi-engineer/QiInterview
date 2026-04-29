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
import { Mail, ShieldCheck, UserPlus } from "lucide-react";

/** P6：邮箱主身份注册。两步：
 *
 *   step1 ``email``     —— 后端寄出 6 位 OTP。响应一律 200 不暴露 email
 *                          是否已注册（用户 UI 只看到"我们已发送验证码到 X"）。
 *   step2 ``otp + password [+ nickname]``  —— 校验 OTP → 创建账号 → 写 cookie。
 *
 * password 仍走前端 RSA-OAEP；UI 提交后立刻 ``setPassword("")`` 缩短明文驻留时间。
 */
export default function RegisterPage() {
  const nav = useNavigate();
  const [params] = useSearchParams();
  const next = params.get("next") || "/setup";
  const registerStart = useAuth((s) => s.registerStart);
  const registerVerify = useAuth((s) => s.registerVerify);

  const [step, setStep] = useState<"email" | "verify">("email");
  const [email, setEmail] = useState("");
  const [otp, setOtp] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  // 昵称非必填；空 = 后端 ``User.username = NULL``
  const [username, setUsername] = useState("");
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [loading, setLoading] = useState(false);

  const _emailLooksValid = (s: string) =>
    /^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/.test(s.trim());

  const submitStart = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setInfo("");
    if (!_emailLooksValid(email)) {
      setError("请输入有效的邮箱地址");
      return;
    }
    setLoading(true);
    try {
      await registerStart(email);
      // 不暴露"邮箱是否已注册" —— 永远显示同样的提示
      setInfo("我们已向该邮箱发送了一封验证码邮件，请在 10 分钟内查收并填写。");
      setStep("verify");
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

  const submitVerify = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setInfo("");
    if (!/^\d{6}$/.test(otp.trim())) {
      setError("请输入收到的 6 位数字验证码");
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
    if (username.trim() && !/^[A-Za-z0-9_-]{3,64}$/.test(username.trim())) {
      setError("昵称仅允许字母 / 数字 / 下划线 / 连字符，长度 3–64");
      return;
    }
    setLoading(true);
    try {
      await registerVerify(email, otp, password, username || undefined);
      setPassword("");
      setConfirm("");
      nav(next, { replace: true });
    } catch (err: any) {
      const status = err?.response?.status;
      if (status === 401) {
        setError("验证码无效或已过期，请重新获取");
      } else if (status === 409) {
        setError("该邮箱或昵称已被占用");
      } else if (status === 400) {
        setError(err?.response?.data?.detail || "注册参数无效");
      } else {
        setError("注册失败：" + (err?.response?.data?.detail || err.message));
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="max-w-md mx-auto pt-12">
      <Card>
        <CardHeader>
          <CardTitle>注册</CardTitle>
          <CardDescription>
            注册后每个用户的面试历史与 API key 缓存独立隔离；忘记密码可通过邮箱找回。
          </CardDescription>
        </CardHeader>
        <CardContent>
          {step === "email" ? (
            <form onSubmit={submitStart} className="space-y-4">
              <div className="space-y-1.5">
                <Label htmlFor="register-email">邮箱</Label>
                <Input
                  id="register-email"
                  type="email"
                  autoComplete="email"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@example.com"
                  autoFocus
                  data-testid="input-register-email"
                />
              </div>
              {info && (
                <div
                  className="rounded-md border border-emerald-300/40 bg-emerald-50 px-3 py-2 text-sm text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100"
                  data-testid="text-register-otp-sent"
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
                data-testid="btn-register-send-otp"
              >
                <Mail className="h-4 w-4 mr-2" />
                {loading ? "发送中…" : "发送验证码"}
              </Button>
              <p className="text-sm text-center text-muted-foreground">
                已有账号？
                <Link
                  to="/login"
                  className="ml-1 underline hover:text-foreground"
                  data-testid="link-to-login"
                >
                  去登录
                </Link>
              </p>
            </form>
          ) : (
            <form onSubmit={submitVerify} className="space-y-4">
              {info && (
                <div
                  className="rounded-md border border-emerald-300/40 bg-emerald-50 px-3 py-2 text-sm text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100"
                  data-testid="text-register-otp-sent"
                >
                  {info}
                </div>
              )}
              <div className="space-y-1.5">
                <Label htmlFor="register-email-shown">邮箱</Label>
                <Input
                  id="register-email-shown"
                  value={email}
                  disabled
                  data-testid="input-register-email-shown"
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="register-otp">验证码</Label>
                <Input
                  id="register-otp"
                  inputMode="numeric"
                  maxLength={6}
                  value={otp}
                  onChange={(e) => setOtp(e.target.value)}
                  placeholder="6 位数字"
                  autoFocus
                  data-testid="input-register-otp"
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="register-username">昵称（可选）</Label>
                <Input
                  id="register-username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="3–64 位字母 / 数字 / _ / -，留空也可以"
                  data-testid="input-register-username"
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="register-password">密码</Label>
                <Input
                  id="register-password"
                  type="password"
                  autoComplete="new-password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="至少 6 位"
                  data-testid="input-register-password"
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="register-password-confirm">确认密码</Label>
                <Input
                  id="register-password-confirm"
                  type="password"
                  autoComplete="new-password"
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  placeholder="再次输入密码"
                  data-testid="input-register-password-confirm"
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
                data-testid="btn-register-submit"
              >
                <UserPlus className="h-4 w-4 mr-2" />
                {loading ? "注册中…" : "完成注册"}
              </Button>
              <Button
                type="button"
                variant="ghost"
                className="w-full"
                onClick={() => {
                  setStep("email");
                  setOtp("");
                  setError("");
                  setInfo("");
                }}
                data-testid="btn-register-back-to-email"
              >
                <ShieldCheck className="h-4 w-4 mr-2" />
                返回上一步换个邮箱
              </Button>
              <p className="text-sm text-center text-muted-foreground">
                已有账号？
                <Link
                  to="/login"
                  className="ml-1 underline hover:text-foreground"
                  data-testid="link-to-login"
                >
                  去登录
                </Link>
              </p>
            </form>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
