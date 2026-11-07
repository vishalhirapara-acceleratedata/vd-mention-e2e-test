select
    count(*) as order_count,
    sum(amount) as total_amount
from {{ source('main', 'orders') }}
