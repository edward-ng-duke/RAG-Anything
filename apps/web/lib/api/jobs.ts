import client from "./client";
import type { JobResponse } from "./types";

export async function getJob(jobId: string) {
  const r = await client.get<JobResponse>(`/v1/jobs/${jobId}`);
  return r.data;
}
