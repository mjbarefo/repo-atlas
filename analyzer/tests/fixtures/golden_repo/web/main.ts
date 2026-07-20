import { request } from "@lib/client";

export function loadSession(): Promise<string> {
  return request("/session");
}

