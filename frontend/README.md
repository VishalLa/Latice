# FinSync — Bank Reconciliation Frontend

Vue 3 (Composition API, `<script setup>`) + Vite + Tailwind CSS frontend for the FinSync Bank
Reconciliation system, wired to the Flask backend (`auth.py`, `bank_rec_api.py`).

## Setup

```bash
npm install
cp .env.example .env   # set VITE_API_BASE_URL to your Flask server, e.g. http://localhost:8000
npm run dev
```

## Folder structure

```
finsync-frontend/
├── index.html
├── package.json
├── vite.config.js
├── tailwind.config.js        # extracted brand palette + tokens
├── postcss.config.js
├── .env.example
└── src/
    ├── main.js
    ├── App.vue
    ├── assets/main.css       # CSS variables for light/dark theming
    ├── router/index.js       # routes + auth guard
    ├── api/
    │   └── axios.js          # JWT bearer interceptor + global error toasts
    ├── stores/                # Pinia
    │   ├── auth.js            # login/register/me, token persistence
    │   ├── runs.js            # upload, polling, run history, downloads
    │   ├── theme.js            # light/dark mode toggle
    │   └── toast.js
    ├── components/
    │   ├── FileUploadDropzone.vue
    │   ├── StatusBadge.vue
    │   ├── ActionModal.vue
    │   ├── ToastContainer.vue
    │   ├── NavBar.vue
    │   ├── ThemeToggle.vue
    │   └── BrandMark.vue      # recreated interlocking-squares mark
    └── views/
        ├── Login.vue
        ├── Register.vue
        └── Dashboard.vue      # upload flow, live status, results, history
```

## Backend endpoints used

| Endpoint | Method | Notes |
|---|---|---|
| `/auth/register` | POST | `{ username, email, password }` |
| `/auth/login` | POST | Returns `{ access_token, user_id }`, saved to `localStorage` + Pinia |
| `/auth/me` | GET | Bearer token required |
| `/api/run_reconciliation` | POST | `multipart/form-data`: `ledger_file`, `bank_file` |
| `/api/run_status/<run_id>` | GET | Polled every 2s until `SUCCESS`/`FAILURE` |
| `/api/download_report/run/<run_id>` | GET | Blob download via `window.URL.createObjectURL` |
| `/api/generate_report/run/<run_id>` | POST | Fallback when the report file is gone from disk (404) |

## Notes

- Run history is kept in `localStorage` (last 25 runs) since the backend has no "list my runs"
  endpoint yet — swap `stores/runs.js`'s `loadHistory`/`addToHistory` for a real API call if one
  is added later.
- The 401 interceptor logs the user out and redirects to `/login` automatically, so components
  never need to handle expired tokens themselves.
- Colors, gradients, and radii in `tailwind.config.js` are extracted directly from the annotated
  screenshots (dark navy `#0F172A → #1C2E53` hero, `#444CE7/#8098F9/#B07BF0` brand marks,
  `#82F9F9` cyan glow, `#F8F8F8/#E8F8F8/#D8E8F8` light-mode surfaces).
