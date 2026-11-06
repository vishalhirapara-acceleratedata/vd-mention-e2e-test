-- Fails if any row in large_orders has amount below 100.
select *
from {{ ref('large_orders') }}
where amount < 100
