import { useEffect, useRef, useState } from "react";
import { apiClient, type JobItem } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ExternalLink, Loader2, RefreshCcw } from "lucide-react";

const SOURCES = [
  { value: "all", label: "全部大厂" },
  { value: "tencent", label: "腾讯" },
  { value: "bytedance", label: "字节跳动" },
  { value: "alibaba", label: "阿里巴巴" },
];

/** s9：单页岗位数。后端默认 page_size=20，与之保持一致。 */
const PAGE_SIZE = 20;

export function JobPicker({
  selectedId,
  onSelect,
}: {
  selectedId?: number | null;
  onSelect: (job: JobItem) => void;
}) {
  const [items, setItems] = useState<JobItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [source, setSource] = useState<string>("all");
  const [q, setQ] = useState("");
  const [cached, setCached] = useState<boolean>(true);
  const [page, setPage] = useState<number>(1);
  const [total, setTotal] = useState<number>(0);

  // s9：滚动到列表底部时触发 page+1 拉取的哨兵；用 IntersectionObserver
  // 监听该元素是否进入滚动容器视口。
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  // 防止 fetch 还在飞行中又被快速滚动多触发的并发护栏。
  const inflightRef = useRef<boolean>(false);

  /** 拉取指定页。``replace=true`` 表示首屏 / 切换 source / 搜索，
   * 其余追加到 ``items`` 后面。 */
  const fetchPage = async (
    targetPage: number,
    opts: { replace?: boolean; refresh?: boolean } = {},
  ) => {
    if (inflightRef.current) return;
    inflightRef.current = true;
    if (opts.replace) setLoading(true);
    else setLoadingMore(true);
    try {
      const data = await apiClient.listJobs({
        source: source === "all" ? undefined : source,
        q: q || undefined,
        page: targetPage,
        pageSize: PAGE_SIZE,
        refresh: opts.refresh,
      });
      setItems((prev) =>
        opts.replace ? data.items : [...prev, ...data.items],
      );
      setCached(data.cached);
      setTotal(data.total);
      setPage(data.page);
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
      setLoadingMore(false);
      inflightRef.current = false;
    }
  };

  // 切换 source 时回到第 1 页（重新替换 items）。
  useEffect(() => {
    void fetchPage(1, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source]);

  // 哨兵进入视口 → 拉下一页（仅当还有未加载内容时）。
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el) return;
    const root = el.closest(".overflow-y-auto") as HTMLElement | null;
    if (items.length >= total) return; // 全部已加载，不再观察
    const obs = new IntersectionObserver(
      (entries) => {
        for (const ent of entries) {
          if (
            ent.isIntersecting &&
            !inflightRef.current &&
            items.length < total
          ) {
            void fetchPage(page + 1);
            break;
          }
        }
      },
      { root, rootMargin: "120px", threshold: 0 },
    );
    obs.observe(el);
    return () => obs.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items.length, total, page]);

  const onRefresh = async () => {
    setRefreshing(true);
    try {
      await apiClient.refreshJobs();
      await fetchPage(1, { replace: true });
    } finally {
      setRefreshing(false);
    }
  };

  const onSearch = () => {
    void fetchPage(1, { replace: true });
  };

  return (
    <div className="space-y-3" data-testid="job-picker">
      <div className="flex flex-wrap gap-2 items-center">
        <Select value={source} onValueChange={setSource}>
          <SelectTrigger className="w-32">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {SOURCES.map((s) => (
              <SelectItem key={s.value} value={s.value}>
                {s.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="检索关键词，如 大模型 / 算法"
          onKeyDown={(e) => {
            if (e.key === "Enter") onSearch();
          }}
          className="flex-1 min-w-[200px]"
        />
        <Button onClick={onSearch} variant="secondary">
          搜索
        </Button>
        <Button
          onClick={onRefresh}
          variant="outline"
          disabled={refreshing}
          title="强制后台刷新"
        >
          {refreshing ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <RefreshCcw className="h-4 w-4" />
          )}
          <span className="ml-2">刷新</span>
        </Button>
      </div>

      {!cached && (
        <p className="text-xs text-amber-600">
          缓存为空，正在后台首次抓取，稍后再点搜索即可。
        </p>
      )}

      <div className="grid gap-2 max-h-[420px] overflow-y-auto pr-1">
        {loading && (
          <div className="flex items-center justify-center py-12 text-muted-foreground">
            <Loader2 className="h-5 w-5 animate-spin mr-2" />
            加载中...
          </div>
        )}
        {!loading && items.length === 0 && (
          <div className="text-center text-muted-foreground py-12">
            暂无岗位，请尝试刷新
          </div>
        )}
        {items.map((it) => {
          const active = selectedId === it.id;
          return (
            <Card
              key={it.id}
              role="button"
              data-job-id={it.id}
              data-testid={`job-card-${it.source}-${it.source_post_id}`}
              onClick={() => onSelect(it)}
              className={`p-4 cursor-pointer transition-colors ${
                active
                  ? "border-accent ring-2 ring-accent/40"
                  : "hover:border-accent/60"
              }`}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 mb-1">
                    <Badge variant="secondary">{labelOf(it.source)}</Badge>
                    {it.location && (
                      <Badge variant="outline">{it.location}</Badge>
                    )}
                    {it.keyword && <Badge>{it.keyword}</Badge>}
                  </div>
                  <div className="font-medium truncate">{it.title}</div>
                  {it.requirement && (
                    <div className="text-xs text-muted-foreground line-clamp-2 mt-1">
                      {it.requirement}
                    </div>
                  )}
                </div>
                <a
                  href={it.raw_url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-xs text-accent hover:underline flex items-center gap-1 shrink-0"
                  onClick={(e) => e.stopPropagation()}
                >
                  原页 <ExternalLink className="h-3 w-3" />
                </a>
              </div>
            </Card>
          );
        })}
        {/* s9：滚动加载哨兵 + 状态行 */}
        {!loading && items.length > 0 && items.length < total && (
          <div
            ref={sentinelRef}
            data-testid="job-picker-sentinel"
            className="flex items-center justify-center py-3 text-xs text-muted-foreground"
          >
            {loadingMore ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin mr-2" />
                加载更多…
              </>
            ) : (
              `已加载 ${items.length} / ${total}，向下滚动加载更多`
            )}
          </div>
        )}
        {!loading && items.length > 0 && items.length >= total && (
          <div className="text-center text-xs text-muted-foreground py-3">
            已加载全部 {total} 个岗位
          </div>
        )}
      </div>
    </div>
  );
}

function labelOf(src: string): string {
  switch (src) {
    case "tencent":
      return "腾讯";
    case "bytedance":
      return "字节";
    case "alibaba":
      return "阿里";
    default:
      return src;
  }
}
