{% macro cents_to_dollars(cents_col) %}
    ({{ cents_col }} / 100.0)
{% endmacro %}

{% macro usd_from_cents(cents_col, currency_col, rate_date_col) %}
    ({{ cents_to_dollars(cents_col) }} * fx.rate_to_usd)
{% endmacro %}

{# Normalize the zoo of legacy status strings to one canonical value. #}
{% macro normalize_order_status(col) %}
    case lower(trim({{ col }}))
        when 'completed' then 'completed'
        when 'complete' then 'completed'
        when 'done' then 'completed'
        when 'canceled' then 'cancelled'
        else lower(trim({{ col }}))
    end
{% endmacro %}

{% macro normalize_payment_status(col) %}
    case lower(trim({{ col }}))
        when 'success' then 'succeeded'
        when 'succeeded' then 'succeeded'
        else lower(trim({{ col }}))
    end
{% endmacro %}

{% macro normalize_subscription_status(col) %}
    case lower(trim({{ col }}))
        when 'active' then 'active'
        when 'canceled' then 'cancelled'
        else lower(trim({{ col }}))
    end
{% endmacro %}
