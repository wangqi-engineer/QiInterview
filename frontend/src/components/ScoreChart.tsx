import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

export interface TrendPoint {
  idx: number;
  score: number;
  delta: number;
}

export function ScoreChart({
  data,
  initial,
}: {
  data: TrendPoint[];
  initial: number;
}) {
  const merged = [{ idx: 0, score: initial, delta: 0 }, ...data];
  return (
    <div style={{ width: "100%", height: 280 }}>
      <ResponsiveContainer>
        <LineChart data={merged} margin={{ top: 16, right: 24, left: 0, bottom: 8 }}>
          <CartesianGrid strokeDasharray="3 3" opacity={0.35} />
          <XAxis dataKey="idx" label={{ value: "轮次", position: "insideBottom", offset: -4 }} />
          <YAxis domain={[0, 100]} />
          <Tooltip
            formatter={(v: number, name: string) => [v, name === "score" ? "分数" : name]}
            labelFormatter={(idx) => (idx === 0 ? "印象分" : `第 ${idx} 轮`)}
          />
          <ReferenceLine y={50} stroke="#dc2626" strokeDasharray="4 4" label="熔断线" />
          <Line
            type="monotone"
            dataKey="score"
            stroke="hsl(var(--accent))"
            strokeWidth={2.5}
            dot={{ r: 4, fill: "hsl(var(--accent))" }}
            activeDot={{ r: 6 }}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
