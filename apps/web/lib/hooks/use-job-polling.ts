"use client";
import { useQuery } from "@tanstack/react-query";
import { getJob } from "@/lib/api/jobs";

const TERMINAL = new Set(["done", "failed"]);

export function useJobPolling(jobId: string | null) {
  return useQuery({
    queryKey: ["job", jobId],
    queryFn: () => getJob(jobId!),
    enabled: !!jobId,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && TERMINAL.has(status) ? false : 3000;
    },
  });
}
