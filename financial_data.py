import os
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

COMPANY_TICKERS = {
    "nvidia": "NVDA",
    "nvda": "NVDA",
    "apple": "AAPL",
    "aapl": "AAPL",
    "microsoft": "MSFT",
    "msft": "MSFT",
    "amd": "AMD",
    "tesla": "TSLA",
    "tsla": "TSLA",
    "amazon": "AMZN",
    "amzn": "AMZN",
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "meta": "META",
}
NEWS_KEYWORDS = {
    "NVDA": ("nvidia", "nvda"),
    "AMD": ("advanced micro devices", "amd"),
    "AAPL": ("apple", "aapl"),
    "MSFT": ("microsoft", "msft"),
    "TSLA": ("tesla", "tsla"),
    "AMZN": ("amazon", "amzn"),
    "GOOGL": ("google", "alphabet", "googl"),
    "META": ("meta platforms", "meta"),
}


def find_ticker(text: str) -> str | None:
    text_lower = text.lower()

    for company, ticker in COMPANY_TICKERS.items():
        if company in text_lower:
            return ticker

    return None


def is_live_price_request(text: str) -> bool:
    text_lower = text.lower()

    price_terms = (
        "price",
        "stock price",
        "trading at",
        "daily change",
        "today's move",
        "today move",
        "quote",
    )

    return any(term in text_lower for term in price_terms)


def get_stock_quote(symbol: str) -> dict:
    if not FINNHUB_API_KEY:
        raise RuntimeError("FINNHUB_API_KEY is missing from .env")

    response = requests.get(
        "https://finnhub.io/api/v1/quote",
        params={
            "symbol": symbol,
            "token": FINNHUB_API_KEY,
        },
        timeout=15,
    )
    response.raise_for_status()

    data = response.json()

    if not data or data.get("c", 0) == 0:
        raise ValueError(f"No quote was returned for {symbol}")

    timestamp = datetime.fromtimestamp(
        data["t"],
        tz=timezone.utc,
    ).strftime("%Y-%m-%d %H:%M UTC")

    return {
        "symbol": symbol,
        "current_price": data["c"],
        "change": data["d"],
        "percent_change": data["dp"],
        "open": data["o"],
        "high": data["h"],
        "low": data["l"],
        "previous_close": data["pc"],
        "timestamp": timestamp,
    }


def format_quote_context(quote: dict) -> str:
    return (
        "Verified live Finnhub market data:\n"
        f"Symbol: {quote['symbol']}\n"
        f"Current price: ${quote['current_price']}\n"
        f"Change: ${quote['change']} "
        f"({quote['percent_change']}%)\n"
        f"Open: ${quote['open']}\n"
        f"Day high: ${quote['high']}\n"
        f"Day low: ${quote['low']}\n"
        f"Previous close: ${quote['previous_close']}\n"
        f"Provider timestamp: {quote['timestamp']}\n"
        "Source: Finnhub"
    )


def get_company_news(
    symbol: str,
    days: int = 7,
    limit: int = 5,
) -> list[dict]:
    if not FINNHUB_API_KEY:
        raise RuntimeError("FINNHUB_API_KEY is missing from .env")

    today = datetime.now(timezone.utc).date()
    start_date = today - timedelta(days=days)

    response = requests.get(
        "https://finnhub.io/api/v1/company-news",
        params={
            "symbol": symbol,
            "from": start_date.isoformat(),
            "to": today.isoformat(),
            "token": FINNHUB_API_KEY,
        },
        timeout=15,
    )
    response.raise_for_status()

    items = response.json()

    if not isinstance(items, list):
        raise ValueError("Invalid news response from Finnhub")

    keywords = NEWS_KEYWORDS.get(
        symbol.upper(),
        (symbol.lower(),),
    )

    relevant_items = []

    for item in items:
        searchable_text = (
            f"{item.get('headline', '')} "
            f"{item.get('summary', '')}"
        ).lower()

        if any(
            keyword.lower() in searchable_text
            for keyword in keywords
        ):
            relevant_items.append(item)

    items = sorted(
        relevant_items,
        key=lambda item: item.get("datetime", 0),
        reverse=True,
    )

    results = []

    for item in items[:limit]:
        timestamp = item.get("datetime", 0)

        published = datetime.fromtimestamp(
            timestamp,
            tz=timezone.utc,
        ).strftime("%Y-%m-%d %H:%M UTC")

        results.append(
            {
                "headline": item.get("headline", "Untitled"),
                "summary": item.get("summary", ""),
                "source": item.get("source", "Unknown"),
                "url": item.get("url", ""),
                "published": published,
            }
        )

    return results


def format_news_context(
    symbol: str,
    news_items: list[dict],
) -> str:
    if not news_items:
        return f"No verified Finnhub news was found for {symbol}."

    lines = [
        f"Verified Finnhub news for {symbol}:",
    ]

    for number, item in enumerate(news_items, start=1):
        lines.extend(
            [
                f"\nItem {number}",
                f"Headline: {item['headline']}",
                f"Summary: {item['summary']}",
                f"Source: {item['source']}",
                f"Published: {item['published']}",
                f"URL: {item['url']}",
            ]
        )

    return "\n".join(lines)