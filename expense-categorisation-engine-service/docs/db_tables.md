# Auto-Categorization Database Schema

This document outlines the database schema required for the **Auto-Categorization Service** to maintain its Layer 1 Deterministic Cache. 

*Note: While the Expense Tracking service maintains the main `Transaction` table, this isolated cache table allows the categorization engine to learn and process rules efficiently without locking the main transaction ledger.*

---

## Categorization Cache Table

**Table Name:** `merchant_category_cache`

**Purpose:** Acts as the Tier 1 deterministic lookup layer. Before sending any transaction to the costly LLM API, the engine checks this table. If a `merchant_name` exists here, the system immediately returns the `assigned_category` with a confidence score of 1.0.

| Name | Data Type | Constraints | Description |
| :--- | :--- | :--- | :--- |
| `rule_id` | `UUID` | Primary Key | Unique identifier for the cache rule. |
| `merchant_name` | `String / VARCHAR` | Unique, Indexed | The cleaned merchant string. Exact match is required here. **Must be indexed** for O(1) lookup speed. |
| `assigned_category`| `String / VARCHAR` | Not Null | The confirmed category for this merchant (e.g., "Dining", "Subscriptions"). |
| `confidence_score` | `Decimal` | Default `1.0` | Confidence level. Usually `1.0` since these rules are either user-verified or high-confidence AI predictions promoted to cache. |
| `source` | `String / VARCHAR` | | Origin of the rule (e.g., `"user_feedback"`, `"llm_high_confidence"`). |
| `override_count` | `Integer` | Default `0` | Tracks how many times users have corrected this specific merchant. Useful for identifying controversial/ambiguous merchants. |
| `created_at` | `Timestamp` | Auto-generated | When this cache rule was first created. |
| `updated_at` | `Timestamp` | Auto-updated | When this cache rule was last modified. |

### System Interaction Context

For architectural clarity, here is how the `merchant_category_cache` interacts with the provided `Transaction` table residing in the Tracking Service:

1. **Ingestion:** A transaction is inserted into the `Transaction` table with `categorisation = null`.
2. **Batch Request:** The Tracking Service sends a subset of fields (`transaction_id`, `merchant_name`, `amount`) to the Categorization API.
3. **Cache Hit:** The Categorization Service queries `merchant_category_cache` where `merchant_name` matches. 
4. **Cache Miss (LLM API):** If no match is found, the engine queries the LLM. 
5. **Update Transaction:** The Categorization Service returns the results, and the Tracking Service executes an `UPDATE` on the `Transaction` table to fill the `categorisation` column.
6. **Self-Healing (Feedback):** If the user changes `categorisation` on the frontend, the Tracking Service triggers the `/feedback` endpoint, which `UPSERT`s the correction into the `merchant_category_cache` table.

---

## Allowed Categories (Enum Reference)

To ensure strict data integrity across both the Tracking Service and the Categorization Service, the `categorisation` column in the `Transaction` table and the `assigned_category` column in the `merchant_category_cache` table should ideally be constrained by a shared standard enumeration:

*   `Dining`
*   `Groceries`
*   `Transport`
*   `Shopping`
*   `Utilities`
*   `Subscriptions`
*   `Entertainment`
*   `Income`
*   `Uncategorized` *(Used strictly when the LLM cannot confidently determine the category)*
