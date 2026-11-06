SELECT SUM(amount) AS total_amount
FROM {{ source('main', 'orders') }}
