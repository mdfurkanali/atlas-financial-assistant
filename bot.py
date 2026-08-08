import re
from datetime import datetime, time as clock_time, timezone
from zoneinfo import ZoneInfo
import tempfile
from pathlib import Path
import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import LinkPreviewOptions , Message
from aiogram.utils.chat_action import ChatActionSender
from dotenv import load_dotenv
from google import genai
from google.genai import types

import database
import financial_data
import document_data


load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.5-flash-lite",
)

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is missing from .env")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is missing from .env")


gemini_client = genai.Client(api_key=GEMINI_API_KEY)
dispatcher = Dispatcher()
active_documents: dict[int, dict] = {}


SYSTEM_PROMPT = """
You are Atlas, a concise financial assistant inside Telegram.

Communicate naturally, like an experienced financial analyst.
Keep normal answers below 150 words.

During onboarding:
- Use two to four short sentences.
- Avoid unexplained jargon.
- Ask exactly one focused question.
- Acknowledge preferences without making current-market claims.

Explain why information matters instead of merely summarizing it.
Ask one focused clarification when a request is ambiguous.
Use previous conversation context when relevant.

Never invent prices, filings, news, dates, figures, or sources.
Use neutral and evidence-based language.

Live information is only available when verified data is explicitly
included in the prompt. If current information is requested without
verified data, explain that it must be verified.

When asked what you remember, answer only with the remembered facts
in one or two sentences. Do not add unrelated analysis or questions.

Do not use words such as current, currently, recent, or latest unless
verified live evidence is included in the prompt.

Use plain text only. Do not use Markdown, asterisks, headings, or tables.

Do not mention prompts, models, databases, tools, or implementation
details.
"""


def format_history(
    messages: list[tuple[str, str]],
) -> str:
    if not messages:
        return "No previous conversation is available."

    lines = []

    for role, content in messages:
        label = "User" if role == "user" else "Atlas"
        lines.append(f"{label}: {content}")

    return "\n".join(lines)


def clean_for_telegram(text: str) -> str:
    return (
        text.replace("**", "")
        .replace("__", "")
        .replace("###", "")
        .replace("##", "")
        .strip()
    )

def find_all_tickers(text: str) -> list[str]:
    text_lower = text.lower()
    symbols = []

    for company, symbol in financial_data.COMPANY_TICKERS.items():
        pattern = rf"\b{re.escape(company.lower())}\b"

        if re.search(pattern, text_lower):
            if symbol not in symbols:
                symbols.append(symbol)

    return symbols


def is_watchlist_add_request(text: str) -> bool:
    text_lower = text.lower()

    phrases = (
        "track ",
        "monitor ",
        "follow ",
        "add to my watchlist",
        "add to watchlist",
    )

    return any(
        phrase in text_lower
        for phrase in phrases
    )


def is_watchlist_remove_request(text: str) -> bool:
    text_lower = text.lower()

    phrases = (
        "stop tracking",
        "stop monitoring",
        "remove from my watchlist",
        "remove from watchlist",
    )

    return any(
        phrase in text_lower
        for phrase in phrases
    )


def is_watchlist_view_request(text: str) -> bool:
    text_lower = text.lower()

    return (
        "my watchlist" in text_lower
        or "what am i tracking" in text_lower
        or "which companies am i monitoring" in text_lower
    )


def is_briefing_request(text: str) -> bool:
    text_lower = text.lower()

    return (
        "briefing" in text_lower
        and any(
            word in text_lower
            for word in (
                "send",
                "schedule",
                "daily",
                "morning",
                "enable",
            )
        )
    )


def is_briefing_disable_request(text: str) -> bool:
    text_lower = text.lower()

    return (
        "briefing" in text_lower
        and any(
            phrase in text_lower
            for phrase in (
                "stop",
                "disable",
                "pause",
                "turn off",
            )
        )
    )


def extract_briefing_time(
    text: str,
) -> clock_time | None:
    twelve_hour_match = re.search(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
        text.lower(),
    )

    if twelve_hour_match:
        hour = int(twelve_hour_match.group(1))
        minute = int(twelve_hour_match.group(2) or 0)
        period = twelve_hour_match.group(3)

        if not 1 <= hour <= 12 or not 0 <= minute <= 59:
            return None

        if period == "pm" and hour != 12:
            hour += 12

        if period == "am" and hour == 12:
            hour = 0

        return clock_time(hour, minute)

    twenty_four_hour_match = re.search(
        r"\b([01]?\d|2[0-3]):([0-5]\d)\b",
        text,
    )

    if twenty_four_hour_match:
        return clock_time(
            int(twenty_four_hour_match.group(1)),
            int(twenty_four_hour_match.group(2)),
        )

    return None

def transcribe_voice(
    file_path: str,
    mime_type: str,
) -> str:
    audio_bytes = Path(file_path).read_bytes()

    audio_part = types.Part.from_bytes(
        data=audio_bytes,
        mime_type=mime_type,
    )

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            audio_part,
            (
                "Transcribe this Telegram voice message accurately. "
                "Return only the spoken words. Do not answer the request."
            ),
        ],
    )

    if not response.text:
        raise ValueError("No speech could be transcribed")

    return response.text.strip()

def analyze_financial_image(
    file_path: str,
    user_instruction: str,
) -> str:
    image_bytes = Path(file_path).read_bytes()

    image_part = types.Part.from_bytes(
        data=image_bytes,
        mime_type="image/jpeg",
    )

    prompt = f"""
{SYSTEM_PROMPT}

The user uploaded an image with this instruction:
{user_instruction}

Analyze the image for a finance professional.

The image may contain a chart, financial table, earnings slide,
financial statement, report page, or business screenshot.

Provide:
- What the image appears to show.
- The most important figures or trends that are clearly readable.
- Why those observations may matter.
- Any visible anomaly, risk, or uncertainty.

Use only information visible in the image.
Do not guess unreadable labels, dates, values, or units.
Clearly state when something cannot be read confidently.
Do not claim the information is current unless a visible date proves it.
Keep the response below 180 words.
Use plain text without Markdown symbols.
"""

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            image_part,
            prompt,
        ],
    )

    if not response.text:
        return "I couldn't extract a useful insight from the image."

    return clean_for_telegram(response.text)

def process_transcribed_request(
    user_id: int,
    transcript: str,
    history: list[tuple[str, str]],
) -> str:
    ticker = financial_data.find_ticker(transcript)

    if (
        ticker
        and financial_data.is_live_price_request(transcript)
    ):
        quote = financial_data.get_stock_quote(ticker)
        return create_quote_reply(quote)

    if ticker and is_news_request(transcript):
        news_items = financial_data.get_company_news(ticker)

        if not news_items:
            return (
                f"I couldn't find sufficiently relevant verified "
                f"news for {ticker} during the last seven days."
            )

        return generate_news_reply(
            transcript,
            ticker,
            news_items,
        )

    active_document = active_documents.get(user_id)

    if (
        active_document
        and is_document_question(transcript)
    ):
        return answer_document_question(
            transcript,
            active_document,
        )

    return generate_reply(
        transcript,
        history,
    )

def create_quote_reply(quote: dict) -> str:
    change = quote["change"]
    percent = quote["percent_change"]

    if change > 0:
        movement = "up"
    elif change < 0:
        movement = "down"
    else:
        movement = "unchanged"

    return (
        f"{quote['symbol']} is ${quote['current_price']:.2f}, "
        f"{movement} ${abs(change):.2f} "
        f"({abs(percent):.2f}%) from the previous close.\n\n"
        f"Session range: ${quote['low']:.2f}–"
        f"${quote['high']:.2f}. "
        f"Previous close: ${quote['previous_close']:.2f}.\n\n"
        f"Source: Finnhub. "
        f"Quote timestamp: {quote['timestamp']}."
    )


def is_news_request(text: str) -> bool:
    text_lower = text.lower()

    news_terms = (
        "news",
        "headline",
        "headlines",
        "announcement",
        "announcements",
        "development",
        "developments",
        "what happened",
        "what should i know",
    )

    return any(
        term in text_lower
        for term in news_terms
    )


def create_news_fallback(
    symbol: str,
    news_items: list[dict],
) -> str:
    if not news_items:
        return (
            f"I couldn't find sufficiently relevant verified "
            f"news for {symbol} in the last seven days."
        )

    lines = [f"Verified {symbol} headlines:"]

    for item in news_items[:3]:
        lines.append(
            f"\n{item['headline']}\n"
            f"{item['source']} · {item['published']}\n"
            f"{item['url']}"
        )

    return "\n".join(lines)

def create_daily_briefing(
    symbols: list[str],
    force: bool = False,
) -> str | None:
    if not symbols:
        return None

    verified_sections = []
    fallback_sections = []
    material_update_found = False
    current_utc = datetime.now(timezone.utc)

    for symbol in symbols:
        quote = financial_data.get_stock_quote(symbol)

        if abs(quote["percent_change"]) >= 1:
            material_update_found = True

        fallback_sections.append(
            f"{symbol}: ${quote['current_price']:.2f}, "
            f"{quote['percent_change']:+.2f}% from the previous close."
        )

        verified_sections.append(
            financial_data.format_quote_context(quote)
        )

        news_items = financial_data.get_company_news(
            symbol,
            days=2,
            limit=2,
        )

        recent_news = []

        for item in news_items:
            try:
                published = datetime.strptime(
                    item["published"],
                    "%Y-%m-%d %H:%M UTC",
                ).replace(tzinfo=timezone.utc)

                age_hours = (
                    current_utc - published
                ).total_seconds() / 3600

                if age_hours <= 24:
                    recent_news.append(item)
                    material_update_found = True

            except ValueError:
                continue

        if recent_news:
            verified_sections.append(
                financial_data.format_news_context(
                    symbol,
                    recent_news,
                )
            )

            fallback_sections.append(
                f"{symbol} news: {recent_news[0]['headline']} "
                f"({recent_news[0]['source']})."
            )

    if not material_update_found and not force:
        return None

    verified_context = "\n\n".join(verified_sections)

    prompt = f"""
{SYSTEM_PROMPT}

Create a personalized morning financial briefing using only this
Finnhub-supplied information:

{verified_context}

Requirements:
- Begin with the most decision-relevant development.
- Cover only material price moves and relevant company news.
- Explain briefly why each selected item may matter.
- Attribute news claims to their publication source.
- Report price movements and news separately.
- Never claim that news caused a price movement unless the supplied
  source explicitly establishes that causal connection.
- Use cautious phrases such as "alongside" instead of "following."
- Do not call third-party reports independently verified.
- Do not provide investment advice.
- Keep the complete briefing below 220 words.
- Use plain text without Markdown symbols.
"""

    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )

        if response.text:
            return clean_for_telegram(response.text)

    except Exception as error:
        logging.warning(
            "AI briefing synthesis unavailable: %s",
            error,
        )

    return (
        "Your watchlist briefing:\n\n"
        + "\n\n".join(fallback_sections)
        + "\n\nSource: Finnhub."
    )

def generate_news_reply(
    user_message: str,
    symbol: str,
    news_items: list[dict],
) -> str:
    verified_news = financial_data.format_news_context(
        symbol,
        news_items,
    )

    prompt = f"""
{SYSTEM_PROMPT}

The following information was retrieved from Finnhub and may be used
as verified current information:

{verified_news}

User request:
{user_message}

Select only the two most decision-relevant articles.

For each article:
- Summarize the source's claim in one sentence.
- Explain why it may matter in one sentence.
- Include the source, publication time, and URL.

Describe these as Finnhub-indexed reports, not verified reports.
Attribute all claims to their original publication.
Do not imply that a headline has been independently confirmed.
Keep the complete response below 140 words.
Use plain text without Markdown symbols.
"""

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )

    if not response.text:
        return create_news_fallback(
            symbol,
            news_items,
        )

    return clean_for_telegram(response.text)

def generate_document_summary(document: dict) -> str:
    truncation_note = (
        "Only the first portion was processed."
        if document["truncated"]
        else "The complete extracted text was processed."
    )

    prompt = f"""
{SYSTEM_PROMPT}

A user uploaded this document:

Filename: {document['filename']}
Pages: {document['page_count']}
Processing note: {truncation_note}

Document text:
{document['text']}

Create a concise executive summary for a finance professional.

Include:
- What the document is about.
- Three important financial or strategic insights.
- The most important risk or uncertainty.
- One suggested follow-up question.

Use only information contained in the document.
Do not invent missing figures or context.
Keep the response below 250 words.
Use plain text without Markdown symbols.
"""

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )

    if not response.text:
        return "I couldn't generate a useful document summary."

    return clean_for_telegram(response.text)

def is_document_question(text: str) -> bool:
    text_lower = text.lower()

    document_terms = (
        "document",
        "report",
        "pdf",
        "filing",
        "page",
        "revenue",
        "profit",
        "margin",
        "risk",
        "liability",
        "cash",
        "debt",
        "assumption",
        "financial statement",
        "balance sheet",
        "income statement",
        "cash flow",
        "summarize",
        "explain",
        "compare",
    )

    return any(
        term in text_lower
        for term in document_terms
    )


def answer_document_question(
    question: str,
    document: dict,
) -> str:
    prompt = f"""
{SYSTEM_PROMPT}

The user is asking about an uploaded financial document.

Filename: {document['filename']}
Pages: {document['page_count']}

Document text:
{document['text']}

User question:
{question}

Answer using only the document.
Include page numbers when they can be identified from the text.
If the document does not contain the answer, say so clearly.
Do not invent figures or assumptions.
Keep the answer below 180 words.
Use plain text without Markdown symbols.
"""

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )

    if not response.text:
        return "I couldn't find a useful answer in the document."

    return clean_for_telegram(response.text)

def generate_reply(
    user_message: str,
    history: list[tuple[str, str]],
) -> str:
    conversation_history = format_history(history)

    prompt = f"""
{SYSTEM_PROMPT}

Recent conversation:
{conversation_history}

Latest user message:
{user_message}
"""

    response = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )

    if not response.text:
        return "I couldn't produce a useful response. Please try again."

    return clean_for_telegram(response.text)


@dispatcher.message(CommandStart())
async def welcome(message: Message) -> None:
    user = message.from_user

    if user:
        await asyncio.to_thread(
            database.upsert_user,
            user.id,
            user.first_name,
            user.username,
        )

    first_name = user.first_name if user else "there"

    welcome_text = (
        f"Hi {first_name}, I'm Atlas. I help you monitor companies, "
        "research financial developments, and turn reports into concise "
        "decision-relevant insights.\n\n"
        "What kind of financial work do you do?"
    )

    await message.answer(welcome_text)


@dispatcher.message(F.text)
async def handle_text(
    message: Message,
    bot: Bot,
) -> None:
    user = message.from_user

    if not user or not message.text:
        return

    user_text = message.text.strip()

    if not user_text:
        return

    try:
        await asyncio.to_thread(
            database.upsert_user,
            user.id,
            user.first_name,
            user.username,
        )

        history = await asyncio.to_thread(
            database.get_recent_messages,
            user.id,
            10,
        )

        await asyncio.to_thread(
            database.save_message,
            user.id,
            "user",
            user_text,
        )
                # View watchlist
        if is_watchlist_view_request(user_text):
            symbols = await asyncio.to_thread(
                database.get_watchlist,
                user.id,
            )

            if symbols:
                reply = (
                    "You’re monitoring "
                    + ", ".join(symbols)
                    + "."
                )
            else:
                reply = (
                    "Your watchlist is empty. Tell me naturally which "
                    "companies you want me to monitor."
                )

            await asyncio.to_thread(
                database.save_message,
                user.id,
                "assistant",
                reply,
            )

            await message.answer(reply)
            return

        # Remove companies from watchlist
        if is_watchlist_remove_request(user_text):
            symbols = find_all_tickers(user_text)

            if not symbols:
                reply = (
                    "Which company would you like me to stop tracking?"
                )
            else:
                removed = []

                for symbol in symbols:
                    was_removed = await asyncio.to_thread(
                        database.remove_from_watchlist,
                        user.id,
                        symbol,
                    )

                    if was_removed:
                        removed.append(symbol)

                if removed:
                    reply = (
                        "I’ve stopped monitoring "
                        + ", ".join(removed)
                        + "."
                    )
                else:
                    reply = (
                        "Those companies were not on your watchlist."
                    )

            await asyncio.to_thread(
                database.save_message,
                user.id,
                "assistant",
                reply,
            )

            await message.answer(reply)
            return

        # Add companies to watchlist
        if is_watchlist_add_request(user_text):
            symbols = find_all_tickers(user_text)

            if not symbols:
                reply = (
                    "Which company would you like me to monitor?"
                )
            else:
                for symbol in symbols:
                    await asyncio.to_thread(
                        database.add_to_watchlist,
                        user.id,
                        symbol,
                    )

                reply = (
                    "I’ll monitor "
                    + ", ".join(symbols)
                    + " for material price moves and company news."
                )

            await asyncio.to_thread(
                database.save_message,
                user.id,
                "assistant",
                reply,
            )

            await message.answer(reply)
            return

        # Disable daily briefing
        if is_briefing_disable_request(user_text):
            existing = await asyncio.to_thread(
                database.get_briefing_preferences,
                user.id,
            )

            saved_time = (
                existing["briefing_time"]
                if existing
                else clock_time(8, 0)
            )

            await asyncio.to_thread(
                database.set_briefing_preferences,
                user.id,
                False,
                saved_time,
                "Asia/Kolkata",
            )

            reply = (
                "Your daily briefing is paused. "
                "You can enable it again whenever you want."
            )

            await asyncio.to_thread(
                database.save_message,
                user.id,
                "assistant",
                reply,
            )

            await message.answer(reply)
            return

        # Enable or update daily briefing
        if is_briefing_request(user_text):
            requested_time = extract_briefing_time(user_text)

            if not requested_time:
                reply = (
                    "What time would you like your daily briefing? "
                    "For example, 8:00 AM."
                )
            else:
                await asyncio.to_thread(
                    database.set_briefing_preferences,
                    user.id,
                    True,
                    requested_time,
                    "Asia/Kolkata",
                )

                display_time = requested_time.strftime("%I:%M %p")

                reply = (
                    f"Your daily briefing is scheduled for "
                    f"{display_time} India time. I’ll focus on your "
                    "watchlist and stay silent when nothing material "
                    "has changed."
                )

            await asyncio.to_thread(
                database.save_message,
                user.id,
                "assistant",
                reply,
            )

            await message.answer(reply)
            return

        ticker = financial_data.find_ticker(user_text)

        # Live stock-price request
        if (
            ticker
            and financial_data.is_live_price_request(user_text)
        ):
            async with ChatActionSender.typing(
                bot=bot,
                chat_id=message.chat.id,
            ):
                try:
                    quote = await asyncio.wait_for(
                        asyncio.to_thread(
                            financial_data.get_stock_quote,
                            ticker,
                        ),
                        timeout=20,
                    )

                    reply = create_quote_reply(quote)

                except asyncio.TimeoutError:
                    logging.warning(
                        "Finnhub quote timed out for %s",
                        ticker,
                    )

                    reply = (
                        f"I couldn't verify {ticker}'s live price "
                        "because the request timed out. "
                        "Please try again shortly."
                    )

                except Exception:
                    logging.exception(
                        "Finnhub quote failed for %s",
                        ticker,
                    )

                    reply = (
                        f"I couldn't verify {ticker}'s live price "
                        "just now. Please try again shortly."
                    )

            await asyncio.to_thread(
                database.save_message,
                user.id,
                "assistant",
                reply,
            )

            await message.answer(reply)
            return

        # Verified company-news request
        if ticker and is_news_request(user_text):
            async with ChatActionSender.typing(
                bot=bot,
                chat_id=message.chat.id,
            ):
                try:
                    news_items = await asyncio.wait_for(
                        asyncio.to_thread(
                            financial_data.get_company_news,
                            ticker,
                        ),
                        timeout=20,
                    )

                    if not news_items:
                        reply = (
                            f"I couldn't find sufficiently relevant "
                            f"verified news for {ticker} during the "
                            "last seven days."
                        )
                    else:
                        try:
                            reply = await asyncio.wait_for(
                                asyncio.to_thread(
                                    generate_news_reply,
                                    user_text,
                                    ticker,
                                    news_items,
                                ),
                                timeout=60,
                            )

                        except Exception as ai_error:
                            logging.warning(
                                "News synthesis unavailable: %s",
                                ai_error,
                            )

                            reply = create_news_fallback(
                                ticker,
                                news_items,
                            )

                except asyncio.TimeoutError:
                    logging.warning(
                        "Finnhub news timed out for %s",
                        ticker,
                    )

                    reply = (
                        f"I couldn't retrieve verified {ticker} news "
                        "because the request timed out. "
                        "Please try again shortly."
                    )

                except Exception:
                    logging.exception(
                        "Finnhub news failed for %s",
                        ticker,
                    )

                    reply = (
                        f"I couldn't retrieve verified {ticker} news "
                        "just now. Please try again shortly."
                    )

            reply = clean_for_telegram(reply)

            await asyncio.to_thread(
                database.save_message,
                user.id,
                "assistant",
                reply,
            )

            await message.answer(
                reply[:4000],
                link_preview_options=LinkPreviewOptions(
                    is_disabled=True,
                ),
            )
            return
        # Uploaded-document follow-up question
        active_document = active_documents.get(user.id)

        if (
            active_document
            and is_document_question(user_text)
        ):
            async with ChatActionSender.typing(
                bot=bot,
                chat_id=message.chat.id,
            ):
                reply = await asyncio.wait_for(
                    asyncio.to_thread(
                        answer_document_question,
                        user_text,
                        active_document,
                    ),
                    timeout=90,
                )

            await asyncio.to_thread(
                database.save_message,
                user.id,
                "assistant",
                reply,
            )

            await message.answer(reply[:4000])
            return

        # Normal conversational request
        async with ChatActionSender.typing(
            bot=bot,
            chat_id=message.chat.id,
        ):
            reply = await asyncio.wait_for(
                asyncio.to_thread(
                    generate_reply,
                    user_text,
                    history,
                ),
                timeout=60,
            )

        reply = clean_for_telegram(reply)

        await asyncio.to_thread(
            database.save_message,
            user.id,
            "assistant",
            reply,
        )

        await message.answer(reply[:4000])

    except asyncio.TimeoutError:
        logging.warning("Gemini request timed out")

        await message.answer(
            "That took longer than expected. Please try again."
        )

    except Exception as error:
        error_text = str(error).lower()

        if (
            "429" in error_text
            or "rate limit" in error_text
            or "quota" in error_text
        ):
            logging.warning("Gemini free-tier limit reached")

            await message.answer(
                "I've reached a temporary AI usage limit. "
                "Please try again in about one minute."
            )

        else:
            logging.exception("Failed to process message")

            await message.answer(
                "I couldn't process that message just now. "
                "Please try again in a moment."
            )

@dispatcher.message(F.photo)
async def handle_photo(
    message: Message,
    bot: Bot,
) -> None:
    user = message.from_user

    if not user or not message.photo:
        return

    largest_photo = message.photo[-1]

    instruction = (
        message.caption.strip()
        if message.caption
        else (
            "Analyze this image and identify the most important "
            "financial information."
        )
    )

    await message.answer(
        "I’m examining the image and extracting the "
        "decision-relevant details."
    )

    try:
        with tempfile.TemporaryDirectory() as temporary_folder:
            image_path = Path(temporary_folder) / "financial_image.jpg"

            await bot.download(
                largest_photo,
                destination=image_path,
            )

            async with ChatActionSender.typing(
                bot=bot,
                chat_id=message.chat.id,
            ):
                reply = await asyncio.wait_for(
                    asyncio.to_thread(
                        analyze_financial_image,
                        str(image_path),
                        instruction,
                    ),
                    timeout=90,
                )

        reply = clean_for_telegram(reply)

        await asyncio.to_thread(
            database.upsert_user,
            user.id,
            user.first_name,
            user.username,
        )

        await asyncio.to_thread(
            database.save_message,
            user.id,
            "user",
            f"Uploaded an image. Instruction: {instruction}",
        )

        await asyncio.to_thread(
            database.save_message,
            user.id,
            "assistant",
            reply,
        )

        await message.answer(reply[:4000])

    except asyncio.TimeoutError:
        logging.warning("Image analysis timed out")

        await message.answer(
            "The image took too long to analyze. "
            "Please try a clearer or smaller image."
        )

    except Exception as error:
        logging.exception("Image analysis failed")

        if (
            "429" in str(error)
            or "quota" in str(error).lower()
        ):
            await message.answer(
                "I reached a temporary AI usage limit. "
                "Please try the image again in one minute."
            )
        else:
            await message.answer(
                "I couldn't analyze that image. "
                "Please try a clearer screenshot or photo."
            )

@dispatcher.message(F.voice)
async def handle_voice(
    message: Message,
    bot: Bot,
) -> None:
    user = message.from_user
    voice = message.voice

    if not user or not voice:
        return

    if voice.duration > 60:
        await message.answer(
            "Please keep voice messages below one minute "
            "for this prototype."
        )
        return

    await message.answer(
        "I’m listening and processing your request."
    )

    try:
        with tempfile.TemporaryDirectory() as temporary_folder:
            voice_path = Path(temporary_folder) / "voice.ogg"

            await bot.download(
                voice,
                destination=voice_path,
            )

            transcript = await asyncio.wait_for(
                asyncio.to_thread(
                    transcribe_voice,
                    str(voice_path),
                    voice.mime_type or "audio/ogg",
                ),
                timeout=60,
            )

        await asyncio.to_thread(
            database.upsert_user,
            user.id,
            user.first_name,
            user.username,
        )

        history = await asyncio.to_thread(
            database.get_recent_messages,
            user.id,
            10,
        )

        await asyncio.to_thread(
            database.save_message,
            user.id,
            "user",
            f"Voice message: {transcript}",
        )

        await message.answer(
            f"I heard: “{transcript}”"
        )

        async with ChatActionSender.typing(
            bot=bot,
            chat_id=message.chat.id,
        ):
            reply = await asyncio.wait_for(
                asyncio.to_thread(
                    process_transcribed_request,
                    user.id,
                    transcript,
                    history,
                ),
                timeout=90,
            )

        reply = clean_for_telegram(reply)

        await asyncio.to_thread(
            database.save_message,
            user.id,
            "assistant",
            reply,
        )

        await message.answer(
            reply[:4000],
            link_preview_options=LinkPreviewOptions(
                is_disabled=True,
            ),
        )

    except asyncio.TimeoutError:
        logging.warning("Voice processing timed out")

        await message.answer(
            "The voice message took too long to process. "
            "Please try a shorter message."
        )

    except Exception as error:
        logging.exception("Voice processing failed")

        if (
            "429" in str(error)
            or "quota" in str(error).lower()
        ):
            await message.answer(
                "I reached a temporary AI usage limit. "
                "Please try the voice message again in one minute."
            )
        else:
            await message.answer(
                "I couldn't understand that voice message. "
                "Please try again or send the request as text."
            )

@dispatcher.message(F.document)
async def handle_document(
    message: Message,
    bot: Bot,
) -> None:
    user = message.from_user
    uploaded = message.document

    if not user or not uploaded:
        return

    filename = uploaded.file_name or "uploaded.pdf"

    if not filename.lower().endswith(".pdf"):
        await message.answer(
            "Please upload a PDF financial report or presentation."
        )
        return

    if uploaded.file_size and uploaded.file_size > 10_000_000:
        await message.answer(
            "That PDF is larger than 10 MB. "
            "Please upload a smaller document for this prototype."
        )
        return

    await message.answer(
        "I’m reading the PDF and preparing a concise "
        "financial summary."
    )

    try:
        with tempfile.TemporaryDirectory() as temporary_folder:
            file_path = Path(temporary_folder) / "uploaded.pdf"

            await bot.download(
                uploaded,
                destination=file_path,
            )

            document = await asyncio.to_thread(
                document_data.extract_pdf_text,
                str(file_path),
            )

            document["filename"] = filename
            active_documents[user.id] = document

            async with ChatActionSender.typing(
                bot=bot,
                chat_id=message.chat.id,
            ):
                summary = await asyncio.wait_for(
                    asyncio.to_thread(
                        generate_document_summary,
                        document,
                    ),
                    timeout=90,
                )

        await asyncio.to_thread(
            database.upsert_user,
            user.id,
            user.first_name,
            user.username,
        )

        await asyncio.to_thread(
            database.save_message,
            user.id,
            "user",
            f"Uploaded PDF: {filename}",
        )

        await asyncio.to_thread(
            database.save_message,
            user.id,
            "assistant",
            summary,
        )

        await message.answer(summary[:4000])

    except asyncio.TimeoutError:
        logging.warning("PDF analysis timed out")

        await message.answer(
            "The document analysis took too long. "
            "Please try a shorter PDF."
        )

    except Exception as error:
        logging.exception("PDF processing failed")

        if "no readable text" in str(error).lower():
            await message.answer(
                "I couldn't extract readable text from this PDF. "
                "It may contain scanned images rather than text."
            )
        elif "429" in str(error) or "quota" in str(error).lower():
            await message.answer(
                "The PDF was readable, but the temporary AI usage "
                "limit was reached. Please try again in one minute."
            )
        else:
            await message.answer(
                "I couldn't process this PDF. "
                "Please try another financial document."
            )

@dispatcher.message()
async def handle_unsupported_message(
    message: Message,
) -> None:
    await message.answer(
        "For now, please send me a text message. "
        "Voice messages, images, and documents will be supported shortly."
    )

async def briefing_scheduler(bot: Bot) -> None:
    while True:
        try:
            preferences = await asyncio.to_thread(
                database.get_enabled_briefings,
            )

            now_utc = datetime.now(timezone.utc)

            for preference in preferences:
                try:
                    local_timezone = ZoneInfo(
                        preference["timezone"]
                    )
                except Exception:
                    logging.warning(
                        "Invalid timezone for user %s",
                        preference["user_id"],
                    )
                    continue

                local_now = now_utc.astimezone(local_timezone)
                local_date = local_now.date()
                scheduled_time = preference["briefing_time"]
                last_sent = preference["last_sent_date"]

                is_due = (
                    local_now.time().replace(tzinfo=None)
                    >= scheduled_time
                )

                already_sent = last_sent == local_date

                if not is_due or already_sent:
                    continue

                try:
                    briefing = await asyncio.wait_for(
                        asyncio.to_thread(
                            create_daily_briefing,
                            preference["symbols"],
                            False,
                        ),
                        timeout=120,
                    )

                    if briefing:
                        await bot.send_message(
                            preference["user_id"],
                            briefing[:4000],
                            link_preview_options=LinkPreviewOptions(
                                is_disabled=True,
                            ),
                        )

                    # Mark the day even when Atlas stays silent.
                    await asyncio.to_thread(
                        database.mark_briefing_sent,
                        preference["user_id"],
                        local_date,
                    )

                except Exception:
                    logging.exception(
                        "Briefing failed for user %s",
                        preference["user_id"],
                    )

        except Exception:
            logging.exception("Briefing scheduler failed")

        await asyncio.sleep(60)

async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=(
            "%(asctime)s %(levelname)s "
            "%(name)s: %(message)s"
        ),
    )

    await asyncio.to_thread(database.init_db)

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    scheduler_task = asyncio.create_task(
        briefing_scheduler(bot)
    )


    try:
        print(
            "Atlas is running with memory, live prices, "
            "and verified company news. Press Ctrl+C to stop."
        )

        await dispatcher.start_polling(bot)

    finally:
        scheduler_task.cancel()

        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass

        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())