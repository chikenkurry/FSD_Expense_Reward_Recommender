# Auto-Categorization Service API Documentation

This document outlines the API endpoints exposed by the Auto-Categorization Engine. This service is designed to be consumed by the **Expense Tracking Service** to automatically predict transaction categories using a two-tier approach (Layer 1: Deterministic Cache, Layer 2: LLM API Fallback).

## Base URL
`http://<categorization-service-host>/api/categorization`

---

## 1. Batch Categorization
**Endpoint:** `POST /batch`

Processes a batch of raw transactions. The engine first checks the internal deterministic cache for known merchants. Any unknown merchants are batched and sent to the LLM for prediction using Structured Outputs.

### Request Payload
**Content-Type:** `application/json`

| Field | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `transactions` | `Array` | Yes | Array of transaction objects to categorize (Max 50 per request). |
| `transactions[].transaction_id` | `UUID` | Yes | The ID of the transaction from the Tracking service. |
| `transactions[].merchant_name` | `String` | Yes | Cleaned merchant name to be evaluated. |
| `transactions[].amount` | `Decimal` | Yes | Transaction amount (used by LLM for disambiguation). |
| `transactions[].currency` | `String` | Yes | ISO 4217 currency code for the transaction. For the current project scope this will usually be `"SGD"`. |
| `transactions[].raw_description` | `String` | No | Original raw string, used as fallback context if needed. |

**Example Request:**
```json
{
  "transactions": [
    {
      "transaction_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
      "merchant_name": "NETFLIX PTE LTD",
      "amount": -15.99,
      "currency": "SGD",
      "raw_description": "VISA POS DEBIT NETFLIX PTE LTD SG"
    },
    {
      "transaction_id": "550e8400-e29b-41d4-a716-446655440000",
      "merchant_name": "NTUC FAIRPRICE",
      "amount": -45.50,
      "currency": "SGD",
      "raw_description": "NTUC FAIRPRICE JURONG EAST"
    }
  ]
}
```

### Response Payload
**Content-Type:** `application/json`

| Field | Type | Description |
| :--- | :--- | :--- |
| `results` | `Array` | Array of categorized transaction results. |
| `results[].transaction_id` | `UUID` | Mapped directly back to the requested transaction ID. |
| `results[].predicted_category` | `String` | The assigned category (or `"Uncategorized"` if unknown). |
| `results[].confidence_score` | `Number` | `1.0` if resolved by Cache, `0.1-0.99` if resolved by LLM. |
| `results[].source` | `String` | `"cache"`, `"llm"`, or `"fallback"`. Useful for monitoring metrics. |
| `results[].reasoning` | `String` | Explanation provided by the LLM (null if resolved by cache). |

**Example Response:**
```json
{
  "results": [
    {
      "transaction_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
      "predicted_category": "Subscriptions",
      "confidence_score": 1.0,
      "source": "cache",
      "reasoning": null
    },
    {
      "transaction_id": "550e8400-e29b-41d4-a716-446655440000",
      "predicted_category": "Groceries",
      "confidence_score": 0.95,
      "source": "llm",
      "reasoning": "Standard grocery store chain based on merchant name."
    }
  ]
}
```

---

## 2. User Feedback / Cache Update
**Endpoint:** `POST /feedback`

Called asynchronously by the Expense Tracking Service whenever a user manually corrects a categorized expense. This updates the Layer 1 Deterministic Cache, bypassing the LLM for future instances of this merchant.

### Request Payload
**Content-Type:** `application/json`

| Field | Type | Required | Description |
| :--- | :--- | :--- | :--- |
| `merchant_name` | `String` | Yes | The exact cleaned merchant string that was corrected. |
| `category_final` | `String` | Yes | The verified category assigned by the user. |
| `override_previous` | `Boolean`| No | If true, forces an update to an existing cache rule (default: false). |

**Example Request:**
```json
{
  "merchant_name": "7-ELEVEN SG",
  "category_final": "Dining",
  "override_previous": true
}
```

### Response Payload
**Content-Type:** `application/json`

| Field | Type | Description |
| :--- | :--- | :--- |
| `success` | `Boolean` | Confirmation that the rule was processed. |
| `cache_updated` | `Boolean` | `true` if a new rule was created or an existing rule modified. |
| `message` | `String` | Server status message. |

**Example Response:**
```json
{
  "success": true,
  "cache_updated": true,
  "message": "Cache rule successfully updated for 7-ELEVEN SG"
}
```
