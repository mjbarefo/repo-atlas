import { endpoint } from "./helper.js";

export async function request(path: string): Promise<string> {
  return endpoint(path);
}

