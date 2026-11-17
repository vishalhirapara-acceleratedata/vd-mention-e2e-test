select
    count(*) as order_count,
    sum(amount) as total_amount,
    sum(amount) / nullif(count(*), 0) as average_amount
from {{ source('main', 'orders') }}
