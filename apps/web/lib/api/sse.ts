export interface SSEEvent {
  event: string;
  data: any;
}

export async function* streamSSE(
  url: string,
  init: RequestInit,
): AsyncGenerator<SSEEvent> {
  const r = await fetch(url, init);
  if (!r.ok || !r.body) {
    const text = await r.text().catch(() => "");
    throw new Error(`SSE failed ${r.status}: ${text || r.statusText}`);
  }
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    while (true) {
      const idx = buffer.indexOf("\n\n");
      if (idx === -1) break;
      const block = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const ev: SSEEvent = { event: "message", data: null };
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) ev.event = line.slice(7).trim();
        else if (line.startsWith("data: ")) {
          const raw = line.slice(6);
          try {
            ev.data = JSON.parse(raw);
          } catch {
            ev.data = raw;
          }
        }
      }
      yield ev;
    }
  }
}
