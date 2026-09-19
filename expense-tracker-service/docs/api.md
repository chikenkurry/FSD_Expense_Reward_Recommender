# HTTP API
**work in progress**

| Method | Path                                  | Expected body    | Returns                                                                                                                         |
| ------ | ------------------------------------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| GET    | /health                               | -                | {status: "okay"} *if the service is alive*                                                                                      |
| PUT    | /v1/upload                            | {file: the file} | {message: "CSV / pdf uploaded successfully"} *invalid file format if error*                                                     |
| GET    | /v1/view/batches                      | -                | {body: contains all the batch info (should be paged)}                                                                           |
| GET    | /v1/view/batch/{batch_id}             | -                | {body: the transactions ids uploaded in the batch,<br>date batch was uploaded}                                                  |
| GET    | /v1/view/transaction/{transaction_id} | -                | {body: user_id, import_batch_id, raw_description, merchant_name, amount, card, currency, categorisation, transaction_date} |

## Auto-categorisation workflow

After a CSV/PDF upload is parsed into transaction rows, the Expense Tracker Service should initiate categorisation for transactions where `categorisation` is null.

1. The tracker stores each uploaded transaction with `raw_description`, cleaned `merchant_name`, `amount`, `currency`, `card`, and `categorisation = null`.
2. The tracker sends uncategorised transactions to the Auto-Categorization Service in batches of up to 50 using `POST /api/categorization/batch`.
3. Each batch item must include `transaction_id`, `merchant_name`, `amount`, `currency`, and optionally `raw_description`.
4. The categorisation service returns `predicted_category` for each `transaction_id`.
5. The tracker updates `Transaction.categorisation = predicted_category`.
6. If a user manually corrects a transaction category later, the tracker calls `POST /api/categorization/feedback` with the corrected `merchant_name` and final category so future matching improves.

The `categorisation` value must use the shared category enum: `Dining`, `Groceries`, `Transport`, `Shopping`, `Utilities`, `Subscriptions`, `Entertainment`, `Income`, or `Uncategorized`.
