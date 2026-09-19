import pandas as pd

# Minimum number of transactions needed before detecting transaction-level anomalies within a category.
MIN_CATEGORY_TRANSACTIONS = 5

# Minimum number of months needed before detecting unusually high spending periods.
MIN_MONTHS = 3

# IQR multiplier used for anomaly detection.
IQR_MULTIPLIER = 1.5

def detect_category_anomalies(group):
    if len(group) < MIN_CATEGORY_TRANSACTIONS:
        return pd.DataFrame()

    Q1 = group["spending"].quantile(0.25)
    Q3 = group["spending"].quantile(0.75)

    IQR = Q3 - Q1

    upper_bound = Q3 + IQR_MULTIPLIER * IQR

    anomalies = group[group["spending"] > upper_bound].copy()

    if anomalies.empty:
        return pd.DataFrame()

    anomalies["anomaly_type"] = "high_transaction"
    anomalies["threshold"] = upper_bound

    return anomalies


def detect_monthly_anomalies(monthly_df):
    if len(monthly_df) < MIN_MONTHS:
        return []

    Q1 = monthly_df["total_spending"].quantile(0.25)
    Q3 = monthly_df["total_spending"].quantile(0.75)

    IQR = Q3 - Q1

    upper_bound = Q3 + IQR_MULTIPLIER * IQR

    anomalies = monthly_df[monthly_df["total_spending"] > upper_bound].copy()

    results = []

    for _, row in anomalies.iterrows():
        results.append({
            "period": str(row["month"]),
            "total_spending": row["total_spending"],
            "transaction_count": int(row["transaction_count"]),
            "threshold": upper_bound,
            "anomaly_type": "high_spending_period"
        })

    return results

def generate_spending_statistics(df):

    df["transaction_date"] = pd.to_datetime(df["transaction_date"])

    # Expenses are negative values
    expenses = df[df["amount"] < 0].copy()

    if expenses.empty:
        return

    expenses["spending"] = expenses["amount"].abs()

    # Overall Spending
    total_spending = expenses["spending"].sum()

    overall = {
        "total_spending": total_spending,
        "transaction_count": len(expenses),
        "average_transaction": expenses["spending"].median(),
        "largest_transaction": expenses["spending"].max()
    }

    # Spending By Category
    category_stats = (
        expenses
        .groupby("categorisation")["spending"]
        .agg(
            total_spending="sum",
            transaction_count="count",
            average_transaction="median"
        )
        .sort_values(
            "total_spending",
            ascending=False
        )
    )

    category_results = []

    for category, row in category_stats.iterrows():

        percentage = row["total_spending"] / total_spending * 100

        category_results.append({
            "category": category,
            "total_spending": row["total_spending"],
            "transaction_count": int(row["transaction_count"]),
            "average_transaction": row["average_transaction"],
            "percentage": round(float(percentage), 2)
        })

    # Spending By Merchant
    merchant_stats = (
        expenses
        .groupby("merchant_name")["spending"]
        .agg(
            total_spending="sum",
            transaction_count="count",
            average_transaction="median"
        )
        .sort_values(
            "total_spending",
            ascending=False
        )
    )

    merchant_results = []

    for merchant, row in merchant_stats.iterrows():

        merchant_results.append({
            "merchant": merchant,
            "total_spending": row["total_spending"],
            "transaction_count": int(row["transaction_count"]),
            "average_transaction": row["average_transaction"]
        })

    # Spending By Card
    card_stats = (
        expenses
        .groupby("card")["spending"]
        .agg(
            total_spending="sum",
            transaction_count="count",
            average_transaction="median"
        )
        .sort_values(
            "total_spending",
            ascending=False
        )
    )

    card_results = []

    for card, row in card_stats.iterrows():

        card_results.append({
            "card": card,
            "total_spending": row["total_spending"],
            "transaction_count": int(row["transaction_count"]),
            "average_transaction": row["average_transaction"]
        })

    # Spending By Month
    expenses["month"] = expenses["transaction_date"].dt.to_period("M")

    monthly_stats = (
        expenses
        .groupby("month")["spending"]
        .agg(
            total_spending="sum",
            transaction_count="count",
            average_transaction="median",
        )
        .reset_index()
        .sort_values("month")
    )

    monthly_results = []

    for _, row in monthly_stats.iterrows():

        monthly_results.append({
            "month": str(row["month"]),
            "total_spending": row["total_spending"],
            "transaction_count": int(row["transaction_count"]),
            "average_transaction": row["average_transaction"]
        })

    # Spending Local vs Overseas
    expenses["location_type"] = expenses["currency"].apply(
        lambda x: "local"
        if x == "SGD"
        else "overseas"
    )

    local_spending = expenses.loc[
        expenses["location_type"] == "local",
        "spending"
    ].sum()

    overseas_spending = expenses.loc[
        expenses["location_type"] == "overseas",
        "spending"
    ].sum()

    local_vs_overseas = {
        "local_spending": local_spending,
        "overseas_spending":  overseas_spending,
    }

    # Detect High Spending Amount per Category
    category_anomalies = pd.concat(
        [
            detect_category_anomalies(group)
            for _, group in expenses.groupby("categorisation")
        ],
        ignore_index=True
    )

    high_transaction_anomalies = []

    if not category_anomalies.empty:

        for _, row in category_anomalies.iterrows():

            high_transaction_anomalies.append({
                "transaction_id": str(
                    row["transaction_id"]
                ),
                "merchant": row["merchant_name"],
                "category": row["categorisation"],
                "spending": row["spending"],
                "currency": row["currency"],
                "transaction_date": (
                    row["transaction_date"]
                    .isoformat()
                ),
                "threshold": row["threshold"],
                "anomaly_type": "high_transaction"
            })

    # Detect High Spending Periods
    high_spending_periods = detect_monthly_anomalies(monthly_stats)

    result = {
        "overall": overall,
        "categories": category_results,
        "top_merchants": merchant_results,
        "card_usage": card_results,
        "monthly_spending": monthly_results,
        "local_vs_overseas": local_vs_overseas,
        "anomalies": {
            "high_transactions": high_transaction_anomalies,
            "high_spending_periods": high_spending_periods
        }
    }

    return result