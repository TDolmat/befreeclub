import * as React from 'react';
import { type WsEvent, wsEventSchema } from '@bfc/shared';

type Handler = (event: WsEvent) => void;

class WsClient {
  private ws: WebSocket | null = null;
  private handlers = new Set<Handler>();
  private retryDelay = 1000;
  private closed = false;

  connect() {
    if (this.ws || this.closed) return;
    const url = new URL('/ws', window.location.href);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    this.ws = new WebSocket(url.toString());

    this.ws.addEventListener('open', () => {
      this.retryDelay = 1000;
    });

    this.ws.addEventListener('message', (evt) => {
      try {
        const data = JSON.parse(evt.data);
        if (data?.type === 'hello') return;
        const parsed = wsEventSchema.safeParse(data);
        if (parsed.success) {
          for (const h of this.handlers) h(parsed.data);
        }
      } catch {
        // ignore
      }
    });

    this.ws.addEventListener('close', () => {
      this.ws = null;
      if (this.closed) return;
      setTimeout(() => this.connect(), this.retryDelay);
      this.retryDelay = Math.min(this.retryDelay * 1.5, 15000);
    });

    this.ws.addEventListener('error', () => {
      this.ws?.close();
    });
  }

  subscribe(handler: Handler): () => void {
    this.handlers.add(handler);
    return () => {
      this.handlers.delete(handler);
    };
  }

  dispose() {
    this.closed = true;
    this.ws?.close();
    this.ws = null;
  }
}

const client = new WsClient();
client.connect();

export function useWsEvent<TType extends WsEvent['type']>(
  type: TType,
  handler: (event: Extract<WsEvent, { type: TType }>) => void,
): void {
  const handlerRef = React.useRef(handler);
  handlerRef.current = handler;

  React.useEffect(() => {
    const unsubscribe = client.subscribe((event) => {
      if (event.type === type) {
        handlerRef.current(event as Extract<WsEvent, { type: TType }>);
      }
    });
    return unsubscribe;
  }, [type]);
}

export function useWsEvents(handler: (event: WsEvent) => void): void {
  const handlerRef = React.useRef(handler);
  handlerRef.current = handler;
  React.useEffect(() => client.subscribe((e) => handlerRef.current(e)), []);
}
