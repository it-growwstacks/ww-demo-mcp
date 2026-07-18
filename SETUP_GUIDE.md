# MCP Server Template — Setup Guide
## GrowwStacks · Build your MCP server in under a day

This is the master template for building MCP (Model Context Protocol) servers.
Clone this template, follow these steps, and you will have a production-grade
MCP server deployed on Cloudflare with auth, permissions, and CI/CD.

---

## What you get from this template

- Cloudflare Worker front door with OAuth 2.1 flow (src/index.ts)
- Python MCP server with 8-layer production architecture
- Clerk authentication (JWT verification)
- Supabase PostgreSQL (data + permissions)
- Rate limiting (60 req/min per user)
- Audit logging (every call logged, PII-safe)
- Input validation (Pydantic)
- Error handling (structured error codes)
- Docker containerization
- GitHub Actions CI/CD (auto-deploy on push)

---

## Step 1 — Create your repo (2 minutes)

1. Go to https://github.com/it-growwstacks/ww-demo-mcp
2. Click "Use this template" → "Create a new repository"
3. Name it: your-project-mcp (e.g. client-xyz-mcp)
4. Make it Private
5. Clone to your machine:

git clone https://github.com/it-growwstacks/your-project-mcp.git
cd your-project-mcp
npm install

---

## Step 2 — Set up Clerk (15 minutes)

1. Go to https://dashboard.clerk.com
2. Create a new application (name: your-project-name)
3. Enable Email as sign-in method
4. Go to Developers → OAuth applications → Create:
   - Name: claude-ai
   - Scopes: email, profile, offline_access
   - Enable Consent screen
   - Redirect URI: https://claude.ai/api/mcp/auth_callback
5. Copy the Client ID and Client Secret
6. Note your instance domain (xxx.clerk.accounts.dev)
7. Create your users (Users → Create User)

You need these values later:
- CLERK_JWKS_URL: https://xxx.clerk.accounts.dev/.well-known/jwks.json
- CLERK_ISSUER: https://xxx.clerk.accounts.dev
- CLERK_OAUTH_CLIENT_ID: (from OAuth app)
- CLERK_OAUTH_CLIENT_SECRET: (from OAuth app)

---

## Step 3 — Set up Supabase (20 minutes)

1. Go to https://supabase.com and create a new project
2. Go to SQL Editor and create your tables:
   - Your data tables (employees, orders, etc.)
   - user_permissions table (required):
```sql
     CREATE TABLE user_permissions (
         clerk_user_id TEXT PRIMARY KEY,
         email TEXT NOT NULL,
         role TEXT NOT NULL DEFAULT 'viewer',
         allowed_tools TEXT[] NOT NULL DEFAULT ARRAY['your_tool_name'],
         allowed_employees TEXT[] DEFAULT NULL
     );
```
3. Insert your users with their Clerk User IDs
4. Go to Project Settings → API Keys (Legacy tab)
5. Copy Project URL and service_role key

You need these values later:
- SUPABASE_URL: https://xxx.supabase.co
- SUPABASE_SERVICE_KEY: (service_role key)

---

## Step 4 — Change the code (1-2 hours)

### 4a. wrangler.jsonc
Change the name:
```json
"name": "your-project-mcp"
```

### 4b. server.py
- Change the FastMCP name: `FastMCP("your-project-name", host="0.0.0.0", port=8000)`
- Delete the example tools
- Write your own tools with @mcp.tool() decorator
- Each tool must include all 8 layers (auth, identity, permissions, rate limit, validation, data fetch, audit, response)
- Copy an existing tool as a starting point

### 4c. supabase_client.py
- Delete the example data functions
- Write your own data functions that query your Supabase tables
- Keep the SheetsError class (it is used by server.py for error handling)
- Keep the get_user_permissions function (it is used for permission checks)

### 4d. validators.py
- Delete the example input models
- Write your own Pydantic models for your tool inputs

### 4e. src/index.ts
- Change the container name: `env.MCP_CONTAINER.idFromName("your-project-mcp")`
- Everything else stays the same

### What NOT to change
- auth.py — works with any Clerk instance
- rate_limiter.py — universal
- audit_logger.py — universal
- error_codes.py — universal
- Dockerfile — works as-is unless you add new Python packages
- .github/workflows/deploy.yml — works as-is
- package.json — works as-is

---

## Step 5 — Set Cloudflare secrets (10 minutes)

First deploy requires manual secret setting. Run each:

npx wrangler secret put SUPABASE_URL
npx wrangler secret put SUPABASE_SERVICE_KEY
npx wrangler secret put CLERK_JWKS_URL
npx wrangler secret put CLERK_ISSUER
npx wrangler secret put CLERK_OAUTH_CLIENT_ID
npx wrangler secret put CLERK_OAUTH_CLIENT_SECRET
npx wrangler secret put RATE_LIMIT_PER_MINUTE      (value: 60)
npx wrangler secret put LOG_LEVEL                   (value: INFO)
npx wrangler secret put MCP_BASE_URL                (value: https://your-project-mcp.manish-98d.workers.dev)


---

## Step 6 — Set up CI/CD (2 minutes)

1. Go to your repo → Settings → Secrets and variables → Actions
2. Add these two repository secrets:
   - CLOUDFLARE_API_TOKEN: (get from team lead)
   - CLOUDFLARE_ACCOUNT_ID: 98d83a06d36ef8227913baaef4bda668

---

## Step 7 — Deploy (automatic)

git add .
git commit -m "Initial deployment"
git push origin main

GitHub Actions automatically builds and deploys to Cloudflare.
Your MCP server will be live at: https://your-project-mcp.manish-98d.workers.dev/mcp

---

## Step 8 — Connect from claude.ai

1. Go to claude.ai → Settings → Connectors
2. Add connector URL: https://your-project-mcp.manish-98d.workers.dev/mcp
3. Click Connect → Log in through Clerk → Allow
4. Your tools are now available in Claude

---

## Managing permissions (no code changes needed)

Add a user:
```sql
INSERT INTO user_permissions (clerk_user_id, email, role, allowed_tools, allowed_employees) VALUES
('user_xxx', 'email@example.com', 'viewer', ARRAY['tool_name'], ARRAY['E001']);
```

Change permissions:
```sql
UPDATE user_permissions SET allowed_tools = ARRAY['tool1', 'tool2'] WHERE email = 'user@example.com';
```

Give admin access (all tools, all data):
```sql
UPDATE user_permissions SET role = 'admin', allowed_tools = ARRAY['all_tools'], allowed_employees = NULL WHERE email = 'user@example.com';
```

---

## Architecture (8-layer production pattern)

Request → Worker (OAuth + auth gate)
→ Layer 2: JWT verification (Clerk)
→ Layer 3: Identity extraction (sub from token)
→ Layer 3.5: Tool permission check (Supabase)
→ Layer 3.6: Data scoping check (Supabase)
→ Layer 4: Rate limiting (60/min)
→ Layer 5: Input validation (Pydantic)
→ Layer 6: Data fetch (Supabase)
→ Layer 7: Audit logging (structlog)
→ Layer 8: Response shaping
→ Clean JSON to Claude

---