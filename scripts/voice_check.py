"""Renders lines into the voice cache and reports the time. Usage: python scripts/voice_check.py "text" | --script"""
import asyncio
import sys
import time

import aiohttp
from livekit.agents import utils
from livekit.plugins import openai

from n8n_worker.config import load
from n8n_worker.voice_cache import VoiceCache


async def main() -> None:
    config = load()
    async with aiohttp.ClientSession() as http:
        engine = openai.TTS(model=config.tts_model, voice=config.tts_voice, speed=config.tts_speed,
                            **({"instructions": config.tts_instructions} if config.tts_instructions else {}))
        cache = VoiceCache(
            engine, config.tts_cache_dir,
            f"{config.tts_model}|{config.tts_voice}|{config.tts_speed}|{config.tts_instructions}")
        texts = sys.argv[1:]
        if texts == ["--script"]:
            from n8n_worker.script import load_script
            texts = [line for line in load_script(config.script_file).lines.values() if "{" not in line]
        for text in texts:
            t0 = time.monotonic()
            await cache.warm(text)
            print(f"{cache.duration(text):.1f}s audio in {int((time.monotonic() - t0) * 1000)}ms | {text[:60]}")


asyncio.run(main())
