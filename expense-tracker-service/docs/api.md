# HTTP API
**work in progress**

| Method | Path                                  | Expected body    | Returns                                                                                                                         |
| ------ | ------------------------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| GET    | /health                               | -                | {status: "okay"} *if the service is alive*                                                                                      |
| PUT    | /v1/upload                            | {file: the file} | {message: "CSV / pdf uploaded successfully"} *invalid file format if error*                                                     |
| GET    | /v1/view/batches                      | -                | {body: contains all the batch info (should be paged)}                                                                           |
| GET    | /v1/view/batch/{batch_id}             | -                | {body: the transactions ids uploaded in the batch,<br>date batch was uploaded}                                                  |
| GET    | /v1/view/transaction/{transaction_id} | -                | {body: user_id, import_batch_id, raw_description, merchant_name, amount, card_used, currency, categorisation, transaction_date} |
