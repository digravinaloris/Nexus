"""bridge -- passerelle entre les threads Flask (synchrones) et l'event loop du bot.

Extrait de main.py (étape 2b-1). Le bot est lu via `core.bot` (jamais `import main`).
"""
import asyncio

from nexus_runtime import core


def run_coroutine(coro, timeout=10):
    """
    Exécute une coroutine discord.py depuis un thread Flask (sync),
    en la poussant dans l'event loop du bot, et attend le résultat.
    """
    future = asyncio.run_coroutine_threadsafe(coro, core.bot.loop)
    return future.result(timeout=timeout)
