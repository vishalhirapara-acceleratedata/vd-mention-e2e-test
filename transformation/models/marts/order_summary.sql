select
    cast(count(*) as bigint) as order_count,
    cast(sum(amount) as bigint) as total_amount
from {{ source('main', 'orders') }}
