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
from collections.abc import Callable

from dotenv import load_dotenv

from src.models.schemas import GeminiTelemetry, TalentClassificationResult

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
    base_backoff: float = 1.5,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> TalentClassificationResult:
    """
    Evaluates comments using Gemini 2.5 Flash with deterministic PII filtering,
    real provider telemetry capture, and exponential backoff under HTTP 429/503 rate pressure.
    """
    clean_text = sanitize_pii(text)
    gemini_key = os.getenv("GEMINI_API_KEY")

    if not gemini_key or "your_gemini" in gemini_key or "AIzaSy" not in gemini_key:
        logger.warning("GEMINI_API_KEY unconfigured or placeholder. Returning deterministic fallback.")
        return TalentClassificationResult(
            is_artist_promotion=False,
            artist_name=None,
            reason="Unconfigured GEMINI_API_KEY",
            confidence=0.0,
            status="FALLBACK",
            telemetry=GeminiTelemetry(
                attempts=0,
                requested_model="gemini-2.5-flash",
            )
        )

    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash:generateContent"
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

    total_start_time = time.perf_counter()
    last_attempt_latency_ms: float | None = None
    completed_attempts = 0

    for attempt in range(1, max_retries + 1):
        completed_attempts = attempt
        attempt_start_time = time.perf_counter()
        try:
            req = urllib.request.Request(
                endpoint,
                data=encoded_payload,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "BeatMatch-Scout/2.0",
                    "x-goog-api-key": gemini_key,
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=12) as response:  # nosec B310  # verified https scheme
                body = response.read().decode("utf-8")
                last_attempt_latency_ms = (time.perf_counter() - attempt_start_time) * 1000.0
                parsed_response = json.loads(body)

            # Extract real usageMetadata safely without heuristics
            usage_meta = parsed_response.get("usageMetadata")
            input_tokens: int | None = None
            output_tokens: int | None = None
            total_tokens: int | None = None

            if isinstance(usage_meta, dict):
                raw_prompt = usage_meta.get("promptTokenCount")
                raw_candidates = usage_meta.get("candidatesTokenCount")
                raw_total = usage_meta.get("totalTokenCount")
                if isinstance(raw_prompt, int) and raw_prompt >= 0:
                    input_tokens = raw_prompt
                if isinstance(raw_candidates, int) and raw_candidates >= 0:
                    output_tokens = raw_candidates
                if isinstance(raw_total, int) and raw_total >= 0:
                    total_tokens = raw_total

            # Extract provider modelVersion safely
            raw_model_ver = parsed_response.get("modelVersion")
            model_version: str | None = None
            if isinstance(raw_model_ver, str) and raw_model_ver.strip():
                model_version = raw_model_ver.strip()

            total_duration_ms = (time.perf_counter() - total_start_time) * 1000.0

            telemetry = GeminiTelemetry(
                latency_ms=round(last_attempt_latency_ms, 2) if last_attempt_latency_ms is not None else None,
                total_duration_ms=round(total_duration_ms, 2),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                model_version=model_version,
                requested_model="gemini-2.5-flash",
                attempts=attempt
            )

            candidates = parsed_response.get("candidates", [])
            if not candidates:
                return TalentClassificationResult(
                    is_artist_promotion=False,
                    artist_name=None,
                    reason="Model returned empty candidate set",
                    confidence=0.0,
                    status="ERROR",
                    error_message="Empty candidate set returned by provider",
                    telemetry=telemetry
                )

            part_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "{}")
            try:
                extracted_json = json.loads(part_text.strip())
            except json.JSONDecodeError as json_err:
                return TalentClassificationResult(
                    is_artist_promotion=False,
                    artist_name=None,
                    reason=f"Failed to parse model response JSON: {json_err}",
                    confidence=0.0,
                    status="ERROR",
                    error_message=f"JSON decode failure: {json_err}",
                    telemetry=telemetry
                )

            return TalentClassificationResult(
                is_artist_promotion=bool(extracted_json.get("is_artist_promotion", False)),
                artist_name=extracted_json.get("artist_name"),
                reason=str(extracted_json.get("reason", "")),
                confidence=float(extracted_json.get("confidence", 0.8)),
                status="SUCCESS",
                telemetry=telemetry
            )

        except urllib.error.HTTPError as http_err:
            last_attempt_latency_ms = (time.perf_counter() - attempt_start_time) * 1000.0
            if http_err.code in (429, 500, 503) and attempt < max_retries:
                sleep_duration = (base_backoff ** attempt) + random.uniform(0.1, 0.6)  # nosec B311  # non-cryptographic jitter
                logger.warning(
                    "Gemini API rate/server limit (HTTP %d). Retry %d/%d in %.2fs",
                    http_err.code, attempt, max_retries, sleep_duration
                )
                sleep_fn(sleep_duration)
                continue
            err_reason = getattr(http_err, 'reason', str(http_err.code))
            logger.error("Gemini API HTTP Error %d on attempt %d: %s", http_err.code, attempt, err_reason)
            break
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, ValueError) as err:
            last_attempt_latency_ms = (time.perf_counter() - attempt_start_time) * 1000.0
            logger.warning("Gemini evaluation error (%s) on attempt %d: %s", err.__class__.__name__, attempt, err)
            break

    total_duration_ms = (time.perf_counter() - total_start_time) * 1000.0

    return TalentClassificationResult(
        is_artist_promotion=False,
        artist_name=None,
        reason="Evaluation exhausted retries or failed network call",
        confidence=0.0,
        status="ERROR",
        error_message="Network call failed or retries exhausted",
        telemetry=GeminiTelemetry(
            latency_ms=round(last_attempt_latency_ms, 2) if last_attempt_latency_ms is not None else None,
            total_duration_ms=round(total_duration_ms, 2),
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            model_version=None,
            requested_model="gemini-2.5-flash",
            attempts=completed_attempts
        )
    )
