import { ApiError, apiErrorOf } from "./errors";
import type { CreateUserIn, UserOut } from "../generated/service";
import { USAGE_COPY } from "../wiki/copy";

export interface HttpRequest { method: "GET" | "POST" | "DELETE"; url: string; headers: Record<string, string>; body?: string }
export interface HttpResponse { status: number; text: string }
/** Resolves with any HTTP status; throws OfflineError only when the server is unreachable. */
export interface JsonTransport { request(req: HttpRequest): Promise<HttpResponse> }
/** Calls onChunk with decoded text for 2xx bodies; for other statuses resolves with the body as `text`. */
export interface StreamTransport {
  stream(req: HttpRequest & { signal?: AbortSignal }, onChunk: (text: string) => void): Promise<HttpResponse>;
}
export interface ClientConfig { baseUrl: string; userId: string }
/** Any transport that sends a JSON request with one of the methods `M` (JsonTransport, wiki/api.ts WikiHttp). */
export interface JsonRequester<M extends string> {
  request(req: { method: M; url: string; headers: Record<string, string>; body?: string }): Promise<HttpResponse>;
}

/**
 * One JSON request, the one every client makes: X-EvoWiki-User, a JSON body when given; non-2xx → ApiError (apiErrorOf),
 * 204 → undefined, a body that is not JSON → `badJson` (default ApiError). Unreachable → the transport's OfflineError.
 */
export async function jsonCall<T, M extends string>(http: JsonRequester<M>, config: ClientConfig, method: M, path: string,
  body?: unknown, badJson: (status: number, text: string) => Error = (s, t) => new ApiError(s, `invalid JSON response: ${t.slice(0, 200)}`)): Promise<T> {
  const headers: Record<string, string> = { "X-EvoWiki-User": config.userId };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await http.request({ method, url: config.baseUrl + path, headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  if (res.status < 200 || res.status >= 300) throw apiErrorOf(res.status, res.text);
  if (res.status === 204) return undefined as T;
  try { return JSON.parse(res.text) as T; } catch { throw badJson(res.status, res.text); }
}

/**
 * The account calls: GET /me with the configured user (X-EvoWiki-User) and POST /users (the account check registers an
 * unknown user id). The wiki, the assistant and 用量 have their own clients (wiki/api.ts, assistant/client.ts,
 * wiki/usage.ts) on the transports above.
 */
export class EvoWikiClient {
  constructor(private readonly config: () => ClientConfig, private readonly http: JsonTransport) {}

  me(): Promise<UserOut> { return this.call("GET", "/me"); }
  async checkServer(): Promise<void> {
    const health = await this.call<{ protocol_version: number }>("GET", "/health");
    if (health.protocol_version !== 3) throw new ApiError(409, USAGE_COPY.versionMismatch, "protocol_mismatch");
    await this.call("GET", "/health/ready");
  }
  createUser(body: CreateUserIn): Promise<UserOut> { return this.call("POST", "/users", body); }
  /** His window is in front of him: the Bot works only for someone who is there (bot.settings.bot_serves). */
  seen(): Promise<void> { return this.call("POST", "/me/seen"); }

  private call<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
    return jsonCall<T, "GET" | "POST">(this.http, this.config(), method, path, body);
  }
}
