"""Bound slow inference separately from request bodies and lightweight endpoints."""
from threading import BoundedSemaphore
from .model_client import ModelUnavailable

slots = BoundedSemaphore(4)


def generate_reply(client, request):
    if not slots.acquire(blocking=False):
        raise ModelUnavailable('Inference is busy; retry shortly')
    try:
        return client.generate(request)
    finally:
        slots.release()
