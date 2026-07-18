import { Container } from "@cloudflare/containers";
import { env } from "cloudflare:workers";

interface Env {
  MCP_CONTAINER: DurableObjectNamespace;
}

export class MCPContainer extends Container {
  defaultPort = 8000;
  sleepAfter = "5m";
  envVars = {
    SUPABASE_URL: env.SUPABASE_URL,
    SUPABASE_SERVICE_KEY: env.SUPABASE_SERVICE_KEY,
    CLERK_JWKS_URL: env.CLERK_JWKS_URL,
    CLERK_ISSUER: env.CLERK_ISSUER,
    CLERK_OAUTH_CLIENT_ID: env.CLERK_OAUTH_CLIENT_ID,
    CLERK_OAUTH_CLIENT_SECRET: env.CLERK_OAUTH_CLIENT_SECRET,
    RATE_LIMIT_PER_MINUTE: env.RATE_LIMIT_PER_MINUTE,
    LOG_LEVEL: env.LOG_LEVEL,
    MCP_BASE_URL: env.MCP_BASE_URL,
  };
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (
      url.pathname === "/.well-known/oauth-protected-resource" ||
      url.pathname === "/.well-known/oauth-protected-resource/mcp"
    ) {
      return new Response(
        JSON.stringify({
          resource: `${url.origin}/mcp`,
          authorization_servers: [url.origin],
        }),
        { headers: { "Content-Type": "application/json" } }
      );
    }

    if (url.pathname === "/.well-known/oauth-authorization-server") {
      const clerkIssuer = env.CLERK_ISSUER || "";
      return new Response(
        JSON.stringify({
          issuer: clerkIssuer,
          authorization_endpoint: `${clerkIssuer}/oauth/authorize`,
          token_endpoint: `${clerkIssuer}/oauth/token`,
          registration_endpoint: `${url.origin}/oauth/register`,
          response_types_supported: ["code"],
          grant_types_supported: ["authorization_code", "refresh_token"],
          code_challenge_methods_supported: ["S256"],
          token_endpoint_auth_methods_supported: ["client_secret_basic", "none"],
        }),
        { headers: { "Content-Type": "application/json" } }
      );
    }

    if (url.pathname === "/oauth/register" && request.method === "POST") {
      const body = await request.json() as Record<string, unknown>;
      const redirectUris = (body.redirect_uris as string[]) || [];
      return new Response(
        JSON.stringify({
          client_id: env.CLERK_OAUTH_CLIENT_ID || "",
          client_secret: env.CLERK_OAUTH_CLIENT_SECRET || "",
          client_id_issued_at: 0,
          client_secret_expires_at: 0,
          redirect_uris: redirectUris,
          token_endpoint_auth_method: "client_secret_basic",
          grant_types: ["authorization_code", "refresh_token"],
          response_types: ["code"],
        }),
        { headers: { "Content-Type": "application/json" } }
      );
    }

    if (url.pathname === "/mcp" && request.method === "POST") {
      const authHeader = request.headers.get("Authorization");
      if (!authHeader || !authHeader.startsWith("Bearer ")) {
        return new Response("Unauthorized", {
          status: 401,
          headers: {
            "WWW-Authenticate": `Bearer resource_metadata="${url.origin}/.well-known/oauth-protected-resource"`,
          },
        });
      }
    }

    const id = env.MCP_CONTAINER.idFromName("ww-demo");
    const container = env.MCP_CONTAINER.get(id);
    return container.fetch(request);
  },
};