"""Disk cache of synthesized lines: a fixed line is rendered once and replayed as the same audio on every call."""
from __future__ import annotations

import asyncio
from array import array
import hashlib
import os
import re
from collections.abc import AsyncIterator
from typing import Any

from livekit import rtc

SAMPLE_RATE = 24000
CHUNK = SAMPLE_RATE // 10 * 2  # 100 ms of 16-bit mono


_SENTENCE = re.compile(r"(?<=[.?!])\s+")


def sentences(text: str) -> list[str]:
    return [part for part in _SENTENCE.split(text.strip()) if part]


def trim(pcm: bytes, floor: int = 350, lead_ms: int = 30, tail_ms: int = 140) -> bytes:
    """Cut the silence around one synthesized sentence, keeping a short natural pause after it."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    loud = [i for i in range(0, len(samples), 24) if abs(samples[i]) > floor]
    if not loud:
        return pcm
    first = max(0, loud[0] - SAMPLE_RATE * lead_ms // 1000)
    last = min(len(samples), loud[-1] + SAMPLE_RATE * tail_ms // 1000)
    return samples[first:last].tobytes()


class VoiceCache:
    def __init__(self, engine: Any, directory: str, identity: str) -> None:
        self._engine, self._dir, self._identity = engine, directory, identity
        os.makedirs(directory, exist_ok=True)

    def path(self, text: str) -> str:
        key = hashlib.sha256(f"trim1|{self._identity}|{text}".encode()).hexdigest()[:32]
        return os.path.join(self._dir, key + ".pcm")

    def has(self, text: str) -> bool:
        return os.path.exists(self.path(text))

    async def render(self, text: str) -> None:
        """Synthesize one line as a single utterance and store it. Safe to call twice."""
        if not text or self.has(text):
            return
        frames = [event.frame async for event in self._engine.synthesize(text)]
        if not frames:
            return
        frame = rtc.combine_audio_frames(frames)
        if frame.sample_rate != SAMPLE_RATE or frame.num_channels != 1:
            return
        tmp = self.path(text) + ".tmp"
        with open(tmp, "wb") as f:
            f.write(trim(bytes(frame.data)))
        os.replace(tmp, self.path(text))

    async def live(self, text: str) -> AsyncIterator[rtc.AudioFrame]:
        """Synthesize a line in one request, play it as it arrives and keep it for next time."""
        collected = []
        async for event in self._engine.synthesize(text):
            collected.append(event.frame)
            yield event.frame
        if collected and not self.has(text):
            frame = rtc.combine_audio_frames(collected)
            if frame.sample_rate == SAMPLE_RATE and frame.num_channels == 1:
                tmp = self.path(text) + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(trim(bytes(frame.data)))
                os.replace(tmp, self.path(text))

    async def frames(self, text: str) -> AsyncIterator[rtc.AudioFrame]:
        with open(self.path(text), "rb") as f:
            data = f.read()
        for offset in range(0, len(data), CHUNK):
            part = data[offset:offset + CHUNK]
            yield rtc.AudioFrame(data=part, sample_rate=SAMPLE_RATE, num_channels=1, samples_per_channel=len(part) // 2)

    def cached(self, text: str) -> bool:
        return all(self.has(part) for part in sentences(text))

    def duration(self, text: str) -> float | None:
        """Seconds of audio for a fully cached line, else None."""
        if not self.cached(text):
            return None
        return sum(os.path.getsize(self.path(part)) for part in sentences(text)) / (SAMPLE_RATE * 2)

    async def warm(self, text: str) -> None:
        for part in sentences(text):
            await self.render(part)

    async def stream(self, text: str) -> AsyncIterator[rtc.AudioFrame]:
        """Audio for a line, sentence by sentence. Cached sentences replay; the others are synthesized
        while the earlier ones play, so there is no gap between them."""
        parts = sentences(text)
        ahead = {
            index: asyncio.create_task(self.render(part))
            for index, part in enumerate(parts) if index > 0 and not self.has(part)
        }
        for index, part in enumerate(parts):
            if index in ahead:
                await ahead[index]
            source = self.frames(part) if self.has(part) else self.live(part)
            async for frame in source:
                yield frame
