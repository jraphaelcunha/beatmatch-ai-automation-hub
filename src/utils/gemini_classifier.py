"""
Talent classification module interfacing Google Gemini 2.5 Flash API.
Executes semantic evaluation of social media comments to discover emerging vocalists.
Includes deterministic pre-LLM PII sanitization and exponential backoff retry mechanics.
"""

import json
import logging
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv

from src.models.schemas import TalentClassificationResult

load_dotenv()
logger = logging.getLogger("gemini_classifier")

EMAIL_REGEX = re.compile(r'[\w\.-]+@[\w\.-]+\.\w+')
PHONE_REGEX = re.compile(r'(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?(?:9\d{4}|\d{4})[-.\s]?\d{4}')
CPF_SSN_REGEX = re.compile(r'\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b|\b\d{3}-\d{2}-\d{4}\b')


def sanitize_pii(text: str) -> str:
    """Deterministically strips personal identifiable information before LLM dispatch."""
    if not text:
        return ""
    sanitized = EMAIL_REGEX.sub("[EMAIL_REDACTED]", text)
    sanitized = PHONE_REGEX.sub("[PHONE_REDACTED]", sanitized)
    sanitized = CPF_SSN_REGEX.sub("[ID_REDACTED]", sanitized)
    return sanitized


def classify_lead_with_gemini(
    text: str,
    max_retries: int = 3,
    base_backoff: float = 1.5
) -> TalentClassificationResult:
    """
    Evaluates comments using Gemini 2.5 Flash with deterministic PII filtering
    and exponential backoff under HTTP 429/503 rate pressure.
    """
    clean_text = sanitize_pii(text)
    gemini_key = os.getenv("GEMINI_API_KEY")

    if not gemini_key or "your_gemini" in gemini_key or "AIzaSy" not in gemini_key:
        logger.warning("GEMINI_API_KEY unconfigured or placeholder. Returning deterministic fallback.")
        return TalentClassificationResult(
            is_artist_promotion=False,
            artist_name=None,
            reason="Unconfigured GEMINI_API_KEY",
            confidence=0.0
        )

    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={gemini_key}"
    )

    prompt = (
        "You are an expert music scout and talent A&R. Analyze the following social media post or comment.\n"
        "Determine if the user is an emerging musical artist (singer, rapper, vocalist, songwriter) "
        "actively promoting their own music, song, track, or channel.\n\n"
        f"Comment/Post: \"{clean_text}\"\n\n"
        "Filter out:\n"
        "- Beatmakers, music producers, loopmakers, and sound designers (e.g., 'listen to my beats', 'prod by me').\n"
        "- General listeners offering reactions or praise (e.g., 'nice track', 'who is here in 2026?').\n"
        "- Spammers advertising commercial services or playlist placements.\n\n"
        "Return a JSON object conforming exactly to this structure:\n"
        "{\n"
        "  \"is_artist_promotion\": true,\n"
        "  \"artist_name\": \"Extracted Name or null\",\n"
        "  \"reason\": \"Concise justification\",\n"
        "  \"confidence\": 0.9\n"
        "}\n"
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }
    encoded_payload = json.dumps(payload).encode("utf-8")

    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(
                endpoint,
                data=encoded_payload,
                headers={"Content-Type": "application/json", "User-Agent": "BeatMatch-Scout/2.0"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=12) as response:
                body = response.read().decode("utf-8")
                parsed_response = json.loads(body)

            candidates = parsed_response.get("candidates", [])
            if not candidates:
                return TalentClassificationResult(
                    is_artist_promotion=False,
                    artist_name=None,
                    reason="Model returned empty candidate set",
                    confidence=0.0
                )

            part_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "{}")
            extracted_json = json.loads(part_text.strip())

            return TalentClassificationResult(
                is_artist_promotion=bool(extracted_json.get("is_artist_promotion", False)),
                artist_name=extracted_json.get("artist_name"),
                reason=str(extracted_json.get("reason", "")),
                confidence=float(extracted_json.get("confidence", 0.8))
            )

        except urllib.error.HTTPError as http_err:
            if http_err.code in (429, 500, 503) and attempt < max_retries:
                sleep_duration = (base_backoff ** attempt) + random.uniform(0.1, 0.6)
                logger.warning(
                    "Gemini API rate/server limit (HTTP %d). Retry %d/%d in %.2fs",
                    http_err.code, attempt, max_retries, sleep_duration
                )
                time.sleep(sleep_duration)
                continue
            logger.error("Gemini API HTTP Error %d on attempt %d: %s", http_err.code, attempt, http_err)
            break
        except Exception as general_err:
            logger.error("Gemini evaluation error on attempt %d: %s", attempt, general_err)
            break

    return TalentClassificationResult(
        is_artist_promotion=False,
        artist_name=None,
        reason="Evaluation exhausted retries or failed network call",
        confidence=0.0
    )
