# Ops dashboard (SPA)

React + TypeScript + Vite + Tailwind. Display-only client for the daemon
dashboard API. Business logic stays in `src/dashboard/`.

```bash
cd web
npm install
npm run dev      # :5173, proxies /api and /ws → :8080
npm run build    # writes web/dist for FastAPI / offline zip
```

Routes: `/jobs`, `/jobs/:jobId`, `/tasks/:issueKey`, `/poll`, `/scheduled`, `/settings`.
