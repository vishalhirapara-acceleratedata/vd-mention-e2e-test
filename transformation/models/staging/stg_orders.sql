with source as (
    select * from {{ source('domain_db', 'orders') }}
),

renamed as (
    select
        order_id,
        amount as order_amount
    from source
)

select * from renamed
