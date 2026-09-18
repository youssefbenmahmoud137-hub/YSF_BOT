# YSF Bot

YSF Bot is a simple Python Telegram bot that welcomes users in Tunisian Arabic and supports persistent points, referrals, and daily rewards.

## Run & Operate

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `python main.py` — run YSF Bot using the `TELEGRAM_BOT_TOKEN` secret
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string for the API server
- Required secret: `TELEGRAM_BOT_TOKEN` — token for YSF Bot
- Required secret: `ADMIN_ID` — Telegram ID that receives private pending-order notifications

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `main.py` — YSF Bot entrypoint and `/start` handler
- `ysf_bot.db` — local SQLite database created at runtime for users, checkout sessions, and orders
- `README.md` — bot setup and run instructions

## Architecture decisions

_Populate as you build — non-obvious choices a reader couldn't infer from the code (3-5 bullets)._

## Product

- Telegram bot named YSF Bot
- `/start` sends the welcome message in Tunisian Arabic
- `/points`, `/invite`, `/daily`, and `/help` provide the points and referral system
- `🛒 متجر YSF` opens the product shop; purchases are completed manually by the administrator
- Orders collect a Game ID and payment reference in private chat, then notify the configured admin with `pending` status

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

_Populate as you build — sharp edges, "always run X before Y" rules._

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
