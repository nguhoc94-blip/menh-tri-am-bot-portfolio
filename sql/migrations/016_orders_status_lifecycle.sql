-- Nhịp 1: khóa lifecycle orders.status (manual verify baseline). Additive only.
ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_status_check;
ALTER TABLE orders
  ADD CONSTRAINT orders_status_check
  CHECK (
    status IN (
      'draft',
      'verification_pending',
      'paid_verified',
      'verify_failed',
      'manual_review'
    )
  );
