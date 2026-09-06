# Runbook — deploy / vận hành (Nhịp 3)

**Ngữ cảnh:** backend `QUAN LY TU VI/TAI_LIEU_DU_AN/backend/`, Render primary, `render.yaml`.

## Biến môi trường (tên — không ghi secret trong runbook)

- `DATABASE_URL` — PostgreSQL (Render managed hoặc external).
- `OPENAI_API_KEY`, `OPENAI_MODEL` (tuỳ chọn).
- Facebook: `FB_VERIFY_TOKEN`, `FB_PAGE_ACCESS_TOKEN`, `FB_APP_SECRET` (bắt buộc nếu bật verify chữ ký webhook).
- `WEBHOOK_SKIP_SIGNATURE_VERIFY` — chỉ staging có kiểm soát; production nên tắt.
- Admin: `ADMIN_BOOTSTRAP_EMAIL`, `ADMIN_BOOTSTRAP_PASSWORD`, `ADMIN_COOKIE_SECURE` (HTTPS → `1`).
- Build metadata (tuỳ chọn): `GIT_SHA`, `BUILD_TIME` — hiển thị trên `/health`.
- Guardrail: `MAX_WEBHOOK_BODY_BYTES` (mặc định 262144), `MAX_INBOUND_TEXT_CHARS` (mặc định 8000).
- `LOG_LEVEL`, `SKIP_DB_BOOTSTRAP` (chỉ khi cần tách job không chạy migration).

## Health checks (khóa Engineering)

| Mục đích | Path | Kỳ vọng |
|-----------|------|---------|
| **Platform liveness (Render)** | `GET /health` | 200, JSON `status=ok` — **không** kiểm tra DB |
| **Readiness / DB** | `GET /readiness` | 200 `ready` hoặc 503 `not_ready` |

## Deploy lần đầu (Render)

- Render Dashboard → **Manual Deploy** hoặc push branch đã liên kết.
- Restart service nếu chỉ cần recycle process (env không đổi).

## Rollback

- Render: chọn **deploy** trước đó (Previous deploy) hoặc revert commit rồi redeploy.
- Ghi lại `GIT_SHA` trước/sau trong báo cáo.

## Verify webhook

1. Facebook Developer → Webhook → **Verify** (GET challenge) với `FB_VERIFY_TOKEN` đúng.
2. Gửi tin test từ Page → xem log `webhook_event_scheduled` / phản hồi bot.
3. Nếu bật chữ ký: không set `WEBHOOK_SKIP_SIGNATURE_VERIFY` trên production.

## Cleanup `webhook_dedupe` (24h)

- **Retention cố định 24 giờ** (semantics lane).
- CLI: `python scripts/cleanup_webhook_dedupe.py` (từ thư mục `backend/`, đã có `DATABASE_URL`).
- Hoặc Admin → **Privacy** → nút cleanup (POST, role writer + audit).
- Cron (Render **Schedule** hoặc tương đương): gọi cùng lệnh CLI mỗi 1h hoặc 6h.

## Admin

- `/admin/login` — sau deploy nhớ bootstrap user qua `ADMIN_BOOTSTRAP_*` lần đầu.
- Ẩn danh baseline: `/admin/privacy` — chỉ writer; mọi thao tác ghi audit.

## GitHub repo private + Render

Nếu repo GitHub chuyển **private**, service Render vẫn build được khi đã kết nối qua **GitHub App** (Dashboard Render → Account / service → kết nối lại nếu deploy báo không pull được). Không lưu PAT trong repo; dùng OAuth/GitHub App của Render.

## Ghi chú staging vs production

Các bước smoke trong `docs/smoke_checklist_nhip3.md` nên chạy trên **staging** trước; đánh dấu mục nào chưa xác minh trên production thật.

## Evidence cho lane completion (team)

Chi tiết từng bước (pytest local, smoke tay, không bắt buộc Git): **`docs/TEAM_EVIDENCE_STEPS.md`**.  
File mẫu: `docs/pytest_last_run.txt`, `docs/git_sha_for_evidence.txt`.
