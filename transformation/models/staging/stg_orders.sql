with source as (
    select * from {{ source('domain_db', 'orders') }}
),

renamed as (
    select
        order_id,
        amount
    from source
)

select * from renamed
