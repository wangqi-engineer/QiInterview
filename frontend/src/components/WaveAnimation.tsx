import { cn } from "@/lib/utils";

export type WaveState = "idle" | "thinking" | "speaking" | "listening";

const STATE_CFG: Record<
  WaveState,
  { ring: string; bar: string; label: string }
> = {
  idle: {
    ring: "bg-muted/40",
    bar: "bg-muted-foreground/40",
    label: "等待中",
  },
  thinking: {
    ring: "bg-amber-400/40",
    bar: "bg-amber-500",
    label: "AI 思考中",
  },
  speaking: {
    ring: "bg-blue-500/40",
    bar: "bg-blue-500",
    label: "AI 发言中",
  },
  listening: {
    ring: "bg-emerald-500/40",
    bar: "bg-emerald-500",
    label: "倾听你的回答",
  },
};

export function WaveAnimation({ state }: { state: WaveState }) {
  const cfg = STATE_CFG[state];
  const animated = state !== "idle";
  return (
    <div className="flex flex-col items-center gap-4">
      <div className="relative w-44 h-44 flex items-center justify-center">
        {animated && (
          <>
            <span
              className={cn(
                "absolute inset-0 rounded-full animate-ripple",
                cfg.ring,
              )}
              style={{ animationDelay: "0s" }}
            />
            <span
              className={cn(
                "absolute inset-0 rounded-full animate-ripple",
                cfg.ring,
              )}
              style={{ animationDelay: "0.6s" }}
            />
          </>
        )}
        <div
          className={cn(
            "relative w-32 h-32 rounded-full bg-card border-2 border-border shadow-lg flex items-end justify-center gap-1.5 px-6 pb-8",
          )}
        >
          {[0, 1, 2, 3, 4].map((i) => (
            <span
              key={i}
              className={cn(
                "w-1.5 rounded-full",
                cfg.bar,
                animated && "animate-wave",
              )}
              style={{
                height: animated ? "30px" : "10px",
                animationDelay: `${i * 0.12}s`,
              }}
            />
          ))}
        </div>
      </div>
      <div className="text-sm text-muted-foreground">{cfg.label}</div>
    </div>
  );
}
