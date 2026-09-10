Import batch table

| Name          | Data type            | Description                                |
| ------------- | -------------------- | ------------------------------------------ |
| import_id     | UUID                 | identifier for import                      |
| file_name     | String / VARCHAR     | name of file uploaded                      |
| rows_imported | int                  | number of transaction rows imported        |
| import_ref    | UUID optional        | link to actual import document if possible |
| created_at    | Timestamp / Datetime | the date and time csv/pdf was uploaded     |

Transaction table

| Name             | Data Type            | Description                                          |
| ---------------- | -------------------- | ---------------------------------------------------- |
| transaction_id   | UUID                 | id of transaction                                    |
| user_id          | UUID                 | id of user                                           |
| import_batch_id  | UUID                 | id of import batch                                   |
| raw_description  | String / VARCHAR     | raw description of the transaction                   |
| merchant_name    | String / VARCHAR     | cleaned/parsed from raw description                  |
| categorisation   | String / VARCHAR     | get from auto categorisation                         |
| amount           | decimal              | negative for expenses, positive for income           |
| card             | String / VARCHAR     | card used for the transaction                        |
| currency         | String / VARCHAR     | which legal tender                                   |
| transaction_date | Timestamp / Datetime | transaction date                                     |
| dedupe_hash      | String / VARCHAR     | dedupe hash generated on import. prevents same entry |
| created_at       | Timestamp / Datetime | the date and time transaction was added              |
