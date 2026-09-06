-- Simplify intake copy: solar calendar only; date + hour (UTC+7) + gender.

INSERT INTO app_config (config_key, config_value)
VALUES
('opening_question_love', jsonb_build_object(
    'text',
    'Để lập lá số, bạn cho mình ngày/tháng/năm sinh (dương lịch), giờ sinh (UTC+7) và giới tính nhé.',
    'cp2_pack', 'Nhip1'
)),
('opening_question_career', jsonb_build_object(
    'text',
    'Để xem hướng công việc, mình cần ngày/tháng/năm sinh (dương lịch), giờ sinh (UTC+7) và giới tính.',
    'cp2_pack', 'Nhip1'
))
ON CONFLICT (config_key) DO UPDATE SET
    config_value = EXCLUDED.config_value,
    updated_at = NOW();
