import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { apiClient, type InterviewOut } from "@/lib/api";
import { formatDateTime } from "@/lib/utils";
import {
  ChevronLeft,
  ChevronRight,
  Eye,
  Loader2,
  RotateCcw,
  Trash2,
} from "lucide-react";

const TYPE_LABEL: Record<string, string> = {
  tech1: "技术一面",
  tech2: "技术二面",
  comprehensive: "综合面",
  hr: "HR 面",
};

const PAGE_SIZE = 10;

export default function HistoryPage() {
  const [items, setItems] = useState<InterviewOut[] | null>(null);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);

  const load = async (targetPage: number = page) => {
    setLoading(true);
    try {
      const data = await apiClient.listInterviews({
        page: targetPage,
        pageSize: PAGE_SIZE,
      });
      setItems(data.items);
      setTotal(data.total);
      setPage(data.page);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onDelete = async (sid: string) => {
    await apiClient.deleteInterview(sid);
    // 删除后若当前页已空且不在第 1 页，自动回退一页；否则原地重载。
    const nextPage =
      items && items.length === 1 && page > 1 ? page - 1 : page;
    void load(nextPage);
  };

  const onDeleteAll = async () => {
    await apiClient.deleteAllInterviews();
    // 一键删除后回到第 1 页（列表必然为空，不再分页）
    void load(1);
  };

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const canPrev = page > 1 && !loading;
  const canNext = page < totalPages && !loading;

  return (
    <Card data-testid="history-page">
      <CardHeader className="flex flex-row items-center justify-between">
        <div>
          <CardTitle>历史面试</CardTitle>
          <CardDescription>
            {total > 0
              ? `共 ${total} 条 · 第 ${page}/${totalPages} 页`
              : "查看复盘报告或删除记录"}
          </CardDescription>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => void load(page)}>
            <RotateCcw className="h-4 w-4 mr-1" />
            刷新
          </Button>
          {total > 0 && (
            <AlertDialog>
              <AlertDialogTrigger asChild>
                <Button
                  variant="destructive"
                  size="sm"
                  data-testid="btn-history-clear-all"
                >
                  <Trash2 className="h-4 w-4 mr-1" />
                  一键删除
                </Button>
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>清空所有历史面试？</AlertDialogTitle>
                  <AlertDialogDescription>
                    将删除全部 {total} 条面试记录、对话与复盘报告，
                    操作不可恢复。请确认后再继续。
                  </AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>取消</AlertDialogCancel>
                  <AlertDialogAction
                    onClick={() => void onDeleteAll()}
                    data-testid="btn-history-clear-all-confirm"
                  >
                    确认清空
                  </AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
          )}
        </div>
      </CardHeader>
      <CardContent>
        {loading && (
          <div className="text-center py-12 text-muted-foreground">
            <Loader2 className="h-5 w-5 mx-auto animate-spin mb-2" />
            加载中...
          </div>
        )}
        {!loading && items && items.length === 0 && (
          <div className="text-center py-16 text-muted-foreground">
            暂无面试记录，
            <Link to="/setup" className="text-accent hover:underline">
              立即开始
            </Link>
          </div>
        )}
        <div className="space-y-2">
          {(items || []).map((it) => (
            <div
              key={it.id}
              className="flex flex-wrap items-center gap-3 p-3 border rounded-md hover:bg-muted/40 transition-colors"
              data-testid={`history-item-${it.id}`}
            >
              <Badge variant="secondary">
                {TYPE_LABEL[it.interview_type] ?? it.interview_type}
              </Badge>
              <Badge variant="outline">
                {it.eval_mode === "realtime" ? "实时评价" : "整体评价"}
              </Badge>
              <div className="flex-1 min-w-[200px] truncate font-medium">
                {it.job_title}
              </div>
              <div className="text-xs text-muted-foreground whitespace-nowrap">
                {formatDateTime(it.created_at)}
              </div>
              <Badge>得分 {it.final_score}</Badge>
              {it.end_reason && (
                <Badge variant="outline" className="text-[10px]">
                  {it.end_reason}
                </Badge>
              )}
              <div className="flex gap-2 ml-auto">
                <Link to={`/report/${it.id}`}>
                  <Button variant="outline" size="sm">
                    <Eye className="h-4 w-4 mr-1" />
                    复盘
                  </Button>
                </Link>
                <AlertDialog>
                  <AlertDialogTrigger asChild>
                    <Button
                      variant="destructive"
                      size="sm"
                      data-testid={`btn-delete-${it.id}`}
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </AlertDialogTrigger>
                  <AlertDialogContent>
                    <AlertDialogHeader>
                      <AlertDialogTitle>删除该面试记录？</AlertDialogTitle>
                      <AlertDialogDescription>
                        关联的所有对话与复盘报告将一并删除，不可恢复。
                      </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                      <AlertDialogCancel>取消</AlertDialogCancel>
                      <AlertDialogAction
                        onClick={() => onDelete(it.id)}
                        data-testid={`btn-delete-confirm-${it.id}`}
                      >
                        确认删除
                      </AlertDialogAction>
                    </AlertDialogFooter>
                  </AlertDialogContent>
                </AlertDialog>
              </div>
            </div>
          ))}
        </div>
        {total > PAGE_SIZE && (
          <div
            className="flex items-center justify-end gap-2 pt-4 mt-2 border-t"
            data-testid="history-pagination"
          >
            <Button
              variant="outline"
              size="sm"
              disabled={!canPrev}
              onClick={() => void load(page - 1)}
              data-testid="history-prev-page"
            >
              <ChevronLeft className="h-4 w-4 mr-1" />
              上一页
            </Button>
            <div className="text-xs text-muted-foreground tabular-nums">
              {page} / {totalPages}
            </div>
            <Button
              variant="outline"
              size="sm"
              disabled={!canNext}
              onClick={() => void load(page + 1)}
              data-testid="history-next-page"
            >
              下一页
              <ChevronRight className="h-4 w-4 ml-1" />
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
