select
    order_id,
    amount
from {{ source('main', 'orders') }}
where amount >= 100
