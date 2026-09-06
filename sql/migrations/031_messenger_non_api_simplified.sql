-- Simplify Messenger non-API copy: solar-only intake, no KB-2 calendar prompts in active use.
-- Payment/CTA/confirmation keys remain in DB for admin archive but runtime no longer reads them.

UPDATE app_config
SET config_value = jsonb_set(
    config_value,
    '{text}',
    to_jsonb(''::text),
    true
)
WHERE config_key IN (
    'confirmation_calendar_prompt',
    'confirmation_calendar_solar',
    'confirmation_calendar_lunar',
    'confirmation_birthdata_summary',
    'confirmation_birthdata_yes',
    'confirmation_birthdata_edit',
    'confirmation_gender_prompt',
    'confirmation_gender_male',
    'confirmation_gender_female',
    'confirmation_birthhour_prompt',
    'confirmation_missing_field_resume',
    'confirmation_missing_field_retry',
    'confirmation_pre_chart_safe_close',
    'payment_pre_cta_final',
    'payment_link_label',
    'payment_link_label_final',
    'payment_link_placeholder_final',
    'premium_copy_love',
    'premium_copy_career',
    'premium_copy_general',
    'premium_trigger_final',
    'premium_handoff_prompt_final',
    'premium_secondary_cta_final',
    'premium_expectation_setting_final',
    'cta_primary_after_free_love',
    'cta_primary_after_free_career',
    'cta_primary_after_free_general',
    'cta_primary_after_free_returning_unpaid',
    'cta_primary_after_free_paid_repeat',
    'cta_secondary_after_free_love',
    'cta_secondary_after_free_career',
    'cta_secondary_after_free_general',
    'cta_secondary_after_free_returning_unpaid',
    'cta_secondary_after_free_paid_repeat',
    'cta_primary_final_love',
    'cta_primary_final_career',
    'cta_primary_final_general',
    'cta_primary_final_returning_unpaid',
    'cta_primary_final_paid_repeat',
    'cta_primary_final_intake_resume',
    'cta_secondary_final_love',
    'cta_secondary_final_career',
    'cta_secondary_final_general',
    'cta_secondary_final_returning_unpaid',
    'cta_secondary_final_paid_repeat',
    'cta_secondary_final_intake_resume',
    'offer_label_love_deep',
    'offer_label_career_deep',
    'offer_label_general_deep'
);

UPDATE app_config
SET config_value = jsonb_set(
    config_value,
    '{text}',
    to_jsonb('Đã xoá phần đã nhập. Bạn cho mình ngày/tháng/năm sinh (dương lịch), giờ sinh (UTC+7) và giới tính nhé.'::text),
    true
)
WHERE config_key = 'intake_resume_qr_edit';
